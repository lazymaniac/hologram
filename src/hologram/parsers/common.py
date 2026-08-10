from __future__ import annotations

import ast
import io
import re
import tokenize
import unicodedata
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import RLock
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


def _python_physical_lines(text: str) -> tuple[str, ...]:
    """Split only the three newline forms recognized by Python source."""
    return tuple(re.split(r"\r\n|\r|\n", text))


def _span_from_character_columns(
    source: SourceFile,
    lines: tuple[str, ...],
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> SourceSpan:
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


@dataclass(frozen=True, slots=True)
class _PythonSourceContext:
    lines: tuple[str, ...]
    line_offsets: tuple[int, ...]
    tokens: tuple[tuple[str, SourceSpan], ...]
    token_starts: tuple[tuple[int, int], ...]


_PYTHON_SOURCE_CONTEXT_LIMIT = 64
_PYTHON_SOURCE_CONTEXTS: OrderedDict[SourceFile, _PythonSourceContext] = OrderedDict()
_PYTHON_SOURCE_CONTEXT_LOCK = RLock()
_IGNORED_PYTHON_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
)


def _build_python_source_context(source: SourceFile) -> _PythonSourceContext:
    lines = _python_physical_lines(source.text)
    offsets = [0]
    offsets.extend(match.end() for match in re.finditer(rb"\r\n|\r|\n", source.raw))
    if len(lines) != len(offsets):
        raise AssertionError("Python physical-line table is inconsistent")
    reader = io.StringIO(source.text, newline=None).readline
    tokens = tuple(
        (
            token.string,
            _span_from_character_columns(
                source,
                lines,
                token.start[0],
                token.start[1],
                token.end[0],
                token.end[1],
            ),
        )
        for token in tokenize.generate_tokens(reader)
        if token.type not in _IGNORED_PYTHON_TOKENS
    )
    token_starts = tuple((span.start_line, span.start_column) for _, span in tokens)
    return _PythonSourceContext(lines, tuple(offsets), tokens, token_starts)


def _python_source_context(source: SourceFile) -> _PythonSourceContext:
    with _PYTHON_SOURCE_CONTEXT_LOCK:
        context = _PYTHON_SOURCE_CONTEXTS.pop(source, None)
        if context is None:
            context = _build_python_source_context(source)
        _PYTHON_SOURCE_CONTEXTS[source] = context
        if len(_PYTHON_SOURCE_CONTEXTS) > _PYTHON_SOURCE_CONTEXT_LIMIT:
            _PYTHON_SOURCE_CONTEXTS.popitem(last=False)
        return context


