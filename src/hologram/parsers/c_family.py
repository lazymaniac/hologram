from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
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
    body_node,
    children,
    field_nodes,
    named_children,
    syntax_diagnostics,
    walk_all,
    walk_owned,
)
from .common import ordered_unique, reference, symbol_id
from .treesitter import ast_field, ast_text, body_events, body_lines, node_span

_TYPE_KINDS = {
    "class_specifier": (SymbolKind.CLASS, "class"),
    "struct_specifier": (SymbolKind.CLASS, "struct"),
    "union_specifier": (SymbolKind.CLASS, "union"),
    "enum_specifier": (SymbolKind.ENUM, "enum"),
}
_TYPE_BOUNDARIES = frozenset(_TYPE_KINDS)
_CALLABLE_BOUNDARIES = frozenset({"function_definition"})
_FACT_BOUNDARIES = _TYPE_BOUNDARIES | _CALLABLE_BOUNDARIES
_DECLARATION_KINDS = frozenset({"declaration", "field_declaration"})
_NAME_KINDS = frozenset(
    {
        "destructor_name",
        "field_identifier",
        "identifier",
        "operator_name",
        "qualified_identifier",
        "template_function",
        "type_identifier",
    }
)
_DECLARATOR_WRAPPERS = frozenset(
    {
        "abstract_array_declarator",
        "array_declarator",
        "attributed_declarator",
        "init_declarator",
        "parenthesized_declarator",
        "pointer_declarator",
        "reference_declarator",
    }
)
_PARAMETER_KINDS = frozenset(
    {
        "optional_parameter_declaration",
        "parameter_declaration",
        "variadic_parameter_declaration",
    }
)
_PRIMITIVES = frozenset(
    {
        "auto",
        "bool",
        "char",
        "char16_t",
        "char32_t",
        "double",
        "float",
        "int",
        "long",
        "short",
        "signed",
        "unsigned",
        "void",
        "wchar_t",
        "_Bool",
        "_Complex",
    }
)
_MODIFIER_KINDS = frozenset(
    {
        "explicit_function_specifier",
        "function_specifier",
        "ms_call_modifier",
        "ms_declspec_modifier",
        "storage_class_specifier",
        "type_qualifier",
        "virtual_specifier",
    }
)
_CONSTRUCT_KINDS = frozenset({"compound_literal_expression", "new_expression"})
_CALL_KINDS = frozenset({"call_expression", *_CONSTRUCT_KINDS})
_QUALIFIER_RE = re.compile(r"\b(?:const|constexpr|restrict|volatile)\b")


def _key(node: object) -> tuple[int, int, str]:
    return (int(node.start_byte), int(node.end_byte), str(node.type))  # type: ignore[attr-defined]


def _normalized(text: str) -> str:
    value = re.sub(r"\s+", " ", text.strip())
    value = re.sub(r"\s*,\s*", ",", value)
    value = re.sub(r"\s*::\s*", "::", value)
    value = re.sub(r"\s+([*&]+)", r"\1", value)
    value = re.sub(r"\[\s+", "[", value)
    value = re.sub(r"\s+\]", "]", value)
    return value


def _strip_templates(text: str) -> str:
    result: list[str] = []
    depth = 0
    for character in text:
        if character == "<":
            depth += 1
        elif character == ">" and depth:
            depth -= 1
        elif depth == 0:
            result.append(character)
    return "".join(result)


def _short_type(type_name: str) -> str:
    value = _QUALIFIER_RE.sub("", type_name)
    value = value.replace("*", " ").replace("&", " ")
    value = re.sub(r"\[.*", "", value)
    value = _strip_templates(value).strip()
    return value.rsplit("::", 1)[-1].split()[-1] if value else "?"


def _object_type(type_name: str) -> str:
    return _normalized(re.sub(r"^(?:(?:const|constexpr)\s+)+", "", type_name))


def _deep_name(node: Any | None) -> Any | None:
    current = node
    while current is not None:
        if current.type in _NAME_KINDS:
            if current.type != "qualified_identifier":
                return current
            return current
        nested = ast_field(current, "declarator")
        if nested is None:
            candidates = [
                child
                for child in named_children(current)
                if child.type in _NAME_KINDS or child.type in _DECLARATOR_WRAPPERS
            ]
            nested = candidates[0] if candidates else None
        current = nested
    return None


