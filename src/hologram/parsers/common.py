from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from typing import TypeVar

from hologram.model import (
    BodyEvent,
    BodyEventKind,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceSpan,
    SymbolId,
    SymbolKind,
)

T = TypeVar("T")


def ordered_unique(values: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(values))


def signature_key(params: Iterable[str]) -> str:
    return f"({','.join(params)})"


def symbol_id(
    source: SourceFile,
    container_path: tuple[str, ...],
    kind: SymbolKind,
    name: str,
    params: Iterable[str] = (),
) -> SymbolId:
    key = (
        signature_key(params)
        if kind
        in {
            SymbolKind.FUNCTION,
            SymbolKind.METHOD,
            SymbolKind.CONSTRUCTOR,
        }
        else ""
    )
    return SymbolId(source.language, source.file, container_path, kind, name, key)


def reference(
    owner: SymbolId | None,
    span: SourceSpan,
    name: str,
    qualifier: str | None,
    kind: ReferenceKind,
    *,
    context: ReferenceContext,
    confidence: ReferenceConfidence,
) -> ReferenceRef:
    """Build a reference only after its evidence strength is made explicit."""
    return ReferenceRef(
        owner,
        span,
        name,
        qualifier,
        kind,
        context,
        confidence,
    )


def _split_top_commas(
    raw: str,
    opens: str = "<([",
    closes: str = ">)]",
) -> list[str]:
    """Split commas outside bracket nesting, preserving component whitespace."""
    parts: list[str] = []
    depth = 0
    current = ""
    for character in raw:
        if character in opens:
            depth += 1
        elif character in closes:
            depth -= 1
        if character == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += character
    if current.strip():
        parts.append(current)
    return parts


def split_top_commas(
    raw: str,
    opens: str = "<([",
    closes: str = ">)]",
) -> tuple[str, ...]:
    return tuple(_split_top_commas(raw, opens, closes))


def tight_type(type_name: str) -> str:
    """Collapse whitespace following commas in a type expression."""
    return re.sub(r",\s+", ",", type_name)


def base_type(type_name: str) -> str:
    """Return a declared type without generic, tuple, or array suffixes."""
    return re.sub(r"[<\[(].*", "", type_name).strip()


_base_type = base_type


def _heritage(segment: str) -> tuple[list[str], list[str]]:
    def names(keyword: str) -> list[str]:
        match = re.search(
            rf"\b{keyword}\s+([\w.<>, \t\n]+?)"
            rf"(?=\bextends\b|\bimplements\b|\bpermits\b|$)",
            segment,
        )
        if not match:
            return []
        return [
            re.sub(r"<.*", "", name.strip()).split(".")[-1]
            for name in match.group(1).split(",")
            if name.strip()
        ]

    supers = names("extends") + names("implements")
    return supers, names("permits")


