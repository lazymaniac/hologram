from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

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
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)

from .common import (
    ast_body_events,
    ast_span,
    base_type,
    ordered_unique,
    reference,
    span_from_character_columns,
    symbol_id,
    tight_type,
)

_ENUM_BASES = frozenset({"Enum", "Flag", "IntEnum", "IntFlag", "StrEnum"})
_PROPERTY_DECORATORS = frozenset({"cached_property", "property"})
_REGISTRATION_CALLS = frozenset(
    {
        "add_listener",
        "register",
        "register_callback",
        "register_handler",
    }
)
_CONFIGURATION_CALLS = frozenset({"config", "configure", "set_callback"})
_CALLBACK_KEYWORDS = frozenset({"callback", "handler", "listener", "target"})
_REFLECTION_CALLS = frozenset({"getattr", "setattr"})
_TYPING_ROLE_CONSTRUCTORS = frozenset({"Annotated", "Literal"})
_WEAK_REFERENCE_CONTEXTS = frozenset(
    {
        ReferenceContext.ANNOTATION,
        ReferenceContext.CONFIG,
        ReferenceContext.REFLECTION,
        ReferenceContext.STRING,
    }
)


def _module_name(file: str) -> str | None:
    path = PurePosixPath(file)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _module_symbol_name(source: SourceFile, module: str | None) -> str:
    return module or PurePosixPath(source.file).stem


def _visibility(name: str) -> Visibility:
    return Visibility.PRIVATE if name.startswith("_") else Visibility.PUBLIC


def _source_text(source: SourceFile, node: ast.AST) -> str:
    segment = ast.get_source_segment(source.text, node)
    return segment if segment is not None else ast.unparse(node)


def _annotation_text(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return tight_type(node.value)
    return tight_type(ast.unparse(node))


def _decorator_name(node: ast.AST) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _decorators(source: SourceFile, node: ast.AST) -> tuple[str, ...]:
    return tuple(
        _source_text(source, decorator)
        for decorator in getattr(node, "decorator_list", ())
    )


def _parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[tuple[str, ...], tuple[Binding, ...]]:
    arguments = node.args
    declared = [*arguments.posonlyargs, *arguments.args]
    if arguments.vararg is not None:
        declared.append(arguments.vararg)
    declared.extend(arguments.kwonlyargs)
    if arguments.kwarg is not None:
        declared.append(arguments.kwarg)

    parameter_types: list[str] = []
    bindings: list[Binding] = []
    for parameter in declared:
        if parameter.arg in {"cls", "self"}:
            continue
        type_name = _annotation_text(parameter.annotation) or "?"
        parameter_types.append(type_name)
        if parameter.annotation is not None:
            bindings.append(Binding(parameter.arg, base_type(type_name)))
    return tuple(parameter_types), tuple(bindings)


def _base_name(node: ast.AST) -> str:
    raw = base_type(ast.unparse(node))
    return raw.rsplit(".", 1)[-1]


def _enum_members(node: ast.ClassDef) -> tuple[tuple[str, ast.AST], ...]:
    members: list[tuple[str, ast.AST]] = []
    for statement in node.body:
        if (
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            members.append((statement.targets[0].id, statement))
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            members.append((statement.target.id, statement))
    return tuple(members)


def _class_fields(node: ast.ClassDef) -> tuple[tuple[str, str, ast.AST], ...]:
    fields: list[tuple[str, str, ast.AST]] = []
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            fields.append(
                (
                    statement.target.id,
                    tight_type(ast.unparse(statement.annotation)),
                    statement,
                )
            )
    return tuple(fields)


def _constant_declarations(
    statements: Iterable[ast.stmt],
) -> tuple[tuple[str, ast.AST], ...]:
    constants: list[tuple[str, ast.AST]] = []
    for statement in statements:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id.isupper()
        ):
            constants.append((statement.targets[0].id, statement))
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id.isupper()
        ):
            constants.append((statement.target.id, statement))
    return tuple(constants)


