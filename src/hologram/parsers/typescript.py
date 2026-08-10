from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from hologram.model import (
    Binding,
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    ImportRef,
    Language,
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

from .common import base_type, ordered_unique, reference, symbol_id, tight_type
from .treesitter import (
    OwnershipContext,
    ast_collect,
    ast_field,
    ast_text,
    body_events,
    body_lines,
    node_span,
    owned_nodes,
    ownership_context,
)

_TYPESCRIPT_LANGUAGES = frozenset(
    {
        Language.TYPESCRIPT,
        Language.JAVASCRIPT,
        Language.TSX,
        Language.VUE,
        Language.SVELTE,
    }
)
_TYPE_KINDS = {
    "abstract_class_declaration": SymbolKind.CLASS,
    "class_declaration": SymbolKind.CLASS,
    "enum_declaration": SymbolKind.ENUM,
    "interface_declaration": SymbolKind.INTERFACE,
    "type_alias_declaration": SymbolKind.TYPE,
}
_CALLABLE_VALUES = frozenset(
    {"arrow_function", "function_expression", "generator_function"}
)
_FUNCTION_DECLARATIONS = frozenset(
    {
        "function_declaration",
        "function_signature",
        "generator_function_declaration",
    }
)
_CALLABLE_DECLARATIONS = frozenset(
    {
        "abstract_method_signature",
        "function_declaration",
        "function_signature",
        "generator_function_declaration",
        "method_definition",
        "method_signature",
    }
)
_FIELD_KINDS = frozenset(
    {
        "abstract_property_signature",
        "property_signature",
        "public_field_definition",
    }
)
_NAMESPACE_KINDS = frozenset({"internal_module", "module"})
_EXPORT_SCOPE_KINDS = frozenset({"program", "statement_block"})
_TYPE_LEAVES = frozenset({"identifier", "type_identifier"})
_NAME_LEAVES = frozenset(
    {
        "identifier",
        "private_property_identifier",
        "property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "type_identifier",
    }
)
_PRIMITIVE_TYPES = frozenset(
    {
        "any",
        "bigint",
        "boolean",
        "never",
        "null",
        "number",
        "object",
        "string",
        "symbol",
        "undefined",
        "unknown",
        "void",
    }
)
_MODIFIER_TOKENS = frozenset(
    {
        "abstract",
        "async",
        "declare",
        "default",
        "export",
        "get",
        "override",
        "readonly",
        "static",
        "set",
    }
)
_REGISTRATION_CALLS = frozenset(
    {
        "add_listener",
        "config",
        "configure",
        "register",
        "register_callback",
        "register_handler",
        "set_callback",
    }
)
_CALLBACK_KEYS = frozenset({"callback", "handler", "listener", "target"})
_IDENTIFIER_RE = re.compile(r"^(?:[^\W\d]|\$)[\w$]*$", re.UNICODE)
_SCRIPT_OPEN_RE = re.compile(br"<script\b[^>]*>", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(br"</script\s*>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Parameter:
    type_name: str
    names: tuple[str, ...]
    node: Any
    type_node: Any | None


@dataclass(frozen=True, slots=True)
class _CallableDraft:
    name: str
    kind: SymbolKind
    declaration: Any
    callable_node: Any
    container_path: tuple[str, ...]
    visibility: Visibility
    annotation_nodes: tuple[Any, ...]
    modifiers: tuple[str, ...]
    class_bindings: tuple[Binding, ...]


@dataclass(frozen=True, slots=True)
class _Callable:
    symbol: Symbol
    node: Any
    owned: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _Region:
    symbol: Symbol
    node: Any


def _children(node: object | None) -> tuple[Any, ...]:
    return tuple(getattr(node, "children", ())) if node is not None else ()


def _named_children(node: object | None) -> tuple[Any, ...]:
    return tuple(child for child in _children(node) if child.is_named)


def _field_nodes(node: object | None, name: str) -> tuple[Any, ...]:
    if node is None:
        return ()
    many = getattr(node, "children_by_field_name", None)
    values = tuple(many(name)) if callable(many) else ()
    if values:
        return values
    value = ast_field(node, name)
    return (value,) if value is not None else ()


def _same_node(left: object | None, right: object | None) -> bool:
    if left is None or right is None:
        return False
    return (
        getattr(left, "start_byte", None) == getattr(right, "start_byte", None)
        and getattr(left, "end_byte", None) == getattr(right, "end_byte", None)
        and getattr(left, "type", None) == getattr(right, "type", None)
    )


def _unique_nodes(nodes: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for node in nodes:
        if all(not _same_node(node, existing) for existing in result):
            result.append(node)
    return tuple(result)


def _field_name(parent: Any | None, node: Any) -> str | None:
    if parent is None:
        return None
    field_name = getattr(parent, "field_name_for_child", None)
    if not callable(field_name):
        return None
    for index, child in enumerate(_children(parent)):
        if _same_node(child, node):
            value = field_name(index)
            return str(value) if value is not None else None
    return None


def _direct_child(node: object | None, kinds: Iterable[str]) -> Any | None:
    selected = frozenset(kinds)
    return next(
        (child for child in _named_children(node) if child.type in selected),
        None,
    )


def _module_name(file: str) -> str:
    return PurePosixPath(file).with_suffix("").as_posix()


def _source_span(source: SourceFile) -> SourceSpan:
    lines = source.raw.splitlines(keepends=True)
    if not lines:
        return SourceSpan(source.file, 1, 0, 1, 0)
    if source.raw.endswith((b"\n", b"\r")):
        return SourceSpan(source.file, 1, 0, len(lines) + 1, 0)
    return SourceSpan(source.file, 1, 0, len(lines), len(lines[-1]))


def _sfc_program_buffers(raw: bytes) -> tuple[tuple[bytes, ...], bool]:
    blank = bytes(
        byte if byte in {0x0A, 0x0D} else 0x20
        for byte in raw
    )
    programs: list[bytes] = []
    position = 0
    unclosed = False
    while (opening := _SCRIPT_OPEN_RE.search(raw, position)) is not None:
        comment = raw.find(b"<!--", position, opening.start())
        if comment >= 0:
            comment_end = raw.find(b"-->", comment + 4)
            if comment_end < 0:
                break
            position = comment_end + 3
            continue
        program = bytearray(blank)
        closing = _SCRIPT_CLOSE_RE.search(raw, opening.end())
        if closing is None:
            program[opening.end() :] = raw[opening.end() :]
            programs.append(bytes(program))
            unclosed = True
            break
        program[opening.end() : closing.start()] = raw[opening.end() : closing.start()]
        programs.append(bytes(program))
        position = closing.end()
    return tuple(programs) or (blank,), unclosed


def _string_value(node: Any | None) -> str:
    if node is None:
        return ""
    fragment = _direct_child(node, {"string_fragment"})
    if fragment is not None:
        return ast_text(fragment)
    raw = ast_text(node)
    if len(raw) >= 2 and raw[:1] in {'"', "'", "`"} and raw[-1:] == raw[:1]:
        return raw[1:-1]
    return raw


def _type_text(node: Any | None) -> str | None:
    if node is None:
        return None
    value = ast_text(node).strip()
    if value.startswith(":"):
        value = value[1:].strip()
    return tight_type(value) or None


def _simple_type(type_name: str) -> str:
    return base_type(type_name.removesuffix("...")).strip() or type_name


def _pattern_names(node: Any | None) -> tuple[str, ...]:
    if node is None:
        return ()
    if node.type in {
        "identifier",
        "private_property_identifier",
        "shorthand_property_identifier_pattern",
        "this",
    }:
        return (ast_text(node),)
    if node.type in {"assignment_pattern", "object_assignment_pattern"}:
        return tuple(
            name
            for child in _field_nodes(node, "left")
            for name in _pattern_names(child)
        )
    if node.type == "pair_pattern":
        return tuple(
            name
            for child in _field_nodes(node, "value")
            for name in _pattern_names(child)
        )
    names: list[str] = []
    for child in _named_children(node):
        field = _field_name(node, child)
        if field in {"key", "right", "type"} or child.type in {
            "type_annotation",
            "type_identifier",
        }:
            continue
        names.extend(_pattern_names(child))
    return ordered_unique(names)


def _parameters(node: Any) -> tuple[_Parameter, ...]:
    roots = (*_field_nodes(node, "parameters"), *_field_nodes(node, "parameter"))
    parameters: list[Any] = []
    for root in roots:
        if root.type == "formal_parameters":
            parameters.extend(_named_children(root))
        else:
            parameters.append(root)
    result: list[_Parameter] = []
    for parameter in parameters:
        if parameter.type in {"required_parameter", "optional_parameter"}:
            pattern = ast_field(parameter, "pattern")
            type_node = ast_field(parameter, "type")
        else:
            pattern = parameter
            type_node = ast_field(parameter, "type")
        names = _pattern_names(pattern)
        if not names:
            continue
        result.append(_Parameter(_type_text(type_node) or "?", names, parameter, type_node))
    return tuple(result)


def _annotation_text(node: Any) -> str:
    return ast_text(node).strip().removeprefix("@")


def _annotations(nodes: Iterable[Any]) -> tuple[str, ...]:
    return ordered_unique(
        text for node in nodes if (text := _annotation_text(node))
    )


def _accessibility(node: Any) -> Visibility | None:
    modifier = _direct_child(node, {"accessibility_modifier"})
    raw = ast_text(modifier)
    return {
        "private": Visibility.PRIVATE,
        "protected": Visibility.PROTECTED,
        "public": Visibility.PUBLIC,
    }.get(raw)


def _modifiers(node: Any, wrapper: Iterable[str] = ()) -> tuple[str, ...]:
    values = list(wrapper)
    for child in _children(node):
        if child.type == "accessibility_modifier":
            continue
        text = ast_text(child).strip()
        if text in _MODIFIER_TOKENS:
            values.append(text)
    return ordered_unique(values)


def _wrapper_modifiers(node: Any) -> tuple[str, ...]:
    return tuple(
        ast_text(child)
        for child in _children(node)
        if not child.is_named and ast_text(child) in {"default", "export"}
    )


def _local_export_names(node: Any) -> frozenset[str]:
    names: list[str] = []
    for child in _named_children(node):
        if (
            child.type != "export_statement"
            or ast_field(child, "source") is not None
            or ast_field(child, "declaration") is not None
        ):
            continue
        clause = _direct_child(child, {"export_clause"})
        for specifier in _named_children(clause):
            if specifier.type != "export_specifier":
                continue
            name = ast_text(ast_field(specifier, "name"))
            if name:
                names.append(name)
    return frozenset(names)


def _visibility(
    node: Any,
    *,
    exported: bool,
    member: bool,
) -> Visibility:
    name = ast_field(node, "name")
    if name is not None and name.type == "private_property_identifier":
        return Visibility.PRIVATE
    return _accessibility(node) or (
        Visibility.PUBLIC if exported or member else Visibility.PRIVATE
    )


def _parameter_bindings(parameters: Iterable[_Parameter]) -> tuple[Binding, ...]:
    return tuple(
        Binding(name, _simple_type(parameter.type_name))
        for parameter in parameters
        for name in parameter.names
    )


def _binding_tuple(bindings: Iterable[Binding]) -> tuple[Binding, ...]:
    values: dict[str, str] = {}
    for binding in bindings:
        values[binding.name] = binding.type_name
    return tuple(Binding(name, type_name) for name, type_name in values.items())


def _inferred_type(value: Any | None) -> str | None:
    if value is None or value.type != "new_expression":
        return None
    constructor = ast_field(value, "constructor")
    return _simple_type(ast_text(constructor)) if constructor is not None else None


def _class_bindings(body: Any | None) -> tuple[Binding, ...]:
    bindings: list[Binding] = []
    for member in _named_children(body):
        if member.type == "public_field_definition":
            value = ast_field(member, "value")
            if value is not None and value.type in _CALLABLE_VALUES:
                continue
            name = ast_text(ast_field(member, "name"))
            type_name = _type_text(ast_field(member, "type")) or _inferred_type(value)
            if name and type_name:
                bindings.append(Binding(name, _simple_type(type_name)))
        elif member.type == "method_definition" and ast_text(ast_field(member, "name")) == "constructor":
            for parameter in _parameters(member):
                if _accessibility(parameter.node) is not None or any(
                    ast_text(child) == "readonly" for child in _children(parameter.node)
                ):
                    bindings.extend(_parameter_bindings((parameter,)))
    return _binding_tuple(bindings)


def _local_bindings(nodes: Iterable[Any]) -> tuple[Binding, ...]:
    bindings: list[Binding] = []
    for candidate in nodes:
        if candidate.type == "variable_declarator":
            type_name = _type_text(ast_field(candidate, "type")) or _inferred_type(
                ast_field(candidate, "value")
            )
            if type_name:
                for name in _pattern_names(ast_field(candidate, "name")):
                    bindings.append(Binding(name, _simple_type(type_name)))
        elif candidate.type == "catch_clause":
            parameter = ast_field(candidate, "parameter")
            type_name = _type_text(ast_field(parameter, "type")) if parameter else None
            if type_name:
                for name in _pattern_names(parameter):
                    bindings.append(Binding(name, _simple_type(type_name)))
    return _binding_tuple(bindings)


def _heritage(node: Any) -> tuple[str, ...]:
    names: list[str] = []
    clause_kinds = {"extends_clause", "extends_type_clause", "implements_clause"}
    relations: list[Any] = []
    for child in _named_children(node):
        if child.type == "class_heritage":
            relations.extend(
                relation
                for relation in _named_children(child)
                if relation.type in clause_kinds
            )
        elif child.type in clause_kinds:
            relations.append(child)
    for relation in relations:
        for child in _named_children(relation):
            raw = ast_text(child).strip()
            if raw:
                names.append(re.sub(r"<.*", "", raw).rsplit(".", 1)[-1])
    return ordered_unique(names)


def _enum_members(body: Any | None) -> tuple[tuple[str, Any], ...]:
    result: list[tuple[str, Any]] = []
    for child in _named_children(body):
        if child.type not in {
            "enum_assignment",
            "number",
            "property_identifier",
            "string",
        }:
            continue
        name_node = ast_field(child, "name") if child.type == "enum_assignment" else child
        name = ast_text(name_node)
        if name:
            result.append((name, child))
    return tuple(result)


def _method_name(node: Any, type_name: str | None = None) -> tuple[str, SymbolKind]:
    raw = ast_text(ast_field(node, "name"))
    if raw == "constructor":
        return type_name or raw, SymbolKind.CONSTRUCTOR
    return raw, SymbolKind.METHOD


def _node_identity(node: Any) -> tuple[int, int, str]:
    return node.start_byte, node.end_byte, node.type


def _wrapped_callables(
    node: Any,
    allowed: frozenset[str],
) -> tuple[Any, ...]:
    if node.type in allowed:
        return (node,)
    if node.type == "export_statement":
        declaration = ast_field(node, "declaration")
        return _wrapped_callables(declaration, allowed) if declaration is not None else ()
    if node.type == "ambient_declaration":
        return tuple(
            callable_node
            for child in _named_children(node)
            for callable_node in _wrapped_callables(child, allowed)
        )
    return ()


def _callable_path_segment(name: str, node: Any) -> str:
    params = tuple(parameter.type_name for parameter in _parameters(node))
    return f"{name}({','.join(params)})"


def _property_modifiers(node: Any) -> tuple[str, ...]:
    return _modifiers(node)


def _type_reference_nodes(roots: Iterable[Any | None]) -> tuple[Any, ...]:
    found: list[Any] = []
    for root in roots:
        if root is None:
            continue
        for node in ast_collect(root, _TYPE_LEAVES):
            parent = node.parent
            if (
                parent is not None
                and parent.type == "type_parameter"
                and _field_name(parent, node) == "name"
            ):
                continue
            if (
                parent is not None
                and parent.type == "nested_type_identifier"
                and _field_name(parent, node) == "module"
            ):
                continue
            found.append(node)
    return tuple(found)


def _qualified_type(node: Any) -> tuple[str | None, str]:
    parent = node.parent
    if parent is not None and parent.type == "nested_type_identifier":
        name_node = ast_field(parent, "name")
        module_node = ast_field(parent, "module")
        if _same_node(name_node, node):
            return ast_text(module_node) or None, ast_text(node)
    return None, ast_text(node)


def _type_references(
    source: SourceFile,
    owner: SymbolId,
    roots: Iterable[Any | None],
) -> tuple[ReferenceRef, ...]:
    references: list[ReferenceRef] = []
    for node in _type_reference_nodes(roots):
        qualifier, name = _qualified_type(node)
        if not name or name in _PRIMITIVE_TYPES:
            continue
        references.append(
            reference(
                owner,
                node_span(source, node),
                name,
                qualifier,
                ReferenceKind.TYPE,
                context=ReferenceContext.TYPE,
                confidence=ReferenceConfidence.DEFINITE,
            )
        )
    return ordered_unique(references)


def _annotation_target(node: Any) -> Any | None:
    target = next((child for child in _named_children(node)), None)
    if target is not None and target.type == "call_expression":
        target = ast_field(target, "function")
    if target is not None and target.type == "member_expression":
        return ast_field(target, "property")
    return target


def _annotation_references(
    source: SourceFile,
    owner: SymbolId,
    nodes: Iterable[Any],
) -> tuple[ReferenceRef, ...]:
    result: list[ReferenceRef] = []
    for decorator in nodes:
        target = _annotation_target(decorator)
        if target is None:
            continue
        name = ast_text(target)
        qualifier = None
        parent = target.parent
        if parent is not None and parent.type == "member_expression":
            qualifier = ast_text(ast_field(parent, "object")) or None
        result.append(
            reference(
                owner,
                node_span(source, target),
                name,
                qualifier,
                ReferenceKind.TYPE,
                context=ReferenceContext.ANNOTATION,
                confidence=ReferenceConfidence.POSSIBLE,
            )
        )
    return ordered_unique(result)


def _argument_count(node: Any) -> int | None:
    arguments = ast_field(node, "arguments")
    if arguments is None:
        return 0
    values = tuple(
        child
        for child in _named_children(arguments)
        if child.type not in {"comment", "ERROR"}
    )
    if any(value.type == "spread_element" for value in values):
        return None
    return len(values)


def _callee(node: Any) -> tuple[str | None, str] | None:
    construct = node.type == "new_expression"
    target = ast_field(node, "constructor" if construct else "function")
    if target is None:
        return None
    while target.type in {"non_null_expression", "parenthesized_expression"}:
        named = _named_children(target)
        if not named:
            break
        target = named[0]
    if target.type == "member_expression":
        name_node = ast_field(target, "property")
        receiver_node = ast_field(target, "object")
        if name_node is None:
            return None
        return ast_text(receiver_node) or None, ast_text(name_node)
    raw = ast_text(target)
    if not raw:
        return None
    if construct:
        raw = re.sub(r"<.*", "", raw)
    if construct and "." in raw:
        receiver, name = raw.rsplit(".", 1)
        return receiver or None, name
    return None, raw


def _calls(
    source: SourceFile,
    owner: SymbolId,
    nodes: Iterable[Any],
) -> tuple[CallRef, ...]:
    calls: list[CallRef] = []
    for candidate in nodes:
        if candidate.type not in {"call_expression", "new_expression"}:
            continue
        parts = _callee(candidate)
        if parts is None:
            continue
        receiver, name = parts
        calls.append(
            CallRef(
                owner,
                node_span(source, candidate),
                name,
                receiver,
                CallKind.CONSTRUCT
                if candidate.type == "new_expression"
                else CallKind.CALL,
                _argument_count(candidate),
            )
        )
    return ordered_unique(sorted(calls, key=lambda call: call.span))


def _node_by_span(source: SourceFile, nodes: Iterable[Any]) -> dict[SourceSpan, Any]:
    result: dict[SourceSpan, Any] = {}
    for node in nodes:
        if node.type in _NAME_LEAVES | {"this", "super"}:
            result[node_span(source, node)] = node
    return result


def _body_references(
    source: SourceFile,
    owner: SymbolId,
    events: Iterable[BodyEvent],
    nodes: Iterable[Any],
) -> tuple[ReferenceRef, ...]:
    by_span = _node_by_span(source, nodes)
    references: list[ReferenceRef] = []
    for event in events:
        if event.kind not in {BodyEventKind.NAME, BodyEventKind.TYPE}:
            continue
        syntax = by_span.get(event.span)
        if syntax is None:
            continue
        name = ast_text(syntax)
        if not name or name in {"super", "this"}:
            continue
        parent = syntax.parent
        field = _field_name(parent, syntax)
        parent_type = parent.type if parent is not None else ""
        if (
            parent_type in _FIELD_KINDS | {"enum_assignment"}
            and field == "name"
        ):
            continue
        if parent_type == "enum_body" and syntax.type in {
            "number",
            "property_identifier",
            "string",
        }:
            continue
        if (
            parent_type == "jsx_attribute"
            and syntax.type == "property_identifier"
        ):
            continue
        if (
            parent_type
            in {
                "jsx_closing_element",
                "jsx_opening_element",
                "jsx_self_closing_element",
            }
            and field == "name"
            and ast_text(syntax)[:1].islower()
        ):
            continue
        if parent is not None and parent.type in {"pair", "pair_pattern"} and field == "key":
            continue
        if parent is not None and parent.type == "nested_type_identifier" and field == "module":
            continue
        qualifier: str | None = None
        if parent is not None and parent.type == "member_expression" and field == "property":
            qualifier = ast_text(ast_field(parent, "object")) or None
        elif event.kind is BodyEventKind.TYPE:
            qualifier, name = _qualified_type(syntax)
        if event.kind is BodyEventKind.TYPE:
            if name in _PRIMITIVE_TYPES:
                continue
            kind = ReferenceKind.TYPE
            context = ReferenceContext.TYPE
        else:
            if not _IDENTIFIER_RE.fullmatch(name):
                continue
            kind = ReferenceKind.NAME
            context = ReferenceContext.CODE
        references.append(
            reference(
                owner,
                event.span,
                name,
                qualifier,
                kind,
                context=context,
                confidence=ReferenceConfidence.DEFINITE,
            )
        )
    return ordered_unique(references)


def _registration_name(call: Any) -> str | None:
    parts = _callee(call)
    return parts[1] if parts is not None else None


def _config_references(
    source: SourceFile,
    owner: SymbolId,
    nodes: Iterable[Any],
) -> tuple[ReferenceRef, ...]:
    references: list[ReferenceRef] = []
    for call in nodes:
        if call.type != "call_expression" or _registration_name(call) not in _REGISTRATION_CALLS:
            continue
        arguments = ast_field(call, "arguments")
        for value in _named_children(arguments):
            if value.type != "object":
                continue
            for pair in _named_children(value):
                if pair.type != "pair":
                    continue
                key = ast_text(ast_field(pair, "key")).strip('"\'')
                callback = ast_field(pair, "value")
                if key not in _CALLBACK_KEYS or callback is None or callback.type != "string":
                    continue
                name = _string_value(callback)
                if not _IDENTIFIER_RE.fullmatch(name):
                    continue
                references.append(
                    reference(
                        owner,
                        node_span(source, callback),
                        name,
                        None,
                        ReferenceKind.NAME,
                        context=ReferenceContext.CONFIG,
                        confidence=ReferenceConfidence.POSSIBLE,
                    )
                )
    return ordered_unique(references)


def _join_reference_events(
    events: tuple[BodyEvent, ...],
    references: Iterable[ReferenceRef],
) -> tuple[BodyEvent, ...]:
    additions = {
        reference.span: reference.name
        for reference in references
        if reference.kind is ReferenceKind.NAME
        and reference.confidence is ReferenceConfidence.POSSIBLE
    }
    existing = {
        (event.kind, event.span) for event in events
    }
    result: list[BodyEvent] = []
    for event in events:
        result.append(event)
        if (
            event.kind is BodyEventKind.LITERAL
            and event.span in additions
            and (BodyEventKind.NAME, event.span) not in existing
        ):
            result.append(BodyEvent(BodyEventKind.NAME, additions[event.span], event.span))
    return tuple(result)


def _imports_and_reexports(
    source: SourceFile,
    root: Any,
) -> tuple[tuple[ImportRef, ...], tuple[Symbol, ...], tuple[Any, ...]]:
    imports: list[ImportRef] = []
    symbols: list[Symbol] = []
    boundaries: list[Any] = []
    for node in ast_collect(root, {"export_statement", "import_statement"}):
        source_node = ast_field(node, "source")
        module = _string_value(source_node)
        if node.type == "import_statement":
            boundaries.append(node)
            clause = _direct_child(node, {"import_clause"})
            if clause is None:
                if source_node is not None:
                    imports.append(ImportRef(node_span(source, source_node), module, None, None))
                continue
            for child in _named_children(clause):
                if child.type == "identifier":
                    imports.append(
                        ImportRef(node_span(source, child), module, "default", ast_text(child))
                    )
                elif child.type == "namespace_import":
                    alias = _direct_child(child, {"identifier"})
                    imports.append(
                        ImportRef(
                            node_span(source, child),
                            module,
                            None,
                            ast_text(alias) or None,
                            wildcard=True,
                        )
                    )
                elif child.type == "named_imports":
                    for specifier in _named_children(child):
                        if specifier.type != "import_specifier":
                            continue
                        name = ast_text(ast_field(specifier, "name"))
                        alias = ast_text(ast_field(specifier, "alias")) or None
                        imports.append(
                            ImportRef(node_span(source, specifier), module, name, alias)
                        )
            continue
        if source_node is None:
            continue
        boundaries.append(node)
        clause = _direct_child(node, {"export_clause", "namespace_export"})
        if clause is None:
            imports.append(
                ImportRef(node_span(source, node), module, None, None, True, True)
            )
            continue
        specifiers = tuple(
            child for child in _named_children(clause) if child.type == "export_specifier"
        )
        if not specifiers and clause.type == "namespace_export":
            alias_node = _direct_child(clause, {"identifier"})
            alias = ast_text(alias_node) or None
            imports.append(
                ImportRef(node_span(source, clause), module, None, alias, True, True)
            )
            if alias:
                symbols.append(
                    Symbol(
                        symbol_id(source, (), SymbolKind.REEXPORT, alias),
                        node_span(source, clause),
                        Visibility.PUBLIC,
                        alias,
                    )
                )
            continue
        for specifier in specifiers:
            imported = ast_text(ast_field(specifier, "name"))
            alias = ast_text(ast_field(specifier, "alias")) or None
            current = alias or imported
            imports.append(
                ImportRef(
                    node_span(source, specifier),
                    module,
                    imported,
                    alias,
                    reexport=True,
                )
            )
            symbols.append(
                Symbol(
                    symbol_id(source, (), SymbolKind.REEXPORT, current),
                    node_span(source, specifier),
                    Visibility.PUBLIC,
                    current,
                    modifiers=("export",),
                )
            )
    return tuple(imports), tuple(symbols), tuple(boundaries)


def _walk_all(root: Any) -> Iterable[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(_children(node)))


def _syntax_diagnostics(
    source: SourceFile,
    root: Any,
    *,
    unclosed_script: bool,
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    if root.has_error:
        erroneous = [
            node
            for node in _walk_all(root)
            if node.is_error or node.is_missing
        ]
        target = min(
            erroneous,
            key=lambda item: (item.start_byte, item.end_byte),
            default=root,
        )
        diagnostics.append(
            Diagnostic(
                "tree-sitter-syntax-error",
                DiagnosticSeverity.ERROR,
                f"{source.file}: TypeScript-family syntax tree contains an error",
                node_span(source, target),
            )
        )
    if unclosed_script:
        diagnostics.append(
            Diagnostic(
                "sfc-unclosed-script",
                DiagnosticSeverity.ERROR,
                f"{source.file}: <script> block is not closed",
            )
        )
    return tuple(diagnostics)


class _Declarations:
    def __init__(self, source: SourceFile) -> None:
        self.source = source
        self.symbols: list[Symbol] = []
        self.callable_drafts: list[_CallableDraft] = []
        self.boundaries: list[Any] = []
        self.references: list[ReferenceRef] = []
        self.regions: list[_Region] = []
        self._export_scopes: list[frozenset[str]] = []
        self._callable_segments: dict[tuple[int, int, str], str] = {}

    @property
    def current_exports(self) -> frozenset[str]:
        return self._export_scopes[-1] if self._export_scopes else frozenset()

    def register_overload_segments(
        self,
        nodes: Iterable[Any],
        *,
        allowed: frozenset[str],
        type_name: str | None = None,
    ) -> None:
        groups: dict[str, list[Any]] = {}
        for node in nodes:
            for callable_node in _wrapped_callables(node, allowed):
                name = (
                    _method_name(callable_node, type_name)[0]
                    if type_name is not None
                    else ast_text(ast_field(callable_node, "name"))
                )
                if name:
                    groups.setdefault(name, []).append(callable_node)
        for name, overloads in groups.items():
            if len(overloads) < 2:
                continue
            for callable_node in overloads:
                self._callable_segments[_node_identity(callable_node)] = (
                    _callable_path_segment(name, callable_node)
                )

    def callable_segment(self, node: Any, name: str) -> str:
        return self._callable_segments.get(_node_identity(node), name)

    def scope(
        self,
        node: Any,
        container_path: tuple[str, ...],
        scope_kind: str,
        *,
        class_bindings: tuple[Binding, ...] = (),
    ) -> None:
        self.register_overload_segments(
            _named_children(node),
            allowed=_FUNCTION_DECLARATIONS,
        )
        export_scope = node.type in _EXPORT_SCOPE_KINDS
        if export_scope:
            self._export_scopes.append(_local_export_names(node))
        try:
            decorators: list[Any] = []
            for child in _named_children(node):
                if child.type == "comment":
                    continue
                if child.type == "decorator":
                    decorators.append(child)
                    continue
                self.declaration(
                    child,
                    container_path,
                    scope_kind,
                    decorators=tuple(decorators),
                    class_bindings=class_bindings,
                )
                decorators.clear()
        finally:
            if export_scope:
                self._export_scopes.pop()

    def declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
        scope_kind: str,
        *,
        exported: bool = False,
        decorators: tuple[Any, ...] = (),
        wrapper_modifiers: tuple[str, ...] = (),
        class_bindings: tuple[Binding, ...] = (),
    ) -> None:
        if node.type == "import_statement":
            self.boundaries.append(node)
            return
        if node.type == "export_statement":
            declaration = ast_field(node, "declaration")
            if declaration is None:
                if ast_field(node, "source") is not None:
                    self.boundaries.append(node)
                return
            wrapper_decorators = tuple(_field_nodes(node, "decorator"))
            self.declaration(
                declaration,
                container_path,
                scope_kind,
                exported=True,
                decorators=(*decorators, *wrapper_decorators),
                wrapper_modifiers=(*wrapper_modifiers, *_wrapper_modifiers(node)),
                class_bindings=class_bindings,
            )
            return
        if node.type == "ambient_declaration":
            for child in _named_children(node):
                self.declaration(
                    child,
                    container_path,
                    scope_kind,
                    exported=exported,
                    decorators=decorators,
                    wrapper_modifiers=(*wrapper_modifiers, "declare"),
                    class_bindings=class_bindings,
                )
            return
        if node.type in _NAMESPACE_KINDS:
            self.namespace_declaration(
                node,
                container_path,
                exported=exported,
                decorators=decorators,
                wrapper_modifiers=wrapper_modifiers,
            )
            return
        if node.type in _TYPE_KINDS:
            self.type_declaration(
                node,
                container_path,
                scope_kind,
                exported=exported,
                decorators=decorators,
                wrapper_modifiers=wrapper_modifiers,
            )
            return
        if node.type in _FUNCTION_DECLARATIONS:
            name = ast_text(ast_field(node, "name"))
            if not name:
                self.scope(node, container_path, scope_kind, class_bindings=class_bindings)
                return
            exported = exported or name in self.current_exports
            self.callable(
                name,
                SymbolKind.FUNCTION,
                node,
                node,
                container_path,
                exported=exported,
                member=False,
                decorators=decorators,
                wrapper_modifiers=wrapper_modifiers,
                class_bindings=(),
            )
            self.scope(
                node,
                (*container_path, self.callable_segment(node, name)),
                "callable",
            )
            return
        if node.type in {"lexical_declaration", "variable_declaration"}:
            self.variables(
                node,
                container_path,
                scope_kind,
                exported=exported,
                wrapper_modifiers=wrapper_modifiers,
            )
            return
        if node.type in _CALLABLE_VALUES:
            name = ast_text(ast_field(node, "name"))
            if name:
                self.callable(
                    name,
                    SymbolKind.FUNCTION,
                    node,
                    node,
                    container_path,
                    exported=exported,
                    member=False,
                    decorators=decorators,
                    wrapper_modifiers=wrapper_modifiers,
                    class_bindings=(),
                )
                self.scope(node, (*container_path, name), "callable")
            else:
                self.scope(node, container_path, scope_kind, class_bindings=class_bindings)
            return
        self.scope(node, container_path, scope_kind, class_bindings=class_bindings)

    def namespace_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        exported: bool,
        decorators: tuple[Any, ...],
        wrapper_modifiers: tuple[str, ...],
    ) -> None:
        name_node = ast_field(node, "name")
        name = (
            _string_value(name_node)
            if name_node is not None and name_node.type == "string"
            else ast_text(name_node)
        )
        if not name:
            self.scope(node, container_path, "module")
            return
        namespace_symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.MODULE, name),
            node_span(self.source, node),
            _visibility(
                node,
                exported=exported or name in self.current_exports,
                member=False,
            ),
            f"module {name}",
            annotations=_annotations(decorators),
            modifiers=_modifiers(node, wrapper_modifiers),
            body_lines=body_lines(ast_field(node, "body")),
        )
        self.symbols.append(namespace_symbol)
        self.boundaries.append(node)
        self.regions.append(_Region(namespace_symbol, node))
        body = ast_field(node, "body")
        if body is not None:
            self.scope(body, (*container_path, name), "module")

    def type_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
        scope_kind: str,
        *,
        exported: bool,
        decorators: tuple[Any, ...],
        wrapper_modifiers: tuple[str, ...],
    ) -> None:
        del scope_kind
        decorators = _unique_nodes((*decorators, *_field_nodes(node, "decorator")))
        name = ast_text(ast_field(node, "name"))
        if not name:
            self.scope(node, container_path, "anonymous")
            return
        exported = exported or name in self.current_exports
        kind = _TYPE_KINDS[node.type]
        body = ast_field(node, "body")
        enum_members = _enum_members(body) if kind is SymbolKind.ENUM else ()
        value = ast_field(node, "value")
        params = (
            tuple(member_name for member_name, _ in enum_members)
            if kind is SymbolKind.ENUM
            else ((_type_text(value) or ""),)
            if kind is SymbolKind.TYPE and value is not None
            else ()
        )
        components = params if kind is SymbolKind.ENUM else ()
        type_symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node, exported=exported, member=False),
            f"{kind.value} {name}" if kind is not SymbolKind.TYPE else f"type {name}",
            params=params,
            supers=_heritage(node),
            components=components,
            annotations=_annotations(decorators),
            modifiers=_modifiers(node, wrapper_modifiers),
        )
        self.symbols.append(type_symbol)
        self.boundaries.append(node)
        type_roots = [value]
        type_roots.extend(
            child
            for child in _named_children(node)
            if child.type
            in {
                "class_heritage",
                "extends_type_clause",
                "type_parameters",
            }
        )
        self.references.extend(_type_references(self.source, type_symbol.id, type_roots))
        self.references.extend(
            _annotation_references(self.source, type_symbol.id, decorators)
        )
        if kind in {SymbolKind.CLASS, SymbolKind.ENUM} and body is not None:
            self.regions.append(_Region(type_symbol, node))
        type_path = (*container_path, name)
        if kind is SymbolKind.ENUM:
            for member_name, member_node in enum_members:
                self.symbols.append(
                    Symbol(
                        symbol_id(
                            self.source,
                            type_path,
                            SymbolKind.CONSTANT,
                            member_name,
                        ),
                        node_span(self.source, member_node),
                        Visibility.PUBLIC,
                        member_name,
                    )
                )
            return
        bindings = _class_bindings(body) if kind is SymbolKind.CLASS else ()
        self.type_members(body, type_path, name, kind, bindings)

    def type_members(
        self,
        body: Any | None,
        container_path: tuple[str, ...],
        type_name: str,
        type_kind: SymbolKind,
        class_bindings: tuple[Binding, ...],
    ) -> None:
        self.register_overload_segments(
            _named_children(body),
            allowed=_CALLABLE_DECLARATIONS,
            type_name=type_name,
        )
        decorators: list[Any] = []
        for member in _named_children(body):
            if member.type == "comment":
                continue
            if member.type == "decorator":
                decorators.append(member)
                continue
            if member.type in _CALLABLE_DECLARATIONS:
                name, kind = _method_name(member, type_name)
                if name:
                    member_decorators = _unique_nodes(
                        (*decorators, *_field_nodes(member, "decorator"))
                    )
                    self.callable(
                        name,
                        kind,
                        member,
                        member,
                        container_path,
                        exported=False,
                        member=True,
                        decorators=member_decorators,
                        wrapper_modifiers=(),
                        class_bindings=class_bindings,
                    )
                    body_node = ast_field(member, "body")
                    if body_node is not None:
                        self.scope(
                            body_node,
                            (*container_path, self.callable_segment(member, name)),
                            "callable",
                        )
            elif member.type in _FIELD_KINDS:
                self.field(
                    member,
                    container_path,
                    type_name,
                    type_kind,
                    _unique_nodes(
                        (*decorators, *_field_nodes(member, "decorator"))
                    ),
                    class_bindings,
                )
            elif member.type in _TYPE_KINDS:
                self.type_declaration(
                    member,
                    container_path,
                    "type",
                    exported=False,
                    decorators=tuple(decorators),
                    wrapper_modifiers=(),
                )
            else:
                self.declaration(
                    member,
                    container_path,
                    "type",
                    decorators=tuple(decorators),
                    class_bindings=class_bindings,
                )
            decorators.clear()

    def field(
        self,
        node: Any,
        container_path: tuple[str, ...],
        type_name: str,
        type_kind: SymbolKind,
        decorators: tuple[Any, ...],
        class_bindings: tuple[Binding, ...],
    ) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        value = ast_field(node, "value")
        if value is not None and value.type in _CALLABLE_VALUES:
            self.callable(
                name,
                SymbolKind.METHOD,
                node,
                value,
                container_path,
                exported=False,
                member=True,
                decorators=decorators,
                wrapper_modifiers=(),
                class_bindings=class_bindings,
            )
            self.scope(value, (*container_path, name), "callable")
            return
        kind = (
            SymbolKind.PROPERTY
            if type_kind is SymbolKind.INTERFACE
            else SymbolKind.CONSTANT
            if {"static", "readonly"}.issubset(_modifiers(node))
            else SymbolKind.FIELD
        )
        field_symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node, exported=False, member=True),
            name,
            returns=_type_text(ast_field(node, "type")),
            annotations=_annotations(decorators),
            modifiers=_property_modifiers(node),
        )
        self.symbols.append(field_symbol)
        self.references.extend(
            _type_references(self.source, field_symbol.id, (ast_field(node, "type"),))
        )
        self.references.extend(
            _annotation_references(self.source, field_symbol.id, decorators)
        )

    def variables(
        self,
        node: Any,
        container_path: tuple[str, ...],
        scope_kind: str,
        *,
        exported: bool,
        wrapper_modifiers: tuple[str, ...],
    ) -> None:
        keyword = next(
            (
                ast_text(child)
                for child in _children(node)
                if not child.is_named and ast_text(child) in {"const", "let", "var"}
            ),
            "let",
        )
        for declarator in _named_children(node):
            if declarator.type != "variable_declarator":
                continue
            name_node = ast_field(declarator, "name")
            names = _pattern_names(name_node)
            value = ast_field(declarator, "value")
            locally_exported = exported or any(
                name in self.current_exports for name in names
            )
            if len(names) == 1 and value is not None and value.type in _CALLABLE_VALUES:
                name = names[0]
                self.callable(
                    name,
                    SymbolKind.FUNCTION,
                    declarator,
                    value,
                    container_path,
                    exported=locally_exported,
                    member=False,
                    decorators=(),
                    wrapper_modifiers=wrapper_modifiers,
                    class_bindings=(),
                )
                self.scope(value, (*container_path, name), "callable")
                continue
            if len(names) == 1 and value is not None and value.type == "object" and self.object_api(value):
                name = names[0]
                object_symbol = Symbol(
                    symbol_id(self.source, container_path, SymbolKind.CLASS, name),
                    node_span(self.source, declarator),
                    Visibility.PUBLIC if locally_exported else Visibility.PRIVATE,
                    f"{keyword} {name}",
                    modifiers=ordered_unique((*wrapper_modifiers, keyword)),
                )
                self.symbols.append(object_symbol)
                self.object_members(value, (*container_path, name))
                continue
            if scope_kind != "module":
                self.scope(declarator, container_path, scope_kind)
                continue
            for name in names:
                kind = SymbolKind.CONSTANT if keyword == "const" else SymbolKind.FIELD
                name_exported = exported or name in self.current_exports
                variable_symbol = Symbol(
                    symbol_id(self.source, container_path, kind, name),
                    node_span(self.source, declarator),
                    Visibility.PUBLIC if name_exported else Visibility.PRIVATE,
                    f"{keyword} {name}",
                    returns=_type_text(ast_field(declarator, "type")),
                    modifiers=ordered_unique((*wrapper_modifiers, keyword)),
                )
                self.symbols.append(variable_symbol)
                self.references.extend(
                    _type_references(
                        self.source,
                        variable_symbol.id,
                        (ast_field(declarator, "type"),),
                    )
                )

    def object_api(self, node: Any) -> bool:
        return any(
            member.type == "method_definition"
            or (
                member.type == "pair"
                and (value := ast_field(member, "value")) is not None
                and value.type in _CALLABLE_VALUES
            )
            for member in _named_children(node)
        )

    def object_members(self, node: Any, container_path: tuple[str, ...]) -> None:
        for member in _named_children(node):
            if member.type == "method_definition":
                name, kind = _method_name(member)
                if name:
                    self.callable(
                        name,
                        kind,
                        member,
                        member,
                        container_path,
                        exported=False,
                        member=True,
                        decorators=(),
                        wrapper_modifiers=(),
                        class_bindings=(),
                    )
                    self.scope(member, (*container_path, name), "callable")
            elif member.type == "pair":
                value = ast_field(member, "value")
                if value is None or value.type not in _CALLABLE_VALUES:
                    continue
                name = ast_text(ast_field(member, "key")).strip('"\'')
                if name:
                    self.callable(
                        name,
                        SymbolKind.METHOD,
                        member,
                        value,
                        container_path,
                        exported=False,
                        member=True,
                        decorators=(),
                        wrapper_modifiers=(),
                        class_bindings=(),
                    )
                    self.scope(value, (*container_path, name), "callable")

    def callable(
        self,
        name: str,
        kind: SymbolKind,
        declaration: Any,
        callable_node: Any,
        container_path: tuple[str, ...],
        *,
        exported: bool,
        member: bool,
        decorators: tuple[Any, ...],
        wrapper_modifiers: tuple[str, ...],
        class_bindings: tuple[Binding, ...],
    ) -> None:
        self.callable_drafts.append(
            _CallableDraft(
                name,
                kind,
                declaration,
                callable_node,
                container_path,
                _visibility(declaration, exported=exported, member=member),
                decorators,
                ordered_unique(
                    (
                        *_modifiers(declaration, wrapper_modifiers),
                        *_modifiers(callable_node),
                    )
                ),
                class_bindings,
            )
        )
        self.boundaries.append(callable_node)

    def freeze_callables(
        self,
        ownership: OwnershipContext,
    ) -> tuple[_Callable, ...]:
        selected: dict[
            SymbolId,
            tuple[_Callable, _CallableDraft, tuple[_Parameter, ...]],
        ] = {}
        for draft in self.callable_drafts:
            parameters = _parameters(draft.callable_node)
            params = tuple(parameter.type_name for parameter in parameters)
            returns = (
                draft.name
                if draft.kind is SymbolKind.CONSTRUCTOR
                else _type_text(ast_field(draft.callable_node, "return_type"))
            )
            suffix = (
                f":{returns}"
                if returns
                and returns != "void"
                and draft.kind is not SymbolKind.CONSTRUCTOR
                else ""
            )
            body = ast_field(draft.callable_node, "body")
            owned = owned_nodes(
                self.source,
                draft.callable_node,
                ownership=ownership,
            )
            bindings = _binding_tuple(
                (
                    *draft.class_bindings,
                    *_parameter_bindings(parameters),
                    *_local_bindings(owned),
                )
            )
            callable_symbol = Symbol(
                symbol_id(
                    self.source,
                    draft.container_path,
                    draft.kind,
                    draft.name,
                    params,
                ),
                node_span(self.source, draft.declaration),
                draft.visibility,
                f"{draft.name}({','.join(params)}){suffix}",
                params=params,
                returns=returns,
                bindings=bindings,
                annotations=_annotations(draft.annotation_nodes),
                modifiers=draft.modifiers,
                body_lines=body_lines(body),
            )
            candidate = _Callable(callable_symbol, draft.callable_node, owned)
            existing = selected.get(callable_symbol.id)
            if existing is None or (
                ast_field(candidate.node, "body") is not None
                and ast_field(existing[0].node, "body") is None
            ):
                selected[callable_symbol.id] = (candidate, draft, parameters)
            self.references.extend(
                _type_references(
                    self.source,
                    callable_symbol.id,
                    (
                        *(parameter.type_node for parameter in parameters),
                        ast_field(draft.callable_node, "return_type"),
                        ast_field(draft.callable_node, "type_parameters"),
                    ),
                )
            )
            self.references.extend(
                _annotation_references(
                    self.source,
                    callable_symbol.id,
                    draft.annotation_nodes,
                )
            )
        for _, draft, parameters in selected.values():
            if draft.kind is not SymbolKind.CONSTRUCTOR:
                continue
            for parameter in parameters:
                if _accessibility(parameter.node) is None and not any(
                    ast_text(child) == "readonly" for child in _children(parameter.node)
                ):
                    continue
                for property_name in parameter.names:
                    self.symbols.append(
                        Symbol(
                            symbol_id(
                                self.source,
                                draft.container_path,
                                SymbolKind.PROPERTY,
                                property_name,
                            ),
                            node_span(self.source, parameter.node),
                            _accessibility(parameter.node) or Visibility.PUBLIC,
                            property_name,
                            returns=parameter.type_name,
                            modifiers=_modifiers(parameter.node),
                        )
                    )
        return tuple(item[0] for item in selected.values())


