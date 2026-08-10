from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from hologram.model import (
    Binding,
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
    body_node,
    body_references,
    children,
    direct_child,
    file_module,
    named_children,
    simple_type,
    syntax_diagnostics,
    walk_all,
    walk_owned,
)
from .common import ordered_unique, reference, symbol_id, tight_type
from .treesitter import ast_field, ast_text, body_events, body_lines, node_span

_ITEM_KINDS = frozenset(
    {
        "const_item",
        "enum_item",
        "function_item",
        "function_signature_item",
        "impl_item",
        "mod_item",
        "static_item",
        "struct_item",
        "trait_item",
        "type_item",
        "union_item",
    }
)
_CALLABLE_KINDS = frozenset({"closure_expression", "function_item"})
_OWNERSHIP_BOUNDARIES = _ITEM_KINDS | _CALLABLE_KINDS | frozenset({"async_block"})
_TYPE_LEAVES = frozenset({"type_identifier"})
_PRIMITIVES = frozenset(
    {
        "bool",
        "char",
        "f32",
        "f64",
        "i128",
        "i16",
        "i32",
        "i64",
        "i8",
        "isize",
        "never",
        "str",
        "u128",
        "u16",
        "u32",
        "u64",
        "u8",
        "usize",
    }
)
_PATTERN_KINDS = frozenset(
    {
        "captured_pattern",
        "identifier",
        "match_pattern",
        "mut_pattern",
        "or_pattern",
        "ref_pattern",
        "reference_pattern",
        "slice_pattern",
        "struct_pattern",
        "tuple_pattern",
        "tuple_struct_pattern",
    }
)


@dataclass(frozen=True, slots=True)
class _Parameter:
    type_name: str
    names: tuple[str, ...]
    node: Any
    type_node: Any | None
    is_self: bool = False


def _source_span(source: SourceFile) -> SourceSpan:
    lines = source.raw.splitlines(keepends=True)
    if not lines:
        return SourceSpan(source.file, 1, 0, 1, 0)
    if source.raw.endswith((b"\n", b"\r")):
        return SourceSpan(source.file, 1, 0, len(lines) + 1, 0)
    last = lines[-1].rstrip(b"\r\n")
    return SourceSpan(source.file, 1, 0, len(lines), len(last))


def _visibility(node: object, *, implicit_public: bool = False) -> Visibility:
    modifier = direct_child(node, {"visibility_modifier"})
    if modifier is None:
        return Visibility.PUBLIC if implicit_public else Visibility.PRIVATE
    text = ast_text(modifier)
    return Visibility.PUBLIC if text == "pub" else Visibility.INTERNAL


def _attribute_nodes(node: object) -> tuple[Any, ...]:
    return tuple(
        child
        for child in named_children(node)
        if child.type
        in {"attribute_item", "inner_attribute_item", "outer_attribute_item"}
    )


def _annotations(node: object) -> tuple[str, ...]:
    values: list[str] = []
    for attribute_item in _attribute_nodes(node):
        attribute = direct_child(attribute_item, {"attribute"})
        text = ast_text(attribute or attribute_item)
        text = text.removeprefix("#![").removeprefix("#[").removesuffix("]")
        if text:
            values.append(text)
    return tuple(values)


def _modifiers(node: object, *, override: bool = False) -> tuple[str, ...]:
    values: list[str] = []
    for child in children(node):
        text = ast_text(child).strip()
        if (
            text in {"async", "const", "default", "extern", "pub", "unsafe"}
            or child.type == "visibility_modifier"
            and text
        ):
            values.append(text)
        elif child.type == "function_modifiers":
            values.extend(
                ast_text(item)
                for item in children(child)
                if ast_text(item) in {"async", "const", "extern", "unsafe"}
            )
    if override:
        values.append("override")
    return ordered_unique(values)