def _binding_leaf(node: Any | None) -> Any | None:
    if node is None:
        return None
    name = _deep_name(node)
    if name is None:
        return None
    if name.type != "qualified_identifier":
        return name
    target = ast_field(name, "name")
    return target if target is not None else name


def _without_node(root: Any | None, omitted: Any | None) -> str:
    if root is None:
        return ""
    raw = bytes(getattr(root, "text", b""))
    if omitted is None:
        return _normalized(ast_text(root))
    start = int(omitted.start_byte) - int(root.start_byte)  # type: ignore[attr-defined]
    end = int(omitted.end_byte) - int(root.start_byte)  # type: ignore[attr-defined]
    if start < 0 or end > len(raw):
        return _normalized(ast_text(root))
    return _normalized((raw[:start] + raw[end:]).decode("utf-8", errors="replace"))


def _declarator_type(base: str, declarator: Any | None) -> str:
    if declarator is not None and declarator.type == "init_declarator":
        declarator = ast_field(declarator, "declarator")
    binder = _binding_leaf(declarator)
    shape = _without_node(declarator, binder)
    if not shape:
        return _normalized(base)
    separator = "" if shape.startswith(("*", "&", "[", "(")) else " "
    return _normalized(f"{base}{separator}{shape}")


def _direct_declarators(node: object | None) -> tuple[Any, ...]:
    values = field_nodes(node, "declarator")
    return tuple(values)


def _function_parts(declarator: Any | None) -> tuple[Any, Any] | None:
    current = declarator
    function = None
    while current is not None:
        if current.type == "function_declarator":
            function = current
            break
        current = ast_field(current, "declarator")
    if function is None:
        return None
    named = ast_field(function, "declarator")
    if named is None or named.type == "parenthesized_declarator":
        return None
    name = _deep_name(named)
    return (function, name) if name is not None else None


def _base_type(node: Any, declarator: Any | None) -> str:
    type_node = ast_field(node, "type")
    pieces: list[str] = []
    for child in named_children(node):
        if (
            declarator is not None and _key(child) == _key(declarator)
        ) or child.type in {
            "attribute_declaration",
            "compound_statement",
            "field_initializer_list",
            "storage_class_specifier",
        }:
            continue
        if child.type == "type_qualifier" and ast_text(child) == "constexpr":
            continue
        if (
            type_node is not None and _key(child) == _key(type_node)
        ) or child.type == "type_qualifier":
            pieces.append(ast_text(child))
    return _normalized(" ".join(pieces) or ast_text(type_node))


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str | None
    type_name: str
    declaration: Any
    type_node: Any | None


def _parameters(function: object) -> tuple[_Parameter, ...]:
    parameter_list = ast_field(function, "parameters")
    values: list[_Parameter] = []
    for parameter in named_children(parameter_list):
        if parameter.type not in _PARAMETER_KINDS:
            continue
        if parameter.type == "variadic_parameter_declaration":
            declarator = ast_field(parameter, "declarator")
            binder = _binding_leaf(declarator)
            type_name = _declarator_type("...", declarator)
            values.append(
                _Parameter(
                    ast_text(binder) or None,
                    type_name or "...",
                    parameter,
                    ast_field(parameter, "type"),
                )
            )
            continue
        declarator = ast_field(parameter, "declarator")
        binder = _binding_leaf(declarator)
        base = _base_type(parameter, declarator)
        type_name = _declarator_type(base, declarator)
        values.append(
            _Parameter(
                ast_text(binder) or None,
                type_name or "?",
                parameter,
                ast_field(parameter, "type"),
            )
        )
    if len(values) == 1 and values[0].name is None and values[0].type_name == "void":
        return ()
    return tuple(values)


def _parameter_bindings(parameters: Iterable[_Parameter]) -> tuple[Binding, ...]:
    return tuple(
        Binding(parameter.name, _short_type(parameter.type_name))
        for parameter in parameters
        if parameter.name is not None
    )


def _modifiers(node: object | None, function: object | None = None) -> tuple[str, ...]:
    roots = (node, function)
    values = [
        ast_text(child).strip()
        for root in roots
        for child in named_children(root)
        if child.type in _MODIFIER_KINDS and ast_text(child).strip()
    ]
    return ordered_unique(values)


