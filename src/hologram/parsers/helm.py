from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

from hologram.model import (
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    SourceFile,
    SourceSpan,
    Symbol,
    SymbolKind,
    Visibility,
)

from .common import symbol_id, validate_body_events

_ACTION_RE = re.compile(rb"\{\{(?P<body>.*?)\}\}", re.DOTALL)
_CHART_NAME_RE = re.compile(rb"(?m)^name:\s*(\S+)")
_VALUE_RE = re.compile(rb"(?m)^([A-Za-z_][\w-]*):")
_TOKEN_RE = re.compile(
    rb'"(?:\\.|[^"\\])*"'
    rb"|`[^`]*`"
    rb"|\$?[A-Za-z_][A-Za-z0-9_.-]*"
    rb"|\.[A-Za-z_][A-Za-z0-9_.-]*"
    rb"|\."
    rb"|-?\d+(?:\.\d+)?"
    rb"|:=|==|!=|<=|>=|\|\||&&"
    rb"|[-+*/%=<>|]"
)
_CONTROL_ACTIONS = {b"if": "if", b"range": "loop", b"with": "if"}
_NON_EVENT_BLOCKS = frozenset({b"block"})
_KEYWORDS = frozenset({b"else"})
_OPERATORS = frozenset(
    {
        b"!=",
        b"%",
        b"&&",
        b"*",
        b"+",
        b"-",
        b"/",
        b":=",
        b"<",
        b"<=",
        b"=",
        b"==",
        b">",
        b">=",
        b"|",
        b"||",
    }
)


@dataclass(frozen=True, slots=True)
class _Token:
    raw: bytes
    start: int
    end: int


@dataclass(slots=True)
class _Definition:
    name: str
    open_start: int
    content_start: int
    stack: list[tuple[str, SourceSpan | None]]
    events: list[BodyEvent] = field(default_factory=list)
    calls: list[CallRef] = field(default_factory=list)


def _line_starts(raw: bytes) -> tuple[int, ...]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer(rb"\r\n|\r|\n", raw))
    return tuple(starts)


def _position(starts: tuple[int, ...], offset: int) -> tuple[int, int]:
    line_index = bisect_right(starts, offset) - 1
    return line_index + 1, offset - starts[line_index]


def _span(
    source: SourceFile,
    starts: tuple[int, ...],
    start: int,
    end: int,
) -> SourceSpan:
    start_line, start_column = _position(starts, start)
    end_line, end_column = _position(starts, end)
    return SourceSpan(
        source.file,
        start_line,
        start_column,
        end_line,
        end_column,
    )


def _action_tokens(match: re.Match[bytes]) -> tuple[_Token, ...]:
    body = match.group("body")
    body_start = match.start("body")
    left = 0
    right = len(body)
    while left < right and body[left : left + 1].isspace():
        left += 1
    if left < right and body[left : left + 1] == b"-":
        left += 1
        while left < right and body[left : left + 1].isspace():
            left += 1
    while right > left and body[right - 1 : right].isspace():
        right -= 1
    if right > left and body[right - 1 : right] == b"-":
        right -= 1
        while right > left and body[right - 1 : right].isspace():
            right -= 1
    content = body[left:right]
    return tuple(
        _Token(
            token.group(),
            body_start + left + token.start(),
            body_start + left + token.end(),
        )
        for token in _TOKEN_RE.finditer(content)
    )


def _string_value(raw: bytes) -> str | None:
    if len(raw) < 2 or raw[:1] not in {b'"', b"`"}:
        return None
    text = raw[1:-1].decode("utf-8")
    if raw[:1] == b'"':
        text = text.replace(r"\"", '"').replace(r"\\", "\\")
    return text


def _literal_text(raw: bytes) -> str | None:
    if raw[:1] in {b'"', b"`"}:
        return "<string>"
    if re.fullmatch(rb"-?\d+(?:\.\d+)?", raw):
        return "<number>"
    if raw in {b"false", b"true"}:
        return "<bool>"
    if raw in {b"nil", b"null"}:
        return "<null>"
    return None