def heritage(segment: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    supers, permits = _heritage(segment)
    return tuple(supers), tuple(permits)


def utf8_byte_column(line: str, character_column: int) -> int:
    """Convert a Unicode code-point column into a UTF-8 byte column."""
    if character_column < 0 or character_column > len(line):
        raise ValueError("character column is outside the source line")
    return len(line[:character_column].encode("utf-8"))


def span_from_character_columns(
    source: SourceFile,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> SourceSpan:
    lines = source.text.splitlines()
    if start_line < 1 or end_line < 1:
        raise ValueError("source lines must be positive")
    try:
        start_text = lines[start_line - 1]
        end_text = lines[end_line - 1]
    except IndexError as error:
        raise ValueError("source line is outside the snapshot") from error
    return SourceSpan(
        source.file,
        start_line,
        utf8_byte_column(start_text, start_column),
        end_line,
        utf8_byte_column(end_text, end_column),
    )


def ast_span(source: SourceFile, node: ast.AST) -> SourceSpan:
    """Convert stdlib AST coordinates, which are already UTF-8 bytes."""
    start_line = getattr(node, "lineno", None)
    start_column = getattr(node, "col_offset", None)
    if start_line is None or start_column is None:
        raise ValueError(f"{type(node).__name__} has no source position")
    end_line = getattr(node, "end_lineno", None) or start_line
    end_column = getattr(node, "end_col_offset", None)
    if end_column is None:
        end_column = start_column
    return SourceSpan(
        source.file,
        start_line,
        start_column,
        end_line,
        end_column,
    )


def body_lines(body: object | None) -> int:
    if body is None:
        return 0
    if hasattr(body, "start_point") and hasattr(body, "end_point"):
        start = body.start_point
        end = body.end_point
        start_row = start.row if hasattr(start, "row") else start[0]
        end_row = end.row if hasattr(end, "row") else end[0]
        return end_row - start_row + 1
    start_line = getattr(body, "lineno", None)
    end_line = getattr(body, "end_lineno", None)
    if start_line is None:
        return 0
    return (end_line or start_line) - start_line + 1


def validate_body_events(events: Iterable[BodyEvent]) -> None:
    """Assert that structured control events are balanced and properly nested."""
    stack: list[str] = []
    for event in events:
        if event.kind is BodyEventKind.CONTROL_ENTER:
            stack.append(event.text)
        elif event.kind is BodyEventKind.CONTROL_EXIT:
            if not stack:
                raise AssertionError("body control stack underflow")
            expected = stack.pop()
            if event.text != expected:
                raise AssertionError(
                    f"body control mismatch: expected {expected!r}, "
                    f"found {event.text!r}"
                )
    if stack:
        raise AssertionError(f"unclosed body controls: {stack!r}")


_PYTHON_OPERATOR_TEXT = {
    ast.Add: "+",
    ast.And: "and",
    ast.BitAnd: "&",
    ast.BitOr: "|",
    ast.BitXor: "^",
    ast.Div: "/",
    ast.Eq: "==",
    ast.FloorDiv: "//",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.Invert: "~",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.LShift: "<<",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.MatMult: "@",
    ast.Mod: "%",
    ast.Mult: "*",
    ast.Not: "not",
    ast.NotEq: "!=",
    ast.NotIn: "not in",
    ast.Or: "or",
    ast.Pow: "**",
    ast.RShift: ">>",
    ast.Sub: "-",
    ast.UAdd: "+",
    ast.USub: "-",
}


def _operator_text(operator: ast.AST) -> str:
    return _PYTHON_OPERATOR_TEXT.get(type(operator), type(operator).__name__.lower())


def _constant_text(value: object) -> str:
    if value is None:
        return "<null>"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float, complex)):
        return "<number>"
    if isinstance(value, (str, bytes)):
        return "<string>"
    return f"<{type(value).__name__.lower()}>"


