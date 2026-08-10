from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
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

from .common import base_type, ordered_unique, reference, symbol_id, tight_type
from .treesitter import (
    ast_field,
    ast_text,
    body_events,
    body_lines,
    node_span,
)

_TYPE_KINDS = {
    "annotation_type_declaration": SymbolKind.INTERFACE,
    "class_declaration": SymbolKind.CLASS,
    "enum_declaration": SymbolKind.ENUM,
    "interface_declaration": SymbolKind.INTERFACE,
    "record_declaration": SymbolKind.RECORD,
}
_CALLABLE_KINDS = frozenset(
    {
        "annotation_type_element_declaration",
        "compact_constructor_declaration",
        "constructor_declaration",
        "method_declaration",
    }
)
_OWNERSHIP_BOUNDARIES = frozenset((*_TYPE_KINDS, *_CALLABLE_KINDS, "lambda_expression"))
_CALL_KINDS = frozenset(
    {
        "array_creation_expression",
        "explicit_constructor_invocation",
        "method_invocation",
        "object_creation_expression",
    }
)
_ANNOTATION_KINDS = frozenset({"annotation", "marker_annotation"})
_COMMENT_KINDS = frozenset({"block_comment", "line_comment"})
_FIELD_KINDS = frozenset({"constant_declaration", "field_declaration"})
_PRIMITIVE_TYPES = frozenset(
    {
        "boolean",
        "byte",
        "char",
        "double",
        "float",
        "int",
        "long",
        "short",
        "var",
        "void",
    }
)
_IMPORT_RE = re.compile(
    r"^import\s+(?P<static>static\s+)?"
    r"(?P<target>(?:[^\W\d]|\$)[\w$]*(?:\.(?:[^\W\d]|\$)[\w$]*)*)"
    r"(?P<wildcard>\.\*)?\s*;$"
)
_IDENTIFIER_RE = re.compile(r"^(?:[^\W\d]|\$)[\w$]*$", re.UNICODE)


@dataclass(frozen=True, slots=True)
class _Parameter:
    type_name: str
    name: str
    node: Any
    type_node: Any
    name_node: Any


def _children(node: object | None) -> tuple[Any, ...]:
    return tuple(getattr(node, "children", ())) if node is not None else ()


def _named_children(node: object | None) -> tuple[Any, ...]:
    return tuple(
        child for child in _children(node) if bool(getattr(child, "is_named", False))
    )


def _field_nodes(node: object | None, name: str) -> tuple[Any, ...]:
    if node is None:
        return ()
    many = getattr(node, "children_by_field_name", None)
    values = tuple(many(name)) if callable(many) else ()
    if values:
        return values
    value = ast_field(node, name)
    return (value,) if value is not None else ()


def _direct_child(node: object | None, kind: str) -> Any | None:
    return next((child for child in _named_children(node) if child.type == kind), None)


def _walk_owned(root: object | None) -> Iterable[Any]:
    if root is None:
        return
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(_children(node)):
            if child is root or child.type not in _OWNERSHIP_BOUNDARIES:
                stack.append(child)


def _module_name(root: Any) -> tuple[str | None, Any | None]:
    package = next(
        (
            child
            for child in _named_children(root)
            if child.type == "package_declaration"
        ),
        None,
    )
    if package is None or bool(getattr(package, "has_error", False)):
        return None, package
    raw = ast_text(package).removeprefix("package").removesuffix(";").strip()
    return raw or None, package


def _imports(source: SourceFile, root: Any) -> tuple[ImportRef, ...]:
    imports: list[ImportRef] = []
    for node in _named_children(root):
        if node.type != "import_declaration" or bool(getattr(node, "has_error", False)):
            continue
        match = _IMPORT_RE.fullmatch(ast_text(node).strip())
        if match is None:
            continue
        target = match.group("target")
        wildcard = match.group("wildcard") is not None
        if wildcard:
            module, name = target, None
        else:
            module, _, name = target.rpartition(".")
            if not module or not name:
                continue
        imports.append(ImportRef(node_span(source, node), module, name, None, wildcard))
    return tuple(imports)


