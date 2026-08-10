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
    simple_type,
    syntax_diagnostics,
    walk_all,
    walk_owned,
)
from .common import ordered_unique, reference, symbol_id, tight_type
from .treesitter import ast_field, ast_text, body_events, body_lines, node_span

_TYPE_KINDS = {
    "class_declaration": SymbolKind.CLASS,
    "enum_declaration": SymbolKind.ENUM,
    "interface_declaration": SymbolKind.INTERFACE,
    "record_declaration": SymbolKind.RECORD,
    "struct_declaration": SymbolKind.CLASS,
}
_CALLABLE_KINDS = frozenset(
    {
        "constructor_declaration",
        "conversion_operator_declaration",
        "destructor_declaration",
        "local_function_statement",
        "method_declaration",
        "operator_declaration",
    }
)
_OWNERSHIP_BOUNDARIES = frozenset(
    {
        *_TYPE_KINDS,
        *_CALLABLE_KINDS,
        "accessor_declaration",
        "anonymous_method_expression",
        "lambda_expression",
    }
)
_PRIMITIVES = frozenset(
    {
        "bool",
        "byte",
        "char",
        "decimal",
        "double",
        "dynamic",
        "float",
        "int",
        "long",
        "nint",
        "nuint",
        "object",
        "sbyte",
        "short",
        "string",
        "uint",
        "ulong",
        "ushort",
        "var",
        "void",
    }
)
_TYPE_LEAVES = frozenset({"identifier", "name", "type_identifier"})
_CALL_KINDS = frozenset(
    {
        "implicit_object_creation_expression",
        "invocation_expression",
        "object_creation_expression",
    }
)


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    type_name: str
    node: Any
    type_node: Any


def _modifier_values(node: object | None) -> tuple[str, ...]:
    return ordered_unique(
        text
        for child in named_children(node)
        if child.type == "modifier"
        if (text := ast_text(child).strip())
    )


def _visibility(
    node: object | None,
    *,
    default: Visibility,
) -> Visibility:
    modifiers = _modifier_values(node)
    if "public" in modifiers:
        return Visibility.PUBLIC
    if "protected" in modifiers:
        return Visibility.PROTECTED
    if "internal" in modifiers:
        return Visibility.INTERNAL
    if "private" in modifiers:
        return Visibility.PRIVATE
    return default


def _attribute_nodes(node: object | None) -> tuple[Any, ...]:
    return tuple(
        attribute
        for child in named_children(node)
        if child.type == "attribute_list"
        for attribute in walk_all(child)
        if attribute.type == "attribute"
    )


def _attribute_target(attribute: Any) -> Any | None:
    target = ast_field(attribute, "name")
    if target is None:
        target = direct_child(attribute, {"identifier", "qualified_name"})
    if target is None:
        return None
    leaves = [node for node in walk_all(target) if node.type in {"identifier", "name"}]
    return leaves[-1] if leaves else target


def _annotations(node: object | None, *, entrypoint: bool = False) -> tuple[str, ...]:
    values = [
        ast_text(target)
        for attribute in _attribute_nodes(node)
        if (target := _attribute_target(attribute)) is not None
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
        for attribute in _attribute_nodes(node)
        if (target := _attribute_target(attribute)) is not None
        if ast_text(target)
    )


def _parameters(node: object | None) -> tuple[_Parameter, ...]:
    values: list[_Parameter] = []
    if node is None:
        return ()
    for parameter in named_children(node):
        if parameter.type != "parameter":
            continue
        name_node = ast_field(parameter, "name")
        type_node = ast_field(parameter, "type")
        if name_node is None or type_node is None:
            continue
        values.append(
            _Parameter(
                ast_text(name_node),
                tight_type(ast_text(type_node)),
                parameter,
                type_node,
            )
        )
    return tuple(values)


def _parameter_bindings(parameters: Iterable[_Parameter]) -> tuple[Binding, ...]:
    return tuple(
        Binding(parameter.name, simple_type(parameter.type_name))
        for parameter in parameters
    )


def _type_leaf_nodes(root: object | None) -> tuple[Any, ...]:
    if root is None:
        return ()
    candidates = tuple(node for node in walk_all(root) if node.type in _TYPE_LEAVES)
    if candidates:
        return candidates
    return (root,) if ast_text(root) not in _PRIMITIVES else ()


def _type_references(
    source: SourceFile,
    owner: SymbolId,
    nodes: Iterable[object | None],
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
        if ast_text(leaf) not in _PRIMITIVES
    )