def _attribute_nodes(node: object | None) -> tuple[Any, ...]:
    return tuple(
        attribute
        for child in named_children(node)
        if child.type == "attribute_declaration"
        for attribute in walk_all(child)
        if attribute.type == "attribute"
    )


def _attribute_name(attribute: object) -> Any | None:
    name = ast_field(attribute, "name")
    if name is not None:
        return name
    return next(
        (
            child
            for child in walk_all(attribute)
            if child is not attribute
            and child.type in {"identifier", "type_identifier"}
        ),
        None,
    )


def _annotations(node: object | None) -> tuple[str, ...]:
    return ordered_unique(
        ast_text(name)
        for attribute in _attribute_nodes(node)
        if (name := _attribute_name(attribute)) is not None
        if ast_text(name)
    )


def _annotation_references(
    source: SourceFile,
    owner: SymbolId,
    node: object | None,
) -> tuple[ReferenceRef, ...]:
    return ordered_unique(
        reference(
            owner,
            node_span(source, name),
            ast_text(name),
            None,
            ReferenceKind.TYPE,
            context=ReferenceContext.ANNOTATION,
            confidence=ReferenceConfidence.POSSIBLE,
        )
        for attribute in _attribute_nodes(node)
        if (name := _attribute_name(attribute)) is not None
        if ast_text(name)
    )


def _type_references(
    source: SourceFile,
    owner: SymbolId,
    roots: Iterable[object | None],
) -> tuple[ReferenceRef, ...]:
    values: list[ReferenceRef] = []
    for root in roots:
        if root is None:
            continue
        leaves = tuple(
            node for node in walk_all(root) if node.type == "type_identifier"
        )
        if not leaves and ast_text(root) and ast_text(root) not in _PRIMITIVES:
            leaves = (root,)
        for leaf in leaves:
            name = _short_type(ast_text(leaf))
            if not name or name in _PRIMITIVES or name == "?":
                continue
            values.append(
                reference(
                    owner,
                    node_span(source, leaf),
                    name,
                    None,
                    ReferenceKind.TYPE,
                    context=ReferenceContext.TYPE,
                    confidence=ReferenceConfidence.DEFINITE,
                )
            )
    return ordered_unique(values)


def _include_module(path: object | None) -> str:
    raw = ast_text(path).strip()
    if len(raw) >= 2 and raw[:1] in {'"', "<"} and raw[-1:] in {'"', ">"}:
        return raw[1:-1]
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw
    return value if isinstance(value, str) else raw


def _imports(source: SourceFile, root: object) -> tuple[ImportRef, ...]:
    return tuple(
        ImportRef(node_span(source, node), module, None, None)
        for node in walk_all(root)
        if node.type == "preproc_include"
        if (module := _include_module(ast_field(node, "path")))
    )


def _qualified_parts(raw: str) -> tuple[str, ...]:
    return tuple(
        part
        for component in raw.lstrip(":").split("::")
        if (part := _strip_templates(component).strip())
    )


def _declared_type_name(node: object) -> str | None:
    name = ast_field(node, "name")
    if name is not None and ast_text(name):
        return ast_text(name)
    parent = getattr(node, "parent", None)
    if parent is not None and parent.type == "type_definition":
        alias = _binding_leaf(ast_field(parent, "declarator"))
        return ast_text(alias) or None
    return None