def _annotation_references(
    source: SourceFile,
    owner: SymbolId,
    node: object,
) -> tuple[ReferenceRef, ...]:
    values: list[ReferenceRef] = []
    for attribute_item in _attribute_nodes(node):
        attribute = direct_child(attribute_item, {"attribute"}) or attribute_item
        name_node = next(
            (
                child
                for child in walk_all(attribute)
                if child.type in {"identifier", "type_identifier"}
            ),
            None,
        )
        if name_node is None:
            continue
        values.append(
            reference(
                owner,
                node_span(source, name_node),
                ast_text(name_node),
                None,
                ReferenceKind.TYPE,
                context=ReferenceContext.ANNOTATION,
                confidence=ReferenceConfidence.DEFINITE,
            )
        )
    return ordered_unique(values)


def _type_text(node: object | None) -> str:
    return tight_type(ast_text(node).strip())


def _binding_type(type_name: str) -> str:
    value = re.sub(r"^&(?:'[^ ]+\s+)?(?:mut\s+)?", "", type_name.strip())
    return simple_type(value) or "?"


def _pattern_names(node: Any | None) -> tuple[str, ...]:
    if node is None:
        return ()
    if node.type == "identifier":
        name = ast_text(node)
        return () if name == "_" else (name,)
    values: list[str] = []
    for child in named_children(node):
        if child.type in {"type_identifier", "scoped_type_identifier"}:
            continue
        field_name = None
        if callable(getattr(node, "field_name_for_child", None)):
            for index, candidate in enumerate(children(node)):
                if candidate.id == child.id:
                    field_name = node.field_name_for_child(index)
                    break
        if field_name in {"name", "path", "type"} and node.type in {
            "field_pattern",
            "struct_pattern",
            "tuple_struct_pattern",
        }:
            continue
        if child.type in _PATTERN_KINDS or child.type in {
            "field_pattern",
            "remaining_field_pattern",
        }:
            values.extend(_pattern_names(child))
    return ordered_unique(values)


def _parameters(node: object | None) -> tuple[_Parameter, ...]:
    values: list[_Parameter] = []
    for parameter in named_children(node):
        if parameter.type == "self_parameter":
            values.append(_Parameter("Self", ("self",), parameter, None, True))
            continue
        if parameter.type != "parameter":
            continue
        type_node = ast_field(parameter, "type")
        pattern = ast_field(parameter, "pattern")
        if type_node is None:
            continue
        values.append(
            _Parameter(
                _type_text(type_node),
                _pattern_names(pattern),
                parameter,
                type_node,
            )
        )
    return tuple(values)


def _type_references(
    source: SourceFile,
    owner: SymbolId,
    roots: Iterable[object | None],
) -> tuple[ReferenceRef, ...]:
    values: list[ReferenceRef] = []
    for root in roots:
        for node in walk_all(root):
            if node.type not in _TYPE_LEAVES:
                continue
            name = ast_text(node)
            if name in _PRIMITIVES or not name:
                continue
            values.append(
                reference(
                    owner,
                    node_span(source, node),
                    name,
                    None,
                    ReferenceKind.TYPE,
                    context=ReferenceContext.TYPE,
                    confidence=ReferenceConfidence.DEFINITE,
                )
            )
    return ordered_unique(values)


def _path_parts(node: object | None) -> tuple[str, ...]:
    if node is None:
        return ()
    raw = ast_text(node).strip()
    return tuple(part for part in raw.split("::") if part)


def _path_import(
    source: SourceFile,
    node: Any,
    parts: tuple[str, ...],
    *,
    alias: str | None,
    wildcard: bool,
    reexport: bool,
) -> ImportRef | None:
    if wildcard:
        if not parts:
            return None
        return ImportRef(
            node_span(source, node),
            "::".join(parts),
            None,
            alias,
            True,
            reexport,
        )
    if not parts:
        return None
    module = "::".join(parts[:-1])
    return ImportRef(
        node_span(source, node),
        module,
        parts[-1],
        alias,
        False,
        reexport,
    )


