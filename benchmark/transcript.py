from __future__ import annotations

import json
import re
from dataclasses import dataclass

_READ_TOOLS = frozenset({"Read"})
_SEARCH_TOOLS = frozenset({"Grep", "Glob"})
_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
_BASH_SEARCH = re.compile(r"\b(grep|rg|find|fd|ag)\b")
_BASH_READ = re.compile(r"\b(cat|head|tail|sed -n|less|more)\b")
_MAP_EVIDENCE = re.compile(r"PROJECT_DIGEST\.md|hologram:v2:(?:start|end)")


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


@dataclass(frozen=True)
class TranscriptSummary:
    terminal_status: str
    terminal_count: int
    is_error: bool
    stop_reason: str | None
    final_answer: str
    reported_model: str | None
    reads: int
    searches: int
    edits: int
    map_hits: int
    turns: int
    tokens_in: int
    tokens_out: int


def _integer(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _assistant_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    fragments = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(fragment for fragment in fragments if fragment)


def _terminal_status(
    *,
    count: int,
    subtype: object,
    is_error: bool,
    stop_reason: str | None,
    answer: str,
    reported_model: str | None,
    requested_model: str,
) -> str:
    if count == 0:
        return "missing_result"
    if count != 1:
        return "multiple_results"
    if type(subtype) is not str or not subtype:
        return "invalid_result"
    if subtype != "success":
        return subtype
    if is_error:
        return "result_error"
    if stop_reason != "end_turn":
        return f"stop_reason_{stop_reason or 'missing'}"
    if not answer.strip():
        return "empty_answer"
    if reported_model != requested_model:
        return "model_mismatch"
    return "success"


def parse_transcript(text: str, *, requested_model: str) -> TranscriptSummary:
    reads = 0
    searches = 0
    edits = 0
    map_hits = 0
    stop_reason: str | None = None
    assistant_answer = ""
    models: set[str] = set()
    terminals: list[dict[str, object]] = []

    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        event_model = event.get("model")
        if isinstance(event_model, str) and event_model:
            models.add(event_model)
        event_type = event.get("type")
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            message_model = message.get("model")
            if isinstance(message_model, str) and message_model:
                models.add(message_model)
            candidate_stop = message.get("stop_reason")
            if isinstance(candidate_stop, str) or candidate_stop is None:
                stop_reason = candidate_stop
            if candidate_answer := _assistant_text(message):
                assistant_answer = candidate_answer
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                tool_input = block.get("input")
                input_text = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                if _MAP_EVIDENCE.search(input_text):
                    map_hits += 1
                if name in _READ_TOOLS:
                    reads += 1
                elif name in _SEARCH_TOOLS:
                    searches += 1
                elif name in _EDIT_TOOLS:
                    edits += 1
                elif name == "Bash" and isinstance(tool_input, dict):
                    command = tool_input.get("command", "")
                    if not isinstance(command, str):
                        continue
                    if _BASH_SEARCH.search(command):
                        searches += 1
                    elif _BASH_READ.search(command):
                        reads += 1
        elif event_type == "result":
            terminals.append(event)

    terminal = terminals[-1] if terminals else {}
    result_answer = terminal.get("result")
    final_answer = result_answer if isinstance(result_answer, str) else assistant_answer
    error_value = terminal.get("is_error", True)
    is_error = error_value if type(error_value) is bool else True
    usage = terminal.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    reported_model = next(iter(models)) if len(models) == 1 else None
    status = _terminal_status(
        count=len(terminals),
        subtype=terminal.get("subtype"),
        is_error=is_error,
        stop_reason=stop_reason,
        answer=final_answer,
        reported_model=reported_model,
        requested_model=requested_model,
    )
    return TranscriptSummary(
        status,
        len(terminals),
        is_error,
        stop_reason,
        final_answer,
        reported_model,
        reads,
        searches,
        edits,
        map_hits,
        _integer(terminal.get("num_turns")),
        _integer(usage.get("input_tokens"))
        + _integer(usage.get("cache_creation_input_tokens"))
        + _integer(usage.get("cache_read_input_tokens")),
        _integer(usage.get("output_tokens")),
    )


def terminal_succeeded(process: ProcessResult, summary: TranscriptSummary) -> bool:
    return (
        type(process) is ProcessResult
        and type(summary) is TranscriptSummary
        and process.returncode == 0
        and not process.timed_out
        and summary.terminal_status == "success"
    )


__all__ = (
    "ProcessResult",
    "TranscriptSummary",
    "parse_transcript",
    "terminal_succeeded",
)