def _modifier_node(node: Any) -> Any | None:
    return _direct_child(node, "modifiers")


def _annotations(node: Any) -> tuple[str, ...]:
    modifiers = _modifier_node(node)
    return tuple(
        ast_text(child).removeprefix("@")
        for child in _named_children(modifiers)
        if child.type in _ANNOTATION_KINDS
    )


def _modifiers(node: Any) -> tuple[str, ...]:
    modifiers = _modifier_node(node)
    return tuple(
        text
        for child in _children(modifiers)
        if child.type not in _ANNOTATION_KINDS | _COMMENT_KINDS
        if (text := ast_text(child).strip())
    )


def _visibility(
    node: Any,
    *,
    default: Visibility = Visibility.INTERNAL,
) -> Visibility:
    modifiers = _modifiers(node)
    if "public" in modifiers:
        return Visibility.PUBLIC
    if "protected" in modifiers:
        return Visibility.PROTECTED
    if "private" in modifiers:
        return Visibility.PRIVATE
    return default


def _simple_type_name(type_name: str) -> str:
    normalized = base_type(tight_type(type_name.removesuffix("..."))).strip()
    normalized = re.sub(r"^(?:\?\s+(?:extends|super)\s+)", "", normalized)
    return normalized.rsplit(".", 1)[-1]


def _parameters(node: Any | None) -> tuple[_Parameter, ...]:
    if node is None:
        return ()
    result: list[_Parameter] = []
    for parameter in _named_children(node):
        if parameter.type not in {
            "formal_parameter",
            "receiver_parameter",
            "spread_parameter",
        }:
            continue
        if parameter.type == "receiver_parameter":
            continue
        type_node = ast_field(parameter, "type")
        name_node = ast_field(parameter, "name")
        if parameter.type == "spread_parameter":
            declarator = _direct_child(parameter, "variable_declarator")
            name_node = ast_field(declarator, "name")
            type_node = next(
                (
                    child
                    for child in _named_children(parameter)
                    if child.type
                    not in {"modifiers", "variable_declarator"} | _ANNOTATION_KINDS
                ),
                None,
            )
        if type_node is None or name_node is None:
            continue
        type_name = tight_type(ast_text(type_node))
        dimensions = ast_field(parameter, "dimensions")
        if dimensions is not None and not type_name.endswith(ast_text(dimensions)):
            type_name += ast_text(dimensions)
        if parameter.type == "spread_parameter":
            type_name += "..."
        result.append(
            _Parameter(
                type_name,
                ast_text(name_node),
                parameter,
                type_node,
                name_node,
            )
        )
    return tuple(result)


def _declarators(node: Any) -> tuple[Any, ...]:
    values = _field_nodes(node, "declarator")
    if values:
        return values
    return tuple(
        child for child in _named_children(node) if child.type == "variable_declarator"
    )


def _binding_tuple(values: Iterable[Binding]) -> tuple[Binding, ...]:
    by_name: dict[str, str] = {}
    for binding in values:
        by_name[binding.name] = binding.type_name
    return tuple(Binding(name, type_name) for name, type_name in by_name.items())


def _parameter_bindings(parameters: Iterable[_Parameter]) -> tuple[Binding, ...]:
    return tuple(
        Binding(parameter.name, _simple_type_name(parameter.type_name))
        for parameter in parameters
    )


def _inferred_type(value: Any | None) -> str | None:
    if value is None:
        return None
    if value.type == "object_creation_expression":
        type_node = ast_field(value, "type")
        return _simple_type_name(ast_text(type_node)) if type_node is not None else None
    if value.type == "array_creation_expression":
        type_node = ast_field(value, "type")
        return (
            f"{_simple_type_name(ast_text(type_node))}[]"
            if type_node is not None
            else None
        )
    if value.type != "method_invocation":
        return None
    receiver = ast_field(value, "object")
    receiver_name = ast_text(receiver).rsplit(".", 1)[-1] if receiver else ""
    if receiver_name[:1].isupper():
        return _simple_type_name(receiver_name)
    return None