def _event(
    source: SourceFile,
    starts: tuple[int, ...],
    kind: BodyEventKind,
    text: str,
    token: _Token,
) -> BodyEvent:
    return BodyEvent(kind, text, _span(source, starts, token.start, token.end))


def _definition_symbol(
    source: SourceFile,
    starts: tuple[int, ...],
    definition: _Definition,
    end: int,
) -> Symbol:
    return Symbol(
        symbol_id(source, (), SymbolKind.FUNCTION, definition.name),
        _span(source, starts, definition.open_start, end),
        Visibility.PUBLIC,
        f'define "{definition.name}"',
        body_lines=0,
    )


def _chart_symbols(
    source: SourceFile,
    starts: tuple[int, ...],
) -> list[Symbol]:
    base = source.file.rsplit("/", 1)[-1]
    symbols: list[Symbol] = []
    if base == "Chart.yaml":
        match = _CHART_NAME_RE.search(source.raw)
        if match is not None:
            name = match.group(1).decode("utf-8")
            symbols.append(
                Symbol(
                    symbol_id(source, (), SymbolKind.CLASS, name),
                    _span(source, starts, match.start(), match.end()),
                    Visibility.PUBLIC,
                    f"chart {name}",
                )
            )
    elif base == "values.yaml":
        for match in _VALUE_RE.finditer(source.raw):
            name = match.group(1).decode("utf-8")
            symbols.append(
                Symbol(
                    symbol_id(source, (), SymbolKind.FUNCTION, name),
                    _span(source, starts, match.start(1), match.end(1)),
                    Visibility.PRIVATE,
                    name,
                )
            )
    return symbols


def _action_events(
    source: SourceFile,
    starts: tuple[int, ...],
    definition: _Definition,
    tokens: tuple[_Token, ...],
) -> None:
    if not tokens:
        return
    for index, token in enumerate(tokens):
        if token.raw in {b"include", b"template"} and index + 1 < len(tokens):
            target = _string_value(tokens[index + 1].raw)
            command_end = next(
                (
                    tokens[candidate_index - 1].end
                    for candidate_index in range(index + 1, len(tokens))
                    if tokens[candidate_index].raw == b"|"
                ),
                tokens[-1].end,
            )
            if target is not None:
                call_span = _span(source, starts, token.start, command_end)
                definition.calls.append(
                    CallRef(
                        symbol_id(
                            source,
                            (),
                            SymbolKind.FUNCTION,
                            definition.name,
                        ),
                        call_span,
                        target,
                        None,
                        CallKind.CALL,
                        None,
                    )
                )
                definition.events.append(
                    BodyEvent(BodyEventKind.CALL, target, call_span)
                )
        literal = _literal_text(token.raw)
        if literal is not None:
            definition.events.append(
                _event(
                    source,
                    starts,
                    BodyEventKind.LITERAL,
                    literal,
                    token,
                )
            )
        elif token.raw in _OPERATORS:
            definition.events.append(
                _event(
                    source,
                    starts,
                    BodyEventKind.OPERATOR,
                    token.raw.decode("ascii"),
                    token,
                )
            )
        elif token.raw in _KEYWORDS:
            definition.events.append(
                _event(
                    source,
                    starts,
                    BodyEventKind.KEYWORD,
                    token.raw.decode("ascii"),
                    token,
                )
            )
        elif token.raw not in {b"include", b"template"}:
            definition.events.append(
                _event(
                    source,
                    starts,
                    BodyEventKind.NAME,
                    token.raw.decode("utf-8"),
                    token,
                )
            )


