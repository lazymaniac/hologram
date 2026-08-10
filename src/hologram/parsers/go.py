from __future__ import annotations

import ast
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
    field_nodes,
    named_children,
    simple_type,
    syntax_diagnostics,
    walk_all,
    walk_owned,
)
from .common import ordered_unique, reference, symbol_id, tight_type
from .treesitter import ast_field, ast_text, body_events, body_lines, node_span

_DECLARATION_BOUNDARIES = frozenset(
    {"function_declaration", "method_declaration", "type_declaration"}
)
_PRIMITIVES = frozenset(
    {
        "any",
        "bool",
        "byte",
        "complex128",
        "complex64",
        "error",
        "float32",
        "float64",
        "int",
        "int16",
        "int32",
        "int64",
        "int8",
        "rune",
        "string",
        "uint",
        "uint16",
        "uint32",
        "uint64",
        "uint8",
        "uintptr",
    }
)
_TYPE_LEAVES = frozenset({"package_identifier", "type_identifier"})


@dataclass(frozen=True, slots=True)
class _Parameter:
    type_name: str
    name: str | None
    node: Any
    type_node: Any


def _visibility(name: str) -> Visibility:
    return Visibility.PUBLIC if name[:1].isupper() else Visibility.PRIVATE


def _type_text(node: object | None) -> str:
    return tight_type(ast_text(node).strip())


def _binding_type(type_name: str) -> str:
    value = type_name.strip().removeprefix("...").strip()
    while value.startswith(("*", "&")):
        value = value[1:].strip()
    return simple_type(value) or "?"


def _parameters(node: object | None) -> tuple[_Parameter, ...]:
    values: list[_Parameter] = []
    for parameter in named_children(node):
        if parameter.type not in {
            "parameter_declaration",
            "variadic_parameter_declaration",
        }:
            continue
        type_node = ast_field(parameter, "type")
        if type_node is None:
            continue
        type_name = _type_text(type_node)
        if parameter.type == "variadic_parameter_declaration":
            type_name = f"...{type_name}"
        names = field_nodes(parameter, "name")
        if not names:
            values.append(_Parameter(type_name, None, parameter, type_node))
            continue
        values.extend(
            _Parameter(type_name, ast_text(name), parameter, type_node)
            for name in names
        )
    return tuple(values)


def _result(node: object) -> tuple[str | None, tuple[_Parameter, ...]]:
    result = ast_field(node, "result")
    if result is None:
        return None, ()
    if result.type != "parameter_list":
        return _type_text(result), ()
    parameters = _parameters(result)
    types = tuple(parameter.type_name for parameter in parameters)
    if not types:
        return None, parameters
    return (types[0] if len(types) == 1 else f"({','.join(types)})"), parameters


def _decode_import_path(node: object | None) -> str:
    raw = ast_text(node)
    if raw.startswith("`") and raw.endswith("`"):
        return raw[1:-1]
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw.strip('"`')
    return value if isinstance(value, str) else raw.strip('"`')


def _module(root: object) -> tuple[str | None, object | None]:
    package = next(
        (child for child in named_children(root) if child.type == "package_clause"),
        None,
    )
    name = next(
        (
            ast_text(child)
            for child in named_children(package)
            if child.type == "package_identifier"
        ),
        "",
    )
    return (name or None), package


def _imports(source: SourceFile, root: object) -> tuple[ImportRef, ...]:
    result: list[ImportRef] = []
    for node in walk_all(root):
        if node.type != "import_spec" or bool(getattr(node, "has_error", False)):
            continue
        path_node = ast_field(node, "path")
        module = _decode_import_path(path_node)
        if not module:
            continue
        alias_node = ast_field(node, "name")
        alias = ast_text(alias_node) if alias_node is not None else None
        result.append(
            ImportRef(
                node_span(source, node),
                module,
                None,
                alias,
                wildcard=alias == ".",
            )
        )
    return tuple(result)


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
            if not name or name in _PRIMITIVES:
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


