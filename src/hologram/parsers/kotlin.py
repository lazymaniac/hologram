from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from hologram.model import (
    Binding,
    BodyIR,
    CallKind,
    CallRef,
    FileIR,
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
    body_node,
    body_references,
    body_span,
    children,
    direct_child,
    file_module,
    named_children,
    same_node,
    simple_type,
    syntax_diagnostics,
    walk_all,
    walk_owned,
)
from .common import ordered_unique, reference, symbol_id, tight_type
from .treesitter import ast_field, ast_text, body_events, body_lines, node_span

_TYPE_KINDS = frozenset({"class_declaration", "object_declaration"})
_CALLABLE_KINDS = frozenset(
    {
        "function_declaration",
        "getter",
        "secondary_constructor",
        "setter",
    }
)
_OWNERSHIP_BOUNDARIES = frozenset(
    {
        *_TYPE_KINDS,
        *_CALLABLE_KINDS,
        "anonymous_function",
        "companion_object",
        "lambda_literal",
    }
)
_ANONYMOUS_CALLABLE_KINDS = frozenset({"anonymous_function", "lambda_literal"})
_FACT_BOUNDARIES = _OWNERSHIP_BOUNDARIES - _ANONYMOUS_CALLABLE_KINDS
_TYPE_NODE_KINDS = frozenset(
    {
        "function_type",
        "nullable_type",
        "parenthesized_type",
        "user_type",
    }
)
_TYPE_LEAF_KINDS = frozenset({"identifier", "type_identifier"})
_PRIMITIVES = frozenset(
    {
        "Any",
        "Boolean",
        "Byte",
        "Char",
        "Double",
        "Float",
        "Int",
        "Long",
        "Nothing",
        "Short",
        "String",
        "UByte",
        "UInt",
        "ULong",
        "UShort",
        "Unit",
    }
)
_CALL_KINDS = frozenset({"call_expression", "constructor_delegation_call"})


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    type_name: str
    node: Any
    type_node: Any
    property: bool = False


def _modifiers_node(node: object | None) -> Any | None:
    return direct_child(node, {"modifiers"})


def _modifier_values(node: object | None) -> tuple[str, ...]:
    modifiers = _modifiers_node(node)
    return ordered_unique(
        text
        for child in named_children(modifiers)
        if child.type != "annotation"
        if (text := ast_text(child).strip())
    )


def _visibility(node: object | None, *, default: Visibility) -> Visibility:
    modifiers = _modifier_values(node)
    if "private" in modifiers:
        return Visibility.PRIVATE
    if "protected" in modifiers:
        return Visibility.PROTECTED
    if "internal" in modifiers:
        return Visibility.INTERNAL
    if "public" in modifiers:
        return Visibility.PUBLIC
    return default


def _annotation_nodes(node: object | None) -> tuple[Any, ...]:
    modifiers = _modifiers_node(node)
    return tuple(
        child for child in named_children(modifiers) if child.type == "annotation"
    )


def _annotation_target(annotation: object) -> Any | None:
    type_node = direct_child(annotation, _TYPE_NODE_KINDS)
    if type_node is None:
        type_node = next(
            (
                node
                for node in walk_all(annotation)
                if node is not annotation and node.type in _TYPE_NODE_KINDS
            ),
            None,
        )
    if type_node is None:
        return None
    leaves = [node for node in walk_all(type_node) if node.type in _TYPE_LEAF_KINDS]
    return leaves[-1] if leaves else type_node


def _annotations(node: object | None, *, entrypoint: bool = False) -> tuple[str, ...]:
    values = [
        ast_text(target)
        for annotation in _annotation_nodes(node)
        if (target := _annotation_target(annotation)) is not None
        if ast_text(target)
    ]
    if entrypoint:
        values.append("entrypoint")
    return ordered_unique(values)


