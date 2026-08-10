from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

from hologram.model import (
    Binding,
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    ImportRef,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)

from ._treesitter_common import (
    argument_count,
    assemble_file_ir,
    binding_tuple,
    field_nodes,
    named_children,
    syntax_diagnostics,
    walk_all,
)
from .common import ordered_unique, reference, symbol_id
from .treesitter import (
    ast_field,
    ast_text,
    body_events,
    body_lines,
    node_span,
    owned_nodes,
    ownership_context,
)

_FUNCTION_KINDS = frozenset({"function_declaration", "function_definition"})
_INDEX_KINDS = frozenset(
    {
        "bracket_index_expression",
        "dot_index_expression",
        "method_index_expression",
    }
)
_IDENTIFIER_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)
_LONG_STRING_RE = re.compile(r"^\[(=*)\[(.*)\]\1\]$", re.DOTALL)


def _key(node: object) -> tuple[int, int, str]:
    return (int(node.start_byte), int(node.end_byte), str(node.type))  # type: ignore[attr-defined]


def _has_function_ancestor(node: object) -> bool:
    current = getattr(node, "parent", None)
    while current is not None:
        if current.type in _FUNCTION_KINDS:
            return True
        current = getattr(current, "parent", None)
    return False


def _string_value(node: object | None) -> str | None:
    raw = ast_text(node).strip()
    if not raw:
        return None
    if match := _LONG_STRING_RE.fullmatch(raw):
        value = match.group(2)
        if value.startswith("\r\n"):
            value = value[2:]
        elif value.startswith(("\r", "\n")):
            value = value[1:]
        return value
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw.strip("\"'") or None
    return value if isinstance(value, str) else None


def _index_parts(node: Any | None) -> tuple[str, ...]:
    if node is None:
        return ()
    if node.type == "identifier":
        return (ast_text(node),)
    if node.type in _INDEX_KINDS:
        table = ast_field(node, "table")
        field = ast_field(node, "field") or ast_field(node, "method")
        prefix = _index_parts(table)
        if node.type == "bracket_index_expression":
            if getattr(field, "type", None) != "string":
                return ()
            name = _string_value(field)
            if not name:
                return ()
        else:
            name = ast_text(field)
        return (*prefix, name) if name else prefix
    raw = ast_text(node)
    return tuple(part for part in re.split(r"[.:]", raw) if part)


def _assignment_pairs(root: object) -> dict[tuple[int, int, str], Any]:
    pairs: dict[tuple[int, int, str], Any] = {}
    for assignment in walk_all(root):
        if assignment.type != "assignment_statement":
            continue
        variables = next(
            (
                child
                for child in named_children(assignment)
                if child.type == "variable_list"
            ),
            None,
        )
        expressions = next(
            (
                child
                for child in named_children(assignment)
                if child.type == "expression_list"
            ),
            None,
        )
        names = named_children(variables)
        values = named_children(expressions)
        if len(names) != len(values):
            continue
        pairs.update(
            {_key(value): name for name, value in zip(names, values, strict=True)}
        )
    return pairs


def _module_candidates(
    root: object,
    pairs: dict[tuple[int, int, str], Any],
) -> tuple[tuple[str, Any], ...]:
    values: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for value_key, name_node in pairs.items():
        if value_key[2] != "table_constructor":
            continue
        if _has_function_ancestor(name_node):
            continue
        parts = _index_parts(name_node)
        if len(parts) != 1:
            continue
        name = parts[0]
        if name not in seen:
            values.append((name, name_node))
            seen.add(name)
    return tuple(values)


def _require_call(node: Any) -> bool:
    return node.type == "function_call" and _index_parts(ast_field(node, "name")) == (
        "require",
    )


def _require_module(node: object) -> str | None:
    arguments = ast_field(node, "arguments")
    values = named_children(arguments)
    if len(values) != 1 or values[0].type != "string":
        return None
    return _string_value(values[0])