def _symbol(
    source: SourceFile,
    container_path: tuple[str, ...],
    kind: SymbolKind,
    name: str,
    node: ast.AST,
    *,
    params: tuple[str, ...] = (),
    signature: str,
    returns: str | None = None,
    supers: tuple[str, ...] = (),
    raises: tuple[str, ...] = (),
    bindings: tuple[Binding, ...] = (),
    components: tuple[str, ...] = (),
    annotations: tuple[str, ...] = (),
    modifiers: tuple[str, ...] = (),
    body_lines: int = 0,
) -> Symbol:
    return Symbol(
        symbol_id(source, container_path, kind, name, params),
        ast_span(source, node),
        _visibility(name),
        signature,
        params,
        returns,
        supers,
        (),
        raises,
        bindings,
        components,
        annotations,
        modifiers,
        body_lines,
    )


class _OwnedBindingRaiseVisitor(ast.NodeVisitor):
    def __init__(self, initial: tuple[Binding, ...]) -> None:
        self.bindings = list(initial)
        self.raises: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            constructor = _call_parts(node.value.func)
            if constructor is not None and constructor[1][:1].isupper():
                self.bindings.append(Binding(node.targets[0].id, constructor[1]))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.bindings.append(
                Binding(node.target.id, base_type(ast.unparse(node.annotation)))
            )
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(target, ast.Name):
                self.raises.append(target.id)
            elif isinstance(target, ast.Attribute):
                self.raises.append(target.attr)
        self.generic_visit(node)