def _declarators(declaration: object | None) -> tuple[Any, ...]:
    if declaration is None:
        return ()
    return tuple(
        child
        for child in named_children(declaration)
        if child.type == "variable_declarator"
    )


def _initializer(declarator: object) -> Any | None:
    named = named_children(declarator)
    name = ast_field(declarator, "name")
    return next(
        (child for child in reversed(named) if child is not name),
        None,
    )


def _inferred_type(value: object | None) -> str | None:
    if value is None:
        return None
    creation = next(
        (
            node
            for node in walk_all(value)
            if node.type
            in {"implicit_object_creation_expression", "object_creation_expression"}
        ),
        None,
    )
    if creation is None:
        return None
    type_node = ast_field(creation, "type")
    return simple_type(ast_text(type_node)) if type_node is not None else None


def _local_bindings(body: object | None) -> tuple[Binding, ...]:
    values: list[Binding] = []
    for node in walk_owned(body, _OWNERSHIP_BOUNDARIES):
        if node.type != "variable_declaration":
            continue
        type_node = ast_field(node, "type")
        declared = tight_type(ast_text(type_node)) if type_node is not None else ""
        for declarator in _declarators(node):
            name_node = ast_field(declarator, "name")
            if name_node is None:
                continue
            type_name = (
                _inferred_type(_initializer(declarator))
                if not declared or declared == "var"
                else simple_type(declared)
            )
            if type_name:
                values.append(Binding(ast_text(name_node), type_name))
    return binding_tuple(values)


def _member_call_parts(function: object | None) -> tuple[str | None, str] | None:
    if function is None:
        return None
    function_kind = str(getattr(function, "type", ""))
    if function_kind in {"member_access_expression", "member_binding_expression"}:
        name_node = ast_field(function, "name")
        expression = ast_field(function, "expression")
        if name_node is None:
            named = named_children(function)
            name_node = named[-1] if named else None
        if name_node is None:
            return None
        receiver = ast_text(expression) if expression is not None else None
        return receiver or None, ast_text(name_node)
    if function_kind == "generic_name":
        name = next(
            (
                ast_text(child)
                for child in named_children(function)
                if child.type == "identifier"
            ),
            ast_text(function),
        )
        return None, name
    name = ast_text(function)
    return (None, name) if name else None


def _call(source: SourceFile, owner: SymbolId, node: Any) -> CallRef | None:
    if node.type in {
        "implicit_object_creation_expression",
        "object_creation_expression",
    }:
        type_node = ast_field(node, "type")
        name = simple_type(ast_text(type_node)) if type_node is not None else "new"
        return CallRef(
            owner,
            node_span(source, node),
            name,
            None,
            CallKind.CONSTRUCT,
            argument_count(node),
        )
    parts = _member_call_parts(ast_field(node, "function"))
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
    nodes = [
        node
        for node in walk_owned(root, _OWNERSHIP_BOUNDARIES)
        if node.type in _CALL_KINDS
    ]
    nodes.sort(key=lambda node: (node.start_byte, node.end_byte))
    return ordered_unique(
        call for node in nodes if (call := _call(source, owner, node)) is not None
    )