def _imports(
    source: SourceFile,
    root: object,
    pairs: dict[tuple[int, int, str], Any],
) -> tuple[ImportRef, ...]:
    values: list[ImportRef] = []
    for node in walk_all(root):
        if not _require_call(node) or not (module := _require_module(node)):
            continue
        alias_node = pairs.get(_key(node))
        parts = _index_parts(alias_node)
        alias = parts[-1] if len(parts) == 1 else None
        values.append(ImportRef(node_span(source, node), module, None, alias))
    return tuple(values)


def _is_local(node: Any) -> bool:
    if any(ast_text(child) == "local" for child in getattr(node, "children", ())):
        return True
    current = node
    while getattr(current, "parent", None) is not None:
        current = current.parent
        if current.type == "variable_declaration":
            return True
        if current.type in _FUNCTION_KINDS:
            return False
    return False


def _parameters(node: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    parameters = ast_field(node, "parameters")
    names = tuple(
        ast_text(name) for name in field_nodes(parameters, "name") if ast_text(name)
    )
    variadic = any(
        ast_text(child) == "..." for child in getattr(parameters, "children", ())
    )
    types = (*("?" for _ in names), *(("...",) if variadic else ()))
    return tuple(types), names


@dataclass(frozen=True, slots=True)
class _Callable:
    node: Any
    name_node: Any
    name: str
    container_path: tuple[str, ...]
    owned_path: tuple[str, ...]
    params: tuple[str, ...]
    parameter_names: tuple[str, ...]
    member: bool
    method: bool
    local: bool


def _field_name(node: object) -> str | None:
    name_node = ast_field(node, "name")
    if name_node is None:
        return None
    if name_node.type == "string":
        return _string_value(name_node)
    parts = _index_parts(name_node)
    return parts[0] if len(parts) == 1 else None


def _returned_table_field_parts(node: object) -> tuple[str, ...]:
    field = getattr(node, "parent", None)
    if field is None or field.type != "field" or ast_field(field, "value") != node:
        return ()
    name = _field_name(field)
    if not name:
        return ()
    parts = [name]
    table = getattr(field, "parent", None)
    while table is not None and table.type == "table_constructor":
        outer_field = getattr(table, "parent", None)
        if (
            outer_field is None
            or outer_field.type != "field"
            or ast_field(outer_field, "value") != table
        ):
            break
        outer_name = _field_name(outer_field)
        if not outer_name:
            return ()
        parts.append(outer_name)
        table = getattr(outer_field, "parent", None)
    expressions = getattr(table, "parent", None)
    returned = getattr(expressions, "parent", None)
    if (
        table is None
        or table.type != "table_constructor"
        or expressions is None
        or expressions.type != "expression_list"
        or returned is None
        or returned.type != "return_statement"
    ):
        return ()
    return tuple(reversed(parts))


def _callables(
    root: object,
    assignments: dict[tuple[int, int, str], Any],
) -> tuple[_Callable, ...]:
    values: list[_Callable] = []
    owned_paths: dict[tuple[int, int, str], tuple[str, ...]] = {}

    def lexical_owner(node: object) -> tuple[str, ...]:
        current = getattr(node, "parent", None)
        while current is not None:
            owned = owned_paths.get(_key(current))
            if owned is not None:
                return owned
            current = getattr(current, "parent", None)
        return ()

    for node in walk_all(root):
        if node.type not in _FUNCTION_KINDS:
            continue
        returned_parts: tuple[str, ...] = ()
        if node.type == "function_declaration":
            name_node = ast_field(node, "name")
            parts = _index_parts(name_node)
        else:
            name_node = assignments.get(_key(node))
            returned_parts = _returned_table_field_parts(node)
            parts = _index_parts(name_node) or returned_parts
        if not parts:
            continue
        name = parts[-1]
        explicit_container = parts[:-1]
        lexical_container = lexical_owner(node)
        container = (*lexical_container, *explicit_container)
        params, parameter_names = _parameters(node)
        owned = (*container, name)
        value = _Callable(
            node,
            name_node,
            name,
            container,
            owned,
            params,
            parameter_names,
            bool(explicit_container or returned_parts),
            bool(name_node is not None and name_node.type == "method_index_expression"),
            _is_local(node) or (not explicit_container and bool(container)),
        )
        values.append(value)
        owned_paths[_key(node)] = owned
    effective: dict[tuple[tuple[str, ...], bool, str, tuple[str, ...]], _Callable] = {}
    for value in values:
        identity = (value.container_path, value.member, value.name, value.params)
        effective[identity] = value
    effective_nodes = {_key(value.node) for value in effective.values()}

    def has_discarded_owner(value: _Callable) -> bool:
        current = getattr(value.node, "parent", None)
        while current is not None:
            current_key = _key(current)
            if current_key in owned_paths and current_key not in effective_nodes:
                return True
            current = getattr(current, "parent", None)
        return False

    return tuple(
        sorted(
            (value for value in effective.values() if not has_discarded_owner(value)),
            key=lambda value: _key(value.node),
        )
    )


def _call_parts(node: object | None) -> tuple[str | None, str] | None:
    parts = _index_parts(node)
    if not parts:
        return None
    return (".".join(parts[:-1]) or None, parts[-1])


def _static_bracket_member(node: object) -> tuple[Any, str, Any] | None:
    if getattr(node, "type", None) != "bracket_index_expression":
        return None
    table = ast_field(node, "table")
    field = ast_field(node, "field")
    if table is None or getattr(field, "type", None) != "string":
        return None
    name = _string_value(field)
    if not name:
        return None
    target = next(
        (child for child in named_children(field) if child.type == "string_content"),
        field,
    )
    return table, name, target


def _span_contains(outer: SourceSpan, inner: SourceSpan) -> bool:
    return (
        outer.file == inner.file
        and (outer.start_line, outer.start_column)
        <= (inner.start_line, inner.start_column)
        and (inner.end_line, inner.end_column) <= (outer.end_line, outer.end_column)
    )


def _join_static_bracket_events(
    source: SourceFile,
    callable_node: object,
    events: tuple[BodyEvent, ...],
    ownership: object,
) -> tuple[BodyEvent, ...]:
    joined = list(events)
    for node in owned_nodes(source, callable_node, ownership=ownership):  # type: ignore[arg-type]
        member = _static_bracket_member(node)
        if member is None:
            continue
        _, name, target = member
        span = node_span(source, target)
        if any(
            event.kind is BodyEventKind.MEMBER and event.span == span
            for event in joined
        ):
            continue
        containing = [
            index
            for index, event in enumerate(joined)
            if _span_contains(event.span, span)
        ]
        insertion = max(containing) + 1 if containing else len(joined)
        joined[insertion:insertion] = [
            BodyEvent(BodyEventKind.MEMBER, name, span),
            BodyEvent(BodyEventKind.NAME, name, span),
        ]
    return tuple(joined)


def _call(source: SourceFile, owner: SymbolId, node: Any) -> CallRef | None:
    parts = _call_parts(ast_field(node, "name"))
    if parts is None:
        return None
    receiver, name = parts
    return CallRef(
        owner,
        node_span(source, node),
        name,
        receiver,
        CallKind.CALL,
        argument_count(node),
    )


def _owned_calls(
    source: SourceFile,
    owner: SymbolId,
    callable_node: object,
    ownership: object,
) -> tuple[CallRef, ...]:
    return ordered_unique(
        call
        for node in owned_nodes(source, callable_node, ownership=ownership)  # type: ignore[arg-type]
        if node.type == "function_call"
        if not (_require_call(node) and _require_module(node) is not None)
        if (call := _call(source, owner, node)) is not None
    )


def _qualifiers(
    source: SourceFile,
    callable_node: object,
    ownership: object,
) -> dict[object, str]:
    values: dict[object, str] = {}
    for node in owned_nodes(source, callable_node, ownership=ownership):  # type: ignore[arg-type]
        if node.type not in _INDEX_KINDS:
            continue
        static = _static_bracket_member(node)
        if static is not None:
            table, _, field = static
            qualifier = ".".join(_index_parts(table))
        else:
            table = ast_field(node, "table")
            field = ast_field(node, "field") or ast_field(node, "method")
            qualifier = ast_text(table)
        if table is not None and field is not None:
            values[node_span(source, field)] = qualifier
    return values


def _references(
    source: SourceFile,
    owner: SymbolId,
    callable_node: object,
    events: tuple[BodyEvent, ...],
    ownership: object,
) -> tuple[ReferenceRef, ...]:
    qualifiers = _qualifiers(source, callable_node, ownership)
    return ordered_unique(
        reference(
            owner,
            event.span,
            event.text,
            qualifiers.get(event.span),
            ReferenceKind.NAME,
            context=ReferenceContext.CODE,
            confidence=ReferenceConfidence.DEFINITE,
        )
        for event in events
        if event.kind is BodyEventKind.NAME
        if _IDENTIFIER_RE.fullmatch(event.text)
    )


def _possible_name_references(
    events: tuple[BodyEvent, ...],
    *,
    exclude: tuple[BodyEvent, ...] = (),
) -> tuple[ReferenceRef, ...]:
    definite = frozenset(exclude)
    return ordered_unique(
        reference(
            None,
            event.span,
            event.text,
            None,
            ReferenceKind.NAME,
            context=ReferenceContext.CODE,
            confidence=ReferenceConfidence.POSSIBLE,
        )
        for event in events
        if event not in definite
        if event.kind is BodyEventKind.NAME
        if _IDENTIFIER_RE.fullmatch(event.text)
    )


def _possible_source_references(
    source: SourceFile,
    root: object,
) -> tuple[ReferenceRef, ...]:
    """Keep conservative reachability from calls and table callback slots."""

    references: list[ReferenceRef] = []
    for node in walk_all(root):
        target: object | None = None
        name: str | None = None
        if node.type == "function_call":
            target = ast_field(node, "name")
            parts = _call_parts(target)
            if parts is not None:
                _receiver, name = parts
        elif node.type == "field":
            value = ast_field(node, "value")
            if value is not None and value.type == "identifier":
                target = value
                name = ast_text(value)
        if target is None or name is None or not _IDENTIFIER_RE.fullmatch(name):
            continue
        references.append(
            reference(
                None,
                node_span(source, target),
                name,
                None,
                ReferenceKind.NAME,
                context=ReferenceContext.CODE,
                confidence=ReferenceConfidence.POSSIBLE,
            )
        )
    return ordered_unique(references)


def _inferred_type(node: object | None) -> str | None:
    if node is None:
        return None
    return {
        "false": "boolean",
        "function_definition": "function",
        "nil": "nil",
        "number": "number",
        "string": "string",
        "table_constructor": "table",
        "true": "boolean",
    }.get(str(getattr(node, "type", "")))


def _constant_declarations(
    source: SourceFile,
    root: object,
    module_names: frozenset[str],
) -> tuple[Symbol, ...]:
    values: dict[SymbolId, Symbol] = {}
    for assignment in walk_all(root):
        if assignment.type != "assignment_statement":
            continue
        current = getattr(assignment, "parent", None)
        if current is not None and current.type == "variable_declaration":
            current = getattr(current, "parent", None)
        if current is not None:
            ancestor = current
            inside_function = False
            while ancestor is not None:
                if ancestor.type in _FUNCTION_KINDS:
                    inside_function = True
                    break
                ancestor = getattr(ancestor, "parent", None)
            if inside_function:
                continue
        variables = next(
            (
                child
                for child in named_children(assignment)
                if child.type == "variable_list"
            ),
            None,
        )
        expressions = next(
            (
                child
                for child in named_children(assignment)
                if child.type == "expression_list"
            ),
            None,
        )
        names = named_children(variables)
        expressions_ = named_children(expressions)
        aligned = len(names) == len(expressions_)
        for index, name_node in enumerate(names):
            parts = _index_parts(name_node)
            if not parts:
                continue
            name = parts[-1]
            container = parts[:-1]
            value = expressions_[index] if aligned else None
            inferred = _inferred_type(value)
            if inferred in {None, "function", "table"}:
                continue
            if container and container[0] not in module_names:
                continue
            target = ast_field(name_node, "field") or ast_field(name_node, "method")
            if target is not None and target.type == "string":
                target = next(
                    (
                        child
                        for child in named_children(target)
                        if child.type == "string_content"
                    ),
                    target,
                )
            target = target or name_node
            symbol = Symbol(
                symbol_id(source, container, SymbolKind.CONSTANT, name),
                node_span(source, target),
                Visibility.PRIVATE if not container else Visibility.PUBLIC,
                name,
                returns=inferred,
            )
            values.setdefault(symbol.id, symbol)
    return tuple(values.values())


def extract(source: SourceFile, parser: object | None):
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("Lua extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    assignments = _assignment_pairs(root)
    modules = _module_candidates(root, assignments)
    module_names = frozenset(name for name, _ in modules)
    callables = _callables(root, assignments)
    named_nodes = tuple(callable_.node for callable_ in callables)
    ownership = ownership_context(named_nodes)
    inclusive_ownership = ownership_context(named_nodes, include_anonymous=True)

    symbols: list[Symbol] = [
        Symbol(
            symbol_id(source, (), SymbolKind.MODULE, name),
            node_span(source, name_node),
            Visibility.PUBLIC,
            f"module {name}",
        )
        for name, name_node in modules
    ]
    symbols.extend(_constant_declarations(source, root, module_names))
    calls: list[CallRef] = []
    references: list[ReferenceRef] = [
        *_possible_name_references(
            body_events(source, root, ownership=inclusive_ownership)
        ),
        *_possible_source_references(source, root),
    ]
    bodies: list[BodyIR] = []
    for callable_ in callables:
        kind = SymbolKind.METHOD if callable_.member else SymbolKind.FUNCTION
        symbol = Symbol(
            symbol_id(
                source,
                callable_.container_path,
                kind,
                callable_.name,
                callable_.params,
            ),
            node_span(source, callable_.node),
            Visibility.PRIVATE
            if callable_.local or callable_.name.startswith("_")
            else Visibility.PUBLIC,
            f"{callable_.name}({','.join(callable_.params)})",
            params=callable_.params,
            bindings=binding_tuple(
                (
                    *(
                        (Binding("self", callable_.container_path[-1]),)
                        if callable_.method
                        else ()
                    ),
                    *(Binding(name, "?") for name in callable_.parameter_names),
                )
            ),
            modifiers=("local",) if callable_.local else (),
            body_lines=body_lines(ast_field(callable_.node, "body")),
        )
        body = ast_field(callable_.node, "body")
        events = body_events(
            source,
            callable_.node,
            ownership=ownership,
        )
        events = _join_static_bracket_events(
            source,
            callable_.node,
            events,
            ownership,
        )
        event_bindings = tuple(
            Binding(event.text, "?")
            for event in events
            if event.kind in {BodyEventKind.LOCAL, BodyEventKind.PARAM}
        )
        symbol = Symbol(
            symbol.id,
            symbol.span,
            symbol.visibility,
            symbol.signature,
            symbol.params,
            symbol.returns,
            symbol.supers,
            symbol.permits,
            symbol.raises,
            binding_tuple((*symbol.bindings, *event_bindings)),
            symbol.components,
            symbol.annotations,
            symbol.modifiers,
            symbol.body_lines,
        )
        symbols.append(symbol)
        if body is not None:
            bodies.append(BodyIR(symbol.id, node_span(source, body), events))
        calls.extend(_owned_calls(source, symbol.id, callable_.node, ownership))
        references.extend(
            _references(source, symbol.id, callable_.node, events, ownership)
        )
        references.extend(
            _possible_name_references(
                body_events(
                    source,
                    callable_.node,
                    ownership=inclusive_ownership,
                ),
                exclude=events,
            )
        )

    module = modules[0][0] if len(modules) == 1 else None
    return assemble_file_ir(
        source,
        module=module,
        symbols=symbols,
        calls=calls,
        imports=_imports(source, root, assignments),
        references=references,
        bodies=bodies,
        diagnostics=syntax_diagnostics(source, root, "Lua"),
    )


__all__ = ["extract"]