def _annotation_references(
    source: SourceFile,
    owner: SymbolId,
    node: object | None,
) -> tuple[ReferenceRef, ...]:
    return ordered_unique(
        reference(
            owner,
            node_span(source, target),
            ast_text(target),
            None,
            ReferenceKind.TYPE,
            context=ReferenceContext.ANNOTATION,
            confidence=ReferenceConfidence.POSSIBLE,
        )
        for annotation in _annotation_nodes(node)
        if (target := _annotation_target(annotation)) is not None
        if ast_text(target)
    )


def _parameter_container(node: object | None) -> Any | None:
    return direct_child(
        node,
        {"class_parameters", "function_value_parameters", "lambda_parameters"},
    )


def _parameters(node: object | None) -> tuple[_Parameter, ...]:
    if node is None:
        return ()
    values: list[_Parameter] = []
    for parameter in named_children(node):
        if parameter.type not in {
            "class_parameter",
            "parameter",
            "variable_declaration",
        }:
            continue
        named = named_children(parameter)
        name_node = next(
            (child for child in named if child.type == "identifier"),
            None,
        )
        type_node = next(
            (child for child in reversed(named) if child.type in _TYPE_NODE_KINDS),
            None,
        )
        if name_node is None or type_node is None:
            continue
        values.append(
            _Parameter(
                ast_text(name_node),
                tight_type(ast_text(type_node)),
                parameter,
                type_node,
                any(child.type in {"val", "var"} for child in children(parameter)),
            )
        )
    return tuple(values)


def _parameter_bindings(parameters: Iterable[_Parameter]) -> tuple[Binding, ...]:
    return tuple(
        Binding(parameter.name, simple_type(parameter.type_name.rstrip("?")))
        for parameter in parameters
    )


def _return_type(node: object) -> tuple[str | None, Any | None]:
    seen_parameters = False
    for child in named_children(node):
        if child.type == "function_value_parameters":
            seen_parameters = True
            continue
        if child.type == "function_body":
            break
        if seen_parameters and child.type in _TYPE_NODE_KINDS:
            return tight_type(ast_text(child)), child
    return None, None


def _extension_receiver(node: object) -> Any | None:
    name = ast_field(node, "name")
    for child in named_children(node):
        if same_node(child, name):
            break
        if child.type in _TYPE_NODE_KINDS:
            return child
    return None


def _type_leaf_nodes(root: object | None) -> tuple[Any, ...]:
    if root is None:
        return ()
    return tuple(node for node in walk_all(root) if node.type in _TYPE_LEAF_KINDS)


def _type_references(
    source: SourceFile,
    owner: SymbolId,
    nodes: Iterable[object | None],
    *,
    include_primitives: bool = False,
) -> tuple[ReferenceRef, ...]:
    return ordered_unique(
        reference(
            owner,
            node_span(source, leaf),
            ast_text(leaf),
            None,
            ReferenceKind.TYPE,
            context=ReferenceContext.TYPE,
            confidence=ReferenceConfidence.DEFINITE,
        )
        for node in nodes
        for leaf in _type_leaf_nodes(node)
        if include_primitives or ast_text(leaf) not in _PRIMITIVES
    )


def _property_parts(node: object) -> tuple[Any | None, Any | None, Any | None]:
    declaration = direct_child(node, {"variable_declaration"})
    if declaration is None:
        return None, None, None
    named = named_children(declaration)
    name_node = next(
        (child for child in named if child.type == "identifier"),
        None,
    )
    type_node = next(
        (child for child in reversed(named) if child.type in _TYPE_NODE_KINDS),
        None,
    )
    declaration_end = int(getattr(declaration, "end_byte", -1))
    value = next(
        (
            child
            for child in named_children(node)
            if int(getattr(child, "start_byte", -1)) >= declaration_end
            and child is not declaration
        ),
        None,
    )
    return name_node, type_node, value