def _flatten_use(
    source: SourceFile,
    node: Any,
    prefix: tuple[str, ...],
    *,
    reexport: bool,
) -> list[ImportRef]:
    if node.type == "use_as_clause":
        path = ast_field(node, "path")
        alias_node = ast_field(node, "alias")
        parts = prefix if ast_text(path) == "self" else (*prefix, *_path_parts(path))
        imported = _path_import(
            source,
            node,
            parts,
            alias=ast_text(alias_node) or None,
            wildcard=False,
            reexport=reexport,
        )
        return [imported] if imported is not None else []
    if node.type == "scoped_use_list":
        path = _path_parts(ast_field(node, "path"))
        base = (*prefix, *path)
        listing = ast_field(node, "list") or direct_child(node, {"use_list"})
        return [
            imported
            for child in named_children(listing)
            for imported in _flatten_use(
                source,
                child,
                base,
                reexport=reexport,
            )
        ]
    if node.type == "use_list":
        return [
            imported
            for child in named_children(node)
            for imported in _flatten_use(
                source,
                child,
                prefix,
                reexport=reexport,
            )
        ]
    if node.type == "use_wildcard":
        imported = _path_import(
            source,
            node,
            prefix,
            alias=None,
            wildcard=True,
            reexport=reexport,
        )
        return [imported] if imported is not None else []
    if node.type == "self":
        imported = _path_import(
            source,
            node,
            prefix,
            alias=None,
            wildcard=False,
            reexport=reexport,
        )
        return [imported] if imported is not None else []
    parts = (*prefix, *_path_parts(node))
    imported = _path_import(
        source,
        node,
        parts,
        alias=None,
        wildcard=False,
        reexport=reexport,
    )
    return [imported] if imported is not None else []


def _imports(source: SourceFile, root: object) -> tuple[ImportRef, ...]:
    values: list[ImportRef] = []
    for node in walk_all(root):
        if node.type != "use_declaration" or bool(getattr(node, "has_error", False)):
            continue
        argument = ast_field(node, "argument")
        if argument is None:
            continue
        reexport = direct_child(node, {"visibility_modifier"}) is not None
        values.extend(_flatten_use(source, argument, (), reexport=reexport))
    return tuple(values)


def _infer_type(node: Any | None) -> str:
    if node is None:
        return "?"
    if node.type == "struct_expression":
        return _binding_type(_type_text(ast_field(node, "name")))
    if node.type in {"reference_expression", "unary_expression"}:
        values = named_children(node)
        return _infer_type(values[-1]) if values else "?"
    if node.type == "call_expression":
        function = ast_field(node, "function")
        if function is None:
            return "?"
        if function.type == "scoped_identifier":
            return _binding_type(ast_text(ast_field(function, "path")))
        return "?"
    if node.type == "string_literal":
        return "str"
    if node.type == "boolean_literal":
        return "bool"
    if node.type in {"integer_literal", "negative_literal"}:
        return "?"
    return "?"


def _local_bindings(body: object | None) -> tuple[Binding, ...]:
    values: list[Binding] = []
    for node in walk_owned(body, _OWNERSHIP_BOUNDARIES):
        if node.type == "let_declaration":
            pattern = ast_field(node, "pattern")
            type_node = ast_field(node, "type")
            type_name = (
                _binding_type(_type_text(type_node))
                if type_node is not None
                else _infer_type(ast_field(node, "value"))
            )
            values.extend(Binding(name, type_name) for name in _pattern_names(pattern))
        elif node.type in {"for_expression", "let_condition"}:
            values.extend(
                Binding(name, "?")
                for name in _pattern_names(ast_field(node, "pattern"))
            )
        elif node.type == "match_arm":
            pattern = ast_field(node, "pattern") or direct_child(
                node,
                {"match_pattern"},
            )
            values.extend(Binding(name, "?") for name in _pattern_names(pattern))
    return binding_tuple(values)


def _callee(node: Any | None) -> tuple[str | None, str] | None:
    if node is None:
        return None
    if node.type == "field_expression":
        field = ast_field(node, "field")
        value = ast_field(node, "value")
        if field is None:
            return None
        return (ast_text(value) if value is not None else None), ast_text(field)
    if node.type in {"scoped_identifier", "scoped_type_identifier"}:
        name = ast_field(node, "name")
        path = ast_field(node, "path")
        if name is None:
            return None
        return (ast_text(path) if path is not None else None), ast_text(name)
    if node.type == "generic_function":
        function = ast_field(node, "function") or next(
            (child for child in named_children(node) if child.type != "type_arguments"),
            None,
        )
        return _callee(function)
    if node.type in {"identifier", "type_identifier"}:
        return None, ast_text(node)
    raw = ast_text(node)
    if "::" in raw:
        receiver, name = raw.rsplit("::", 1)
        return receiver, re.sub(r"::<.*", "", name)
    return (None, raw) if raw else None