class _DeclarationVisitor(ast.NodeVisitor):
    def __init__(self, source: SourceFile) -> None:
        self.source = source
        self.symbols: list[Symbol] = []
        self.callables: list[tuple[Symbol, ast.FunctionDef | ast.AsyncFunctionDef]] = []
        self.classes: list[tuple[Symbol, ast.ClassDef]] = []
        self._containers: list[str] = []
        self._scope_kinds: list[str] = []

    @property
    def container_path(self) -> tuple[str, ...]:
        return tuple(self._containers)

    def visit_Module(self, node: ast.Module) -> None:
        for name, declaration in _constant_declarations(node.body):
            self.symbols.append(
                _symbol(
                    self.source,
                    (),
                    SymbolKind.CONSTANT,
                    name,
                    declaration,
                    signature=name,
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        base_names = tuple(_base_name(base) for base in node.bases)
        is_enum = bool(set(base_names) & _ENUM_BASES)
        enum_members = _enum_members(node) if is_enum else ()
        fields = _class_fields(node) if not is_enum else ()
        params = (
            tuple(name for name, _ in enum_members)
            if is_enum
            else tuple(type_name for _, type_name, _ in fields)
        )
        components = (
            tuple(name for name, _ in enum_members)
            if is_enum
            else tuple(name for name, _, _ in fields)
        )
        kind = SymbolKind.ENUM if is_enum else SymbolKind.CLASS
        class_symbol = _symbol(
            self.source,
            self.container_path,
            kind,
            node.name,
            node,
            params=params,
            signature=f"class {node.name}",
            supers=() if is_enum else base_names,
            components=components,
            annotations=_decorators(self.source, node),
        )
        self.symbols.append(class_symbol)
        self.classes.append((class_symbol, node))

        self._containers.append(node.name)
        self._scope_kinds.append("class")
        declarations = (
            enum_members
            if is_enum
            else tuple((name, declaration) for name, _, declaration in fields)
        )
        declared_names = {name for name, _ in declarations}
        for name, declaration in declarations:
            field_kind = SymbolKind.CONSTANT if is_enum else SymbolKind.FIELD
            self.symbols.append(
                _symbol(
                    self.source,
                    self.container_path,
                    field_kind,
                    name,
                    declaration,
                    signature=name,
                )
            )
        for name, declaration in _constant_declarations(node.body):
            if name in declared_names:
                continue
            self.symbols.append(
                _symbol(
                    self.source,
                    self.container_path,
                    SymbolKind.CONSTANT,
                    name,
                    declaration,
                    signature=name,
                )
            )
        for statement in node.body:
            self.visit(statement)
        self._scope_kinds.pop()
        self._containers.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        params, parameter_bindings = _parameters(node)
        returns = _annotation_text(node.returns)
        ret_suffix = f":{returns}" if returns and returns != "None" else ""
        decorator_names = {
            name
            for decorator in node.decorator_list
            if (name := _decorator_name(decorator)) is not None
        }
        if decorator_names & _PROPERTY_DECORATORS:
            kind = SymbolKind.PROPERTY
        elif self._scope_kinds and self._scope_kinds[-1] == "class":
            kind = SymbolKind.METHOD
        else:
            kind = SymbolKind.FUNCTION
        owned = _OwnedBindingRaiseVisitor(parameter_bindings)
        for statement in node.body:
            owned.visit(statement)
        modifiers = ("async",) if isinstance(node, ast.AsyncFunctionDef) else ()
        function_symbol = _symbol(
            self.source,
            self.container_path,
            kind,
            node.name,
            node,
            params=params,
            signature=f"{node.name}({','.join(params)}){ret_suffix}",
            returns=returns,
            raises=ordered_unique(owned.raises),
            bindings=ordered_unique(owned.bindings),
            annotations=_decorators(self.source, node),
            modifiers=modifiers,
            body_lines=(node.end_lineno or node.lineno) - node.lineno + 1,
        )
        self.symbols.append(function_symbol)
        self.callables.append((function_symbol, node))

        self._containers.append(node.name)
        self._scope_kinds.append("function")
        for statement in node.body:
            self.visit(statement)
        self._scope_kinds.pop()
        self._containers.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _call_parts(function: ast.AST) -> tuple[str | None, str] | None:
    if isinstance(function, ast.Name):
        return None, function.id
    if isinstance(function, ast.Attribute):
        receiver = function.value.id if isinstance(function.value, ast.Name) else None
        return receiver, function.attr
    return None


def _arity(node: ast.Call) -> int | None:
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        return None
    if any(keyword.arg is None for keyword in node.keywords):
        return None
    return len(node.args) + len(node.keywords)


def _suffix_span(source: SourceFile, node: ast.Attribute) -> SourceSpan:
    span = ast_span(source, node)
    width = len(node.attr.encode("utf-8"))
    return SourceSpan(
        source.file,
        span.end_line,
        span.end_column - width,
        span.end_line,
        span.end_column,
    )


def _type_constructor_name(
    node: ast.AST,
    aliases: Mapping[str, str],
) -> str | None:
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    else:
        return None
    return aliases.get(name, name)


def _typing_constructor_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        if statement.level or statement.module != "typing":
            continue
        for imported in statement.names:
            if imported.name not in _TYPING_ROLE_CONSTRUCTORS:
                continue
            aliases[imported.asname or imported.name] = imported.name
    return aliases


class _OwnedFactVisitor(ast.NodeVisitor):
    def __init__(
        self,
        source: SourceFile,
        owner: SymbolId,
        events: tuple[BodyEvent, ...] = (),
        *,
        context: ReferenceContext = ReferenceContext.CODE,
        type_constructor_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.source = source
        self.owner = owner
        self.calls: list[CallRef] = []
        self.references: list[ReferenceRef] = []
        self.context = context
        self._type_constructor_aliases = dict(type_constructor_aliases or ())
        self._event_spans_by_end = {
            (
                event.kind,
                event.text,
                event.span.end_line,
                event.span.end_column,
            ): event.span
            for event in events
        }

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.generic_visit(node)

    def _visit_in_context(
        self,
        node: ast.AST,
        context: ReferenceContext,
    ) -> None:
        previous = self.context
        self.context = context
        self.visit(node)
        self.context = previous

    def _confidence(self) -> ReferenceConfidence:
        return (
            ReferenceConfidence.POSSIBLE
            if self.context in _WEAK_REFERENCE_CONTEXTS
            else ReferenceConfidence.DEFINITE
        )

    def visit_Call(self, node: ast.Call) -> None:
        parts = _call_parts(node.func)
        if parts is not None:
            receiver, name = parts
            self.calls.append(
                CallRef(
                    self.owner,
                    ast_span(self.source, node),
                    name,
                    receiver,
                    CallKind.CONSTRUCT if name[:1].isupper() else CallKind.CALL,
                    _arity(node),
                )
            )
            self._recognized_string_references(node, name)
        self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def _recognized_string_references(self, node: ast.Call, name: str) -> None:
        candidates: list[tuple[ast.Constant, ReferenceContext]] = []
        if name in _REGISTRATION_CALLS:
            context = (
                ReferenceContext.ANNOTATION
                if self.context is ReferenceContext.ANNOTATION
                else ReferenceContext.REFLECTION
            )
            candidates.extend(
                (argument, context)
                for argument in node.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            )
        if name in _CONFIGURATION_CALLS:
            candidates.extend(
                (keyword.value, ReferenceContext.CONFIG)
                for keyword in node.keywords
                if keyword.arg in _CALLBACK_KEYWORDS
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            )
        if name in _REFLECTION_CALLS and len(node.args) >= 2:
            candidate = node.args[1]
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                candidates.append((candidate, ReferenceContext.REFLECTION))
        for candidate, context in candidates:
            callback_name = candidate.value
            if not isinstance(callback_name, str):
                continue
            self.references.append(
                reference(
                    self.owner,
                    ast_span(self.source, candidate),
                    callback_name,
                    None,
                    ReferenceKind.NAME,
                    context=context,
                    confidence=ReferenceConfidence.POSSIBLE,
                )
            )

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        kind = (
            ReferenceKind.TYPE
            if self.context is ReferenceContext.TYPE
            else ReferenceKind.NAME
        )
        self.references.append(
            reference(
                self.owner,
                ast_span(self.source, node),
                node.id,
                None,
                kind,
                context=self.context,
                confidence=self._confidence(),
            )
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.visit(node.value)
        if not isinstance(node.ctx, ast.Load):
            return
        kind = (
            ReferenceKind.TYPE
            if self.context is ReferenceContext.TYPE
            else ReferenceKind.NAME
        )
        span = self._event_span(kind, node.attr, node) or _suffix_span(
            self.source, node
        )
        qualifier = node.value.id if isinstance(node.value, ast.Name) else None
        self.references.append(
            reference(
                self.owner,
                span,
                node.attr,
                qualifier,
                kind,
                context=self.context,
                confidence=self._confidence(),
            )
        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self.context is not ReferenceContext.TYPE:
            self.generic_visit(node)
            return
        self.visit(node.value)
        arguments = (
            node.slice.elts if isinstance(node.slice, ast.Tuple) else (node.slice,)
        )
        constructor = _type_constructor_name(
            node.value,
            self._type_constructor_aliases,
        )
        if constructor == "Literal":
            for argument in arguments:
                self._visit_in_context(argument, ReferenceContext.ANNOTATION)
            return
        if constructor == "Annotated" and arguments:
            self.visit(arguments[0])
            for metadata in arguments[1:]:
                self._visit_in_context(metadata, ReferenceContext.ANNOTATION)
            return
        for argument in arguments:
            self.visit(argument)

    def visit_Constant(self, node: ast.Constant) -> None:
        if self.context is not ReferenceContext.TYPE or not isinstance(node.value, str):
            return
        try:
            expression = ast.parse(node.value, mode="eval")
        except SyntaxError:
            return
        names = ordered_unique(
            candidate.attr if isinstance(candidate, ast.Attribute) else candidate.id
            for candidate in ast.walk(expression)
            if isinstance(candidate, ast.Attribute)
            or (isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Load))
        )
        for name in names:
            self.references.append(
                reference(
                    self.owner,
                    ast_span(self.source, node),
                    name,
                    None,
                    ReferenceKind.TYPE,
                    context=ReferenceContext.ANNOTATION,
                    confidence=ReferenceConfidence.POSSIBLE,
                )
            )

    def _event_span(
        self,
        kind: ReferenceKind,
        text: str,
        node: ast.AST,
    ) -> SourceSpan | None:
        event_kind = (
            BodyEventKind.TYPE if kind is ReferenceKind.TYPE else BodyEventKind.NAME
        )
        container = ast_span(self.source, node)
        return self._event_spans_by_end.get(
            (
                event_kind,
                text,
                container.end_line,
                container.end_column,
            )
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        self._visit_in_context(node.annotation, ReferenceContext.TYPE)
        if node.value is not None:
            self.visit(node.value)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self._visit_in_context(node.type, ReferenceContext.TYPE)
        for statement in node.body:
            self.visit(statement)


def _facts_for_callable(
    source: SourceFile,
    symbol: Symbol,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    events: tuple[BodyEvent, ...],
    type_constructor_aliases: Mapping[str, str],
) -> tuple[tuple[CallRef, ...], tuple[ReferenceRef, ...]]:
    visitor = _OwnedFactVisitor(
        source,
        symbol.id,
        events,
        type_constructor_aliases=type_constructor_aliases,
    )
    for decorator in node.decorator_list:
        visitor._visit_in_context(decorator, ReferenceContext.ANNOTATION)
    arguments = node.args
    for parameter in [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
    ]:
        if parameter.annotation is not None:
            visitor._visit_in_context(parameter.annotation, ReferenceContext.TYPE)
    if arguments.vararg is not None and arguments.vararg.annotation is not None:
        visitor._visit_in_context(arguments.vararg.annotation, ReferenceContext.TYPE)
    if arguments.kwarg is not None and arguments.kwarg.annotation is not None:
        visitor._visit_in_context(arguments.kwarg.annotation, ReferenceContext.TYPE)
    defaults: list[ast.expr] = [*arguments.defaults]
    defaults.extend(default for default in arguments.kw_defaults if default is not None)
    for default in defaults:
        visitor.visit(default)
    if node.returns is not None:
        visitor._visit_in_context(node.returns, ReferenceContext.TYPE)
    for statement in node.body:
        visitor.visit(statement)
    return tuple(visitor.calls), tuple(visitor.references)


def _facts_for_module(
    source: SourceFile,
    symbol: Symbol,
    tree: ast.Module,
    events: tuple[BodyEvent, ...],
    type_constructor_aliases: Mapping[str, str],
) -> tuple[tuple[CallRef, ...], tuple[ReferenceRef, ...]]:
    visitor = _OwnedFactVisitor(
        source,
        symbol.id,
        events,
        type_constructor_aliases=type_constructor_aliases,
    )
    for statement in tree.body:
        visitor.visit(statement)
    return tuple(visitor.calls), tuple(visitor.references)


def _facts_for_class(
    source: SourceFile,
    symbol: Symbol,
    node: ast.ClassDef,
    type_constructor_aliases: Mapping[str, str],
) -> tuple[tuple[CallRef, ...], tuple[ReferenceRef, ...]]:
    visitor = _OwnedFactVisitor(
        source,
        symbol.id,
        type_constructor_aliases=type_constructor_aliases,
    )
    for decorator in node.decorator_list:
        visitor._visit_in_context(decorator, ReferenceContext.ANNOTATION)
    for base in node.bases:
        visitor._visit_in_context(base, ReferenceContext.TYPE)
    for keyword in node.keywords:
        visitor._visit_in_context(keyword.value, ReferenceContext.TYPE)
    for statement in node.body:
        visitor.visit(statement)
    return tuple(visitor.calls), tuple(visitor.references)


def _imports(source: SourceFile, tree: ast.Module) -> tuple[ImportRef, ...]:
    imports: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ImportRef(
                        ast_span(source, alias),
                        alias.name,
                        None,
                        alias.asname,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                imports.append(
                    ImportRef(
                        ast_span(source, alias),
                        module,
                        None if alias.name == "*" else alias.name,
                        alias.asname,
                        alias.name == "*",
                    )
                )
    imports.sort(key=lambda item: item.span)
    return tuple(imports)


def _syntax_diagnostic(source: SourceFile, error: SyntaxError) -> Diagnostic:
    span: SourceSpan | None = None
    if error.lineno is not None and error.offset is not None:
        end_line = error.end_lineno or error.lineno
        end_offset = error.end_offset or error.offset
        try:
            span = span_from_character_columns(
                source,
                error.lineno,
                max(error.offset - 1, 0),
                end_line,
                max(end_offset - 1, error.offset),
            )
        except ValueError:
            span = None
    return Diagnostic(
        "python-syntax-error",
        DiagnosticSeverity.ERROR,
        f"{source.file}: {error.msg}",
        span,
    )


def _scope_span(source: SourceFile, body: list[ast.stmt]) -> SourceSpan:
    if not body:
        return SourceSpan(source.file, 1, 0, 1, 0)
    first = ast_span(source, body[0])
    last = ast_span(source, body[-1])
    return SourceSpan(
        source.file,
        first.start_line,
        first.start_column,
        last.end_line,
        last.end_column,
    )


def _module_symbol(
    source: SourceFile,
    module: str | None,
    tree: ast.Module,
) -> Symbol:
    name = _module_symbol_name(source, module)
    span = _scope_span(source, tree.body)
    return Symbol(
        symbol_id(source, (), SymbolKind.MODULE, name),
        span,
        Visibility.PUBLIC,
        f"module {name}",
        body_lines=span.end_line - span.start_line + 1 if tree.body else 0,
    )


def _join_reference_events(
    events: tuple[BodyEvent, ...],
    references: tuple[ReferenceRef, ...],
) -> tuple[BodyEvent, ...]:
    required = {
        (reference.span, reference.name)
        for reference in references
        if reference.kind is ReferenceKind.NAME
        and reference.confidence is ReferenceConfidence.POSSIBLE
    }
    existing = {
        (event.span, event.text) for event in events if event.kind is BodyEventKind.NAME
    }
    joined: list[BodyEvent] = []
    for event in events:
        joined.append(event)
        key = next(
            (
                candidate
                for candidate in required
                if candidate[0] == event.span and candidate not in existing
            ),
            None,
        )
        if event.kind is not BodyEventKind.LITERAL or key is None:
            continue
        joined.append(BodyEvent(BodyEventKind.NAME, key[1], event.span))
        existing.add(key)
    return tuple(joined)


def extract(source: SourceFile, parser: object | None) -> FileIR:
    del parser
    module = _module_name(source.file)
    try:
        tree = ast.parse(source.text, filename=source.file)
    except SyntaxError as error:
        return FileIR(
            source, module=module, diagnostics=(_syntax_diagnostic(source, error),)
        )

    declarations = _DeclarationVisitor(source)
    declarations.visit(tree)
    type_constructor_aliases = _typing_constructor_aliases(tree)
    module_symbol = _module_symbol(source, module, tree)
    symbols = (
        module_symbol,
        *sorted(declarations.symbols, key=lambda item: item.span),
    )
    bodies: list[BodyIR] = []
    calls: list[CallRef] = []
    references: list[ReferenceRef] = []

    module_events = ast_body_events(
        source,
        tree,
        type_constructor_aliases=type_constructor_aliases,
    )
    module_calls, module_references = _facts_for_module(
        source,
        module_symbol,
        tree,
        module_events,
        type_constructor_aliases,
    )
    module_events = _join_reference_events(module_events, module_references)
    bodies.append(BodyIR(module_symbol.id, module_symbol.span, module_events))
    calls.extend(module_calls)
    references.extend(module_references)

    for symbol, node in declarations.callables:
        events = ast_body_events(
            source,
            node,
            type_constructor_aliases=type_constructor_aliases,
        )
        owned_calls, owned_references = _facts_for_callable(
            source,
            symbol,
            node,
            events,
            type_constructor_aliases,
        )
        events = _join_reference_events(events, owned_references)
        bodies.append(BodyIR(symbol.id, _scope_span(source, node.body), events))
        calls.extend(owned_calls)
        references.extend(owned_references)
    for symbol, class_node in declarations.classes:
        owned_calls, owned_references = _facts_for_class(
            source,
            symbol,
            class_node,
            type_constructor_aliases,
        )
        calls.extend(owned_calls)
        references.extend(owned_references)

    calls = list(ordered_unique(sorted(calls, key=lambda item: item.span)))
    references = list(ordered_unique(sorted(references, key=lambda item: item.span)))
    bodies.sort(key=lambda item: item.span)
    return FileIR(
        source,
        module=module,
        symbols=symbols,
        calls=tuple(calls),
        imports=_imports(source, tree),
        references=tuple(references),
        bodies=tuple(bodies),
    )


__all__ = ["extract"]
