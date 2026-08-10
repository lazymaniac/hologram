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
_INDEX_KINDS = frozenset({"dot_index_expression", "method_index_expression"})
_IDENTIFIER_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)


def _key(node: object) -> tuple[int, int, str]:
    return (int(node.start_byte), int(node.end_byte), str(node.type))  # type: ignore[attr-defined]


def _string_value(node: object | None) -> str | None:
    raw = ast_text(node).strip()
    if not raw:
        return None
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        if raw.startswith("[[") and raw.endswith("]]"):
            return raw[2:-2]
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


def _returned_names(root: object) -> frozenset[str]:
    values: set[str] = set()
    for node in walk_all(root):
        if node.type != "return_statement":
            continue
        parent = getattr(node, "parent", None)
        inside_function = False
        while parent is not None:
            if parent.type in _FUNCTION_KINDS:
                inside_function = True
                break
            parent = getattr(parent, "parent", None)
        if inside_function:
            continue
        values.update(
            ast_text(child) for child in walk_all(node) if child.type == "identifier"
        )
    return frozenset(values)


def _module_candidates(
    root: object,
    pairs: dict[tuple[int, int, str], Any],
) -> tuple[tuple[str, Any], ...]:
    returned = _returned_names(root)
    qualified_roots = {
        parts[0]
        for node in walk_all(root)
        if node.type == "function_declaration"
        if len(parts := _index_parts(ast_field(node, "name"))) > 1
    }
    values: list[tuple[str, Any]] = []
    for value_key, name_node in pairs.items():
        if value_key[2] != "table_constructor":
            continue
        parts = _index_parts(name_node)
        if len(parts) != 1:
            continue
        name = parts[0]
        if name in returned or name in qualified_roots:
            values.append((name, name_node))
    return ordered_unique(values)


def _require_call(node: Any) -> bool:
    return node.type == "function_call" and _index_parts(ast_field(node, "name")) == (
        "require",
    )


def _require_module(node: object) -> str | None:
    arguments = ast_field(node, "arguments")
    string = next(
        (child for child in walk_all(arguments) if child.type == "string"),
        None,
    )
    return _string_value(string)


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
    current = node
    while getattr(current, "parent", None) is not None:
        current = current.parent
        if current.type == "variable_declaration":
            return True
        if current.type in _FUNCTION_KINDS:
            return False
    return any(ast_text(child) == "local" for child in getattr(node, "children", ()))


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
        name_node = (
            ast_field(node, "name")
            if node.type == "function_declaration"
            else assignments.get(_key(node))
        )
        parts = _index_parts(name_node)
        if not parts:
            continue
        name = parts[-1]
        explicit_container = parts[:-1]
        container = explicit_container or lexical_owner(node)
        params, parameter_names = _parameters(node)
        signature_key = f"({','.join(params)})"
        owned = (*container, f"{name}{signature_key}")
        value = _Callable(
            node,
            name_node,
            name,
            container,
            owned,
            params,
            parameter_names,
            bool(explicit_container),
            bool(name_node is not None and name_node.type == "method_index_expression"),
            _is_local(node) or (not explicit_container and bool(container)),
        )
        values.append(value)
        owned_paths[_key(node)] = owned
    return tuple(values)


def _call_parts(node: object | None) -> tuple[str | None, str] | None:
    parts = _index_parts(node)
    if not parts:
        return None
    return (".".join(parts[:-1]) or None, parts[-1])


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
        if not _require_call(node)
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
        table = ast_field(node, "table")
        field = ast_field(node, "field") or ast_field(node, "method")
        if table is not None and field is not None:
            values[node_span(source, field)] = ast_text(table)
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
    values: list[Symbol] = []
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
            if not parts or not parts[-1].isupper():
                continue
            name = parts[-1]
            container = parts[:-1]
            if not container and name in module_names:
                continue
            if container and container[0] not in module_names:
                continue
            target = (
                ast_field(name_node, "field")
                or ast_field(name_node, "method")
                or name_node
            )
            value = expressions_[index] if aligned else None
            values.append(
                Symbol(
                    symbol_id(source, container, SymbolKind.CONSTANT, name),
                    node_span(source, target),
                    Visibility.PRIVATE if not container else Visibility.PUBLIC,
                    name,
                    returns=_inferred_type(value),
                )
            )
    return ordered_unique(values)


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
    ownership = ownership_context(named_nodes, include_anonymous=True)

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
    references: list[ReferenceRef] = []
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