def span_from_character_columns(
    source: SourceFile,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> SourceSpan:
    return _span_from_character_columns(
        source,
        _python_physical_lines(source.text),
        start_line,
        start_column,
        end_line,
        end_column,
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
    def __init__(
        self,
        source: SourceFile,
        callable_node: ast.AST,
        type_constructor_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.source = source
        self.callable_node = callable_node
        self.events: list[BodyEvent] = []
        self._events_seen: set[BodyEvent] = set()
        context = _python_source_context(source)
        self._line_offsets = context.line_offsets
        self._tokens = context.tokens
        self._token_starts = context.token_starts
        self._type_constructor_aliases = dict(type_constructor_aliases or ())
        self._external_bindings = self._declared_external_bindings()
        self._comprehension_targets = self._comprehension_target_nodes()

    def _owned_nodes(self) -> Iterable[ast.AST]:
        roots = getattr(self.callable_node, "body", ())
        stack = list(reversed(roots if isinstance(roots, list) else (roots,)))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            stack.extend(reversed(tuple(ast.iter_child_nodes(node))))

    def _declared_external_bindings(self) -> frozenset[str]:
        names: set[str] = set()
        for node in self._owned_nodes():
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
        return frozenset(names)

    def _comprehension_target_nodes(self) -> frozenset[int]:
        targets: set[int] = set()
        for node in self._owned_nodes():
            if isinstance(node, ast.comprehension):
                targets.update(
                    id(child)
                    for child in ast.walk(node.target)
                    if isinstance(child, ast.Name)
                )
        return frozenset(targets)

    def event(
        self,
        kind: BodyEventKind,
        text: str,
        node: ast.AST,
        *,
        span: SourceSpan | None = None,
    ) -> None:
        event = BodyEvent(kind, text, span or ast_span(self.source, node))
        if event not in self._events_seen:
            self._events_seen.add(event)
            self.events.append(event)

    @staticmethod
    def _start(span: SourceSpan) -> tuple[int, int]:
        return span.start_line, span.start_column

    @staticmethod
    def _end(span: SourceSpan) -> tuple[int, int]:
        return span.end_line, span.end_column

    def _tokens_in_range(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> tuple[tuple[str, SourceSpan], ...]:
        first = bisect_left(self._token_starts, start)
        last = bisect_left(self._token_starts, end, lo=first)
        return tuple(
            (token_text, span)
            for token_text, span in self._tokens[first:last]
            if self._end(span) <= end
        )

    def name_span(
        self,
        name: str,
        node: ast.AST,
        *,
        last: bool = False,
        after: ast.AST | None = None,
        before: ast.AST | None = None,
    ) -> SourceSpan:
        container = ast_span(self.source, node)
        start = (
            self._end(ast_span(self.source, after)) if after else self._start(container)
        )
        end = (
            self._start(ast_span(self.source, before))
            if before
            else self._end(container)
        )
        normalized = unicodedata.normalize("NFKC", name)
        matches = tuple(
            span
            for token_text, span in self._tokens_in_range(start, end)
            if unicodedata.normalize("NFKC", token_text) == normalized
        )
        if not matches:
            raise AssertionError(
                f"source token for {name!r} not found in {type(node).__name__}"
            )
        return matches[-1] if last else matches[0]

    def binding_event(
        self,
        name: str,
        node: ast.AST,
        *,
        last: bool = False,
        after: ast.AST | None = None,
        before: ast.AST | None = None,
    ) -> None:
        kind = (
            BodyEventKind.NAME
            if name in self._external_bindings
            else BodyEventKind.LOCAL
        )
        self.event(
            kind,
            name,
            node,
            span=self.name_span(
                name,
                node,
                last=last,
                after=after,
                before=before,
            ),
        )

    def operator_span(
        self,
        text: str,
        left: ast.AST,
        right: ast.AST,
        *,
        from_node_start: bool = False,
    ) -> SourceSpan:
        left_span = ast_span(self.source, left)
        right_span = ast_span(self.source, right)
        start = self._start(left_span) if from_node_start else self._end(left_span)
        end = self._start(right_span)
        return self._operator_span_in_range(
            text,
            start,
            end,
            context=f"{type(left).__name__} and {type(right).__name__}",
        )

    def _operator_span_in_range(
        self,
        text: str,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        context: str,
    ) -> SourceSpan:
        parts = text.split()
        candidates = self._tokens_in_range(start, end)
        matches: list[SourceSpan] = []
        for index in range(len(candidates) - len(parts) + 1):
            selected = candidates[index : index + len(parts)]
            if [token_text for token_text, _ in selected] != parts:
                continue
            matches.append(
                SourceSpan(
                    self.source.file,
                    selected[0][1].start_line,
                    selected[0][1].start_column,
                    selected[-1][1].end_line,
                    selected[-1][1].end_column,
                )
            )
        if not matches:
            matches.extend(self._raw_operator_spans(text, start, end))
        if len(matches) != 1:
            raise AssertionError(
                f"expected one {text!r} operator in {context}, found {len(matches)}"
            )
        return matches[0]

    def prefix_operator_span(self, text: str, node: ast.AST) -> SourceSpan:
        end = self._start(ast_span(self.source, node))
        callable_start = self._start(ast_span(self.source, self.callable_node))
        candidates = self._tokens_in_range(callable_start, end)
        matches = tuple(span for token_text, span in candidates if token_text == text)
        if not matches:
            raise AssertionError(
                f"source token for prefix {text!r} not found before "
                f"{type(node).__name__}"
            )
        selected = matches[-1]
        intervening = tuple(
            token_text
            for token_text, span in candidates
            if self._start(span) >= self._end(selected) and self._end(span) <= end
        )
        if intervening:
            raise AssertionError(
                f"prefix {text!r} is not adjacent to {type(node).__name__}"
            )
        return selected

    def _absolute_offset(self, position: tuple[int, int]) -> int:
        line, column = position
        return self._line_offsets[line - 1] + column

    def _position_from_offset(self, offset: int) -> tuple[int, int]:
        line_index = bisect_right(self._line_offsets, offset) - 1
        return line_index + 1, offset - self._line_offsets[line_index]

    def _raw_operator_spans(
        self,
        text: str,
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> tuple[SourceSpan, ...]:
        absolute_start = self._absolute_offset(start)
        segment = self.source.raw[absolute_start : self._absolute_offset(end)]
        parts = tuple(part.encode() for part in text.split())
        if len(parts) == 1:
            pattern = re.escape(parts[0])
        else:
            separator = rb"(?:\s|\\(?:\r\n|\r|\n)|#[^\r\n]*(?:\r\n|\r|\n|$))+"
            pattern = separator.join(re.escape(part) for part in parts)
        spans: list[SourceSpan] = []
        for match in re.finditer(pattern, segment):
            match_start = self._position_from_offset(absolute_start + match.start())
            match_end = self._position_from_offset(absolute_start + match.end())
            spans.append(
                SourceSpan(
                    self.source.file,
                    match_start[0],
                    match_start[1],
                    match_end[0],
                    match_end[1],
                )
            )
        return tuple(spans)

    def operator_event(
        self,
        text: str,
        left: ast.AST,
        right: ast.AST,
        *,
        from_node_start: bool = False,
    ) -> None:
        self.event(
            BodyEventKind.OPERATOR,
            text,
            left,
            span=self.operator_span(
                text,
                left,
                right,
                from_node_start=from_node_start,
            ),
        )

    def prefix_operator_event(self, text: str, node: ast.AST) -> None:
        self.event(
            BodyEventKind.OPERATOR,
            text,
            node,
            span=self.prefix_operator_span(text, node),
        )

    def dict_unpack_operator_event(
        self,
        value: ast.AST,
        previous_value: ast.AST | None,
        dictionary: ast.Dict,
    ) -> None:
        container = ast_span(self.source, dictionary)
        start = (
            self._end(ast_span(self.source, previous_value))
            if previous_value is not None
            else self._start(container)
        )
        span = self._operator_span_in_range(
            "**",
            start,
            self._start(ast_span(self.source, value)),
            context="dict unpack entry",
        )
        self.event(BodyEventKind.OPERATOR, "**", value, span=span)

    def visit_formatted_value(self, node: ast.FormattedValue) -> None:
        self.visit(node.value)
        if not isinstance(node.format_spec, ast.JoinedStr):
            return
        for value in node.format_spec.values:
            if isinstance(value, ast.FormattedValue):
                self.visit_formatted_value(value)

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
            self.visit_arguments(arguments)
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

    def visit_arguments(self, arguments: ast.arguments) -> None:
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
            self.visit_parameter(arguments.vararg, None, prefix="*")
        for parameter, default in zip(
            arguments.kwonlyargs,
            arguments.kw_defaults,
            strict=True,
        ):
            self.visit_parameter(parameter, default)
        if arguments.kwarg is not None:
            self.visit_parameter(arguments.kwarg, None, prefix="**")

    def visit_parameter(
        self,
        parameter: ast.arg,
        default: ast.expr | None,
        *,
        prefix: str | None = None,
    ) -> None:
        if prefix is not None:
            self.prefix_operator_event(prefix, parameter)
        self.event(
            BodyEventKind.PARAM,
            parameter.arg,
            parameter,
            span=self.name_span(parameter.arg, parameter),
        )
        if parameter.annotation is not None:
            self.visit_annotation(parameter.annotation)
        if default is not None:
            self.operator_event("=", parameter, default)
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
        if isinstance(node, ast.Subscript):
            self.visit_annotation(node.value)
            arguments = (
                node.slice.elts if isinstance(node.slice, ast.Tuple) else (node.slice,)
            )
            constructor_name = (
                node.value.id
                if isinstance(node.value, ast.Name)
                else node.value.attr
                if isinstance(node.value, ast.Attribute)
                else None
            )
            constructor = (
                self._type_constructor_aliases.get(
                    constructor_name,
                    constructor_name,
                )
                if constructor_name is not None
                else None
            )
            if constructor == "Literal":
                for argument in arguments:
                    self.visit(argument)
                return
            if constructor == "Annotated" and arguments:
                self.visit_annotation(arguments[0])
                for metadata in arguments[1:]:
                    self.visit(metadata)
                return
            for argument in arguments:
                self.visit_annotation(argument)
            return
        for child in ast.iter_child_nodes(node):
            self.visit_annotation(child)

    def attribute_span(self, node: ast.Attribute) -> SourceSpan:
        return self.name_span(node.attr, node, last=True)

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.binding_event(node.name, node)
            return
        if isinstance(node, ast.Lambda):
            self.visit_arguments(node.args)
            self.visit(node.body)
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
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self.event(BodyEventKind.KEYWORD, "import", node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.asname is not None:
                    name = alias.asname
                    last = True
                elif isinstance(node, ast.Import):
                    name = alias.name.split(".", 1)[0]
                    last = False
                else:
                    name = alias.name
                    last = False
                self.binding_event(name, alias, last=last)
            return
        if isinstance(node, ast.Call):
            name = self.call_name(node.func)
            kind = BodyEventKind.CONSTRUCT if name[:1].isupper() else BodyEventKind.CALL
            self.event(kind, name, node)
            self.visit(node.func)
            arguments = sorted(
                (*node.args, *node.keywords),
                key=lambda item: self._start(ast_span(self.source, item)),
            )
            for argument in arguments:
                if isinstance(argument, ast.keyword):
                    operator_text = "=" if argument.arg is not None else "**"
                    self.operator_event(
                        operator_text,
                        argument,
                        argument.value,
                        from_node_start=True,
                    )
                    self.visit(argument.value)
                else:
                    self.visit(argument)
            return
        if isinstance(node, ast.Starred):
            self.operator_event("*", node, node.value, from_node_start=True)
            self.visit(node.value)
            return
        if isinstance(node, ast.Dict):
            previous_value: ast.AST | None = None
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    self.dict_unpack_operator_event(value, previous_value, node)
                else:
                    self.visit(key)
                self.visit(value)
                previous_value = value
            return
        if isinstance(node, ast.Name):
            kind = (
                BodyEventKind.LOCAL
                if isinstance(node.ctx, ast.Store)
                and (
                    id(node) in self._comprehension_targets
                    or node.id not in self._external_bindings
                )
                else BodyEventKind.NAME
            )
            self.event(kind, node.id, node)
            return
        if isinstance(node, ast.Attribute):
            self.visit(node.value)
            span = self.attribute_span(node)
            self.event(
                BodyEventKind.MEMBER,
                node.attr,
                node,
                span=span,
            )
            self.event(BodyEventKind.NAME, node.attr, node, span=span)
            return
        if isinstance(node, ast.Constant):
            self.event(BodyEventKind.LITERAL, _constant_text(node.value), node)
            return
        if isinstance(node, ast.JoinedStr):
            self.event(BodyEventKind.LITERAL, "<string>", node)
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    self.visit_formatted_value(value)
            return
        if isinstance(node, ast.MatchAs):
            if node.pattern is not None:
                self.visit(node.pattern)
            if node.name is not None:
                self.binding_event(node.name, node, last=True)
            return
        if isinstance(node, ast.MatchStar):
            if node.name is not None:
                self.binding_event(node.name, node, last=True)
            return
        if isinstance(node, ast.MatchMapping):
            for key, pattern in zip(node.keys, node.patterns, strict=True):
                self.visit(key)
                self.visit(pattern)
            if node.rest is not None:
                self.binding_event(node.rest, node, last=True)
            return
        if isinstance(node, ast.AnnAssign):
            self.visit(node.target)
            self.visit_annotation(node.annotation)
            if node.value is not None:
                self.operator_event("=", node.annotation, node.value)
                self.visit(node.value)
            return
        if isinstance(node, ast.Assign):
            following = (*node.targets[1:], node.value)
            for target, right in zip(node.targets, following, strict=True):
                self.visit(target)
                self.operator_event("=", target, right)
            self.visit(node.value)
            return
        if isinstance(node, ast.AugAssign):
            self.visit(node.target)
            self.operator_event(_operator_text(node.op) + "=", node.target, node.value)
            self.visit(node.value)
            return
        if isinstance(node, ast.NamedExpr):
            self.visit(node.target)
            self.operator_event(":=", node.target, node.value)
            self.visit(node.value)
            return
        if isinstance(node, ast.BinOp):
            self.visit(node.left)
            self.operator_event(_operator_text(node.op), node.left, node.right)
            self.visit(node.right)
            return
        if isinstance(node, ast.BoolOp):
            self.visit(node.values[0])
            for left, right in zip(node.values[:-1], node.values[1:], strict=True):
                self.operator_event(_operator_text(node.op), left, right)
                self.visit(right)
            return
        if isinstance(node, ast.Compare):
            self.visit(node.left)
            left = node.left
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                self.operator_event(_operator_text(operator), left, comparator)
                self.visit(comparator)
                left = comparator
            return
        if isinstance(node, ast.UnaryOp):
            self.operator_event(
                _operator_text(node.op),
                node,
                node.operand,
                from_node_start=True,
            )
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
            self.binding_event(
                node.name,
                node,
                after=node.type,
                before=node.body[0] if node.body else None,
            )
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
    source: SourceFile,
    callable_node: ast.AST,
    *,
    type_constructor_aliases: Mapping[str, str] | None = None,
) -> tuple[BodyEvent, ...]:
    """Emit one Python scope's events without entering named declarations.

    Anonymous lambdas are expressions owned by the nearest named or module scope,
    so their parameters, defaults, and bodies are traversed in place.
    """
    if not isinstance(
        callable_node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module),
    ):
        raise TypeError("callable_node must be a Python module, function, or lambda")
    return _AstBodyEventWalker(
        source,
        callable_node,
        type_constructor_aliases,
    ).walk()


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
