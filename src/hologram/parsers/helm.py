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

_CHART_NAME_RE = re.compile(rb"(?m)^name:\s*(\S+)")
_VALUE_RE = re.compile(r"(?m)^([A-Za-z_][\w-]*):")
_TOKEN_RE = re.compile(
    rb'"(?:\\.|[^"\\])*"'
    rb"|`[^`]*`"
    rb"|'(?:\\.|[^'\\])*'"
    rb"|\$?[A-Za-z_][A-Za-z0-9_.-]*"
    rb"|\.[A-Za-z_][A-Za-z0-9_.-]*"
    rb"|\."
    rb"|-?\d+(?:\.\d+)?"
    rb"|:=|==|!=|<=|>=|\|\||&&"
    rb"|[-+*/%=<>|]"
)
_CONTROL_ACTIONS = {b"if": "if", b"range": "loop", b"with": "if"}
_NON_EVENT_BLOCKS = frozenset({b"block"})
_KEYWORDS = frozenset({b"else", b"if"})
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


@dataclass(frozen=True, slots=True)
class _Action:
    start: int
    end: int
    body_start: int
    body_end: int
    comment: bool = False


@dataclass(slots=True)
class _Definition:
    name: str
    open_start: int
    content_start: int
    events: list[BodyEvent] = field(default_factory=list)
    calls: list[CallRef] = field(default_factory=list)


@dataclass(slots=True)
class _Frame:
    keyword: bytes
    definition: _Definition | None
    span: SourceSpan
    control: str | None = None
    final_else: bool = False


def _line_starts(raw: bytes) -> tuple[int, ...]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer(rb"\r\n|\r|\n", raw))
    return tuple(starts)


def _utf8_offsets(text: str) -> tuple[int, ...]:
    offsets = [0]
    for character in text:
        offsets.append(offsets[-1] + len(character.encode("utf-8")))
    return tuple(offsets)


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


def _syntax_diagnostic(
    source: SourceFile,
    starts: tuple[int, ...],
    message: str,
    start: int,
    end: int,
) -> Diagnostic:
    return Diagnostic(
        "helm-syntax-error",
        DiagnosticSeverity.ERROR,
        f"{source.file}: {message}",
        _span(source, starts, start, end),
    )


def _comment_opener(raw: bytes, body_start: int) -> int | None:
    cursor = body_start
    if raw[cursor : cursor + 1] == b"-":
        cursor += 1
    while raw[cursor : cursor + 1].isspace():
        cursor += 1
    return cursor if raw[cursor : cursor + 2] == b"/*" else None


def _scan_actions(
    source: SourceFile,
    starts: tuple[int, ...],
) -> tuple[tuple[_Action, ...], tuple[Diagnostic, ...]]:
    raw = source.raw
    actions: list[_Action] = []
    diagnostics: list[Diagnostic] = []
    cursor = 0
    while True:
        action_start = raw.find(b"{{", cursor)
        if action_start < 0:
            break
        body_start = action_start + 2
        comment_start = _comment_opener(raw, body_start)
        if comment_start is not None:
            comment_end = raw.find(b"*/", comment_start + 2)
            if comment_end < 0:
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "unterminated template comment",
                        action_start,
                        len(raw),
                    )
                )
                break
            tail = comment_end + 2
            while raw[tail : tail + 1].isspace():
                tail += 1
            if raw[tail : tail + 1] == b"-":
                tail += 1
                while raw[tail : tail + 1].isspace():
                    tail += 1
            if raw[tail : tail + 2] != b"}}":
                delimiter = raw.find(b"}}", tail)
                diagnostic_end = len(raw) if delimiter < 0 else delimiter + 2
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "unexpected content after template comment",
                        action_start,
                        diagnostic_end,
                    )
                )
                if delimiter < 0:
                    break
                tail = delimiter
            action_end = tail + 2
            actions.append(
                _Action(
                    action_start,
                    action_end,
                    body_start,
                    tail,
                    comment=True,
                )
            )
            cursor = action_end
            continue

        quote: int | None = None
        escaped = False
        index = body_start
        while index < len(raw):
            byte = raw[index]
            if quote is not None:
                if quote != ord("`") and escaped:
                    escaped = False
                elif quote != ord("`") and byte == ord("\\"):
                    escaped = True
                elif byte == quote:
                    quote = None
                index += 1
                continue
            if byte in {ord('"'), ord("'"), ord("`")}:
                quote = byte
                index += 1
                continue
            if raw[index : index + 2] == b"}}":
                action_end = index + 2
                actions.append(_Action(action_start, action_end, body_start, index))
                cursor = action_end
                break
            index += 1
        else:
            message = (
                "unterminated template string"
                if quote is not None
                else "unterminated template action"
            )
            diagnostics.append(
                _syntax_diagnostic(
                    source,
                    starts,
                    message,
                    action_start,
                    len(raw),
                )
            )
            break
    return tuple(actions), tuple(diagnostics)


def _action_tokens(action: _Action, raw: bytes) -> tuple[_Token, ...]:
    body = raw[action.body_start : action.body_end]
    body_start = action.body_start
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
    if raw[:1] == b"`":
        return raw[1:-1].decode("utf-8")
    content = raw[1:-1]
    decoded = bytearray()
    escapes = {
        ord('"'): ord('"'),
        ord("\\"): ord("\\"),
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
    }
    index = 0
    while index < len(content):
        byte = content[index]
        if byte != ord("\\") or index + 1 == len(content):
            decoded.append(byte)
            index += 1
            continue
        following = content[index + 1]
        decoded.append(escapes.get(following, following))
        index += 2
    return decoded.decode("utf-8")


