from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from hologram.model import (
    Binding,
    BodyEvent,
    BodyEventKind,
    BodyIR,
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
)

from .common import ordered_unique, reference
from .treesitter import ast_field, node_span

_IDENTIFIER_RE = re.compile(r"^(?:[^\W\d]|\$)[\w$]*$", re.UNICODE)
_BODY_KINDS = frozenset(
    {
        "arrow_expression_clause",
        "block",
        "compound_statement",
        "constructor_body",
        "function_body",
        "statement_block",
    }
)


def children(node: object | None) -> tuple[Any, ...]:
    return tuple(getattr(node, "children", ())) if node is not None else ()


def named_children(node: object | None) -> tuple[Any, ...]:
    return tuple(
        child for child in children(node) if bool(getattr(child, "is_named", False))
    )


def field_nodes(node: object | None, name: str) -> tuple[Any, ...]:
    if node is None:
        return ()
    many = getattr(node, "children_by_field_name", None)
    values = tuple(many(name)) if callable(many) else ()
    if values:
        return values
    value = ast_field(node, name)
    return (value,) if value is not None else ()


def direct_child(node: object | None, kinds: Iterable[str]) -> Any | None:
    selected = frozenset(kinds)
    return next(
        (child for child in named_children(node) if child.type in selected),
        None,
    )


def same_node(left: object | None, right: object | None) -> bool:
    if left is None or right is None:
        return False
    return (
        getattr(left, "start_byte", None) == getattr(right, "start_byte", None)
        and getattr(left, "end_byte", None) == getattr(right, "end_byte", None)
        and getattr(left, "type", None) == getattr(right, "type", None)
    )


def _next_preorder(
    cursor: Any,
    boundary_kinds: frozenset[str],
) -> Any | None:
    if cursor.goto_first_child():
        node = cursor.node
        if node.type not in boundary_kinds:
            return node
    while True:
        if cursor.goto_next_sibling():
            node = cursor.node
            if node.type not in boundary_kinds:
                return node
        elif not cursor.goto_parent():
            return None


def _walk_cursor(
    root: object,
    boundary_kinds: frozenset[str],
    *,
    include_root: bool,
) -> Iterable[Any]:
    walk = getattr(root, "walk", None)
    if not callable(walk):
        raise TypeError("Tree-sitter traversal requires Node.walk()")
    cursor = walk()
    del root
    if include_root:
        yield cursor.node
    while True:
        node = _next_preorder(cursor, boundary_kinds)
        if node is None:
            return
        yield node
        node = None


def walk_all(root: object | None) -> Iterable[Any]:
    if root is None:
        return
    yield from _walk_cursor(root, frozenset(), include_root=True)


def walk_owned(
    root: object | None,
    boundaries: Iterable[str],
    *,
    include_root: bool = True,
) -> Iterable[Any]:
    if root is None:
        return
    boundary_kinds = frozenset(boundaries)
    yield from _walk_cursor(
        root,
        boundary_kinds,
        include_root=include_root,
    )


def binding_tuple(bindings: Iterable[Binding]) -> tuple[Binding, ...]:
    values: dict[str, str] = {}
    for binding in bindings:
        values[binding.name] = binding.type_name
    return tuple(Binding(name, type_name) for name, type_name in values.items())


def simple_type(type_name: str) -> str:
    normalized = type_name.strip().lstrip("*&").removeprefix("mut ").strip()
    normalized = re.sub(r"[<\[(].*", "", normalized).strip()
    return normalized.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def file_module(file: str) -> str:
    return PurePosixPath(file).with_suffix("").as_posix()


def body_node(node: object) -> Any | None:
    body = ast_field(node, "body")
    if body is not None:
        return body
    return direct_child(node, _BODY_KINDS)


def body_span(source: SourceFile, node: object) -> SourceSpan | None:
    body = body_node(node)
    return node_span(source, body) if body is not None else None


def argument_count(node: object) -> int | None:
    arguments = ast_field(node, "arguments")
    if arguments is None:
        arguments = direct_child(
            node, {"argument_list", "arguments", "value_arguments"}
        )
    if arguments is None:
        return 0
    return sum(
        child.type not in {"block_comment", "comment", "line_comment", "ERROR"}
        for child in named_children(arguments)
    )


def body_references(
    owner: SymbolId,
    events: Iterable[BodyEvent],
    *,
    primitives: Iterable[str] = (),
    ignored_names: Iterable[str] = (),
) -> tuple[ReferenceRef, ...]:
    primitive_names = frozenset(primitives)
    ignored = frozenset(ignored_names)
    result: list[ReferenceRef] = []
    for event in events:
        if event.kind is BodyEventKind.NAME:
            kind = ReferenceKind.NAME
            context = ReferenceContext.CODE
        elif event.kind is BodyEventKind.TYPE:
            kind = ReferenceKind.TYPE
            context = ReferenceContext.TYPE
        else:
            continue
        if (
            event.text in primitive_names
            or event.text in ignored
            or not _IDENTIFIER_RE.fullmatch(event.text)
        ):
            continue
        result.append(
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
    return ordered_unique(result)


def syntax_diagnostics(
    source: SourceFile,
    root: object,
    language_name: str,
) -> tuple[Diagnostic, ...]:
    if not bool(getattr(root, "has_error", False)):
        return ()
    erroneous = [
        node
        for node in walk_all(root)
        if bool(getattr(node, "is_error", False))
        or bool(getattr(node, "is_missing", False))
    ]
    target = min(
        erroneous,
        key=lambda node: (
            int(getattr(node, "start_byte", 0)),
            int(getattr(node, "end_byte", 0)),
        ),
        default=root,
    )
    return (
        Diagnostic(
            "tree-sitter-syntax-error",
            DiagnosticSeverity.ERROR,
            f"{source.file}: {language_name} syntax tree contains an error",
            node_span(source, target),
        ),
    )


def assemble_file_ir(
    source: SourceFile,
    *,
    module: str | None,
    symbols: Iterable[Symbol],
    calls: Iterable[CallRef] = (),
    imports: Iterable[ImportRef] = (),
    references: Iterable[ReferenceRef] = (),
    bodies: Iterable[BodyIR] = (),
    diagnostics: Iterable[Diagnostic] = (),
) -> FileIR:
    call_values = ordered_unique(calls)
    reference_values = ordered_unique(references)
    return FileIR(
        source,
        module=module,
        symbols=tuple(symbols),
        calls=tuple(
            sorted(
                call_values,
                key=lambda item: (item.span.start_line, item.span.start_column),
            )
        ),
        imports=ordered_unique(imports),
        references=tuple(
            sorted(
                reference_values,
                key=lambda item: (item.span.start_line, item.span.start_column),
            )
        ),
        bodies=tuple(bodies),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "argument_count",
    "assemble_file_ir",
    "binding_tuple",
    "body_node",
    "body_references",
    "body_span",
    "children",
    "direct_child",
    "field_nodes",
    "file_module",
    "named_children",
    "same_node",
    "simple_type",
    "syntax_diagnostics",
    "walk_all",
    "walk_owned",
]