def _call_from_node(
    source: SourceFile,
    owner: SymbolId,
    node: Any,
) -> CallRef | None:
    if node.type == "struct_expression":
        name_node = ast_field(node, "name")
        if name_node is None:
            return None
        body = ast_field(node, "body")
        return CallRef(
            owner,
            node_span(source, node),
            _type_text(name_node),
            None,
            CallKind.CONSTRUCT,
            len(named_children(body)),
        )
    if node.type != "call_expression":
        return None
    callee = _callee(ast_field(node, "function"))
    if callee is None or not callee[1]:
        return None
    receiver, name = callee
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
        for node in walk_owned(root, _OWNERSHIP_BOUNDARIES)
        if node.type in {"call_expression", "struct_expression"}
        if (call := _call_from_node(source, owner, node)) is not None
    )


def _trait_bounds(node: object | None) -> tuple[str, ...]:
    return ordered_unique(
        _binding_type(ast_text(child))
        for child in walk_all(node)
        if child.type in {"type_identifier", "scoped_type_identifier"}
        and ast_text(child)
    )


class _Extractor:
    def __init__(self, source: SourceFile, root: object) -> None:
        self.source = source
        self.root = root
        self.symbols: list[Symbol] = []
        self.calls: list[CallRef] = []
        self.references: list[ReferenceRef] = []
        self.bodies: list[BodyIR] = []
        relations: dict[str, list[str]] = {}
        for node in walk_all(root):
            if node.type != "impl_item":
                continue
            target = _binding_type(_type_text(ast_field(node, "type")))
            trait = ast_field(node, "trait")
            trait_name = _binding_type(_type_text(trait)) if trait is not None else ""
            if target and trait_name:
                relations.setdefault(target, []).append(trait_name)
        self.relations = {
            name: ordered_unique(values) for name, values in relations.items()
        }

    def extract_region(self, node: object, container_path: tuple[str, ...]) -> None:
        for child in named_children(node):
            if child.type in {"struct_item", "union_item"}:
                self.struct_item(child, container_path)
            elif child.type == "enum_item":
                self.enum_item(child, container_path)
            elif child.type == "trait_item":
                self.trait_item(child, container_path)
            elif child.type == "impl_item":
                self.impl_item(child, container_path)
            elif child.type in {"function_item", "function_signature_item"}:
                self.callable(child, container_path, None)
            elif child.type in {"const_item", "static_item"}:
                self.value_item(child, container_path)
            elif child.type == "type_item":
                self.type_item(child, container_path)
            elif child.type == "mod_item":
                self.module_item(child, container_path)
            elif child.type not in _ITEM_KINDS:
                self.extract_region(child, container_path)

    def add_symbol(self, symbol: Symbol, node: object) -> None:
        self.symbols.append(symbol)
        self.references.extend(_annotation_references(self.source, symbol.id, node))

    def struct_item(self, node: Any, container_path: tuple[str, ...]) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        body = ast_field(node, "body")
        fields = tuple(
            child
            for child in named_children(body)
            if child.type in {"field_declaration", "ordered_field_declaration"}
        )
        params = tuple(_type_text(ast_field(field, "type")) for field in fields)
        named_fields = tuple(
            field for field in fields if ast_field(field, "name") is not None
        )
        components = tuple(ast_text(ast_field(field, "name")) for field in named_fields)
        kind = SymbolKind.CLASS
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node),
            f"{'union' if node.type == 'union_item' else 'struct'} {name}",
            params=params,
            supers=self.relations.get(name, ()),
            components=components,
            annotations=_annotations(node),
            modifiers=_modifiers(node),
        )
        self.add_symbol(symbol, node)
        self.references.extend(
            _type_references(
                self.source, symbol.id, (body, ast_field(node, "type_parameters"))
            )
        )
        owned_path = (*container_path, name)
        for field in named_fields:
            name_node = ast_field(field, "name")
            type_node = ast_field(field, "type")
            field_name = ast_text(name_node)
            field_symbol = Symbol(
                symbol_id(self.source, owned_path, SymbolKind.FIELD, field_name),
                node_span(self.source, field),
                _visibility(field),
                field_name,
                returns=_type_text(type_node) or None,
                annotations=_annotations(field),
                modifiers=_modifiers(field),
            )
            self.add_symbol(field_symbol, field)
            self.references.extend(
                _type_references(self.source, field_symbol.id, (type_node,))
            )

    def enum_item(self, node: object, container_path: tuple[str, ...]) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        body = ast_field(node, "body")
        variants = tuple(
            child for child in named_children(body) if child.type == "enum_variant"
        )
        names = tuple(ast_text(ast_field(variant, "name")) for variant in variants)
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.ENUM, name),
            node_span(self.source, node),
            _visibility(node),
            f"enum {name}",
            params=names,
            supers=self.relations.get(name, ()),
            components=names,
            annotations=_annotations(node),
            modifiers=_modifiers(node),
        )
        self.add_symbol(symbol, node)
        owned_path = (*container_path, name)
        for variant in variants:
            variant_name = ast_text(ast_field(variant, "name"))
            if not variant_name:
                continue
            constant = Symbol(
                symbol_id(
                    self.source,
                    owned_path,
                    SymbolKind.CONSTANT,
                    variant_name,
                ),
                node_span(self.source, variant),
                Visibility.PUBLIC,
                variant_name,
                returns=name,
                annotations=_annotations(variant),
            )
            self.add_symbol(constant, variant)

    def trait_item(self, node: object, container_path: tuple[str, ...]) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        bounds = ast_field(node, "bounds")
        supers = _trait_bounds(bounds)
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.INTERFACE, name),
            node_span(self.source, node),
            _visibility(node),
            f"trait {name}",
            supers=supers,
            annotations=_annotations(node),
            modifiers=_modifiers(node),
        )
        self.add_symbol(symbol, node)
        self.references.extend(_type_references(self.source, symbol.id, (bounds,)))
        owned_path = (*container_path, name)
        body = ast_field(node, "body")
        for member in named_children(body):
            if member.type in {"function_item", "function_signature_item"}:
                self.callable(
                    member,
                    owned_path,
                    name,
                    implicit_public=symbol.visibility is Visibility.PUBLIC,
                )
            elif member.type in {"const_item", "static_item"}:
                self.value_item(member, owned_path, implicit_public=True)
            elif member.type == "type_item":
                self.type_item(member, owned_path, implicit_public=True)

    def impl_item(self, node: object, container_path: tuple[str, ...]) -> None:
        target = _binding_type(_type_text(ast_field(node, "type")))
        if not target:
            return
        trait = ast_field(node, "trait")
        override = trait is not None
        owned_path = (*container_path, target)
        body = ast_field(node, "body")
        for member in named_children(body):
            if member.type == "function_item":
                self.callable(
                    member,
                    owned_path,
                    target,
                    override=override,
                )
            elif member.type in {"const_item", "static_item"}:
                self.value_item(member, owned_path)
            elif member.type == "type_item":
                self.type_item(member, owned_path)

    def value_item(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        implicit_public: bool = False,
    ) -> None:
        name_node = ast_field(node, "name")
        name = ast_text(name_node)
        if not name:
            return
        type_node = ast_field(node, "type")
        kind = SymbolKind.CONSTANT if node.type == "const_item" else SymbolKind.FIELD
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node, implicit_public=implicit_public),
            name,
            returns=_type_text(type_node) or None,
            annotations=_annotations(node),
            modifiers=_modifiers(node),
        )
        self.add_symbol(symbol, node)
        self.references.extend(_type_references(self.source, symbol.id, (type_node,)))
        value = ast_field(node, "value")
        self.calls.extend(_calls(self.source, symbol.id, value))

    def type_item(
        self,
        node: object,
        container_path: tuple[str, ...],
        *,
        implicit_public: bool = False,
    ) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        type_node = ast_field(node, "type")
        value = _type_text(type_node)
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.TYPE, name),
            node_span(self.source, node),
            _visibility(node, implicit_public=implicit_public),
            f"type {name}",
            params=(value,) if value else (),
            annotations=_annotations(node),
            modifiers=_modifiers(node),
        )
        self.add_symbol(symbol, node)
        self.references.extend(_type_references(self.source, symbol.id, (type_node,)))

    def module_item(self, node: object, container_path: tuple[str, ...]) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.MODULE, name),
            node_span(self.source, node),
            _visibility(node),
            f"module {name}",
            annotations=_annotations(node),
            modifiers=_modifiers(node),
        )
        self.add_symbol(symbol, node)
        body = ast_field(node, "body") or direct_child(node, {"declaration_list"})
        if body is not None:
            self.extract_region(body, (*container_path, name))

    def callable(
        self,
        node: object,
        container_path: tuple[str, ...],
        self_type: str | None,
        *,
        implicit_public: bool = False,
        override: bool = False,
    ) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        parameters = _parameters(ast_field(node, "parameters"))
        declared = tuple(parameter for parameter in parameters if not parameter.is_self)
        params = tuple(parameter.type_name for parameter in declared)
        return_node = ast_field(node, "return_type")
        returns = _type_text(return_node) or None
        kind = SymbolKind.METHOD if self_type is not None else SymbolKind.FUNCTION
        body = body_node(node)
        parameter_bindings: list[Binding] = []
        for parameter in parameters:
            type_name = (
                self_type
                if parameter.is_self and self_type
                else _binding_type(parameter.type_name)
            )
            parameter_bindings.extend(
                Binding(binding_name, type_name) for binding_name in parameter.names
            )
        suffix = f":{returns}" if returns else ""
        annotations = list(_annotations(node))
        if name == "main" and self_type is None:
            annotations.append("entrypoint")
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name, params),
            node_span(self.source, node),
            _visibility(node, implicit_public=implicit_public),
            f"{name}({','.join(params)}){suffix}",
            params=params,
            returns=returns,
            bindings=binding_tuple((*parameter_bindings, *_local_bindings(body))),
            annotations=ordered_unique(annotations),
            modifiers=_modifiers(node, override=override),
            body_lines=body_lines(body),
        )
        self.add_symbol(symbol, node)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (*(parameter.type_node for parameter in declared), return_node),
            )
        )
        if body is None:
            return
        events = body_events(self.source, node)
        self.bodies.append(BodyIR(symbol.id, node_span(self.source, body), events))
        self.calls.extend(_calls(self.source, symbol.id, body))
        self.references.extend(
            body_references(
                symbol.id,
                events,
                primitives=_PRIMITIVES,
                ignored_names={name},
            )
        )
        segment = f"{name}{symbol.id.signature_key}"
        self.extract_region(body, (*container_path, segment))


def extract(source: SourceFile, parser: object | None):
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("Rust extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    module = file_module(source.file)
    extractor = _Extractor(source, root)
    module_symbol = Symbol(
        symbol_id(source, (), SymbolKind.MODULE, module),
        _source_span(source),
        Visibility.PUBLIC,
        f"module {module}",
        annotations=tuple(
            ast_text(direct_child(node, {"attribute"}) or node)
            .removeprefix("#![")
            .removeprefix("#[")
            .removesuffix("]")
            for node in named_children(root)
            if node.type == "inner_attribute_item"
        ),
    )
    extractor.symbols.append(module_symbol)
    extractor.extract_region(root, ())
    return assemble_file_ir(
        source,
        module=module,
        symbols=extractor.symbols,
        calls=extractor.calls,
        imports=_imports(source, root),
        references=extractor.references,
        bodies=extractor.bodies,
        diagnostics=syntax_diagnostics(source, root, "Rust"),
    )


__all__ = ["extract"]