def _finish_definition(
    source: SourceFile,
    starts: tuple[int, ...],
    definition: _Definition,
    *,
    body_end: int,
    symbol_end: int,
    partial: bool,
) -> tuple[Symbol, BodyIR, tuple[CallRef, ...]]:
    if partial:
        eof_span = _span(source, starts, symbol_end, symbol_end)
        while len(definition.stack) > 1:
            control, _ = definition.stack.pop()
            if control:
                definition.events.append(
                    BodyEvent(BodyEventKind.CONTROL_EXIT, control, eof_span)
                )
    validate_body_events(definition.events)
    symbol = _definition_symbol(source, starts, definition, symbol_end)
    body = BodyIR(
        symbol.id,
        _span(source, starts, definition.content_start, body_end),
        tuple(definition.events),
    )
    calls = tuple(
        CallRef(
            symbol.id,
            call.span,
            call.name,
            call.receiver,
            call.kind,
            call.arity,
        )
        for call in definition.calls
    )
    return symbol, body, calls


def extract(source: SourceFile, parser: object | None) -> FileIR:
    del parser
    parts = source.file.split("/")
    base = parts[-1]
    in_chart = "templates" in parts or base in {"Chart.yaml", "values.yaml"}
    if not in_chart:
        return FileIR(source)

    starts = _line_starts(source.raw)
    symbols = _chart_symbols(source, starts)
    bodies: list[BodyIR] = []
    calls: list[CallRef] = []
    diagnostics: list[Diagnostic] = []
    definition: _Definition | None = None

    for match in _ACTION_RE.finditer(source.raw):
        tokens = _action_tokens(match)
        if not tokens:
            continue
        keyword = tokens[0].raw
        if definition is None:
            if keyword != b"define" or len(tokens) < 2:
                continue
            name = _string_value(tokens[1].raw)
            if name is None:
                diagnostics.append(
                    Diagnostic(
                        "helm-syntax-error",
                        DiagnosticSeverity.ERROR,
                        f"{source.file}: template definition name must be a string",
                        _span(source, starts, match.start(), match.end()),
                    )
                )
                continue
            definition = _Definition(
                name,
                match.start(),
                match.end(),
                [("", _span(source, starts, match.start(), match.end()))],
            )
            continue

        if keyword in _CONTROL_ACTIONS:
            control = _CONTROL_ACTIONS[keyword]
            control_span = _span(source, starts, match.start(), match.end())
            definition.events.append(
                BodyEvent(BodyEventKind.CONTROL_ENTER, control, control_span)
            )
            definition.stack.append((control, control_span))
            _action_events(source, starts, definition, tokens[1:])
            continue
        if keyword in _NON_EVENT_BLOCKS:
            definition.stack.append(("", None))
            _action_events(source, starts, definition, tokens[1:])
            continue
        if keyword == b"end":
            if not definition.stack:
                diagnostics.append(
                    Diagnostic(
                        "helm-syntax-error",
                        DiagnosticSeverity.ERROR,
                        f"{source.file}: unexpected template end",
                        _span(source, starts, match.start(), match.end()),
                    )
                )
                continue
            control, _ = definition.stack.pop()
            if control:
                definition.events.append(
                    BodyEvent(
                        BodyEventKind.CONTROL_EXIT,
                        control,
                        _span(source, starts, match.start(), match.end()),
                    )
                )
            if not definition.stack:
                symbol, body, owned_calls = _finish_definition(
                    source,
                    starts,
                    definition,
                    body_end=match.start(),
                    symbol_end=match.end(),
                    partial=False,
                )
                symbols.append(symbol)
                bodies.append(body)
                calls.extend(owned_calls)
                definition = None
            continue
        _action_events(source, starts, definition, tokens)

    if definition is not None:
        symbol, body, owned_calls = _finish_definition(
            source,
            starts,
            definition,
            body_end=len(source.raw),
            symbol_end=len(source.raw),
            partial=True,
        )
        symbols.append(symbol)
        bodies.append(body)
        calls.extend(owned_calls)
        diagnostics.append(
            Diagnostic(
                "helm-syntax-error",
                DiagnosticSeverity.ERROR,
                f"{source.file}: unclosed template definition {definition.name!r}",
                symbol.span,
            )
        )

    return FileIR(
        source,
        symbols=tuple(symbols),
        calls=tuple(calls),
        bodies=tuple(bodies),
        diagnostics=tuple(diagnostics),
    )


__all__ = ["extract"]