def _declarator_names(node: object | None) -> tuple[Any, ...]:
    if node is None:
        return ()
    direct = tuple(
        child
        for child in named_children(node)
        if child.type in {"identifier", "blank_identifier"}
    )
    if direct:
        return direct
    return tuple(
        child
        for child in walk_all(node)
        if child is not node and child.type in {"identifier", "blank_identifier"}
    )


def _infer_type(node: Any | None) -> str:
    if node is None:
        return "?"
    if node.type == "expression_list":
        values = named_children(node)
        return _infer_type(values[0]) if len(values) == 1 else "?"
    if node.type == "composite_literal":
        return _binding_type(_type_text(ast_field(node, "type")))
    if node.type in {"unary_expression", "parenthesized_expression"}:
        nested = named_children(node)
        return _infer_type(nested[-1]) if nested else "?"
    if node.type == "call_expression":
        return "?"
    if node.type in {"int_literal", "float_literal", "imaginary_literal"}:
        return "int" if node.type == "int_literal" else "float64"
    if node.type in {"interpreted_string_literal", "raw_string_literal"}:
        return "string"
    if node.type in {"false", "true"}:
        return "bool"
    return "?"


def _local_bindings(body: object | None) -> tuple[Binding, ...]:
    result: list[Binding] = []
    for node in walk_owned(body, _DECLARATION_BOUNDARIES):
        if node.type == "var_spec":
            names = field_nodes(node, "name")
            type_node = ast_field(node, "type")
            value = ast_field(node, "value")
            values = named_children(value) if value is not None else ()
            for index, name_node in enumerate(names):
                name = ast_text(name_node)
                if name == "_":
                    continue
                type_name = (
                    _binding_type(_type_text(type_node))
                    if type_node is not None
                    else _infer_type(values[index] if index < len(values) else None)
                )
                result.append(Binding(name, type_name))
        elif node.type == "short_var_declaration":
            left = ast_field(node, "left")
            right = ast_field(node, "right")
            names = _declarator_names(left)
            values = named_children(right)
            for index, name_node in enumerate(names):
                name = ast_text(name_node)
                if name == "_":
                    continue
                value = values[index] if index < len(values) else None
                result.append(Binding(name, _infer_type(value)))
        elif node.type == "range_clause" and ":=" in ast_text(node):
            for name_node in _declarator_names(ast_field(node, "left")):
                name = ast_text(name_node)
                if name != "_":
                    result.append(Binding(name, "?"))
        elif node.type == "func_literal":
            result.extend(
                Binding(parameter.name, _binding_type(parameter.type_name))
                for parameter in _parameters(ast_field(node, "parameters"))
                if parameter.name is not None and parameter.name != "_"
            )
    return binding_tuple(result)


def _call_from_node(
    source: SourceFile,
    owner: SymbolId,
    node: object,
) -> CallRef | None:
    if getattr(node, "type", "") == "composite_literal":
        type_node = ast_field(node, "type")
        if type_node is None:
            return None
        body = ast_field(node, "body")
        return CallRef(
            owner,
            node_span(source, node),
            _type_text(type_node),
            None,
            CallKind.CONSTRUCT,
            len(named_children(body)),
        )
    if getattr(node, "type", "") != "call_expression":
        return None
    function = ast_field(node, "function")
    if function is None:
        return None
    receiver: str | None = None
    if function.type == "selector_expression":
        name_node = ast_field(function, "field")
        receiver_node = ast_field(function, "operand")
        if name_node is None:
            return None
        name = ast_text(name_node)
        receiver = ast_text(receiver_node) if receiver_node is not None else None
    else:
        raw = ast_text(function)
        name = re.sub(r"\[.*", "", raw).rsplit(".", 1)[-1]
    if not name:
        return None
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
        for node in walk_owned(root, _DECLARATION_BOUNDARIES)
        if node.type in {"call_expression", "composite_literal"}
        if (call := _call_from_node(source, owner, node)) is not None
    )