class _Scopes:
    def __init__(self, root: object) -> None:
        self.namespaces: dict[tuple[int, int, str], tuple[str, ...]] = {}
        self.types: dict[tuple[int, int, str], tuple[str, ...]] = {}
        self.callables: dict[tuple[int, int, str], tuple[str, ...]] = {}
        self.member_visibility: dict[tuple[int, int, str], Visibility] = {}
        self._index(root)

    def owner(self, node: object | None, *, callables: bool = True) -> tuple[str, ...]:
        current = getattr(node, "parent", None)
        while current is not None:
            key = _key(current)
            if callables and key in self.callables:
                return self.callables[key]
            if key in self.types:
                return self.types[key]
            if key in self.namespaces:
                return self.namespaces[key]
            current = getattr(current, "parent", None)
        return ()

    def namespace_owner(self, node: object | None) -> tuple[str, ...]:
        current = getattr(node, "parent", None)
        while current is not None:
            value = self.namespaces.get(_key(current))
            if value is not None:
                return value
            current = getattr(current, "parent", None)
        return ()

    def access(self, node: Any, default: Visibility) -> Visibility:
        current = node
        while getattr(current, "parent", None) is not None:
            value = self.member_visibility.get(_key(current))
            if value is not None:
                return value
            current = current.parent
        return default

    def inside_callable_before_type(self, node: object) -> bool:
        current = getattr(node, "parent", None)
        while current is not None:
            key = _key(current)
            if key in self.types:
                return False
            if key in self.callables:
                return True
            current = getattr(current, "parent", None)
        return False

    def anonymous_namespace(self, node: object) -> bool:
        current = getattr(node, "parent", None)
        while current is not None:
            if (
                current.type == "namespace_definition"
                and ast_field(current, "name") is None
            ):
                return True
            current = getattr(current, "parent", None)
        return False

    def _index(self, root: object) -> None:
        for node in walk_all(root):
            if node.type == "namespace_definition":
                raw = ast_text(ast_field(node, "name"))
                parts = _qualified_parts(raw)
                self.namespaces[_key(node)] = (*self.owner(node), *parts)
            elif node.type in _TYPE_KINDS:
                if name := _declared_type_name(node):
                    self.types[_key(node)] = (*self.owner(node), name)
                    self._index_access(node)
            elif node.type == "function_definition":
                declarator = ast_field(node, "declarator")
                callable_parts = _function_parts(declarator)
                if callable_parts is None:
                    continue
                function, name_node = callable_parts
                parameters = _parameters(function)
                name_parts = _qualified_parts(ast_text(name_node))
                if not name_parts:
                    continue
                lexical = self.owner(node)
                if len(name_parts) > 1:
                    lexical = (*self.namespace_owner(node), *name_parts[:-1])
                signature = f"({','.join(p.type_name for p in parameters)})"
                self.callables[_key(node)] = (
                    *lexical,
                    f"{name_parts[-1]}{signature}",
                )

    def _index_access(self, node: Any) -> None:
        body = ast_field(node, "body")
        if body is None:
            return
        current = (
            Visibility.PRIVATE if node.type == "class_specifier" else Visibility.PUBLIC
        )
        for member in named_children(body):
            if member.type == "access_specifier":
                raw = ast_text(member).rstrip(":")
                current = {
                    "private": Visibility.PRIVATE,
                    "protected": Visibility.PROTECTED,
                    "public": Visibility.PUBLIC,
                }.get(raw, current)
            else:
                self.member_visibility[_key(member)] = current


def _enum_members(node: object) -> tuple[Any, ...]:
    body = ast_field(node, "body")
    return tuple(child for child in named_children(body) if child.type == "enumerator")


def _base_classes(node: object) -> tuple[str, ...]:
    clause = next(
        (child for child in named_children(node) if child.type == "base_class_clause"),
        None,
    )
    if clause is None:
        return ()
    values: list[str] = []
    for child in named_children(clause):
        if child.type == "access_specifier":
            continue
        name = _short_type(_normalized(ast_text(child)))
        if name and name != "?":
            values.append(name)
    return ordered_unique(values)


def _field_declarations(node: object) -> tuple[Any, ...]:
    body = ast_field(node, "body")
    return tuple(
        child for child in named_children(body) if child.type == "field_declaration"
    )


def _is_constant(node: object) -> bool:
    return any(
        ast_text(child) in {"const", "constexpr"}
        for child in named_children(node)
        if child.type == "type_qualifier"
    )


def _local_bindings(body: object | None) -> tuple[Binding, ...]:
    values: list[Binding] = []
    for node in walk_owned(body, _FACT_BOUNDARIES):
        if node.type not in _DECLARATION_KINDS:
            continue
        for declarator in _direct_declarators(node):
            if _function_parts(declarator) is not None:
                continue
            binder = _binding_leaf(declarator)
            if binder is None:
                continue
            declared = _declarator_type(_base_type(node, declarator), declarator)
            values.append(Binding(ast_text(binder), _short_type(declared)))
    return binding_tuple(values)