def _local_bindings(body: Any | None) -> tuple[Binding, ...]:
    bindings: list[Binding] = []
    for node in _walk_owned(body):
        if node.type == "local_variable_declaration":
            type_node = ast_field(node, "type")
            declared_type = tight_type(ast_text(type_node)) if type_node else ""
            for declarator in _declarators(node):
                name_node = ast_field(declarator, "name")
                if name_node is None:
                    continue
                type_name = declared_type
                if declared_type == "var":
                    type_name = _inferred_type(ast_field(declarator, "value")) or "var"
                bindings.append(
                    Binding(ast_text(name_node), _simple_type_name(type_name))
                )
        elif node.type in {
            "catch_formal_parameter",
            "enhanced_for_statement",
            "resource",
        }:
            type_node = ast_field(node, "type") or _direct_child(node, "catch_type")
            name_node = ast_field(node, "name")
            if type_node is not None and name_node is not None:
                bindings.append(
                    Binding(ast_text(name_node), _simple_type_name(ast_text(type_node)))
                )
    return _binding_tuple(bindings)


def _wrapper_types(node: Any | None) -> tuple[str, ...]:
    if node is None:
        return ()
    type_list = _direct_child(node, "type_list")
    candidates = _named_children(type_list or node)
    return ordered_unique(
        name
        for candidate in candidates
        if (name := _simple_type_name(ast_text(candidate)))
        and name not in {"extends", "implements", "permits", "throws"}
    )