def _call_parts(node: object) -> tuple[str | None, str] | None:
    if getattr(node, "type", "") == "constructor_delegation_call":
        token = next(
            (child for child in children(node) if child.type in {"super", "this"}),
            None,
        )
        return None, ast_text(token) or "this"
    callee = next(
        (child for child in named_children(node) if child.type != "value_arguments"),
        None,
    )
    if callee is None:
        return None
    if callee.type == "navigation_expression":
        named = named_children(callee)
        if not named:
            return None
        return ast_text(named[0]) or None, ast_text(named[-1])
    raw = ast_text(callee)
    return (None, raw) if raw else None


def _is_construct(node: object, name: str) -> bool:
    return getattr(node, "type", "") == "constructor_delegation_call" or bool(
        name and name[0].isupper()
    )


def _call(source: SourceFile, owner: SymbolId, node: Any) -> CallRef | None:
    parts = _call_parts(node)
    if parts is None:
        return None
    receiver, name = parts
    return CallRef(
        owner,
        node_span(source, node),
        name,
        receiver,
        CallKind.CONSTRUCT if _is_construct(node, name) else CallKind.CALL,
        argument_count(node),
    )


def _calls(
    source: SourceFile,
    owner: SymbolId,
    root: object | None,
) -> tuple[CallRef, ...]:
    nodes = [
        node
        for node in walk_owned(root, _FACT_BOUNDARIES)
        if node.type in _CALL_KINDS
    ]
    nodes.sort(key=lambda node: (node.start_byte, node.end_byte))
    return ordered_unique(
        call for node in nodes if (call := _call(source, owner, node)) is not None
    )


def _inferred_type(value: object | None) -> str | None:
    if value is None:
        return None
    if getattr(value, "type", "") in _ANONYMOUS_CALLABLE_KINDS:
        return None
    calls = [node for node in walk_all(value) if node.type == "call_expression"]
    calls.sort(key=lambda node: (node.start_byte, node.end_byte))
    for call in calls:
        parts = _call_parts(call)
        if parts is not None and _is_construct(call, parts[1]):
            return simple_type(parts[1])
    return None


def _local_bindings(body: object | None) -> tuple[Binding, ...]:
    values: list[Binding] = []
    for node in walk_owned(body, _FACT_BOUNDARIES):
        if node.type in _ANONYMOUS_CALLABLE_KINDS:
            parameter_root = _parameter_container(node)
            parameters = _parameters(parameter_root)
            values.extend(_parameter_bindings(parameters))
            typed_names = {parameter.name for parameter in parameters}
            values.extend(
                Binding(ast_text(name), "?")
                for parameter in named_children(parameter_root)
                for name in named_children(parameter)
                if parameter.type == "variable_declaration"
                and name.type == "identifier"
                and ast_text(name) not in typed_names
            )
        if node.type != "property_declaration":
            continue
        name_node, type_node, value = _property_parts(node)
        if name_node is None:
            continue
        type_name = (
            simple_type(tight_type(ast_text(type_node)).rstrip("?"))
            if type_node is not None
            else _inferred_type(value)
        )
        values.append(Binding(ast_text(name_node), type_name or "?"))
    return binding_tuple(values)


def _delegated_types(node: object) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    specifiers = direct_child(node, {"delegation_specifiers"})
    names: list[str] = []
    type_nodes: list[Any] = []
    for specifier in named_children(specifiers):
        if specifier.type != "delegation_specifier":
            continue
        candidate = next(
            (
                nested
                for nested in walk_all(specifier)
                if nested.type in _TYPE_NODE_KINDS
            ),
            None,
        )
        if candidate is None:
            continue
        names.append(simple_type(tight_type(ast_text(candidate))))
        type_nodes.append(candidate)
    return ordered_unique(names), tuple(type_nodes)