def _call_parts(function: Any | None) -> tuple[str | None, str] | None:
    if function is None:
        return None
    if function.type == "field_expression":
        field = ast_field(function, "field")
        argument = ast_field(function, "argument")
        if field is None:
            return None
        return ast_text(argument) or None, ast_text(field)
    raw = ast_text(function)
    parts = _qualified_parts(raw)
    if len(parts) > 1:
        return "::".join(parts[:-1]), parts[-1]
    return (None, parts[0]) if parts else None


def _call(source: SourceFile, owner: SymbolId, node: Any) -> CallRef | None:
    if node.type == "new_expression":
        type_node = ast_field(node, "type")
        name = _short_type(ast_text(type_node))
        return CallRef(
            owner,
            node_span(source, node),
            name,
            None,
            CallKind.CONSTRUCT,
            argument_count(node),
        )
    if node.type == "compound_literal_expression":
        type_node = ast_field(node, "type") or next(
            (child for child in named_children(node) if child.type.endswith("type")),
            None,
        )
        name = _short_type(ast_text(type_node))
        initializer = ast_field(node, "value") or next(
            (
                child
                for child in named_children(node)
                if child.type in {"initializer_list", "field_designator"}
            ),
            None,
        )
        return CallRef(
            owner,
            node_span(source, node),
            name,
            None,
            CallKind.CONSTRUCT,
            len(named_children(initializer)),
        )
    parts = _call_parts(ast_field(node, "function"))
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


def _calls(
    source: SourceFile,
    owner: SymbolId,
    root: object | None,
) -> tuple[CallRef, ...]:
    return ordered_unique(
        call
        for node in walk_owned(root, _FACT_BOUNDARIES)
        if node.type in _CALL_KINDS
        if (call := _call(source, owner, node)) is not None
    )


def _event_qualifiers(root: object | None) -> dict[tuple[int, int], str]:
    values: dict[tuple[int, int], str] = {}
    for node in walk_owned(root, _FACT_BOUNDARIES):
        if node.type != "field_expression":
            continue
        field = ast_field(node, "field")
        argument = ast_field(node, "argument")
        if field is not None and argument is not None:
            values[(field.start_byte, field.end_byte)] = ast_text(argument)
    return values


def _body_references(
    source: SourceFile,
    owner: SymbolId,
    body: object | None,
    events: Iterable[BodyEvent],
) -> tuple[ReferenceRef, ...]:
    qualifiers = _event_qualifiers(body)
    event_keys = {
        node_span(source, node): (node.start_byte, node.end_byte)
        for node in walk_owned(body, _FACT_BOUNDARIES)
    }
    values: list[ReferenceRef] = []
    for event in events:
        if event.kind is BodyEventKind.TYPE:
            kind = ReferenceKind.TYPE
            context = ReferenceContext.TYPE
        elif event.kind is BodyEventKind.NAME:
            kind = ReferenceKind.NAME
            context = ReferenceContext.CODE
        else:
            continue
        name = _short_type(event.text) if kind is ReferenceKind.TYPE else event.text
        if not name or name in _PRIMITIVES or not re.fullmatch(r"[^\W\d]\w*", name):
            continue
        key = event_keys.get(event.span)
        values.append(
            reference(
                owner,
                event.span,
                name,
                qualifiers.get(key) if key is not None else None,
                kind,
                context=context,
                confidence=ReferenceConfidence.DEFINITE,
            )
        )
    return ordered_unique(values)


@dataclass(frozen=True, slots=True)
class _Entry:
    symbol: Symbol
    definition: bool
    declared_visibility: Visibility | None