def _base_types(node: object) -> tuple[str, ...]:
    base_list = direct_child(node, {"base_list"})
    if base_list is None:
        return ()
    return ordered_unique(
        simple_type(tight_type(ast_text(child)))
        for child in named_children(base_list)
        if ast_text(child)
    )


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
        if span is None:
            return
        events = body_events(self.source, node)
        self.bodies.append(BodyIR(symbol.id, span, events))
        self.calls.extend(_calls(self.source, symbol.id, body_node(node)))
        self.references.extend(
            body_references(
                symbol.id,
                events,
                primitives=_PRIMITIVES,
                ignored_names={"base", "this"},
            )
        )

    def field(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        default_visibility: Visibility,
    ) -> tuple[Binding, ...]:
        declaration = direct_child(node, {"variable_declaration"})
        type_node = ast_field(declaration, "type")
        type_name = tight_type(ast_text(type_node))
        modifiers = _modifier_values(node)
        kind = SymbolKind.CONSTANT if "const" in modifiers else SymbolKind.FIELD
        bindings: list[Binding] = []
        for declarator in _declarators(declaration):
            name_node = ast_field(declarator, "name")
            if name_node is None:
                continue
            name = ast_text(name_node)
            symbol = Symbol(
                symbol_id(self.source, container_path, kind, name),
                node_span(self.source, declarator),
                _visibility(node, default=default_visibility),
                name,
                returns=type_name or None,
                annotations=_annotations(node),
                modifiers=modifiers,
            )
            self.symbols.append(symbol)
            self.references.extend(
                _type_references(self.source, symbol.id, (type_node,))
            )
            self.add_annotations(symbol.id, node)
            value = _initializer(declarator)
            self.calls.extend(_calls(self.source, symbol.id, value))
            if type_name:
                bindings.append(Binding(name, simple_type(type_name)))
        return binding_tuple(bindings)

    def property(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        default_visibility: Visibility,
    ) -> Binding | None:
        name_node = ast_field(node, "name")
        type_node = ast_field(node, "type")
        if name_node is None:
            return None
        name = ast_text(name_node)
        type_name = tight_type(ast_text(type_node))
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.PROPERTY, name),
            node_span(self.source, node),
            _visibility(node, default=default_visibility),
            name,
            returns=type_name or None,
            annotations=_annotations(node),
            modifiers=_modifier_values(node),
            body_lines=body_lines(body_node(node)),
        )
        self.symbols.append(symbol)
        self.references.extend(_type_references(self.source, symbol.id, (type_node,)))
        self.add_annotations(symbol.id, node)
        self.body_facts(symbol, node)
        return Binding(name, simple_type(type_name)) if type_name else None

    def callable(
        self,
        node: Any,
        container_path: tuple[str, ...],
        type_name: str | None,
        class_bindings: tuple[Binding, ...],
        *,
        default_visibility: Visibility,
    ) -> None:
        constructor = node.type == "constructor_declaration"
        name_node = ast_field(node, "name")
        name = ast_text(name_node) if name_node is not None else ""
        if constructor and not name:
            name = type_name or "constructor"
        if not name:
            return
        parameters = _parameters(ast_field(node, "parameters"))
        params = tuple(parameter.type_name for parameter in parameters)
        return_node = None if constructor else ast_field(node, "returns")
        returns = (
            type_name if constructor else tight_type(ast_text(return_node)) or None
        )
        modifiers = _modifier_values(node)
        entrypoint = name == "Main" and "static" in modifiers
        kind = (
            SymbolKind.CONSTRUCTOR
            if constructor
            else SymbolKind.FUNCTION
            if node.type == "local_function_statement" or type_name is None
            else SymbolKind.METHOD
        )
        suffix = (
            f":{returns}" if returns and returns != "void" and not constructor else ""
        )
        body = body_node(node)
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
                    *_parameter_bindings(parameters),
                    *_local_bindings(body),
                )
            ),
            annotations=_annotations(node, entrypoint=entrypoint),
            modifiers=modifiers,
            # v1 reported constructor bodies as unknown while canonical BodyIR
            # below still retains the complete constructor body and events.
            body_lines=0 if constructor else body_lines(body),
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (*(parameter.type_node for parameter in parameters), return_node),
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
            elif nested.type == "local_function_statement":
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
            if node.type in _TYPE_KINDS or node.type == "local_function_statement":
                found.append(node)
                continue
            if node.type in _CALLABLE_KINDS or node.type in {
                "anonymous_method_expression",
                "lambda_expression",
            }:
                continue
            stack.extend(reversed(children(node)))
        return tuple(found)

    def type_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        default_visibility: Visibility = Visibility.INTERNAL,
    ) -> None:
        name_node = ast_field(node, "name")
        if name_node is None:
            return
        name = ast_text(name_node)
        kind = _TYPE_KINDS[node.type]
        body = ast_field(node, "body")
        parameters_node = direct_child(node, {"parameter_list"})
        parameters = _parameters(parameters_node) if kind is SymbolKind.RECORD else ()
        enum_members = (
            tuple(
                member
                for member in walk_all(body)
                if member.type == "enum_member_declaration"
            )
            if kind is SymbolKind.ENUM
            else ()
        )
        components = (
            tuple(parameter.name for parameter in parameters)
            if kind is SymbolKind.RECORD
            else tuple(ast_text(ast_field(member, "name")) for member in enum_members)
            if kind is SymbolKind.ENUM
            else ()
        )
        params = (
            tuple(parameter.type_name for parameter in parameters)
            if kind is SymbolKind.RECORD
            else components
            if kind is SymbolKind.ENUM
            else ()
        )
        modifiers = _modifier_values(node)
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node, default=default_visibility),
            f"{kind.value} {name}",
            params=params,
            supers=_base_types(node),
            components=components,
            annotations=_annotations(node),
            modifiers=modifiers,
        )
        self.symbols.append(symbol)
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (
                    *(parameter.type_node for parameter in parameters),
                    direct_child(node, {"base_list"}),
                ),
            )
        )
        self.add_annotations(symbol.id, node)
        owned_path = (*container_path, name)

        class_bindings: list[Binding] = list(_parameter_bindings(parameters))
        for parameter in parameters:
            component = Symbol(
                symbol_id(
                    self.source,
                    owned_path,
                    SymbolKind.PROPERTY,
                    parameter.name,
                ),
                node_span(self.source, parameter.node),
                Visibility.PUBLIC,
                parameter.name,
                returns=parameter.type_name,
            )
            self.symbols.append(component)
            self.references.extend(
                _type_references(self.source, component.id, (parameter.type_node,))
            )

        for member in enum_members:
            member_name_node = ast_field(member, "name")
            if member_name_node is None:
                continue
            member_name = ast_text(member_name_node)
            self.symbols.append(
                Symbol(
                    symbol_id(
                        self.source,
                        owned_path,
                        SymbolKind.CONSTANT,
                        member_name,
                    ),
                    node_span(self.source, member),
                    Visibility.PUBLIC,
                    member_name,
                )
            )

        if body is None:
            return
        members = named_children(body)
        for member in members:
            if member.type == "field_declaration":
                class_bindings.extend(
                    self.field(
                        member,
                        owned_path,
                        default_visibility=Visibility.PRIVATE,
                    )
                )
            elif member.type == "property_declaration":
                binding = self.property(
                    member,
                    owned_path,
                    default_visibility=(
                        Visibility.PUBLIC
                        if kind is SymbolKind.INTERFACE
                        else Visibility.PRIVATE
                    ),
                )
                if binding is not None:
                    class_bindings.append(binding)

        frozen_class_bindings = binding_tuple(class_bindings)
        for member in members:
            if member.type in _TYPE_KINDS:
                self.type_declaration(
                    member,
                    owned_path,
                    default_visibility=Visibility.PRIVATE,
                )
            elif member.type in _CALLABLE_KINDS:
                self.callable(
                    member,
                    owned_path,
                    name,
                    frozen_class_bindings,
                    default_visibility=(
                        Visibility.PUBLIC
                        if kind is SymbolKind.INTERFACE
                        else Visibility.PRIVATE
                    ),
                )