def _heritage(node: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    supers: list[str] = []
    permits: list[str] = []
    for child in _named_children(node):
        if child.type in {"extends_interfaces", "super_interfaces", "superclass"}:
            supers.extend(_wrapper_types(child))
        elif child.type == "permits":
            permits.extend(_wrapper_types(child))
    return ordered_unique(supers), ordered_unique(permits)


def _throws(node: Any) -> tuple[str, ...]:
    throws = _direct_child(node, "throws")
    return _wrapper_types(throws)


def _members(body: Any | None) -> Iterable[Any]:
    for child in _named_children(body):
        if child.type == "enum_body_declarations":
            yield from _named_children(child)
        else:
            yield child


def _class_bindings(
    body: Any | None,
    record_parameters: tuple[_Parameter, ...],
) -> tuple[Binding, ...]:
    bindings: list[Binding] = list(_parameter_bindings(record_parameters))
    for member in _members(body):
        if member.type not in _FIELD_KINDS:
            continue
        type_node = ast_field(member, "type")
        if type_node is None:
            continue
        type_name = _simple_type_name(ast_text(type_node))
        for declarator in _declarators(member):
            name_node = ast_field(declarator, "name")
            if name_node is not None:
                bindings.append(Binding(ast_text(name_node), type_name))
    return _binding_tuple(bindings)


def _argument_count(node: Any) -> int | None:
    arguments = ast_field(node, "arguments")
    if arguments is not None:
        return sum(
            child.type not in {"block_comment", "line_comment", "ERROR"}
            for child in _named_children(arguments)
        )
    if node.type == "array_creation_expression":
        dimensions = [
            child for child in _named_children(node) if child.type == "dimensions_expr"
        ]
        return len(dimensions)
    return 0


def _call(node: Any, owner: SymbolId, source: SourceFile) -> CallRef | None:
    if node.type == "method_invocation":
        name_node = ast_field(node, "name")
        if name_node is None:
            return None
        receiver_node = ast_field(node, "object")
        return CallRef(
            owner,
            node_span(source, node),
            ast_text(name_node),
            ast_text(receiver_node) if receiver_node is not None else None,
            CallKind.CALL,
            _argument_count(node),
        )
    if node.type in {"object_creation_expression", "array_creation_expression"}:
        type_node = ast_field(node, "type")
        if type_node is None:
            return None
        name = _simple_type_name(ast_text(type_node))
    elif node.type == "explicit_constructor_invocation":
        raw = ast_text(node).lstrip()
        name = "this" if raw.startswith("this") else "super"
    else:
        return None
    return CallRef(
        owner,
        node_span(source, node),
        name,
        None,
        CallKind.CONSTRUCT,
        _argument_count(node),
    )


def _calls(
    source: SourceFile, owner: SymbolId, region: Any | None
) -> tuple[CallRef, ...]:
    return ordered_unique(
        call
        for node in _walk_owned(region)
        if node.type in _CALL_KINDS
        if (call := _call(node, owner, source)) is not None
    )


def _body_references(
    owner: SymbolId,
    events: Iterable[BodyEvent],
    *,
    annotation_spans: Iterable[Any] = (),
    ignored_type_spans: Iterable[Any] = (),
) -> tuple[ReferenceRef, ...]:
    references: list[ReferenceRef] = []
    weak_spans = tuple(annotation_spans)
    ignored_types = frozenset(ignored_type_spans)
    for event in events:
        if event.kind is BodyEventKind.NAME:
            kind = ReferenceKind.NAME
            context = ReferenceContext.CODE
        elif event.kind is BodyEventKind.TYPE:
            kind = ReferenceKind.TYPE
            context = ReferenceContext.TYPE
        else:
            continue
        if any(_inside(event.span, span) for span in weak_spans):
            continue
        if event.kind is BodyEventKind.TYPE and event.span in ignored_types:
            continue
        if not _IDENTIFIER_RE.fullmatch(event.text) or event.text in _PRIMITIVE_TYPES:
            continue
        references.append(
            reference(
                owner,
                event.span,
                event.text,
                None,
                kind,
                context=context,
                confidence=ReferenceConfidence.DEFINITE,
            )
        )
    return ordered_unique(references)


def _type_reference_nodes(node: Any | None) -> Iterable[Any]:
    if node is None:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "scoped_type_identifier":
            leaves = tuple(_raw_type_identifier_nodes(current))
            for leaf in leaves:
                if leaf is leaves[-1] or ast_text(leaf)[:1].isupper():
                    yield leaf
            continue
        if current.type == "type_identifier":
            yield current
            continue
        stack.extend(reversed(_named_children(current)))


def _raw_type_identifier_nodes(node: Any | None) -> Iterable[Any]:
    if node is None:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "type_identifier":
            yield current
        else:
            stack.extend(reversed(_named_children(current)))


def _ignored_qualified_type_spans(source: SourceFile, root: Any) -> tuple[Any, ...]:
    ignored: list[Any] = []
    for node in _walk_all(root):
        if node.type != "scoped_type_identifier":
            continue
        leaves = tuple(_raw_type_identifier_nodes(node))
        ignored.extend(
            node_span(source, leaf)
            for leaf in leaves[:-1]
            if not ast_text(leaf)[:1].isupper()
        )
    return ordered_unique(ignored)


def _type_references(
    source: SourceFile,
    owner: SymbolId,
    nodes: Iterable[Any | None],
) -> tuple[ReferenceRef, ...]:
    return ordered_unique(
        reference(
            owner,
            node_span(source, type_node),
            ast_text(type_node),
            None,
            ReferenceKind.TYPE,
            context=ReferenceContext.TYPE,
            confidence=ReferenceConfidence.DEFINITE,
        )
        for node in nodes
        for type_node in _type_reference_nodes(node)
    )


def _annotation_references(
    source: SourceFile,
    owner: SymbolId,
    declaration: Any,
) -> tuple[ReferenceRef, ...]:
    references: list[ReferenceRef] = []
    modifiers = _modifier_node(declaration)
    for annotation in _named_children(modifiers):
        if annotation.type not in _ANNOTATION_KINDS:
            continue
        name_node = ast_field(annotation, "name")
        if name_node is None:
            continue
        qualified = ast_text(name_node)
        qualifier, _, name = qualified.rpartition(".")
        reference_name_node = ast_field(name_node, "name") or name_node
        references.append(
            reference(
                owner,
                node_span(source, reference_name_node),
                name or qualified,
                qualifier or None,
                ReferenceKind.TYPE,
                context=ReferenceContext.ANNOTATION,
                confidence=ReferenceConfidence.POSSIBLE,
            )
        )
        if (name or qualified) != "EventListener":
            continue
        for literal in _walk_owned(ast_field(annotation, "arguments")):
            if literal.type != "string_literal":
                continue
            raw = ast_text(literal)
            if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
                continue
            callback = raw[1:-1]
            if not _IDENTIFIER_RE.fullmatch(callback):
                continue
            references.append(
                reference(
                    owner,
                    node_span(source, literal),
                    callback,
                    name or qualified,
                    ReferenceKind.NAME,
                    context=ReferenceContext.ANNOTATION,
                    confidence=ReferenceConfidence.POSSIBLE,
                )
            )
    return ordered_unique(references)


def _walk_all(root: Any) -> Iterable[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(_children(node)))


def _nested_types(root: Any | None) -> Iterable[Any]:
    if root is None:
        return
    stack = list(reversed(_children(root)))
    while stack:
        node = stack.pop()
        if node.type in _TYPE_KINDS:
            yield node
            continue
        if node.type in _CALLABLE_KINDS:
            continue
        stack.extend(reversed(_children(node)))


def _inside(inner: Any, outer: Any) -> bool:
    return (outer.start_line, outer.start_column) <= (
        inner.start_line,
        inner.start_column,
    ) and (inner.end_line, inner.end_column) <= (outer.end_line, outer.end_column)


def _syntax_diagnostics(source: SourceFile, root: Any) -> tuple[Diagnostic, ...]:
    if not bool(getattr(root, "has_error", False)):
        return ()
    erroneous = [
        node
        for node in _walk_all(root)
        if bool(getattr(node, "is_error", False))
        or bool(getattr(node, "is_missing", False))
    ]
    target = min(
        erroneous,
        key=lambda node: (node.start_byte, node.end_byte),
        default=root,
    )
    return (
        Diagnostic(
            "tree-sitter-syntax-error",
            DiagnosticSeverity.ERROR,
            f"{source.file}: Java syntax tree contains an error",
            node_span(source, target),
        ),
    )


class _Extractor:
    def __init__(self, source: SourceFile, root: Any) -> None:
        self.source = source
        self.root = root
        self.symbols: list[Symbol] = []
        self.calls: list[CallRef] = []
        self.references: list[ReferenceRef] = []
        self.bodies: list[BodyIR] = []

    def extract_types(self) -> None:
        for declaration in _named_children(self.root):
            if declaration.type in _TYPE_KINDS:
                self.type_declaration(declaration, ())

    def type_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
    ) -> None:
        kind = _TYPE_KINDS[node.type]
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        body = ast_field(node, "body")
        record_parameters = (
            _parameters(ast_field(node, "parameters"))
            if kind is SymbolKind.RECORD
            else ()
        )
        record_parameter_events: tuple[BodyEvent, ...] = ()
        record_parameter_node = ast_field(node, "parameters")
        if record_parameter_node is not None:
            parameter_span = node_span(self.source, record_parameter_node)
            record_parameter_events = tuple(
                event
                for event in body_events(self.source, node)
                if _inside(event.span, parameter_span)
            )
        enum_constants = tuple(
            member for member in _members(body) if member.type == "enum_constant"
        )
        supers, permits = _heritage(node)
        modifiers = _modifiers(node)
        components = (
            tuple(parameter.name for parameter in record_parameters)
            if kind is SymbolKind.RECORD
            else tuple(
                ast_text(ast_field(constant, "name")) for constant in enum_constants
            )
        )
        params = (
            tuple(parameter.type_name for parameter in record_parameters)
            if kind is SymbolKind.RECORD
            else components
            if kind is SymbolKind.ENUM
            else ()
        )
        signature_kind = (
            "interface" if node.type == "annotation_type_declaration" else kind.value
        )
        prefix = "sealed " if "sealed" in modifiers else ""
        type_symbol = Symbol(
            symbol_id(self.source, container_path, kind, name),
            node_span(self.source, node),
            _visibility(node),
            f"{prefix}{signature_kind} {name}",
            params=params,
            supers=supers,
            permits=permits,
            components=components,
            annotations=_annotations(node),
            modifiers=modifiers,
        )
        self.symbols.append(type_symbol)
        self.references.extend(
            _annotation_references(self.source, type_symbol.id, node)
        )
        self.references.extend(
            _type_references(
                self.source,
                type_symbol.id,
                (
                    *(
                        child
                        for child in _named_children(node)
                        if child.type
                        in {
                            "extends_interfaces",
                            "super_interfaces",
                            "superclass",
                            "permits",
                        }
                    ),
                ),
            )
        )

        owned_path = (*container_path, name)
        for parameter in record_parameters:
            component = Symbol(
                symbol_id(
                    self.source,
                    owned_path,
                    SymbolKind.FIELD,
                    parameter.name,
                ),
                node_span(self.source, parameter.node),
                Visibility.PRIVATE,
                parameter.name,
            )
            self.symbols.append(component)
            self.references.extend(
                _type_references(
                    self.source,
                    component.id,
                    (parameter.type_node,),
                )
            )
            self.references.extend(
                _annotation_references(self.source, component.id, parameter.node)
            )

        class_bindings = _class_bindings(body, record_parameters)
        for member in _members(body):
            if member.type == "enum_constant":
                self.enum_constant(member, owned_path)
            elif member.type in _FIELD_KINDS:
                self.field_declaration(
                    member,
                    owned_path,
                    implicit_public=kind is SymbolKind.INTERFACE,
                )
            elif member.type in _CALLABLE_KINDS:
                self.callable_declaration(
                    member,
                    owned_path,
                    name,
                    class_bindings,
                    record_parameters,
                    record_parameter_events,
                    implicit_public=kind is SymbolKind.INTERFACE,
                )
            elif member.type in _TYPE_KINDS:
                self.type_declaration(member, owned_path)

    def enum_constant(
        self,
        node: Any,
        container_path: tuple[str, ...],
    ) -> None:
        name = ast_text(ast_field(node, "name"))
        if not name:
            return
        symbol = Symbol(
            symbol_id(self.source, container_path, SymbolKind.CONSTANT, name),
            node_span(self.source, node),
            Visibility.PUBLIC,
            name,
            annotations=_annotations(node),
        )
        self.symbols.append(symbol)
        self.calls.extend(_calls(self.source, symbol.id, node))
        self.references.extend(_annotation_references(self.source, symbol.id, node))

    def field_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
        *,
        implicit_public: bool,
    ) -> None:
        type_node = ast_field(node, "type")
        modifiers = _modifiers(node)
        constant = node.type == "constant_declaration" or (
            "static" in modifiers and "final" in modifiers
        )
        kind = SymbolKind.CONSTANT if constant else SymbolKind.FIELD
        for declarator in _declarators(node):
            name = ast_text(ast_field(declarator, "name"))
            if not name:
                continue
            symbol = Symbol(
                symbol_id(self.source, container_path, kind, name),
                node_span(self.source, declarator),
                _visibility(
                    node,
                    default=(
                        Visibility.PUBLIC if implicit_public else Visibility.INTERNAL
                    ),
                ),
                name,
                annotations=_annotations(node),
                modifiers=modifiers,
            )
            self.symbols.append(symbol)
            self.references.extend(
                _type_references(self.source, symbol.id, (type_node,))
            )
            self.references.extend(_annotation_references(self.source, symbol.id, node))
            value = ast_field(declarator, "value")
            self.calls.extend(_calls(self.source, symbol.id, value))

    def callable_declaration(
        self,
        node: Any,
        container_path: tuple[str, ...],
        type_name: str,
        class_bindings: tuple[Binding, ...],
        record_parameters: tuple[_Parameter, ...],
        record_parameter_events: tuple[BodyEvent, ...],
        *,
        implicit_public: bool,
    ) -> None:
        constructor = node.type in {
            "compact_constructor_declaration",
            "constructor_declaration",
        }
        name = ast_text(ast_field(node, "name")) or type_name
        if node.type == "compact_constructor_declaration":
            parameters = record_parameters
        else:
            parameters = _parameters(ast_field(node, "parameters"))
        params = tuple(parameter.type_name for parameter in parameters)
        return_node = None if constructor else ast_field(node, "type")
        returns = type_name if constructor else tight_type(ast_text(return_node))
        raises = _throws(node)
        body = ast_field(node, "body")
        bindings = _binding_tuple(
            (
                *class_bindings,
                *_parameter_bindings(parameters),
                *_local_bindings(body),
            )
        )
        kind = SymbolKind.CONSTRUCTOR if constructor else SymbolKind.METHOD
        suffix = (
            f":{returns}" if returns and returns != "void" and not constructor else ""
        )
        symbol = Symbol(
            symbol_id(self.source, container_path, kind, name, params),
            node_span(self.source, node),
            _visibility(
                node,
                default=(Visibility.PUBLIC if implicit_public else Visibility.INTERNAL),
            ),
            f"{name}({','.join(params)}){suffix}",
            params=params,
            returns=returns,
            raises=raises,
            bindings=bindings,
            annotations=_annotations(node),
            modifiers=_modifiers(node),
            body_lines=body_lines(body),
        )
        self.symbols.append(symbol)
        self.references.extend(_annotation_references(self.source, symbol.id, node))
        parameter_annotation_spans = tuple(
            node_span(self.source, annotation)
            for parameter in parameters
            for annotation in _named_children(_modifier_node(parameter.node))
            if annotation.type in _ANNOTATION_KINDS
        )
        for parameter in parameters:
            self.references.extend(
                _annotation_references(self.source, symbol.id, parameter.node)
            )
        self.references.extend(
            _type_references(
                self.source,
                symbol.id,
                (
                    *(parameter.type_node for parameter in parameters),
                    return_node,
                    _direct_child(node, "throws"),
                ),
            )
        )
        if body is None:
            return
        callable_events = body_events(self.source, node)
        events = callable_events
        if node.type == "compact_constructor_declaration":
            events = (*record_parameter_events, *callable_events)
        self.bodies.append(BodyIR(symbol.id, node_span(self.source, body), events))
        self.calls.extend(_calls(self.source, symbol.id, body))
        self.references.extend(
            _body_references(
                symbol.id,
                callable_events,
                annotation_spans=parameter_annotation_spans,
                ignored_type_spans=_ignored_qualified_type_spans(self.source, node),
            )
        )
        for declaration in _nested_types(body):
            self.type_declaration(declaration, (*container_path, name))


def extract(source: SourceFile, parser: object | None) -> FileIR:
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("Java extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    module, package = _module_name(root)
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
    extractor.extract_types()
    return FileIR(
        source,
        module=module,
        symbols=tuple(extractor.symbols),
        calls=tuple(
            sorted(
                ordered_unique(extractor.calls),
                key=lambda call: (
                    call.span.start_line,
                    call.span.start_column,
                ),
            )
        ),
        imports=_imports(source, root),
        references=tuple(
            sorted(
                ordered_unique(extractor.references),
                key=lambda item: (
                    item.span.start_line,
                    item.span.start_column,
                ),
            )
        ),
        bodies=tuple(extractor.bodies),
        diagnostics=_syntax_diagnostics(source, root),
    )


__all__ = ["extract"]