def _body_span(source: SourceFile, node: Any) -> SourceSpan | None:
    body = ast_field(node, "body")
    return node_span(source, body) if body is not None else None


def _covering_span(source: SourceFile, nodes: Iterable[Any]) -> SourceSpan:
    spans = tuple(node_span(source, node) for node in nodes)
    first, last = spans[0], spans[-1]
    return SourceSpan(
        source.file,
        first.start_line,
        first.start_column,
        last.end_line,
        last.end_column,
    )


def _owned_facts(
    source: SourceFile,
    owner: SymbolId,
    node: Any,
    nodes: tuple[Any, ...],
    ownership: OwnershipContext,
) -> tuple[tuple[BodyEvent, ...], tuple[CallRef, ...], tuple[ReferenceRef, ...]]:
    events = body_events(source, node, ownership=ownership)
    config = _config_references(source, owner, nodes)
    events = _join_reference_events(events, config)
    references = (
        *_body_references(source, owner, events, nodes),
        *config,
    )
    return events, _calls(source, owner, nodes), references


def extract(source: SourceFile, parser: object | None) -> FileIR:
    if source.language not in _TYPESCRIPT_LANGUAGES:
        raise ValueError(f"unsupported TypeScript-family language {source.language}")
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("TypeScript-family extraction requires a Tree-sitter parser")

    unclosed_script = False
    if source.language in {Language.VUE, Language.SVELTE}:
        parse_buffers, unclosed_script = _sfc_program_buffers(source.raw)
    else:
        parse_buffers = (source.raw,)
    trees = tuple(
        parser.parse(parse_bytes)  # type: ignore[attr-defined]
        for parse_bytes in parse_buffers
    )
    roots = tuple(tree.root_node for tree in trees)
    module = _module_name(source.file)
    module_span = _covering_span(source, roots)
    module_symbol = Symbol(
        symbol_id(source, (), SymbolKind.MODULE, module),
        module_span,
        Visibility.PUBLIC,
        f"module {module}",
        body_lines=module_span.end_line - module_span.start_line + 1,
    )

    imports: list[ImportRef] = []
    reexports: list[Symbol] = []
    declarations = _Declarations(source)
    for root in roots:
        root_imports, root_reexports, import_boundaries = _imports_and_reexports(
            source,
            root,
        )
        imports.extend(root_imports)
        reexports.extend(root_reexports)
        declarations.boundaries.extend(import_boundaries)
        declarations.scope(root, (), "module")
    boundaries = tuple(declarations.boundaries)
    ownership = ownership_context(boundaries, include_anonymous=True)
    callables = declarations.freeze_callables(ownership)

    symbols: list[Symbol] = [module_symbol]
    if source.language in {Language.VUE, Language.SVELTE}:
        component_name = PurePosixPath(source.file).stem
        symbols.append(
            Symbol(
                symbol_id(source, (), SymbolKind.CLASS, component_name),
                _source_span(source),
                Visibility.PUBLIC,
                f"component {component_name}",
            )
        )
    symbols.extend(reexports)
    symbols.extend(declarations.symbols)
    symbols.extend(item.symbol for item in callables)
    symbols.sort(key=lambda item: item.span)

    calls: list[CallRef] = []
    references: list[ReferenceRef] = list(declarations.references)
    bodies: list[BodyIR] = []

    module_events: list[BodyEvent] = []
    for root in roots:
        root_nodes = owned_nodes(source, root, ownership=ownership)
        events, root_calls, root_references = _owned_facts(
            source,
            module_symbol.id,
            root,
            root_nodes,
            ownership,
        )
        module_events.extend(events)
        calls.extend(root_calls)
        references.extend(root_references)
    bodies.append(
        BodyIR(
            module_symbol.id,
            module_span,
            tuple(module_events),
        )
    )

    for item in callables:
        body_span = _body_span(source, item.node)
        if body_span is None:
            continue
        events, item_calls, item_references = _owned_facts(
            source,
            item.symbol.id,
            item.node,
            item.owned,
            ownership,
        )
        bodies.append(BodyIR(item.symbol.id, body_span, events))
        calls.extend(item_calls)
        references.extend(item_references)

    for region in declarations.regions:
        body_span = _body_span(source, region.node)
        if body_span is None:
            continue
        region_nodes = owned_nodes(source, region.node, ownership=ownership)
        events, region_calls, region_references = _owned_facts(
            source,
            region.symbol.id,
            region.node,
            region_nodes,
            ownership,
        )
        bodies.append(BodyIR(region.symbol.id, body_span, events))
        calls.extend(region_calls)
        references.extend(region_references)

    diagnostics = tuple(
        diagnostic
        for index, root in enumerate(roots)
        for diagnostic in _syntax_diagnostics(
            source,
            root,
            unclosed_script=unclosed_script and index == len(roots) - 1,
        )
    )

    return FileIR(
        source,
        module=module,
        symbols=tuple(symbols),
        calls=tuple(
            sorted(
                ordered_unique(calls),
                key=lambda call: (call.span.start_line, call.span.start_column),
            )
        ),
        imports=tuple(imports),
        references=tuple(
            sorted(
                ordered_unique(references),
                key=lambda item: (item.span.start_line, item.span.start_column),
            )
        ),
        bodies=tuple(sorted(bodies, key=lambda item: item.span)),
        diagnostics=ordered_unique(diagnostics),
    )


__all__ = ["extract"]