class _Extractor:
    def __init__(self, source: SourceFile, root: object) -> None:
        self.source = source
        self.root = root
        self.symbols: list[Symbol] = []
        self.calls: list[CallRef] = []
        self.references: list[ReferenceRef] = []
        self.bodies: list[BodyIR] = []
        self.struct_bindings: dict[str, tuple[Binding, ...]] = {}

    def extract_region(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        nested_only: bool = False,
    ) -> None:
        for child in named_children(node):
            if child.type == "type_declaration":
                for spec in named_children(child):
                    if spec.type == "type_spec":
                        self.type_spec(spec, container_path)
                continue
            if child.type in {"function_declaration", "method_declaration"}:
                if not nested_only:
                    self.callable(child, container_path)
                continue
            if child.type in {"const_declaration", "var_declaration"}:
                if not nested_only:
                    self.value_declaration(child, container_path)
                continue
            self.extract_region(child, container_path, nested_only=nested_only)

    def type_spec(self, node: object, container_path: tuple[str, ...]) -> None:
        name_node = ast_field(node, "name")
        type_node = ast_field(node, "type")
        name = ast_text(name_node)
        if not name or type_node is None:
            return
        if type_node.type == "struct_type":
            kind = SymbolKind.CLASS
            signature = f"struct {name}"
            field_list = next(
                (
                    child
                    for child in named_children(type_node)
                    if child.type == "field_declaration_list"
                ),
                None,
            )
            fields = tuple(
                child
                for child in named_children(field_list)
                if child.type == "field_declaration"
            )
            params: list[str] = []
            components: list[str] = []
            supers: list[str] = []
            class_bindings: list[Binding] = []
            for field in fields:
                field_type = ast_field(field, "type")
                type_name = _type_text(field_type)
                names = field_nodes(field, "name")
                if not names:
                    embedded = _binding_type(type_name)
                    if embedded:
                        supers.append(embedded)
                    continue
                for field_name_node in names:
                    field_name = ast_text(field_name_node)
                    params.append(type_name)
                    components.append(field_name)
                    class_bindings.append(Binding(field_name, _binding_type(type_name)))
        elif type_node.type == "interface_type":
            kind = SymbolKind.INTERFACE
            signature = f"interface {name}"
            params = []
            components = []
            supers = []
            class_bindings = []
            for member in named_children(type_node):
                if member.type in {"method_elem", "method_spec"}:
                    continue
                if member.type in {"type_identifier", "qualified_type"}:
                    supers.append(_binding_type(ast_text(member)))
        else:
            kind = SymbolKind.TYPE
            signature = f"type {name}"
            params = [_type_text(type_node)] if ast_text(type_node) else []
            components = []
            supers = []
            class_bindings = []
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(name),
            signature,
            params=tuple(params),
            supers=ordered_unique(supers),
            components=tuple(components),
        )
        self.symbols.append(symbol)
        self.references.extend(_type_references(self.source, symbol.id, (type_node,)))
        owned_path = (*container_path, name)
        if kind is SymbolKind.CLASS:
            self.struct_bindings[name] = binding_tuple(class_bindings)
            for field in fields:
                type_ref = ast_field(field, "type")
                for field_name_node in field_nodes(field, "name"):
                    field_name = ast_text(field_name_node)
                    field_symbol = Symbol(
                        symbol_id(
                            self.source,
                            owned_path,
                            SymbolKind.FIELD,
                            field_name,
                        ),
                        node_span(self.source, field_name_node),
                        _visibility(field_name),
                        field_name,
                        returns=_type_text(type_ref) or None,
                    )
                    self.symbols.append(field_symbol)
                    self.references.extend(
                        _type_references(self.source, field_symbol.id, (type_ref,))
                    )
        elif kind is SymbolKind.INTERFACE:
            for member in named_children(type_node):
                if member.type in {"method_elem", "method_spec"}:
                    self.interface_method(member, owned_path)

    def interface_method(
        self,
        node: object,
        container_path: tuple[str, ...],
    ) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        parameters = _parameters(ast_field(node, "parameters"))
        params = tuple(parameter.type_name for parameter in parameters)
        returns, result_parameters = _result(node)
        suffix = f":{returns}" if returns else ""
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.METHOD, name, params),
            node_span(self.source, node),
            _visibility(name),
            f"{name}({','.join(params)}){suffix}",
            params=params,
            returns=returns,
            bindings=binding_tuple(
                Binding(parameter.name, _binding_type(parameter.type_name))
                for parameter in (*parameters, *result_parameters)
                if parameter.name is not None and parameter.name != "_"
            ),
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (
                    *(parameter.type_node for parameter in parameters),
                    *(parameter.type_node for parameter in result_parameters),
                    ast_field(node, "result"),
                ),
            )
        )

    def value_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
    ) -> None:
        kind = (
            SymbolKind.CONSTANT
            if node.type == "const_declaration"
            else SymbolKind.FIELD
        )
        spec_kind = "const_spec" if kind is SymbolKind.CONSTANT else "var_spec"
        for spec in named_children(node):
            if spec.type != spec_kind:
                continue
            type_node = ast_field(spec, "type")
            value_node = ast_field(spec, "value")
            names = field_nodes(spec, "name")
            values = named_children(value_node)
            aligned_values = len(values) == len(names)
            for index, name_node in enumerate(names):
                name = ast_text(name_node)
                if not name or name == "_":
                    continue
                value = values[index] if aligned_values else value_node
                inferred = (
                    _type_text(type_node)
                    if type_node is not None
                    else _infer_type(value)
                )
                returns = inferred if inferred != "?" else None
                symbol = Symbol(
                    symbol_id(self.source, container_path, kind, name),
                    node_span(self.source, name_node),
                    _visibility(name),
                    name,
                    returns=returns,
                )
                self.symbols.append(symbol)
                self.references.extend(
                    _type_references(self.source, symbol.id, (type_node,))
                )
                self.calls.extend(_calls(self.source, symbol.id, value))

    def callable(self, node: Any, container_path: tuple[str, ...]) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        receiver_parameters = (
            _parameters(ast_field(node, "receiver"))
            if node.type == "method_declaration"
            else ()
        )
        container = (
            _binding_type(receiver_parameters[0].type_name)
            if receiver_parameters
            else None
        )
        owned_path = (*container_path, container) if container else container_path
        parameters = _parameters(ast_field(node, "parameters"))
        params = tuple(parameter.type_name for parameter in parameters)
        returns, result_parameters = _result(node)
        kind = SymbolKind.METHOD if container else SymbolKind.FUNCTION
        body = body_node(node)
        all_parameters = (*receiver_parameters, *parameters, *result_parameters)
        class_bindings = self.struct_bindings.get(container or "", ())
        bindings = binding_tuple(
            (
                *class_bindings,
                *(
                    Binding(parameter.name, _binding_type(parameter.type_name))
                    for parameter in all_parameters
                    if parameter.name is not None and parameter.name != "_"
                ),
                *_local_bindings(body),
            )
        )
        suffix = f":{returns}" if returns else ""
        annotations = ("entrypoint",) if name == "main" and container is None else ()
        symbol = Symbol(
            symbol_id(self.source, owned_path, kind, name, params),
            node_span(self.source, node),
            _visibility(name),
            f"{name}({','.join(params)}){suffix}",
            params=params,
            returns=returns,
            bindings=bindings,
            annotations=annotations,
            body_lines=body_lines(body),
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (
                    *(parameter.type_node for parameter in all_parameters),
                    ast_field(node, "result"),
                ),
            )
        )
        if body is None:
            return
        events = body_events(self.source, node, include_anonymous=True)
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
        callable_segment = f"{name}{symbol.id.signature_key}"
        self.extract_region(
            body,
            (*owned_path, callable_segment),
            nested_only=True,
        )


def extract(source: SourceFile, parser: object | None):
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("Go extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    module, package = _module(root)
    extractor = _Extractor(source, root)
    if module is not None and package is not None:
        extractor.symbols.append(
            Symbol(
                symbol_id(source, (), SymbolKind.MODULE, module),
                node_span(source, package),
                Visibility.PUBLIC,
                f"module {module}",
            )
        )
    extractor.extract_region(root, ())
    return assemble_file_ir(
        source,
        module=module,
        symbols=extractor.symbols,
        calls=extractor.calls,
        imports=_imports(source, root),
        references=extractor.references,
        bodies=extractor.bodies,
        diagnostics=syntax_diagnostics(source, root, "Go"),
    )


__all__ = ["extract"]