def _namespace_name(node: object | None) -> str | None:
    name = ast_field(node, "name")
    return ast_text(name) or None


def _imports(source: SourceFile, root: Any) -> tuple[ImportRef, ...]:
    values: list[ImportRef] = []
    for node in walk_all(root):
        if node.type != "using_directive":
            continue
        alias_node = ast_field(node, "name")
        targets = [
            child
            for child in named_children(node)
            if child is not alias_node and child.type != "modifier"
        ]
        if not targets:
            continue
        target = targets[-1]
        raw = ast_text(target)
        alias = ast_text(alias_node) or None
        static = any(ast_text(child) == "static" for child in children(node))
        if alias is not None or static:
            module, separator, imported_name = raw.rpartition(".")
            name: str | None = imported_name
            if not separator:
                module, name = raw, None
            values.append(
                ImportRef(
                    node_span(source, node),
                    module,
                    name,
                    alias,
                    False,
                )
            )
        else:
            values.append(ImportRef(node_span(source, node), raw, None, None, True))
    return tuple(values)


def extract(source: SourceFile, parser: object | None) -> FileIR:
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("C# extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    namespace = next(
        (
            node
            for node in walk_all(root)
            if node.type
            in {"file_scoped_namespace_declaration", "namespace_declaration"}
        ),
        None,
    )
    module = _namespace_name(namespace) or file_module(source.file)
    module_span = node_span(source, namespace if namespace is not None else root)
    module_symbol = Symbol(
        symbol_id(source, (), SymbolKind.MODULE, module),
        module_span,
        Visibility.PUBLIC,
        f"module {module}",
    )
    extractor = _Extractor(source, root)
    extractor.symbols.append(module_symbol)

    if namespace is not None and namespace.type == "namespace_declaration":
        declaration_root = ast_field(namespace, "body")
    else:
        declaration_root = root
    for declaration in named_children(declaration_root):
        if declaration.type in _TYPE_KINDS:
            extractor.type_declaration(declaration, ())
        elif declaration.type in _CALLABLE_KINDS:
            extractor.callable(
                declaration,
                (),
                None,
                (),
                default_visibility=Visibility.INTERNAL,
            )

    return assemble_file_ir(
        source,
        module=module,
        symbols=_prefer_implemented_callables(extractor.symbols),
        calls=extractor.calls,
        imports=_imports(source, root),
        references=extractor.references,
        bodies=extractor.bodies,
        diagnostics=syntax_diagnostics(source, root, "C#"),
    )


__all__ = ["extract"]