class _Extractor:
    def __init__(self, source: SourceFile, root: object) -> None:
        self.source = source
        self.root = root
        self.scopes = _Scopes(root)
        self.entries: dict[SymbolId, _Entry] = {}
        self.calls: list[CallRef] = []
        self.references: list[ReferenceRef] = []
        self.bodies: dict[SymbolId, BodyIR] = {}

    def add(
        self,
        symbol: Symbol,
        *,
        definition: bool = False,
        declared_visibility: Visibility | None = None,
    ) -> None:
        current = self.entries.get(symbol.id)
        if current is None:
            self.entries[symbol.id] = _Entry(
                symbol,
                definition,
                declared_visibility,
            )
            return
        prefer_new = definition and not current.definition
        preferred = symbol if prefer_new else current.symbol
        other = current.symbol if prefer_new else symbol
        visibility = (
            current.declared_visibility or declared_visibility or preferred.visibility
        )
        merged = replace(
            preferred,
            visibility=visibility,
            annotations=ordered_unique(
                (*current.symbol.annotations, *symbol.annotations)
            ),
            modifiers=ordered_unique((*current.symbol.modifiers, *symbol.modifiers)),
            supers=ordered_unique((*preferred.supers, *other.supers)),
            components=ordered_unique((*preferred.components, *other.components)),
            bindings=preferred.bindings or other.bindings,
        )
        self.entries[symbol.id] = _Entry(
            merged,
            current.definition or definition,
            current.declared_visibility or declared_visibility,
        )

    def module_symbols(self) -> None:
        seen: set[tuple[str, ...]] = set()
        for node in walk_all(self.root):
            if node.type != "namespace_definition":
                continue
            path = self.scopes.namespaces.get(_key(node), ())
            name_node = ast_field(node, "name")
            parent = self.scopes.owner(node)
            name_leaves = tuple(
                child
                for child in walk_all(name_node)
                if child.type == "namespace_identifier"
            )
            for index in range(len(parent), len(path)):
                declared_path = path[: index + 1]
                if declared_path in seen:
                    continue
                seen.add(declared_path)
                name = declared_path[-1]
                self.add(
                    Symbol(
                        symbol_id(
                            self.source,
                            declared_path[:-1],
                            SymbolKind.MODULE,
                            name,
                        ),
                        node_span(
                            self.source,
                            name_leaves[index - len(parent)]
                            if index - len(parent) < len(name_leaves)
                            else name_node or node,
                        ),
                        Visibility.PUBLIC,
                        f"module {name}",
                        modifiers=("inline",)
                        if any(ast_text(child) == "inline" for child in children(node))
                        else (),
                    )
                )

    def types(self) -> None:
        for node in walk_all(self.root):
            if node.type not in _TYPE_KINDS:
                continue
            name = _declared_type_name(node)
            if not name:
                continue
            path = self.scopes.types.get(_key(node), (*self.scopes.owner(node), name))
            container_path = path[:-1]
            kind, label = _TYPE_KINDS[node.type]
            enumerators = _enum_members(node) if kind is SymbolKind.ENUM else ()
            fields = _field_declarations(node) if kind is SymbolKind.CLASS else ()
            field_values: list[tuple[Any, Any, str]] = []
            for field in fields:
                for declarator in _direct_declarators(field):
                    if _function_parts(declarator) is not None:
                        continue
                    binder = _binding_leaf(declarator)
                    if binder is None:
                        continue
                    field_values.append(
                        (
                            field,
                            binder,
                            _object_type(
                                _declarator_type(
                                    _base_type(field, declarator), declarator
                                )
                            ),
                        )
                    )
            components = (
                tuple(ast_text(ast_field(item, "name")) for item in enumerators)
                if kind is SymbolKind.ENUM
                else tuple(ast_text(binder) for _, binder, _ in field_values)
            )
            params = (
                components
                if kind is SymbolKind.ENUM
                else tuple(type_name for _, _, type_name in field_values)
            )
            visibility = self.scopes.access(
                node,
                Visibility.PRIVATE
                if self.scopes.anonymous_namespace(node)
                else Visibility.PUBLIC,
            )
            type_symbol = Symbol(
                symbol_id(self.source, container_path, kind, name),
                node_span(self.source, node),
                visibility,
                f"{label} {name}",
                params=params,
                supers=_base_classes(node),
                components=components,
                annotations=_annotations(node),
                modifiers=(label,) if label == "union" else (),
            )
            self.add(type_symbol)
            self.references.extend(
                _type_references(
                    self.source,
                    type_symbol.id,
                    (
                        next(
                            (
                                child
                                for child in named_children(node)
                                if child.type == "base_class_clause"
                            ),
                            None,
                        ),
                    ),
                )
            )
            self.references.extend(
                _annotation_references(self.source, type_symbol.id, node)
            )
            owned = (*container_path, name)
            for enumerator in enumerators:
                name_node = ast_field(enumerator, "name")
                enum_name = ast_text(name_node)
                if not enum_name:
                    continue
                self.add(
                    Symbol(
                        symbol_id(
                            self.source,
                            owned,
                            SymbolKind.CONSTANT,
                            enum_name,
                        ),
                        node_span(self.source, name_node),
                        Visibility.PUBLIC,
                        enum_name,
                        returns=name,
                    )
                )
            for field, binder, type_name in field_values:
                field_name = ast_text(binder)
                field_symbol = Symbol(
                    symbol_id(
                        self.source,
                        owned,
                        SymbolKind.CONSTANT
                        if _is_constant(field)
                        else SymbolKind.FIELD,
                        field_name,
                    ),
                    node_span(self.source, binder),
                    self.scopes.access(field, Visibility.PUBLIC),
                    field_name,
                    returns=type_name or None,
                    annotations=_annotations(field),
                    modifiers=_modifiers(field),
                )
                self.add(field_symbol)
                self.references.extend(
                    _type_references(
                        self.source,
                        field_symbol.id,
                        (ast_field(field, "type"),),
                    )
                )
                self.references.extend(
                    _annotation_references(self.source, field_symbol.id, field)
                )

    def aliases(self) -> None:
        for node in walk_all(self.root):
            if node.type not in {"alias_declaration", "type_definition"}:
                continue
            alias_node = _binding_leaf(
                ast_field(node, "declarator") or ast_field(node, "name")
            )
            name = ast_text(alias_node)
            if not name:
                continue
            declared = ast_field(node, "type")
            if declared is not None and declared.type in _TYPE_KINDS:
                inner_name = _declared_type_name(declared)
                if inner_name == name:
                    continue
            owner = self.scopes.owner(node)
            symbol = Symbol(
                symbol_id(self.source, owner, SymbolKind.TYPE, name),
                node_span(self.source, alias_node),
                self.scopes.access(node, Visibility.PUBLIC),
                f"type {name}",
                params=(_normalized(ast_text(declared)),)
                if declared is not None
                else (),
            )
            self.add(symbol)
            self.references.extend(
                _type_references(self.source, symbol.id, (declared,))
            )

    def values(self) -> None:
        for node in walk_all(self.root):
            if node.type != "declaration" or self.scopes.inside_callable_before_type(
                node
            ):
                continue
            if (
                getattr(node, "parent", None) is not None
                and node.parent.type == "type_definition"
            ):
                continue
            for declarator in _direct_declarators(node):
                if _function_parts(declarator) is not None:
                    continue
                binder = _binding_leaf(declarator)
                if binder is None:
                    continue
                name = ast_text(binder)
                owner = self.scopes.owner(node, callables=False)
                type_name = _object_type(
                    _declarator_type(_base_type(node, declarator), declarator)
                )
                symbol = Symbol(
                    symbol_id(
                        self.source,
                        owner,
                        SymbolKind.CONSTANT if _is_constant(node) else SymbolKind.FIELD,
                        name,
                    ),
                    node_span(self.source, binder),
                    Visibility.PRIVATE
                    if "static" in _modifiers(node)
                    or self.scopes.anonymous_namespace(node)
                    else Visibility.PUBLIC,
                    name,
                    returns=type_name or None,
                    annotations=_annotations(node),
                    modifiers=_modifiers(node),
                )
                self.add(symbol)
                self.references.extend(
                    _type_references(
                        self.source,
                        symbol.id,
                        (ast_field(node, "type"),),
                    )
                )

    def callables(self) -> None:
        for node in walk_all(self.root):
            if node.type == "function_definition":
                self._callable_node(node, definition=True)
            elif node.type in _DECLARATION_KINDS:
                if self.scopes.inside_callable_before_type(node):
                    continue
                self._callable_node(node, definition=False)

    def _callable_node(self, node: Any, *, definition: bool) -> None:
        for declarator in _direct_declarators(node):
            parts = _function_parts(declarator)
            if parts is None:
                continue
            function, name_node = parts
            parameters = _parameters(function)
            param_types = tuple(parameter.type_name for parameter in parameters)
            qualified = _qualified_parts(ast_text(name_node))
            if not qualified:
                continue
            name = qualified[-1]
            lexical_owner = self.scopes.owner(node, callables=False)
            owner = lexical_owner
            if len(qualified) > 1:
                owner = (*self.scopes.namespace_owner(node), *qualified[:-1])
            enclosing_type = owner[-1] if owner else None
            constructor = enclosing_type is not None and name == enclosing_type
            kind = (
                SymbolKind.CONSTRUCTOR
                if constructor
                else SymbolKind.METHOD
                if enclosing_type is not None
                and any(path == owner for path in self.scopes.types.values())
                else SymbolKind.FUNCTION
            )
            base = _base_type(node, declarator)
            wrapper = _without_node(declarator, function)
            returns = (
                name
                if constructor
                else _normalized(
                    f"{base}{'' if wrapper.startswith(('*', '&')) else ' '}{wrapper}"
                ).strip()
                or None
            )
            body = body_node(node)
            modifiers = _modifiers(node, function)
            visibility = self.scopes.access(
                node,
                Visibility.PRIVATE
                if ("static" in modifiers and kind is SymbolKind.FUNCTION)
                or self.scopes.anonymous_namespace(node)
                else Visibility.PUBLIC,
            )
            suffix = (
                f":{returns}"
                if returns and returns != "void" and kind is not SymbolKind.CONSTRUCTOR
                else ""
            )
            symbol = Symbol(
                symbol_id(self.source, owner, kind, name, param_types),
                node_span(self.source, node),
                visibility,
                f"{name}({','.join(param_types)}){suffix}",
                params=param_types,
                returns=returns,
                bindings=binding_tuple(
                    (*_parameter_bindings(parameters), *_local_bindings(body))
                ),
                annotations=_annotations(node),
                modifiers=modifiers,
                body_lines=body_lines(body),
            )
            declared_visibility = visibility if not definition else None
            self.add(
                symbol,
                definition=definition and body is not None,
                declared_visibility=declared_visibility,
            )
            self.references.extend(
                _type_references(
                    self.source,
                    symbol.id,
                    (
                        ast_field(node, "type"),
                        *(parameter.type_node for parameter in parameters),
                    ),
                )
            )
            self.references.extend(_annotation_references(self.source, symbol.id, node))
            if body is None:
                continue
            events = body_events(self.source, node, include_anonymous=True)
            existing_names = {binding.name for binding in symbol.bindings}
            anonymous_bindings = tuple(
                Binding(event.text, "?")
                for event in events
                if event.kind in {BodyEventKind.LOCAL, BodyEventKind.PARAM}
                and event.text not in existing_names
            )
            if anonymous_bindings:
                entry = self.entries[symbol.id]
                self.entries[symbol.id] = replace(
                    entry,
                    symbol=replace(
                        entry.symbol,
                        bindings=binding_tuple(
                            (*entry.symbol.bindings, *anonymous_bindings)
                        ),
                    ),
                )
            self.bodies[symbol.id] = BodyIR(
                symbol.id,
                node_span(self.source, body),
                events,
            )
            self.calls.extend(_calls(self.source, symbol.id, body))
            self.references.extend(
                _body_references(self.source, symbol.id, body, events)
            )

    def symbols(self) -> tuple[Symbol, ...]:
        return tuple(entry.symbol for entry in self.entries.values())


def extract(source: SourceFile, parser: object | None):
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("C-family extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    extractor = _Extractor(source, root)
    extractor.module_symbols()
    extractor.types()
    extractor.aliases()
    extractor.values()
    extractor.callables()
    top_namespaces = ordered_unique(
        path
        for key, path in extractor.scopes.namespaces.items()
        if path
        and not any(
            parent_key != key
            and len(parent_path) < len(path)
            and path[: len(parent_path)] == parent_path
            for parent_key, parent_path in extractor.scopes.namespaces.items()
        )
    )
    module = ".".join(top_namespaces[0]) if len(top_namespaces) == 1 else None
    return assemble_file_ir(
        source,
        module=module,
        symbols=extractor.symbols(),
        calls=extractor.calls,
        imports=_imports(source, root),
        references=extractor.references,
        bodies=extractor.bodies.values(),
        diagnostics=syntax_diagnostics(
            source,
            root,
            "C++" if source.language.value == "cpp" else "C",
        ),
    )


__all__ = ["extract"]