def _literal_text(raw: bytes) -> str | None:
    if raw[:1] in {b'"', b"'", b"`"}:
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
        chart_match = _CHART_NAME_RE.search(source.raw)
        if chart_match is not None:
            name = chart_match.group(1).decode("utf-8")
            symbols.append(
                Symbol(
                    symbol_id(source, (), SymbolKind.CLASS, name),
                    _span(
                        source,
                        starts,
                        chart_match.start(),
                        chart_match.end(),
                    ),
                    Visibility.PUBLIC,
                    f"chart {name}",
                )
            )
    elif base == "values.yaml":
        text = source.text
        offsets = _utf8_offsets(text)
        for value_match in _VALUE_RE.finditer(text):
            name = value_match.group(1)
            symbols.append(
                Symbol(
                    symbol_id(source, (), SymbolKind.FUNCTION, name),
                    _span(
                        source,
                        starts,
                        offsets[value_match.start(1)],
                        offsets[value_match.end(1)],
                    ),
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
    open_controls: tuple[str, ...] = (),
) -> tuple[Symbol, BodyIR, tuple[CallRef, ...]]:
    if open_controls:
        eof_span = _span(source, starts, symbol_end, symbol_end)
        for control in reversed(open_controls):
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
    actions, scan_diagnostics = _scan_actions(source, starts)
    diagnostics = list(scan_diagnostics)
    definition: _Definition | None = None
    stack: list[_Frame] = []

    for action in actions:
        if action.comment:
            continue
        tokens = _action_tokens(action, source.raw)
        if not tokens:
            continue
        keyword = tokens[0].raw
        action_span = _span(source, starts, action.start, action.end)

        if keyword == b"define":
            if definition is not None or stack:
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "nested template definition",
                        action.start,
                        action.end,
                    )
                )
                continue
            name = _string_value(tokens[1].raw) if len(tokens) >= 2 else None
            if name is None:
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "template definition name must be a string",
                        action.start,
                        action.end,
                    )
                )
                continue
            definition = _Definition(name, action.start, action.end)
            stack.append(_Frame(b"define", definition, action_span))
            continue

        if keyword in _CONTROL_ACTIONS:
            control = _CONTROL_ACTIONS[keyword]
            if definition is not None:
                definition.events.append(
                    BodyEvent(BodyEventKind.CONTROL_ENTER, control, action_span)
                )
                _action_events(source, starts, definition, tokens[1:])
            stack.append(_Frame(keyword, definition, action_span, control=control))
            continue

        if keyword in _NON_EVENT_BLOCKS:
            if definition is not None:
                _action_events(source, starts, definition, tokens[1:])
            stack.append(_Frame(keyword, definition, action_span))
            continue

        if keyword == b"else":
            if not stack or stack[-1].keyword not in _CONTROL_ACTIONS:
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "unexpected template else",
                        action.start,
                        action.end,
                    )
                )
                continue
            frame = stack[-1]
            else_if = len(tokens) > 1 and tokens[1].raw == b"if"
            if frame.final_else:
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "unexpected template else after final else",
                        action.start,
                        action.end,
                    )
                )
                continue
            frame.final_else = not else_if
            if definition is not None:
                _action_events(source, starts, definition, tokens)
            continue

        if keyword == b"end":
            if not stack:
                diagnostics.append(
                    _syntax_diagnostic(
                        source,
                        starts,
                        "unexpected template end",
                        action.start,
                        action.end,
                    )
                )
                continue
            frame = stack.pop()
            if frame.control is not None and frame.definition is not None:
                frame.definition.events.append(
                    BodyEvent(BodyEventKind.CONTROL_EXIT, frame.control, action_span)
                )
            if frame.keyword == b"define" and frame.definition is not None:
                symbol, body, owned_calls = _finish_definition(
                    source,
                    starts,
                    frame.definition,
                    body_end=action.start,
                    symbol_end=action.end,
                )
                symbols.append(symbol)
                bodies.append(body)
                calls.extend(owned_calls)
                definition = None
            continue

        if definition is not None:
            _action_events(source, starts, definition, tokens)

    if definition is not None:
        open_controls = tuple(
            frame.control
            for frame in stack
            if frame.definition is definition and frame.control is not None
        )
        symbol, body, owned_calls = _finish_definition(
            source,
            starts,
            definition,
            body_end=len(source.raw),
            symbol_end=len(source.raw),
            open_controls=open_controls,
        )
        symbols.append(symbol)
        bodies.append(body)
        calls.extend(owned_calls)
        diagnostics.append(
            _syntax_diagnostic(
                source,
                starts,
                f"unclosed template definition {definition.name!r}",
                definition.open_start,
                len(source.raw),
            )
        )

    for frame in stack:
        if frame.keyword == b"define":
            continue
        diagnostics.append(
            Diagnostic(
                "helm-syntax-error",
                DiagnosticSeverity.ERROR,
                (f"{source.file}: unclosed template {frame.keyword.decode('ascii')}"),
                frame.span,
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