def _prefer_implemented_callables(symbols: Iterable[Symbol]) -> tuple[Symbol, ...]:
    """Keep a concrete declaration before a same-name bodyless signature."""
    values = list(symbols)
    callable_kinds = {
        SymbolKind.CONSTRUCTOR,
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.PROPERTY,
    }
    for index, symbol in enumerate(values):
        if symbol.kind not in callable_kinds or symbol.body_lines:
            continue
        replacement = next(
            (
                candidate_index
                for candidate_index in range(index + 1, len(values))
                if values[candidate_index].kind is symbol.kind
                and values[candidate_index].name == symbol.name
                and values[candidate_index].body_lines
            ),
            None,
        )
        if replacement is not None:
            values[index], values[replacement] = values[replacement], symbol
    return tuple(values)


class _Extractor:
    def __init__(self, source: SourceFile, root: Any) -> None:
        self.source = source
        self.root = root
        self.symbols: list[Symbol] = []
        self.calls: list[CallRef] = []
        self.references: list[ReferenceRef] = []
        self.bodies: list[BodyIR] = []

    def add_annotations(self, owner: SymbolId, node: object | None) -> None:
        self.references.extend(_annotation_references(self.source, owner, node))

    def body_facts(self, symbol: Symbol, node: Any) -> None:
        span = body_span(self.source, node)
        region = body_node(node)
        if node.type == "secondary_constructor":
            delegation = direct_child(node, {"constructor_delegation_call"})
            if region is None:
                region = delegation
                span = (
                    node_span(self.source, delegation)
                    if delegation is not None
                    else None
                )
            elif delegation is not None:
                first = node_span(self.source, delegation)
                last = node_span(self.source, region)
                span = SourceSpan(
                    self.source.file,
                    first.start_line,
                    first.start_column,
                    last.end_line,
                    last.end_column,
                )
        if span is None:
            return
        events = body_events(self.source, node, include_anonymous=True)
        self.bodies.append(BodyIR(symbol.id, span, events))
        call_roots = [
            root
            for root in (
                direct_child(node, {"constructor_delegation_call"}),
                body_node(node),
            )
            if root is not None
        ]
        for root in call_roots or ([region] if region is not None else []):
            self.calls.extend(_calls(self.source, symbol.id, root))
        self.references.extend(
            body_references(
                symbol.id,
                events,
                primitives=_PRIMITIVES,
                ignored_names={"super", "this"},
            )
        )

    def property(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        default_visibility: Visibility,
    ) -> Binding | None:
        name_node, type_node, value = _property_parts(node)
        if name_node is None:
            return None
        name = ast_text(name_node)
        type_name = tight_type(ast_text(type_node)) if type_node is not None else None
        modifiers = _modifier_values(node)
        kind = SymbolKind.CONSTANT if "const" in modifiers else SymbolKind.PROPERTY
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node, default=default_visibility),
            name,
            returns=type_name,
            annotations=_annotations(node),
            modifiers=modifiers,
        )
        self.symbols.append(symbol)
        self.references.extend(_type_references(self.source, symbol.id, (type_node,)))
        self.add_annotations(symbol.id, node)
        self.calls.extend(_calls(self.source, symbol.id, value))
        inferred = (
            simple_type(type_name.rstrip("?")) if type_name else _inferred_type(value)
        )
        return Binding(name, inferred) if inferred else None

    def type_alias(
        self,
        node: Any,
        container_path: tuple[str, ...],
    ) -> None:
        name_node = ast_field(node, "type")
        target = next(
            (
                child
                for child in reversed(named_children(node))
                if not same_node(child, name_node)
                and child.type in _TYPE_NODE_KINDS
            ),
            None,
        )
        name = ast_text(name_node)
        value = tight_type(ast_text(target))
        if not name or target is None or not value:
            return
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.TYPE, name),
            node_span(self.source, node),
            _visibility(node, default=Visibility.PUBLIC),
            f"type {name}",
            params=(value,),
            annotations=_annotations(node),
            modifiers=_modifier_values(node),
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (target,),
                include_primitives=True,
            )
        )
        self.add_annotations(symbol.id, node)

    def callable(
        self,
        node: Any,
        container_path: tuple[str, ...],
        type_name: str | None,
        class_bindings: tuple[Binding, ...],
        *,
        default_visibility: Visibility,
    ) -> None:
        constructor = node.type == "secondary_constructor"
        name_node = ast_field(node, "name")
        name = type_name if constructor else ast_text(name_node)
        if not name:
            return
        receiver_node = None if constructor else _extension_receiver(node)
        receiver_type = (
            tight_type(ast_text(receiver_node)) if receiver_node is not None else None
        )
        parameter_node = _parameter_container(node)
        parameters = _parameters(parameter_node)
        params = (
            *((receiver_type,) if receiver_type else ()),
            *(parameter.type_name for parameter in parameters),
        )
        returns, return_node = (type_name, None) if constructor else _return_type(node)
        kind = (
            SymbolKind.CONSTRUCTOR
            if constructor
            else SymbolKind.METHOD
            if type_name is not None
            else SymbolKind.FUNCTION
        )
        modifiers = ordered_unique(
            (*_modifier_values(node), *(("extension",) if receiver_type else ()))
        )
        entrypoint = name == "main" and type_name is None
        body = body_node(node)
        suffix = (
            f":{returns}" if returns and returns != "Unit" and not constructor else ""
        )
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name, params),
            node_span(self.source, node),
            _visibility(node, default=default_visibility),
            f"{name}({','.join(params)}){suffix}",
            params=params,
            returns=returns,
            bindings=binding_tuple(
                (
                    *class_bindings,
                    *(
                        (Binding("this", simple_type(receiver_type.rstrip("?"))),)
                        if receiver_type
                        else ()
                    ),
                    *_parameter_bindings(parameters),
                    *_local_bindings(body),
                )
            ),
            annotations=_annotations(node, entrypoint=entrypoint),
            modifiers=modifiers,
            body_lines=body_lines(body),
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (*(parameter.type_node for parameter in parameters), return_node),
            )
        )
        if receiver_node is not None:
            self.references.extend(
                _type_references(
                    self.source,
                    symbol.id,
                    (receiver_node,),
                    include_primitives=True,
                )
            )
        self.add_annotations(symbol.id, node)
        self.body_facts(symbol, node)

        if body is None:
            return
        callable_segment = f"{name}{symbol.id.signature_key}"
        for nested in self._nested_declarations(body):
            if nested.type in _TYPE_KINDS:
                self.type_declaration(nested, (*container_path, callable_segment))
            elif nested.type == "function_declaration":
                self.callable(
                    nested,
                    (*container_path, callable_segment),
                    None,
                    (),
                    default_visibility=Visibility.PRIVATE,
                )

    def _nested_declarations(self, root: object) -> tuple[Any, ...]:
        found: list[Any] = []
        stack = list(reversed(children(root)))
        while stack:
            node = stack.pop()
            if node.type in _TYPE_KINDS or node.type == "function_declaration":
                found.append(node)
                continue
            if node.type in _CALLABLE_KINDS - _ANONYMOUS_CALLABLE_KINDS:
                continue
            stack.extend(reversed(children(node)))
        return tuple(found)

    def primary_constructor(
        self,
        node: Any,
        container_path: tuple[str, ...],
        type_name: str,
        parameters: tuple[_Parameter, ...],
        class_bindings: tuple[Binding, ...],
    ) -> None:
        params = tuple(parameter.type_name for parameter in parameters)
        symbol = Symbol(
            symbol_id(
                self.source,
                container_path,
                SymbolKind.CONSTRUCTOR,
                type_name,
                params,
            ),
            node_span(self.source, node),
            _visibility(node, default=Visibility.PUBLIC),
            f"{type_name}({','.join(params)})",
            params=params,
            returns=type_name,
            bindings=binding_tuple((*class_bindings, *_parameter_bindings(parameters))),
            annotations=_annotations(node),
            modifiers=_modifier_values(node),
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (parameter.type_node for parameter in parameters),
            )
        )
        self.add_annotations(symbol.id, node)

    def companion(
        self,
        node: Any,
        container_path: tuple[str, ...],
    ) -> None:
        name_node = ast_field(node, "name")
        name = ast_text(name_node) or "Companion"
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.CLASS, name),
            node_span(self.source, node),
            _visibility(node, default=Visibility.PUBLIC),
            f"object {name}",
            annotations=_annotations(node),
            modifiers=ordered_unique(("companion", *_modifier_values(node))),
        )
        self.symbols.append(symbol)
        self.add_annotations(symbol.id, node)
        owned_path = (*container_path, name)
        self.type_body(
            direct_child(node, {"class_body"}),
            owned_path,
            name,
            SymbolKind.CLASS,
            (),
        )

    def type_body(
        self,
        body: object | None,
        owned_path: tuple[str, ...],
        type_name: str,
        kind: SymbolKind,
        initial_bindings: tuple[Binding, ...],
    ) -> None:
        if body is None:
            return
        class_bindings: list[Binding] = list(initial_bindings)
        members = named_children(body)
        for member in members:
            if member.type == "property_declaration":
                binding = self.property(
                    member,
                    owned_path,
                    default_visibility=Visibility.PUBLIC,
                )
                if binding is not None:
                    class_bindings.append(binding)
        frozen_bindings = binding_tuple(class_bindings)
        for member in members:
            if member.type in _TYPE_KINDS:
                self.type_declaration(member, owned_path)
            elif member.type == "companion_object":
                self.companion(member, owned_path)
            elif member.type in _CALLABLE_KINDS:
                self.callable(
                    member,
                    owned_path,
                    type_name,
                    frozen_bindings,
                    default_visibility=Visibility.PUBLIC,
                )

    def type_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
    ) -> None:
        name_node = ast_field(node, "name")
        if name_node is None:
            return
        name = ast_text(name_node)
        modifiers = _modifier_values(node)
        raw_tokens = {child.type for child in children(node)}
        interface = "interface" in raw_tokens
        enum = "enum" in modifiers
        data = "data" in modifiers
        kind = (
            SymbolKind.ENUM
            if enum
            else SymbolKind.INTERFACE
            if interface
            else SymbolKind.RECORD
            if data
            else SymbolKind.CLASS
        )
        constructor = direct_child(node, {"primary_constructor"})
        parameters = _parameters(_parameter_container(constructor))
        body = direct_child(node, {"class_body", "enum_class_body"})
        enum_entries = (
            tuple(child for child in named_children(body) if child.type == "enum_entry")
            if kind is SymbolKind.ENUM
            else ()
        )
        components = (
            tuple(parameter.name for parameter in parameters if parameter.property)
            if kind is SymbolKind.RECORD
            else tuple(
                ast_text(direct_child(entry, {"identifier"}))
                for entry in enum_entries
            )
            if kind is SymbolKind.ENUM
            else ()
        )
        params = (
            tuple(parameter.type_name for parameter in parameters)
            if parameters
            else components
            if kind is SymbolKind.ENUM
            else ()
        )
        supers, super_nodes = _delegated_types(node)
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node, default=Visibility.PUBLIC),
            f"{kind.value} {name}",
            params=params,
            supers=supers,
            components=components,
            annotations=_annotations(node),
            modifiers=modifiers,
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (*(parameter.type_node for parameter in parameters), *super_nodes),
            )
        )
        self.add_annotations(symbol.id, node)
        owned_path = (*container_path, name)

        class_bindings = _parameter_bindings(parameters)
        for parameter in parameters:
            if not parameter.property:
                continue
            property_symbol = Symbol(
                symbol_id(
                    self.source,
                    owned_path,
                    SymbolKind.PROPERTY,
                    parameter.name,
                ),
                node_span(self.source, parameter.node),
                _visibility(parameter.node, default=Visibility.PUBLIC),
                parameter.name,
                returns=parameter.type_name,
                annotations=_annotations(parameter.node),
                modifiers=ordered_unique(
                    (
                        "val"
                        if any(
                            child.type == "val" for child in children(parameter.node)
                        )
                        else "var",
                        *_modifier_values(parameter.node),
                    )
                ),
            )
            self.symbols.append(property_symbol)
            self.references.extend(
                _type_references(
                    self.source,
                    property_symbol.id,
                    (parameter.type_node,),
                )
            )

        for entry in enum_entries:
            entry_name_node = direct_child(entry, {"identifier"})
            if entry_name_node is None:
                continue
            entry_name = ast_text(entry_name_node)
            constant = Symbol(
                symbol_id(
                    self.source,
                    owned_path,
                    SymbolKind.CONSTANT,
                    entry_name,
                ),
                node_span(self.source, entry),
                Visibility.PUBLIC,
                entry_name,
                annotations=_annotations(entry),
                modifiers=_modifier_values(entry),
            )
            self.symbols.append(constant)
            self.add_annotations(constant.id, entry)

        if constructor is not None:
            self.primary_constructor(
                constructor,
                owned_path,
                name,
                parameters,
                class_bindings,
            )
        self.type_body(body, owned_path, name, kind, class_bindings)