class _AstBodyEventWalker:
    def __init__(self, source: SourceFile, callable_node: ast.AST) -> None:
        self.source = source
        self.callable_node = callable_node
        self.events: list[BodyEvent] = []

    def event(
        self,
        kind: BodyEventKind,
        text: str,
        node: ast.AST,
        *,
        span: SourceSpan | None = None,
    ) -> None:
        self.events.append(BodyEvent(kind, text, span or ast_span(self.source, node)))

    def control(
        self,
        text: str,
        node: ast.AST,
        visit: Callable[[], None],
    ) -> None:
        span = ast_span(self.source, node)
        self.event(BodyEventKind.CONTROL_ENTER, text, node, span=span)
        visit()
        self.event(BodyEventKind.CONTROL_EXIT, text, node, span=span)

    def walk(self) -> tuple[BodyEvent, ...]:
        arguments = getattr(self.callable_node, "args", None)
        if isinstance(arguments, ast.arguments):
            positional = [*arguments.posonlyargs, *arguments.args]
            positional_defaults: list[ast.expr | None] = [None] * (
                len(positional) - len(arguments.defaults)
            )
            positional_defaults.extend(arguments.defaults)
            for parameter, default in zip(
                positional,
                positional_defaults,
                strict=True,
            ):
                self.visit_parameter(parameter, default)
            if arguments.vararg is not None:
                self.visit_parameter(arguments.vararg, None)
            for parameter, default in zip(
                arguments.kwonlyargs,
                arguments.kw_defaults,
                strict=True,
            ):
                self.visit_parameter(parameter, default)
            if arguments.kwarg is not None:
                self.visit_parameter(arguments.kwarg, None)
        returns = getattr(self.callable_node, "returns", None)
        if returns is not None:
            self.visit_annotation(returns)
        body = getattr(self.callable_node, "body", ())
        if isinstance(body, ast.AST):
            self.visit(body)
        else:
            for statement in body:
                self.visit(statement)
        result = tuple(self.events)
        validate_body_events(result)
        return result

    def visit_parameter(
        self,
        parameter: ast.arg,
        default: ast.expr | None,
    ) -> None:
        span = ast_span(self.source, parameter)
        name_span = SourceSpan(
            self.source.file,
            span.start_line,
            span.start_column,
            span.start_line,
            span.start_column + len(parameter.arg.encode("utf-8")),
        )
        self.event(
            BodyEventKind.PARAM,
            parameter.arg,
            parameter,
            span=name_span,
        )
        if parameter.annotation is not None:
            self.visit_annotation(parameter.annotation)
        if default is not None:
            self.visit(default)

    def visit_annotation(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            self.event(BodyEventKind.TYPE, node.id, node)
            return
        if isinstance(node, ast.Attribute):
            self.visit_annotation(node.value)
            self.event(
                BodyEventKind.TYPE,
                node.attr,
                node,
                span=self.attribute_span(node),
            )
            return
        if isinstance(node, ast.Constant):
            self.event(BodyEventKind.TYPE, str(node.value), node)
            return
        for child in ast.iter_child_nodes(node):
            self.visit_annotation(child)

    def attribute_span(self, node: ast.Attribute) -> SourceSpan:
        span = ast_span(self.source, node)
        width = len(node.attr.encode("utf-8"))
        if span.end_column < width:
            return span
        return SourceSpan(
            self.source.file,
            span.end_line,
            span.end_column - width,
            span.end_line,
            span.end_column,
        )

    def visit(self, node: ast.AST) -> None:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            return
        if isinstance(node, ast.If):
            self.control("if", node, lambda: self._visit_if(node))
            return
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            self.control("loop", node, lambda: self._visit_loop(node))
            return
        if isinstance(node, (ast.Try, ast.TryStar)):
            self.control("try", node, lambda: self._visit_try(node))
            return
        if isinstance(node, ast.ExceptHandler):
            self.control("catch", node, lambda: self._visit_except(node))
            return
        if isinstance(node, ast.Match):
            self.control("match", node, lambda: self._visit_match(node))
            return
        if isinstance(node, ast.IfExp):
            self.control("if", node, lambda: self._visit_children(node))
            return
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            self.control("loop", node, lambda: self._visit_children(node))
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            self.control("with", node, lambda: self._visit_children(node))
            return
        if isinstance(node, ast.Call):
            name = self.call_name(node.func)
            kind = BodyEventKind.CONSTRUCT if name[:1].isupper() else BodyEventKind.CALL
            self.event(kind, name, node)
            self.visit(node.func)
            for argument in node.args:
                self.visit(argument)
            for argument_keyword in node.keywords:
                self.visit(argument_keyword.value)
            return
        if isinstance(node, ast.Name):
            kind = (
                BodyEventKind.LOCAL
                if isinstance(node.ctx, (ast.Store, ast.Del))
                else BodyEventKind.NAME
            )
            self.event(kind, node.id, node)
            return
        if isinstance(node, ast.Attribute):
            self.visit(node.value)
            self.event(
                BodyEventKind.MEMBER,
                node.attr,
                node,
                span=self.attribute_span(node),
            )
            return
        if isinstance(node, ast.Constant):
            self.event(BodyEventKind.LITERAL, _constant_text(node.value), node)
            return
        if isinstance(node, ast.JoinedStr):
            self.event(BodyEventKind.LITERAL, "<string>", node)
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    self.visit(value.value)
            return
        if isinstance(node, ast.AnnAssign):
            self.visit(node.target)
            self.visit_annotation(node.annotation)
            if node.value is not None:
                self.event(BodyEventKind.OPERATOR, "=", node)
                self.visit(node.value)
            return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self.visit(target)
            self.event(BodyEventKind.OPERATOR, "=", node)
            self.visit(node.value)
            return
        if isinstance(node, ast.AugAssign):
            self.visit(node.target)
            self.event(BodyEventKind.OPERATOR, _operator_text(node.op) + "=", node)
            self.visit(node.value)
            return
        if isinstance(node, ast.NamedExpr):
            self.visit(node.target)
            self.event(BodyEventKind.OPERATOR, ":=", node)
            self.visit(node.value)
            return
        if isinstance(node, ast.BinOp):
            self.visit(node.left)
            self.event(BodyEventKind.OPERATOR, _operator_text(node.op), node)
            self.visit(node.right)
            return
        if isinstance(node, ast.BoolOp):
            for index, value in enumerate(node.values):
                if index:
                    self.event(BodyEventKind.OPERATOR, _operator_text(node.op), node)
                self.visit(value)
            return
        if isinstance(node, ast.Compare):
            self.visit(node.left)
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                self.event(BodyEventKind.OPERATOR, _operator_text(operator), node)
                self.visit(comparator)
            return
        if isinstance(node, ast.UnaryOp):
            self.event(BodyEventKind.OPERATOR, _operator_text(node.op), node)
            self.visit(node.operand)
            return
        keyword_text = self.keyword(node)
        if keyword_text is not None:
            self.event(BodyEventKind.KEYWORD, keyword_text, node)
        self._visit_children(node)

    def _visit_if(self, node: ast.If) -> None:
        self.event(BodyEventKind.KEYWORD, "if", node)
        self.visit(node.test)
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        self.event(BodyEventKind.KEYWORD, "loop", node)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self.visit(node.target)
            self.visit(node.iter)
        else:
            self.visit(node.test)
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        self.event(BodyEventKind.KEYWORD, "try", node)
        for statement in node.body:
            self.visit(statement)
        for handler in node.handlers:
            self.visit(handler)
        for statement in node.orelse:
            self.visit(statement)
        if node.finalbody:
            self.control("finally", node, lambda: self._visit_all(node.finalbody))

    def _visit_except(self, node: ast.ExceptHandler) -> None:
        self.event(BodyEventKind.KEYWORD, "catch", node)
        if node.type is not None:
            self.visit_annotation(node.type)
        if node.name:
            span = ast_span(self.source, node)
            self.events.append(BodyEvent(BodyEventKind.LOCAL, node.name, span))
        for statement in node.body:
            self.visit(statement)

    def _visit_match(self, node: ast.Match) -> None:
        self.event(BodyEventKind.KEYWORD, "match", node)
        self.visit(node.subject)
        for case in node.cases:
            self.visit(case.pattern)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def _visit_children(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def _visit_all(self, nodes: Iterable[ast.AST]) -> None:
        for node in nodes:
            self.visit(node)

    @staticmethod
    def call_name(function: ast.AST) -> str:
        if isinstance(function, ast.Name):
            return function.id
        if isinstance(function, ast.Attribute):
            return function.attr
        return type(function).__name__.lower()

    @staticmethod
    def keyword(node: ast.AST) -> str | None:
        keywords: tuple[tuple[type[ast.AST], str], ...] = (
            (ast.Return, "return"),
            (ast.Raise, "raise"),
            (ast.Yield, "yield"),
            (ast.YieldFrom, "yield"),
            (ast.Await, "await"),
            (ast.Break, "break"),
            (ast.Continue, "continue"),
            (ast.Pass, "pass"),
            (ast.Assert, "assert"),
            (ast.Delete, "delete"),
            (ast.Import, "import"),
            (ast.ImportFrom, "import"),
            (ast.Global, "global"),
            (ast.Nonlocal, "nonlocal"),
        )
        for kind, text in keywords:
            if isinstance(node, kind):
                return text
        return None


def ast_body_events(
    source: SourceFile, callable_node: ast.AST
) -> tuple[BodyEvent, ...]:
    """Emit facts from one stdlib-AST callable without entering nested callables."""
    if not isinstance(
        callable_node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
    ):
        raise TypeError("callable_node must be a Python function or lambda")
    return _AstBodyEventWalker(source, callable_node).walk()


body_events = ast_body_events


__all__ = [
    "ast_body_events",
    "ast_span",
    "base_type",
    "body_events",
    "body_lines",
    "heritage",
    "ordered_unique",
    "reference",
    "signature_key",
    "span_from_character_columns",
    "split_top_commas",
    "symbol_id",
    "tight_type",
    "utf8_byte_column",
    "validate_body_events",
]