def _module_name(root: object) -> tuple[str | None, Any | None]:
    header = direct_child(root, {"package_header"})
    qualified = direct_child(header, {"qualified_identifier"})
    return (ast_text(qualified) or None), header


def _imports(source: SourceFile, root: object) -> tuple[ImportRef, ...]:
    values: list[ImportRef] = []
    for node in named_children(root):
        if node.type != "import":
            continue
        qualified = direct_child(node, {"qualified_identifier"})
        if qualified is None:
            continue
        raw = ast_text(qualified)
        node_children = children(node)
        wildcard = any(child.type == "*" for child in node_children)
        alias = None
        if any(child.type == "as" for child in node_children):
            alias_node = next(
                (
                    child
                    for child in reversed(named_children(node))
                    if child.type == "identifier"
                ),
                None,
            )
            alias = ast_text(alias_node) or None
        if alias is not None or wildcard:
            module, name = raw, None
        else:
            module, separator, name = raw.rpartition(".")
            if not separator:
                module, name = raw, None
        values.append(
            ImportRef(
                node_span(source, node),
                module,
                name,
                alias,
                wildcard,
            )
        )
    return tuple(values)


def extract(source: SourceFile, parser: object | None) -> FileIR:
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("Kotlin extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    declared_module, package = _module_name(root)
    module = declared_module or file_module(source.file)
    module_symbol = Symbol(
        symbol_id(source, (), SymbolKind.MODULE, module),
        node_span(source, package if package is not None else root),
        Visibility.PUBLIC,
        f"module {module}",
    )
    extractor = _Extractor(source, root)
    extractor.symbols.append(module_symbol)
    for declaration in named_children(root):
        if declaration.type in _TYPE_KINDS:
            extractor.type_declaration(declaration, ())
        elif declaration.type == "type_alias":
            extractor.type_alias(declaration, ())
        elif declaration.type == "property_declaration":
            extractor.property(
                declaration,
                (),
                default_visibility=Visibility.PUBLIC,
            )
        elif declaration.type == "function_declaration":
            extractor.callable(
                declaration,
                (),
                None,
                (),
                default_visibility=Visibility.PUBLIC,
            )
        elif declaration.type == "object_declaration":
            extractor.type_declaration(declaration, ())

    return assemble_file_ir(
        source,
        module=module,
        symbols=_prefer_implemented_callables(extractor.symbols),
        calls=extractor.calls,
        imports=_imports(source, root),
        references=extractor.references,
        bodies=extractor.bodies,
        diagnostics=syntax_diagnostics(source, root, "Kotlin"),
    )


__all__ = ["extract"]
