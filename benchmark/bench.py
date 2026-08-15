#!/usr/bin/env python3
"""bench: measure agents with vs without a hologram digest.

Subcommands: run (execute the task matrix headlessly), report (aggregate results).
Every claude invocation goes through an injectable runner so the harness is
testable without spending tokens.
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import json
import os
import platform
import random
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram.cli import _contained_target  # noqa: E402

HOLOGRAM = Path(__file__).resolve().parents[1] / "hologram.py"
_SETUP_REF = "refs/bench/setup"
_ACCEPT_TIMEOUT_SECONDS = 300
_DIAGNOSTIC_LIMIT = 4000
_CONDITIONS = {"A", "AC", "AR", "B"}
_SAFE_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESULT_SCHEMA_VERSION = 3
_REVIEW_CAPTURE_SCHEMA_VERSION = 1
_SCHEDULE_REVISION = "counterbalanced-rotation-v1"
_DEFAULT_RUNNER_FAILURE_LIMIT = 2
_TOOL_REVISION = hashlib.sha256(
    hologram._generator_fingerprint() + Path(__file__).read_bytes()
).hexdigest()[:12]


def _safe_context_targets(workspace: Path) -> list[Path]:
    """Context targets that cannot redirect benchmark setup outside its clone."""
    workspace = workspace.resolve()
    try:
        return [_contained_target(workspace, target,
                                  "benchmark context target")
                for target in hologram.context_targets(workspace)]
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from None


@dataclass
class Task:
    id: str
    kind: str                 # "reuse" | "navigate" | "fix"
    prompt: str
    accept_cmd: str           # shell; {ws} is replaced with the workspace path
    expect_reuse: list[str] = field(default_factory=list)
    expect_answer: list[str] = field(default_factory=list)  # regexes vs result text
    expect_in_new_code: list[str] = field(default_factory=list)  # names required in added lines
    scope_in_tests: bool = False  # restrict scope match to added lines in test files
    max_turns: int | None = None  # per-task override: the session-length dial
    effort: str | None = None  # per-task reasoning-effort override
    # A structural command is not, by itself, a semantic correctness judge.
    # These fields reserve a provenance-compatible contract for stronger
    # judges without changing the meaning of `accepted`.
    manual_only: bool = False
    judge: dict[str, object] = field(default_factory=dict)
    # The default command protocol follows common Unix test/search tools:
    # zero passes, one is an observed task rejection, and every other exit is
    # judge infrastructure failure unless the task opts in explicitly.
    accept_pass_codes: list[int] = field(default_factory=lambda: [0])
    accept_fail_codes: list[int] = field(default_factory=lambda: [1])
    # Opt-in assertion that the configured automatic evidence is strong enough
    # to support a semantic pass/fail claim. Structural commands leave this off.
    semantic_judge: bool = False


@dataclass
class Config:
    corpus: Path
    tasks: list[Task]
    model: str = "sonnet"
    max_turns: int = 40
    lang: list[str] = field(default_factory=list)  # map filter for condition A
    budget: int | None = None  # token budget for the embedded map
    effort: str | None = None  # reasoning effort requested from the CLI
    revision: str = ""  # immutable task-file content identity


@dataclass
class RunnerOutcome:
    """Captured process outcome for an agent or acceptance command.

    Injected benchmark runners may still return a raw transcript string.  The
    harness promotes a valid terminal transcript to a successful outcome so
    existing task fixtures and third-party runners remain compatible.
    """

    stdout: str = ""
    stderr: str = ""
    returncode: int | None = 0
    duration_seconds: float = 0.0
    timed_out: bool = False
    status: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status is None:
            if self.timed_out:
                self.status = "timeout"
            elif self.error is not None:
                self.status = "error"
            elif self.returncode == 0:
                self.status = "ok"
            else:
                self.status = "nonzero"

    @property
    def ok(self) -> bool:
        return (self.status == "ok" and not self.timed_out
                and self.returncode == 0 and self.error is None)


_CONFIG_KEYS = {
    "corpus", "tasks", "model", "max_turns", "lang", "budget", "effort",
}
_TASK_KEYS = {
    "id", "kind", "prompt", "accept_cmd", "expect_reuse", "expect_answer",
    "expect_in_new_code", "scope_in_tests", "max_turns", "effort",
    "manual_only", "accept_pass_codes", "accept_fail_codes",
    "semantic_judge", "judge",
}


def load_tasks(path: Path) -> Config:
    raw = path.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"task file {path}: invalid JSON: {exc}") from None
    try:
        if not isinstance(data, dict):
            raise TypeError("top level must be an object")
        unknown_config = sorted(set(data) - _CONFIG_KEYS)
        if unknown_config:
            raise ValueError("unknown top-level field(s): "
                             + ", ".join(unknown_config))
        if not isinstance(data.get("tasks"), list):
            raise TypeError("tasks must be a list")
        for index, task_data in enumerate(data["tasks"]):
            if not isinstance(task_data, dict):
                raise TypeError(f"task {index} must be an object")
            unknown_task = sorted(set(task_data) - _TASK_KEYS)
            if unknown_task:
                task_id = task_data.get("id", index)
                raise ValueError(
                    f"task {task_id}: unknown field(s): "
                    + ", ".join(unknown_task))
        tasks = [Task(id=t["id"], kind=t["kind"], prompt=t["prompt"],
                      accept_cmd=t["accept_cmd"],
                      expect_reuse=t.get("expect_reuse", []),
                      expect_answer=t.get("expect_answer", []),
                      expect_in_new_code=t.get("expect_in_new_code", []),
                      scope_in_tests=t.get("scope_in_tests", False),
                      max_turns=t.get("max_turns"),
                      effort=t.get("effort"),
                      manual_only=t.get("manual_only", False),
                      accept_pass_codes=t.get("accept_pass_codes", [0]),
                      accept_fail_codes=t.get("accept_fail_codes", [1]),
                      semantic_judge=t.get("semantic_judge", False),
                      judge=t.get("judge", {}))
                 for t in data["tasks"]]
        config = Config(corpus=Path(data["corpus"]).expanduser().resolve(),
                        tasks=tasks,
                        model=data.get("model", "sonnet"),
                        max_turns=data.get("max_turns", 40),
                        lang=data.get("lang", []),
                        budget=data.get("budget"),
                        effort=data.get("effort"),
                        revision=hashlib.sha256(raw.encode()).hexdigest()[:12])
        _validate_config(config, path)
        return config
    except KeyError as exc:
        raise SystemExit(f"task file {path}: missing field {exc}") from None
    except (TypeError, ValueError, AttributeError) as exc:
        raise SystemExit(f"task file {path}: invalid field: {exc}") from None


def _validate_config(config: Config, path: Path | str = "task file") -> None:
    """Reject an invalid experiment before a provider call can spend tokens."""
    errors: list[str] = []
    ids: set[str] = set()
    if (not isinstance(config.model, str) or not config.model.strip()):
        errors.append("model must be a non-empty string")
    if (not isinstance(config.max_turns, int)
            or isinstance(config.max_turns, bool) or config.max_turns <= 0):
        errors.append("max_turns must be positive")
    if (config.budget is not None
            and (not isinstance(config.budget, int)
                 or isinstance(config.budget, bool) or config.budget <= 0)):
        errors.append("budget must be positive when present")
    if (not isinstance(config.lang, list)
            or not all(isinstance(lang, str) and lang for lang in config.lang)):
        errors.append("lang must be a list of non-empty strings")
    if config.effort not in (None, "low", "medium", "high"):
        errors.append(f"unknown effort {config.effort!r}")
    for task in config.tasks:
        if not isinstance(task.id, str) or not _SAFE_TASK_ID.fullmatch(task.id):
            errors.append(f"unsafe task id {task.id!r}")
        elif task.id in ids:
            errors.append(f"duplicate task id {task.id!r}")
        if isinstance(task.id, str):
            ids.add(task.id)
        if task.kind not in ("reuse", "navigate", "fix"):
            errors.append(f"task {task.id}: unknown kind {task.kind!r}")
        if not isinstance(task.prompt, str) or not task.prompt.strip():
            errors.append(f"task {task.id}: prompt must be a non-empty string")
        if not isinstance(task.accept_cmd, str) or not task.accept_cmd.strip():
            errors.append(f"task {task.id}: accept_cmd must be a non-empty string")
        if (task.max_turns is not None
                and (not isinstance(task.max_turns, int)
                     or isinstance(task.max_turns, bool)
                     or task.max_turns <= 0)):
            errors.append(f"task {task.id}: max_turns must be positive")
        if task.effort not in (None, "low", "medium", "high"):
            errors.append(f"task {task.id}: unknown effort {task.effort!r}")
        if not isinstance(task.manual_only, bool):
            errors.append(f"task {task.id}: manual_only must be boolean")
        if not isinstance(task.semantic_judge, bool):
            errors.append(f"task {task.id}: semantic_judge must be boolean")
        if task.manual_only and task.semantic_judge:
            errors.append(
                f"task {task.id}: manual_only and semantic_judge conflict")
        for field_name in ("accept_pass_codes", "accept_fail_codes"):
            codes = getattr(task, field_name)
            if (not isinstance(codes, list)
                    or not all(isinstance(code, int)
                               and not isinstance(code, bool) for code in codes)):
                errors.append(f"task {task.id}: {field_name} must be list[int]")
            elif len(set(codes)) != len(codes):
                errors.append(f"task {task.id}: {field_name} must be unique")
        if isinstance(task.accept_pass_codes, list) and not task.accept_pass_codes:
            errors.append(f"task {task.id}: accept_pass_codes must not be empty")
        if (isinstance(task.accept_pass_codes, list)
                and isinstance(task.accept_fail_codes, list)
                and set(task.accept_pass_codes) & set(task.accept_fail_codes)):
            errors.append(
                f"task {task.id}: acceptance pass/fail codes must be disjoint")
        if (not isinstance(task.judge, dict)
                or not all(isinstance(key, str) for key in task.judge)):
            errors.append(f"task {task.id}: judge must be an object")
        else:
            try:
                json.dumps(task.judge, sort_keys=True)
            except (TypeError, ValueError) as exc:
                errors.append(f"task {task.id}: judge is not JSON-safe: {exc}")
        for field_name in ("expect_reuse", "expect_answer",
                           "expect_in_new_code"):
            value = getattr(task, field_name)
            if (not isinstance(value, list)
                    or not all(isinstance(item, str) and item
                               for item in value)):
                errors.append(
                    f"task {task.id}: {field_name} must be list[non-empty str]")
        if not isinstance(task.scope_in_tests, bool):
            errors.append(f"task {task.id}: scope_in_tests must be boolean")
        if (isinstance(task.expect_answer, list)
                and all(isinstance(item, str) for item in task.expect_answer)):
            for pattern in task.expect_answer:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"task {task.id}: invalid answer regex: {exc}")
        if isinstance(task.accept_cmd, str):
            try:
                task.accept_cmd.format(ws="/tmp/bench-workspace", sha="0" * 40)
            except (KeyError, IndexError, ValueError) as exc:
                errors.append(f"task {task.id}: invalid accept_cmd format: {exc}")
    if not config.tasks:
        errors.append("at least one task is required")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))


def _validate_matrix(config: Config, conditions: list[str], reps: int,
                     only: list[str] | None) -> list[Task]:
    if not conditions:
        raise SystemExit("at least one condition is required")
    unknown_conditions = [c for c in conditions if c not in _CONDITIONS]
    if unknown_conditions:
        raise SystemExit("unknown condition(s): " + ", ".join(unknown_conditions))
    if len(set(conditions)) != len(conditions):
        raise SystemExit("conditions must be unique")
    if reps <= 0:
        raise SystemExit("--reps must be positive")
    ids = {task.id for task in config.tasks}
    unknown_tasks = sorted(set(only or ()) - ids)
    if unknown_tasks:
        raise SystemExit("unknown --only task(s): " + ", ".join(unknown_tasks))
    tasks = [task for task in config.tasks
             if only is None or task.id in set(only)]
    if not tasks:
        raise SystemExit("task selection is empty")
    return tasks


def _adhoc_config_revision(task: Task) -> str:
    payload = json.dumps(asdict(task), sort_keys=True, separators=(",", ":"))
    return "adhoc-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def _identity(prefix: str, payload: object) -> str:
    """Stable identity for an immutable experiment or result cell."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode()
    return f"{prefix}-" + hashlib.sha256(encoded).hexdigest()[:20]


def _task_revision(task: Task) -> str:
    return _identity("task", asdict(task))


def _judge_config_revision(task: Task) -> str:
    return _identity("judge", {
        "accept_cmd": task.accept_cmd,
        "accept_pass_codes": task.accept_pass_codes,
        "accept_fail_codes": task.accept_fail_codes,
        "expect_reuse": task.expect_reuse,
        "expect_answer": task.expect_answer,
        "expect_in_new_code": task.expect_in_new_code,
        "scope_in_tests": task.scope_in_tests,
        "manual_only": task.manual_only,
        "semantic_judge": task.semantic_judge,
        "judge": task.judge,
    })


_READ_TOOLS = {"Read"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
_BASH_SEARCH = re.compile(r"\b(grep|rg|find|fd|ag)\b")
_BASH_READ = re.compile(r"\b(cat|head|tail|sed -n|less|more)\b")
_REVIEW_HEADER = re.compile(
    r"(?m)^hologram review vs (?P<revision>[^:\n]+): "
    r"(?P<count>\d+) finding(?:\(s\)|s)?\s*$")
_REVIEW_SOURCE_COMMAND = re.compile(
    r"(?:\bgit\b[^\n|;&]*\bcommit\b|\bhologram(?:\.py)?\b[^\n|;&]*\breview\b)")


def _content_text(value: object) -> str:
    """Text carried by a stream-json content value."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        return _content_text(value.get("content", ""))
    return ""


def _review_tool_results(event: dict, bash_commands: dict[str, str]) -> list[str]:
    """Results of an actual commit/review Bash tool, not arbitrary file text."""
    if event.get("type") != "user":
        return []
    content = (event.get("message") or {}).get("content", [])
    if not isinstance(content, list):
        return []
    results: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        command = bash_commands.get(str(block.get("tool_use_id", "")), "")
        if _REVIEW_SOURCE_COMMAND.search(command):
            results.append(_content_text(block.get("content", "")))
    return results


def parse_transcript(text: str) -> dict:
    """Tool-call counts and usage from a claude stream-json transcript.
    Agents search/read through Bash as often as through dedicated tools, so
    Bash commands are classified too. The three input-token sources remain
    separate; legacy `tokens_in` is their sum. Tolerant of non-JSON noise
    lines."""
    m = {"reads": 0, "searches": 0, "edits": 0,
         "turns": 0, "input_tokens": 0,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
         "tokens_in_fresh": 0,
         "tokens_in_cache_created": 0, "tokens_in_cache_read": 0,
         "tokens_in": 0, "tokens_out": 0,
         "files_read": 0, "result_text": ""}
    read_paths: set[str] = set()
    # Ordered event stream for the review-action proxy: hook output (which
    # lands in user-event tool_results), edits, and commits.  Assistant prose
    # is deliberately excluded so quoting the hook output cannot count.
    events: list[tuple] = []
    bash_commands: dict[str, str] = {}
    review_seen = False
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                if name in _READ_TOOLS:
                    m["reads"] += 1
                    fp = (block.get("input") or {}).get("file_path")
                    if fp:
                        read_paths.add(fp)
                elif name in _SEARCH_TOOLS:
                    m["searches"] += 1
                elif name in _EDIT_TOOLS:
                    m["edits"] += 1
                    fp = (block.get("input") or {}).get("file_path")
                    if fp:
                        events.append(("edit", fp))
                elif name == "Bash":
                    cmd = (block.get("input") or {}).get("command", "")
                    tool_id = block.get("id")
                    if tool_id:
                        bash_commands[str(tool_id)] = cmd
                    if _BASH_SEARCH.search(cmd):
                        m["searches"] += 1
                    elif _BASH_READ.search(cmd):
                        m["reads"] += 1
                    if re.search(r"\bgit\b[^\n|;&]*\bcommit\b", cmd):
                        events.append(("commit", ""))
        elif ev.get("type") == "user":
            for tool_text in _review_tool_results(ev, bash_commands):
                for match in _REVIEW_HEADER.finditer(tool_text):
                    review_seen = True
                    if int(match.group("count")) > 0:
                        events.append((
                            "review",
                            re.findall(r"\bin ([^\s:]+?\.[A-Za-z0-9]+)"
                                       r"(?=\s|:|$)", tool_text),
                        ))
        elif ev.get("type") == "result":
            usage = ev.get("usage") or {}
            m["turns"] = int(ev.get("num_turns", 0))
            m["tokens_in_fresh"] = int(usage.get("input_tokens", 0))
            m["tokens_in_cache_created"] = int(
                usage.get("cache_creation_input_tokens", 0))
            m["tokens_in_cache_read"] = int(
                usage.get("cache_read_input_tokens", 0))
            m["input_tokens"] = m["tokens_in_fresh"]
            m["cache_creation_input_tokens"] = m[
                "tokens_in_cache_created"]
            m["cache_read_input_tokens"] = m["tokens_in_cache_read"]
            m["tokens_in"] = (m["tokens_in_fresh"]
                              + m["tokens_in_cache_created"]
                              + m["tokens_in_cache_read"])
            m["tokens_out"] = int(usage.get("output_tokens", 0))
            m["result_text"] = str(ev.get("result", ""))
    m["files_read"] = len(read_paths)
    m["review_seen"] = review_seen
    action_proxy = _acted_on_findings(events)
    m["review_action_proxy"] = action_proxy
    # Kept for existing result consumers; this is an ordered-action proxy,
    # not proof that a reported finding was actually resolved.
    m["acted_on_findings"] = action_proxy
    return m


def _acted_on_findings(events: list[tuple]) -> bool:
    """Proxy: a review is followed by a relevant edit and later commit.

    This intentionally does not claim that the finding was resolved.  It only
    establishes an ordered, transcript-visible action after a real hook event.
    """
    for i, (kind, payload) in enumerate(events):
        if kind != "review":
            continue
        for j in range(i + 1, len(events)):
            if events[j][0] != "edit":
                continue
            path = events[j][1]
            named = payload
            if named and not any(path.endswith(f) for f in named):
                continue
            if any(k[0] == "commit" for k in events[j + 1:]):
                return True
    return False


def _sig_lines(digest: str) -> list[str]:
    out = []
    for ln in digest.splitlines():
        s = ln.strip()
        if s and not s.startswith(("#", "·", "-", "»", "?")) and "(" in s:
            out.append(s)
    return out


def _fn_name(sig_line: str) -> str:
    return sig_line.split("(", 1)[0].strip().lstrip("-").split(",")[-1]


def _chain(sig_line: str) -> list[str]:
    if " > " not in sig_line:
        return []
    return [c.strip() for c in sig_line.split(" > ", 1)[1].split(",")]


def judge_reuse(before: str, after: str, expect_reuse: list[str]) -> dict:
    """Compare digests around a run. reused = expected symbols named in a new
    line's call chain. duplicated = new functions name-similar to an expected
    symbol that do NOT call it."""
    old = set(_sig_lines(before))
    new_lines = [ln for ln in _sig_lines(after) if ln not in old]
    reused: list[str] = []
    duplicated: list[str] = []
    for target in expect_reuse:
        tshort = target.rsplit(".", 1)[-1].lower()
        hit = any(tshort in (c.rsplit(".", 1)[-1].lower() for c in _chain(ln))
                  for ln in new_lines)
        if hit:
            reused.append(target)
            continue
        for ln in new_lines:
            name = _fn_name(ln)
            sim = difflib.SequenceMatcher(None, name.lower(), tshort).ratio()
            if sim >= 0.6 and name.lower() != tshort:
                duplicated.append(name)
                break
    return {"new_lines": new_lines,
            "reused": sorted(set(reused)),
            "duplicated": sorted(set(duplicated))}


def _setup_sha(ws: Path) -> str:
    """Return the committed setup baseline without touching the worktree."""
    result = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--verify", _SETUP_REF],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "HEAD"


def _record_clean_setup(ws: Path) -> str:
    """Record HEAD under git metadata and require a pristine judged baseline."""
    sha = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(ws), "update-ref", _SETUP_REF, sha],
                   check=True, capture_output=True, text=True)
    status = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain=v1",
         "--untracked-files=all"], check=True,
        capture_output=True, text=True).stdout
    if status:
        raise RuntimeError(
            "benchmark setup did not produce a clean baseline:\n" + status)
    return sha


def _added_lines(ws: Path, test_only: bool = False,
                 setup_sha: str | None = None) -> list[str]:
    """Lines the agent added, from `git diff` against the recorded setup
    commit — robust against agents committing their own work (corpus
    conventions often require it) and where transcripts are not (Edit
    payloads are fragments; Bash-written files never appear as payloads)."""
    out = subprocess.run(["git", "-C", str(ws), "diff", "--unified=0",
                          setup_sha or _setup_sha(ws)],
                         capture_output=True, text=True).stdout
    added: list[str] = []
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):]
        elif line.startswith("+") and not line.startswith("+++"):
            if test_only and (current is None
                              or not hologram._is_test_path(current)):
                continue
            added.append(line[1:])
    return added


def judge_scope(ws: Path, expect: list[str], test_only: bool = False,
                setup_sha: str | None = None) -> bool | None:
    """Did every expected collaborator name appear in newly written code?"""
    if not expect:
        return None
    joined = "\n".join(_added_lines(ws, test_only, setup_sha))
    return all(re.search(rf"\b{re.escape(name)}\b", joined) for name in expect)


_EFFORT_TOKENS = {"low": "4096", "medium": "16384", "high": "63999"}
_CLI_HAS_EFFORT: bool | None = None


def _effort_invocation(effort: str | None) -> tuple[list[str], dict[str, str]]:
    """Extra argv/env to request a reasoning-effort level; no-op when the
    installed CLI supports neither mechanism."""
    global _CLI_HAS_EFFORT
    if not effort:
        return [], {}
    if _CLI_HAS_EFFORT is None:
        probe = _run_process(["claude", "--help"], Path.cwd(), 15)
        help_text = probe.stdout if probe.ok else ""
        _CLI_HAS_EFFORT = "--effort" in help_text
    if _CLI_HAS_EFFORT:
        return ["--effort", effort], {}
    if effort in _EFFORT_TOKENS:
        return [], {"MAX_THINKING_TOKENS": _EFFORT_TOKENS[effort]}
    return [], {}


_BASE_CLAUDE_MD = """# Working notes

Complete the requested task directly. Keep changes minimal and idiomatic.

IMPORTANT: This checkout is the whole project. Work ONLY inside the current
working directory. If anything above or in other docs names a different
project directory or branch, it refers to another checkout that must not be
read or modified — ignore it.
"""


def make_workspace(corpus: Path, ws: Path, condition: str,
                   lang: list[str] | None = None,
                   budget: int | None = None) -> Path:
    """Detached local clone of the corpus, prepared for one condition.
    A = map only, AC = map plus shipped coaching, AR = shipped init/hooks, and
    B = control. The corpus's own CLAUDE.md is preserved, and the setup is
    committed in the detached clone so that any later `git diff` shows exactly
    what the agent changed.

    Every condition clones because a worktree's `.git` pointer both breaks AR
    hooks and reveals the source checkout path. The origin remote is removed so
    a stray `git push` has nowhere to land."""
    if condition not in _CONDITIONS:
        raise ValueError(f"unknown benchmark condition {condition!r}")
    head = subprocess.run(["git", "-C", str(corpus), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "clone", "--no-hardlinks", "-q", str(corpus),
                    str(ws)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "checkout", "-q", "--detach",
                    head], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "remote", "remove", "origin"],
                   check=True, capture_output=True)
    # A corpus whose committed context files already carry an embedded map
    # would contaminate the control condition — strip any pre-existing
    # blocks in both conditions; A rebuilds its own below. Context files may
    # also name the corpus's home checkout by absolute path. Any absolute path
    # that resolves outside the workspace is rewritten to the workspace itself.
    abs_path_re = re.compile(r"(?:/Users|/home)/[^\s`'\")\]]+")
    for target in _safe_context_targets(ws):
        if not target.is_file():
            continue
        text = target.read_text()
        span = hologram.embed._block_span(text)
        if span is not None:
            text = (text[:span[0]] + text[span[1]:]).strip("\n")
            text = text + "\n" if text else ""
        def _confine(m: "re.Match[str]") -> str:
            p = Path(m.group(0))
            try:
                p.resolve().relative_to(ws.resolve())
                return m.group(0)
            except ValueError:
                return str(ws)
        target.write_text(abs_path_re.sub(_confine, text))
    try:
        claude_path = _contained_target(
            ws.resolve(), ws.resolve() / "CLAUDE.md",
            "benchmark context target")
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from None
    existing = claude_path.read_text() if claude_path.exists() else ""
    claude_md = (existing.rstrip("\n") + "\n\n" if existing else "") + _BASE_CLAUDE_MD
    claude_path.write_text(claude_md)
    if condition in ("A", "AC", "AR"):
        verb = "init" if condition == "AR" else "build"  # AR installs hooks
        cmd = [sys.executable, str(HOLOGRAM), verb, "--root", str(ws),
               "--quiet", "--warn-tokens", "0"]
        for l in (lang or []):
            cmd += ["--lang", l]
        if budget:
            cmd += ["--budget", str(budget)]
        subprocess.run(cmd, check=True)
        if condition == "A":
            # A = map without the coaching sentence; AC = shipped note
            from hologram.embed import _COACH_SENTENCE
            for target in _safe_context_targets(ws):
                if target.is_file():
                    target.write_text(
                        target.read_text().replace(_COACH_SENTENCE, ""))
    subprocess.run(["git", "-C", str(ws), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "-c", "user.email=bench@bench",
                    "-c", "user.name=bench", "commit", "-qm", "bench setup"],
                   check=True, capture_output=True)
    _record_clean_setup(ws)
    return ws


def drop_workspace(corpus: Path, ws: Path) -> None:
    subprocess.run(["git", "-C", str(corpus), "worktree", "remove", "--force",
                    str(ws)], capture_output=True)
    shutil.rmtree(ws, ignore_errors=True)


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Stop a timed-out command and descendants in its private session."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # The session leader can exit while a descendant ignores SIGTERM and keeps
    # inherited pipes/files alive. Always sweep the process group with SIGKILL.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_process(command, cwd: Path, timeout: float, *, shell: bool = False,
                 env: dict[str, str] | None = None) -> RunnerOutcome:
    """Capture one command with timeout enforcement over its process group."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
            start_new_session=True)
    except OSError as exc:
        return RunnerOutcome(stderr=str(exc), returncode=None,
                             duration_seconds=time.monotonic() - started,
                             status="error", error=repr(exc))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return RunnerOutcome(stdout=stdout, stderr=stderr,
                             returncode=process.returncode,
                             duration_seconds=time.monotonic() - started)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        return RunnerOutcome(stdout=_as_text(stdout), stderr=_as_text(stderr),
                             returncode=None,
                             duration_seconds=time.monotonic() - started,
                             timed_out=True, error=str(exc))


def claude_runner(prompt: str, ws: Path, model: str, max_turns: int,
                  effort: str | None = None) -> RunnerOutcome:
    """The only function that spends tokens. Runs claude headless in the
    workspace and captures a structured outcome."""
    extra_args, extra_env = _effort_invocation(effort)
    env = {**os.environ, **extra_env} if extra_env else None
    return _run_process(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
         "--max-turns", str(max_turns), "--model", model,
         "--dangerously-skip-permissions", *extra_args],
        ws, 1800, env=env)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _terminal_result(text: str) -> dict | None:
    """Return the last terminal stream-json event, if present."""
    terminal: dict | None = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            terminal = event
    return terminal


# Exhausting `--max-turns` is the session-length dial doing its job, not a
# broken runner: the cell is a real observation of an agent that did not finish
# in the budgeted turns, which is exactly what the conditions are compared on.
_TERMINAL_TASK_OUTCOMES = {"error_max_turns"}


def _terminal_transcript_error(terminal: dict) -> str | None:
    """Return an explicit *infrastructure* error carried by a terminal event.

    Provider/runner failures become infrastructure errors and leave the cell
    unobserved. Declared task outcomes do not, even though the CLI flags them
    with `is_error`, so a turn-limited session still reaches the judges and the
    aggregates instead of being dropped and tripping the circuit breaker."""
    subtype = str(terminal.get("subtype", ""))
    if subtype in _TERMINAL_TASK_OUTCOMES:
        return None
    if terminal.get("is_error") or subtype.startswith("error"):
        return subtype or _as_text(terminal.get("result")) or "runner result error"
    return None


def _terminal_protocol_error(terminal: dict, *,
                             require_result_text: bool) -> str | None:
    """Validate the minimum successful stream-json contract used by metrics."""
    turns = terminal.get("num_turns")
    if (not isinstance(turns, int) or isinstance(turns, bool) or turns < 0):
        return "terminal result has no valid num_turns"
    usage = terminal.get("usage")
    if not isinstance(usage, dict):
        return "terminal result has no usage object"
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            return f"terminal usage has no valid {key}"
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if key not in usage:
            continue
        value = usage[key]
        if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            return f"terminal usage has invalid {key}"
    if "is_error" in terminal and not isinstance(terminal["is_error"], bool):
        return "terminal result has invalid is_error"
    if "subtype" in terminal and not isinstance(terminal["subtype"], str):
        return "terminal result has invalid subtype"
    if require_result_text and not isinstance(terminal.get("result"), str):
        return "terminal result has no answer text"
    if "result" in terminal and not isinstance(terminal["result"], str):
        return "terminal result has invalid answer text"
    return None


def _apply_terminal_status(outcome: RunnerOutcome, *,
                           require_terminal: bool,
                           require_result_text: bool = False) -> RunnerOutcome:
    if outcome.status == "ok" and not outcome.ok:
        outcome.status = ("timeout" if outcome.timed_out else
                          "nonzero" if outcome.returncode not in (None, 0) else
                          "error")
    terminal = _terminal_result(outcome.stdout) if outcome.ok else None
    if outcome.ok and terminal is None and require_terminal:
        outcome.status = "invalid_transcript"
        outcome.error = "missing terminal stream-json result"
        return outcome
    terminal_error = (_terminal_transcript_error(terminal)
                      if terminal is not None else None)
    if terminal_error:
        outcome.status = "result_error"
        outcome.error = terminal_error
        return outcome
    protocol_error = (_terminal_protocol_error(
        terminal, require_result_text=(
            require_result_text
            and str(terminal.get("subtype", ""))
            not in _TERMINAL_TASK_OUTCOMES))
        if terminal is not None else None)
    if protocol_error:
        outcome.status = "invalid_transcript"
        outcome.error = protocol_error
    return outcome


def _invoke_runner(runner, prompt: str, ws: Path, model: str,
                   max_turns: int, effort: str | None, *,
                   require_result_text: bool = False) -> RunnerOutcome:
    """Invoke old string runners and new structured runners uniformly."""
    started = time.monotonic()
    try:
        raw = runner(prompt, ws, model, max_turns, effort)
    except subprocess.TimeoutExpired as exc:
        return RunnerOutcome(stdout=_as_text(exc.stdout),
                             stderr=_as_text(exc.stderr), returncode=None,
                             duration_seconds=time.monotonic() - started,
                             timed_out=True, error=str(exc))
    except Exception as exc:  # runner infrastructure must become a result row
        return RunnerOutcome(stderr=str(exc), returncode=None,
                             duration_seconds=time.monotonic() - started,
                             status="error", error=repr(exc))
    elapsed = time.monotonic() - started
    if isinstance(raw, str):
        return _apply_terminal_status(
            RunnerOutcome(stdout=raw, duration_seconds=elapsed),
            require_terminal=True, require_result_text=require_result_text)
    if isinstance(raw, RunnerOutcome):
        # A custom structured runner can omit timing and let the harness fill it.
        if raw.duration_seconds <= 0:
            raw.duration_seconds = elapsed
        raw.stdout = _as_text(raw.stdout)
        raw.stderr = _as_text(raw.stderr)
        return _apply_terminal_status(
            raw, require_terminal=True,
            require_result_text=require_result_text)
    return RunnerOutcome(
        stderr=f"runner returned unsupported {type(raw).__name__}",
        returncode=None, duration_seconds=elapsed, status="error",
        error=f"unsupported runner result: {type(raw).__name__}")


def _run_acceptance(command: str, ws: Path) -> RunnerOutcome:
    return _run_process(command, ws, _ACCEPT_TIMEOUT_SECONDS, shell=True)


def _classify_acceptance(outcome: RunnerOutcome,
                         task: Task) -> tuple[str | None, str | None]:
    """Map a process exit onto pass/fail; undeclared exits are infrastructure."""
    if outcome.status not in ("ok", "nonzero"):
        return None, str(outcome.status)
    if outcome.returncode in task.accept_pass_codes:
        return "pass", None
    if outcome.returncode in task.accept_fail_codes:
        return "fail", None
    return None, f"unexpected_exit:{outcome.returncode}"


def _excerpt(value: str) -> tuple[str, bool]:
    if len(value) <= _DIAGNOSTIC_LIMIT:
        return value, False
    return value[-_DIAGNOSTIC_LIMIT:], True


def _persist_outcome(results_dir: Path, stem: str, outcome: RunnerOutcome,
                     stdout_suffix: str, stderr_suffix: str) -> dict:
    """Persist full process output and return compact row-safe diagnostics."""
    stdout_path = results_dir / f"{stem}{stdout_suffix}"
    stderr_path = results_dir / f"{stem}{stderr_suffix}"
    # Run IDs make these immutable evidence objects. Exclusive creation also
    # refuses a pre-planted symlink instead of following it.
    evidence: dict[str, object] = {}
    for stream, path, content in (("stdout", stdout_path, outcome.stdout),
                                  ("stderr", stderr_path, outcome.stderr)):
        encoded = content.encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        evidence[f"{stream}_artifact"] = path.name
        evidence[f"{stream}_size"] = len(encoded)
        evidence[f"{stream}_sha256"] = hashlib.sha256(encoded).hexdigest()
    _fsync_directory(results_dir)
    stdout_excerpt, stdout_truncated = _excerpt(outcome.stdout)
    stderr_excerpt, stderr_truncated = _excerpt(outcome.stderr)
    return {
        "status": outcome.status,
        "returncode": outcome.returncode,
        "duration_seconds": round(outcome.duration_seconds, 6),
        "timed_out": outcome.timed_out,
        "error": outcome.error,
        "stdout": stdout_excerpt,
        "stderr": stderr_excerpt,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        **evidence,
    }


def _fsync_directory(path: Path) -> None:
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _artifact_matches(results_dir: Path, info: dict,
                      stream: str) -> bool:
    name = info.get(f"{stream}_artifact")
    expected_size = info.get(f"{stream}_size")
    expected_hash = info.get(f"{stream}_sha256")
    if (not isinstance(name, str) or not name or Path(name).name != name
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool) or expected_size < 0
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        return False
    path = results_dir / name
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return False
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    finally:
        os.close(fd)
    return size == expected_size and digest.hexdigest() == expected_hash


def _resume_evidence_intact(row: dict, results_dir: Path) -> bool:
    if row.get("schema_version") != _RESULT_SCHEMA_VERSION:
        return False
    for section in ("runner", "acceptance"):
        info = row.get(section)
        if not isinstance(info, dict):
            return False
        if not all(_artifact_matches(results_dir, info, stream)
                   for stream in ("stdout", "stderr")):
            return False
    return True


def _embedded_map_info(ws: Path) -> dict:
    for target in _safe_context_targets(ws):
        digest = hologram.embedded_digest(target)
        if not digest:
            continue
        header = digest.split("\n", 1)[0]
        budget_match = re.search(
            r"· budget (\d+)(?: (?P<mode>[LA])(?P<detail>\d+))?", header)
        return {
            "effective_map_tokens": hologram.estimate_tokens(digest),
            "effective_map_detail": (int(budget_match.group("detail") or 0)
                                     if budget_match else 0),
            "effective_map_adaptive": bool(
                budget_match and budget_match.group("mode") == "A"),
            "effective_map_budget": (int(budget_match.group(1))
                                     if budget_match else None),
        }
    return {"effective_map_tokens": None, "effective_map_detail": None,
            "effective_map_adaptive": None, "effective_map_budget": None}


def _digest_of(ws: Path) -> str:
    return hologram.build_digest(ws)


def _corpus_revision(corpus: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(corpus), "rev-parse", "HEAD"],
        capture_output=True, text=True)
    revision = result.stdout.strip()
    if result.returncode != 0 or not revision:
        raise SystemExit(f"benchmark corpus has no readable HEAD: {corpus}")
    return revision


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runtime_provenance(runner_mode: str, *,
                        runner_version: str | None = None) -> dict:
    return {
        "runner": ("builtin-dry-run" if runner_mode == "dry-run"
                   else "claude-cli" if runner_mode == "unsafe-host"
                   else runner_mode),
        "runner_version": runner_version or "not-preflighted",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "schedule_revision": _SCHEDULE_REVISION,
    }


def _preflight_runner(runner_mode: str) -> dict:
    """Fail before matrix execution when the real runner is unavailable."""
    if runner_mode == "dry-run":
        return _runtime_provenance(
            runner_mode, runner_version=f"builtin-{_TOOL_REVISION}")
    outcome = _run_process(["claude", "--version"], Path.cwd(), 15)
    if not outcome.ok:
        detail = (outcome.stderr or outcome.error or outcome.status or
                  "unknown error")
        raise SystemExit(f"runner preflight failed: {detail}")
    version = re.sub(r"\s+", " ", outcome.stdout.strip())[:256]
    if not version:
        raise SystemExit("runner preflight failed: empty version response")
    return _runtime_provenance(runner_mode, runner_version=version)


def _experiment_spec(config: Config, runner_mode: str,
                     host_execution_acknowledged: bool,
                     order_seed: int | None = None, *,
                     conditions: list[str] | None = None,
                     reps: int = 1,
                     tasks: list[Task] | None = None,
                     runner_provenance: dict | None = None) -> dict:
    """Immutable experiment identity and its reproducible ordering seed."""
    base = {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "hologram_version": hologram.__version__,
        "tool_revision": _TOOL_REVISION,
        "corpus_revision": _corpus_revision(config.corpus),
        "config_revision": config.revision,
        "model": config.model,
        "max_turns": config.max_turns,
        "effort": config.effort,
        "lang": config.lang,
        "budget": config.budget,
        "runner_mode": runner_mode,
        "runner_provenance": (runner_provenance
                              or _runtime_provenance(runner_mode)),
        "host_execution_acknowledged": host_execution_acknowledged,
        # The matrix affects condition order and carryover, so it is part of
        # exact compatibility even when two invocations share an individual
        # task/condition/rep coordinate.
        "conditions": list(conditions or ()),
        "reps": reps,
        "task_selection": [task.id for task in (tasks or config.tasks)],
    }
    if order_seed is None:
        # Reproducible by default, while the hashed base makes unrelated
        # experiments start from unrelated permutations.
        order_seed = int(_identity("seed", base).split("-", 1)[1][:16], 16)
    if not isinstance(order_seed, int) or isinstance(order_seed, bool):
        raise SystemExit("--seed must be an integer")
    experiment_id = _identity("experiment", {**base,
                                                "order_seed": order_seed})
    return {**base, "order_seed": order_seed,
            "experiment_id": experiment_id}


def _cell_spec(experiment_id: str, task: Task, condition: str, rep: int,
               max_turns: int, effort: str | None) -> dict:
    common = {
        "experiment_id": experiment_id,
        "task": task.id,
        "task_revision": _task_revision(task),
        "judge_config_revision": _judge_config_revision(task),
        "rep": rep,
        "max_turns": max_turns,
        "effort": effort,
    }
    return {
        **common,
        "pair_id": _identity("pair", common),
        "cell_id": _identity("cell", {**common, "condition": condition}),
    }


def _counterbalanced_schedule(tasks: list[Task], conditions: list[str],
                              reps: int, seed: int) -> list[dict]:
    """Rotate a seeded condition permutation across task/rep blocks."""
    base = list(conditions)
    random.Random(seed).shuffle(base)
    schedule: list[dict] = []
    block = 0
    for task in tasks:
        for rep in range(reps):
            offset = block % len(base)
            order = base[offset:] + base[:offset]
            for order_index, condition in enumerate(order):
                schedule.append({"task": task, "condition": condition,
                                 "rep": rep, "condition_order": list(order),
                                 "order_index": order_index,
                                 "block_index": block})
            block += 1
    return schedule


def _infra_reason(row: dict) -> str | None:
    legacy = row.get("schema_version") in (None, 1)
    runner_status = row.get("runner_status", "ok" if legacy else "missing")
    if runner_status != "ok":
        return f"runner:{runner_status}"
    if row.get("schema_version") == _RESULT_SCHEMA_VERSION:
        if (row.get("runner_mode") == "dry-run"
                and row.get("acceptance_status") == "skipped_dry_run"):
            return None
        verdict = row.get("acceptance_verdict")
        if verdict not in ("pass", "fail"):
            reason = (row.get("acceptance_infra_reason")
                      or row.get("acceptance_status") or "missing")
            return f"acceptance:{reason}"
        if (row.get("condition") == "AR"
                and row.get("runner_mode") != "dry-run"):
            review = row.get("review_findings")
            status = (review.get("status")
                      if isinstance(review, dict) else "missing")
            if status != "ok":
                return f"review:{status}"
        return None
    acceptance_status = row.get(
        "acceptance_status", "ok" if legacy else "missing")
    if acceptance_status not in ("ok", "nonzero"):
        return f"acceptance:{acceptance_status}"
    return None


def _terminal_cell(row: dict, results_dir: Path | None = None) -> bool:
    """A fully observed cell, including a valid command rejection."""
    if _infra_reason(row) is not None:
        return False
    if (row.get("schema_version") == _RESULT_SCHEMA_VERSION
            and row.get("condition") == "AR"
            and row.get("runner_mode") != "dry-run"
            and (not isinstance(row.get("review_findings"), dict)
                 or row["review_findings"].get("status") != "ok")):
        # A transient hook/capture/final-review failure leaves the task verdict
        # intact, but the AR experiment cell is not fully observed and must be
        # retried by --resume.
        return False
    if results_dir is not None and row.get("schema_version") == _RESULT_SCHEMA_VERSION:
        return _resume_evidence_intact(row, results_dir)
    return True


def _resumable_block(rows: list[dict],
                     results_dir: Path | None = None) -> bool:
    """A treatment/control block must resume as one same-wave unit."""
    if not rows or not all(_terminal_cell(row, results_dir) for row in rows):
        return False
    schema3 = any(row.get("schema_version") == _RESULT_SCHEMA_VERSION
                  for row in rows)
    waves = [row.get("wave_id") for row in rows]
    if schema3 and (any(not wave for wave in waves) or len(set(waves)) != 1):
        return False
    models = [row.get("resolved_model") for row in rows]
    if any(model is not None for model in models) and len(set(models)) != 1:
        return False
    return True


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"invalid JSON in {path} line {line_number}: {exc}") from None
        if not isinstance(row, dict):
            raise SystemExit(f"invalid result in {path} line {line_number}: "
                             "expected an object")
        rows.append(row)
    return rows


def _atomic_replace(path: Path, content: bytes) -> None:
    """Replace a file atomically after syncing content and its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _append_jsonl_atomic(path: Path, row: dict) -> None:
    """Crash-safe, process-safe append via a locked atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        previous = path.read_bytes() if path.exists() else b""
        if previous and not previous.endswith(b"\n"):
            raise RuntimeError(f"refusing to append to partial JSONL file: {path}")
        line = json.dumps(row, sort_keys=True, separators=(",", ":"))
        _atomic_replace(path, previous + line.encode() + b"\n")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _empty_review_measurement(status: str, *,
                              hook_events: int | None = None) -> dict:
    """Stable result shape for unavailable or inapplicable review evidence."""
    return {
        "schema_version": _REVIEW_CAPTURE_SCHEMA_VERSION,
        "status": status,
        "hook_events": hook_events,
        "baseline_count": None,
        "final_count": None,
        "resolved_count": None,
        "persisting_count": None,
        "new_final_count": None,
        "items": [],
        "new_final": [],
    }


def _install_review_capture(ws: Path, capture_path: Path,
                            lang: list[str] | None = None) -> None:
    """Replace only AR's review command with the single-pass probe.

    The shipped build command, human review text, and fail-open hook behavior
    remain unchanged. Installation happens after the benchmark setup commit so
    that setup itself cannot enter the measurement ledger.
    """
    hook = ws / ".git" / "hooks" / "post-commit"
    lines = hook.read_text().splitlines()
    candidates = [
        index for index, line in enumerate(lines)
        if " review HEAD~1 " in line
        and line.endswith(" || true # hologram:managed")
    ]
    if len(candidates) != 1:
        raise RuntimeError("AR post-commit review hook is not instrumentable")
    index = candidates[0]
    build, separator, _review = lines[index].partition(" && ")
    if not separator:
        raise RuntimeError("AR post-commit review hook has no build phase")
    command = [
        sys.executable, str(Path(__file__).resolve()), "_review-hook",
        "HEAD~1", "--root", str(ws), "--capture", str(capture_path),
    ]
    for language in lang or ():
        command.extend(["--lang", language])
    lines[index] = (
        f"{build} && {shlex.join(command)}"
        " || true # hologram:managed"
    )
    hook.write_text("\n".join(lines) + "\n")


def _run_review_hook(root: Path, rev: str, capture_path: Path,
                     lang: list[str] | None = None) -> int:
    """Render the normal hook report and privately capture its stable IDs."""
    from hologram.review import render_report, run_review_findings

    findings = run_review_findings(
        root, rev, langs=set(lang) if lang else None)
    report_text = render_report(findings, rev)
    if report_text:
        sys.stdout.write(report_text)
        sys.stdout.flush()
    try:
        from hologram.gather import _git_env
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, env=_git_env()).stdout.strip()
        capture = {
            "schema_version": _REVIEW_CAPTURE_SCHEMA_VERSION,
            # Commit identity is ephemeral coverage metadata. It never enters
            # the result row or report, and lets the parent reject stale hook
            # events left behind by resets/rebases.
            "head": head,
            "findings": [
                {"id": finding.id, "check": finding.check}
                for finding in sorted(findings,
                                      key=lambda finding: (finding.check,
                                                           finding.id))
            ],
        }
        _append_jsonl_atomic(capture_path, capture)
    except Exception:
        # Review remains informational. A missing capture becomes an explicit
        # incomplete measurement in the parent harness, never a hook failure.
        pass
    return 0


def _read_review_capture(path: Path) -> dict[str, dict[str, str]]:
    """Validate the private hook ledger without echoing captured content."""
    if not path.exists():
        return {}
    events: dict[str, dict[str, str]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError("invalid review capture") from None
        if (not isinstance(record, dict)
                or set(record) != {"schema_version", "head", "findings"}
                or record.get("schema_version")
                != _REVIEW_CAPTURE_SCHEMA_VERSION
                or not isinstance(record.get("head"), str)
                or not re.fullmatch(r"[0-9a-f]{40,64}", record["head"])
                or not isinstance(record.get("findings"), list)):
            raise ValueError("invalid review capture")
        if record["head"] in events:
            raise ValueError("duplicate review capture")
        findings: dict[str, str] = {}
        for item in record["findings"]:
            if (not isinstance(item, dict)
                    or set(item) != {"id", "check"}
                    or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"hr[1-9][0-9]*-[0-9a-f]{20}",
                                        item["id"])
                    or item.get("check") not in {
                        "dup", "recover", "dead", "orphan", "api", "place",
                    }):
                raise ValueError("invalid review capture")
            previous = findings.setdefault(item["id"], item["check"])
            if previous != item["check"]:
                raise ValueError("inconsistent review capture")
        events[record["head"]] = findings
    return events


def _agent_commit_ids(ws: Path, setup_sha: str) -> list[str]:
    outcome = subprocess.run(
        ["git", "-C", str(ws), "rev-list", "--reverse",
         f"{setup_sha}..HEAD"],
        capture_output=True, text=True)
    if outcome.returncode != 0:
        raise RuntimeError("cannot verify review hook coverage")
    commits = [line.strip() for line in outcome.stdout.splitlines()
               if line.strip()]
    if any(not re.fullmatch(r"[0-9a-f]{40,64}", commit)
           for commit in commits):
        raise RuntimeError("cannot verify review hook coverage")
    return commits


def _review_final_state(condition: str, ws: Path, setup_sha: str,
                        capture_path: Path | None,
                        lang: list[str] | None = None, *,
                        intent_ok: bool = True) -> dict:
    """Compare hook-emitted IDs with the cumulative final working tree."""
    if condition != "AR":
        return _empty_review_measurement("not_applicable")
    if capture_path is None:
        return _empty_review_measurement("incomplete")
    if not intent_ok:
        return _empty_review_measurement("error")
    try:
        events = _read_review_capture(capture_path)
        commits = _agent_commit_ids(ws, setup_sha)
        missing = [commit for commit in commits if commit not in events]
        if missing:
            return _empty_review_measurement(
                "incomplete", hook_events=len(commits) - len(missing))
        baseline: dict[str, str] = {}
        for commit in commits:
            for finding_id, check in events[commit].items():
                previous = baseline.setdefault(finding_id, check)
                if previous != check:
                    raise ValueError("inconsistent review capture")
        hook_events = len(commits)
    except (OSError, ValueError, RuntimeError):
        return _empty_review_measurement("incomplete")

    try:
        from hologram.review import run_review_findings
        final_findings = run_review_findings(
            ws, setup_sha, langs=set(lang) if lang else None)
        final: dict[str, str] = {}
        for finding in final_findings:
            previous = final.setdefault(finding.id, finding.check)
            if previous != finding.check:
                raise ValueError("inconsistent final review IDs")
    except (Exception, SystemExit):
        return _empty_review_measurement("error", hook_events=hook_events)

    baseline_ids = set(baseline)
    final_ids = set(final)
    resolved = baseline_ids - final_ids
    persisting = baseline_ids & final_ids
    new_final = final_ids - baseline_ids
    items = [
        {"id": finding_id, "check": baseline[finding_id],
         "state": "resolved" if finding_id in resolved else "persisting"}
        for finding_id in sorted(baseline_ids,
                                 key=lambda item: (baseline[item], item))
    ]
    new_items = [
        {"id": finding_id, "check": final[finding_id]}
        for finding_id in sorted(new_final,
                                 key=lambda item: (final[item], item))
    ]
    return {
        "schema_version": _REVIEW_CAPTURE_SCHEMA_VERSION,
        "status": "ok",
        "hook_events": hook_events,
        "baseline_count": len(baseline_ids),
        "final_count": len(final_ids),
        "resolved_count": len(resolved),
        "persisting_count": len(persisting),
        "new_final_count": len(new_final),
        "items": items,
        "new_final": new_items,
    }


def _resolved_model(text: str) -> str | None:
    """Best-effort resolved provider model from the transcript."""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for source in (event, event.get("message") or {}):
            model = source.get("model") if isinstance(source, dict) else None
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


def _semantic_result(task: Task, *, runner_ok: bool,
                     acceptance_verdict: str | None,
                     answer_ok: bool | None, scope_ok: bool | None,
                     runner_mode: str) -> str:
    if runner_mode == "dry-run":
        return "not_judged"
    if not runner_ok or acceptance_verdict not in ("pass", "fail"):
        return "infra_error"
    if task.manual_only:
        return "pending_manual"
    if not task.semantic_judge:
        return "not_judged"
    passed = acceptance_verdict == "pass"
    if answer_ok is not None:
        passed = passed and answer_ok
    if scope_ok is not None:
        passed = passed and scope_ok
    return "pass" if passed else "fail"


def run_one(corpus: Path, task: Task, condition: str, rep: int,
            results_dir: Path, model: str, max_turns: int,
            runner=claude_runner, lang: list[str] | None = None,
            budget: int | None = None,
            effort: str | None = None,
            config_revision: str | None = None, *,
            experiment_id: str | None = None,
            cell_id: str | None = None, pair_id: str | None = None,
            order_seed: int | None = None,
            condition_order: list[str] | None = None,
            order_index: int | None = None,
            runner_mode: str = "custom",
            host_execution_acknowledged: bool = False,
            expected_corpus_revision: str | None = None,
            experiment_conditions: list[str] | None = None,
            experiment_reps: int | None = None,
            experiment_tasks: list[str] | None = None,
            execute_acceptance: bool = True,
            runner_provenance: dict | None = None,
            wave_id: str | None = None,
            wave_started_at: str | None = None,
            execution_index: int | None = None,
            block_index: int | None = None) -> dict:
    if not _SAFE_TASK_ID.fullmatch(task.id):
        raise ValueError(f"unsafe task id {task.id!r}")
    if condition not in _CONDITIONS:
        raise ValueError(f"unknown benchmark condition {condition!r}")
    if rep < 0 or max_turns <= 0:
        raise ValueError("rep must be non-negative and max_turns positive")
    if budget is not None and budget <= 0:
        raise ValueError("budget must be positive when present")
    if runner is claude_runner and not host_execution_acknowledged:
        raise ValueError(
            "real benchmark sessions require an explicit unsafe-host "
            "acknowledgement")
    _validate_config(
        Config(corpus=corpus, tasks=[task], model=model,
               max_turns=max_turns, lang=list(lang or ()), budget=budget,
               effort=effort),
        "benchmark run")
    config_revision = config_revision or _adhoc_config_revision(task)
    runner_provenance = (runner_provenance
                         or _runtime_provenance(runner_mode))
    if experiment_id is None:
        direct_config = Config(
            corpus=corpus, tasks=[task], model=model, max_turns=max_turns,
            lang=list(lang or ()), budget=budget, effort=effort,
            revision=config_revision)
        experiment = _experiment_spec(
            direct_config, runner_mode, host_execution_acknowledged, order_seed,
            conditions=[condition], reps=rep + 1, tasks=[task],
            runner_provenance=runner_provenance)
        experiment_id = experiment["experiment_id"]
        order_seed = experiment["order_seed"]
    expected_cell = _cell_spec(experiment_id, task, condition, rep,
                               max_turns, effort)
    if cell_id is not None and cell_id != expected_cell["cell_id"]:
        raise ValueError("cell_id does not match the immutable cell inputs")
    if pair_id is not None and pair_id != expected_cell["pair_id"]:
        raise ValueError("pair_id does not match the immutable pair inputs")
    cell_id = expected_cell["cell_id"]
    pair_id = expected_cell["pair_id"]
    condition_order = list(condition_order or [condition])
    experiment_conditions = list(experiment_conditions or [condition])
    experiment_reps = experiment_reps if experiment_reps is not None else rep + 1
    experiment_tasks = list(experiment_tasks or [task.id])
    if (len(set(experiment_conditions)) != len(experiment_conditions)
            or set(condition_order) != set(experiment_conditions)):
        raise ValueError("condition order is inconsistent with the experiment")
    if experiment_reps <= rep or experiment_reps <= 0:
        raise ValueError("repetition is outside the experiment matrix")
    if (task.id not in experiment_tasks
            or len(set(experiment_tasks)) != len(experiment_tasks)):
        raise ValueError("task is outside the experiment matrix")
    if (condition not in condition_order or order_index is None
            and len(condition_order) != 1):
        raise ValueError("condition order does not describe this cell")
    order_index = (condition_order.index(condition)
                   if order_index is None else order_index)
    if (order_index < 0 or order_index >= len(condition_order)
            or condition_order[order_index] != condition):
        raise ValueError("condition order index does not match condition")
    results_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    wave_id = wave_id or f"wave-{uuid.uuid4().hex}"
    wave_started_at = wave_started_at or _utc_now()
    attempt_started_at = _utc_now()
    ws = Path(tempfile.gettempdir()) / f"hologram-bench-{run_id}"
    review_capture_dir: Path | None = None
    review_capture_path: Path | None = None
    try:
        make_workspace(corpus, ws, condition, lang=lang, budget=budget)
        # The agent can mutate arbitrary refs in its clone. Capture the judged
        # baseline before execution and never trust the ref again in this run.
        setup_sha = _setup_sha(ws)
        if condition == "AR" and runner_mode != "dry-run":
            try:
                review_capture_dir = Path(tempfile.mkdtemp(
                    prefix=f"hologram-review-capture-{run_id}-"))
                review_capture_path = review_capture_dir / "events.jsonl"
                _install_review_capture(
                    ws, review_capture_path, lang=lang)
            except Exception as exc:
                if review_capture_dir is not None:
                    shutil.rmtree(review_capture_dir, ignore_errors=True)
                review_capture_dir = None
                review_capture_path = None
                # AR without its identity capture is not the planned
                # experiment. Fail before invoking a paid provider rather than
                # spending tokens on a cell that can never be resumed or
                # matched as complete.
                raise RuntimeError(
                    "AR review capture setup failed before runner execution"
                ) from exc
        corpus_revision = subprocess.run(
            ["git", "-C", str(ws), "rev-parse", f"{setup_sha}^"],
            capture_output=True, text=True).stdout.strip()
        if (expected_corpus_revision is not None
                and corpus_revision != expected_corpus_revision):
            raise RuntimeError(
                "corpus HEAD changed after experiment identity was created")
        before = _digest_of(ws)
        map_info = _embedded_map_info(ws)
        outcome = _invoke_runner(
            runner, task.prompt, ws, model, max_turns, effort,
            require_result_text=bool(task.expect_answer or task.manual_only))
        transcript = outcome.stdout
        stem = f"{task.id}-{condition}-{rep}-{run_id}"
        runner_info = _persist_outcome(results_dir, stem, outcome,
                                       ".jsonl", ".runner.stderr")
        # intent-to-add so brand-new files show up in `git diff`-based acceptance
        intent = subprocess.run(["git", "-C", str(ws), "add", "-N", "."],
                                capture_output=True, text=True)
        # Scan only after intent-to-add: scan_files uses git ls-files, so doing
        # this later would make all newly created source files invisible.
        after = _digest_of(ws)
        verdict = judge_reuse(before, after, task.expect_reuse)
        if runner_mode == "dry-run":
            review_findings = _empty_review_measurement("not_applicable")
        else:
            try:
                review_findings = _review_final_state(
                    condition, ws, setup_sha, review_capture_path, lang=lang,
                    intent_ok=intent.returncode == 0)
            except (Exception, SystemExit):
                # Structured review is an auxiliary measurement. It must never
                # change whether the task runner or configured judge succeeded.
                review_findings = _empty_review_measurement("error")
        if execute_acceptance:
            command = task.accept_cmd.format(ws=ws, sha=setup_sha)
            acceptance = _run_acceptance(command, ws)
        else:
            acceptance = RunnerOutcome(
                returncode=None, status="skipped_dry_run")
        if intent.returncode != 0:
            acceptance.status = "error"
            acceptance.error = "git add -N failed before acceptance"
            acceptance.stderr = (intent.stderr + "\n" + acceptance.stderr).strip()
        acceptance_verdict, acceptance_infra_reason = (
            _classify_acceptance(acceptance, task)
            if execute_acceptance else (
                None, "git_add_failed" if intent.returncode != 0 else None))
        acceptance_info = _persist_outcome(
            results_dir, stem, acceptance, ".accept.stdout", ".accept.stderr")
        accepted = outcome.ok and acceptance_verdict == "pass"
        scope_ok = judge_scope(ws, task.expect_in_new_code,
                               task.scope_in_tests, setup_sha)
        metrics = parse_transcript(transcript)
        result_text = metrics.pop("result_text")
        answer_ok = (outcome.ok and all(
                         re.search(rx, result_text, re.I | re.S)
                         for rx in task.expect_answer)
                     if task.expect_answer else None)
        semantic_verdict = _semantic_result(
            task, runner_ok=outcome.ok,
            acceptance_verdict=acceptance_verdict,
            answer_ok=answer_ok, scope_ok=scope_ok,
            runner_mode=runner_mode)
        return {"run_id": run_id,
                "experiment_id": experiment_id, "cell_id": cell_id,
                "pair_id": pair_id,
                "task_revision": expected_cell["task_revision"],
                "judge_config_revision": expected_cell["judge_config_revision"],
                "manual_only": task.manual_only,
                "semantic_judge": task.semantic_judge,
                "accept_pass_codes": task.accept_pass_codes,
                "accept_fail_codes": task.accept_fail_codes,
                "task": task.id, "kind": task.kind,
                "condition": condition,
                "rep": rep, "model": model, "effort": effort,
                "max_turns": max_turns,
                "runner_mode": runner_mode,
                "runner_provenance": runner_provenance,
                "resolved_model": _resolved_model(transcript),
                "host_execution_acknowledged": host_execution_acknowledged,
                "wave_id": wave_id,
                "wave_started_at": wave_started_at,
                "attempt_started_at": attempt_started_at,
                "execution_index": execution_index,
                "block_index": block_index,
                "experiment_conditions": experiment_conditions,
                "experiment_reps": experiment_reps,
                "experiment_tasks": experiment_tasks,
                "order_seed": order_seed,
                "condition_order": condition_order,
                "order_index": order_index,
                "corpus_revision": corpus_revision,
                "config_revision": config_revision,
                "schema_version": _RESULT_SCHEMA_VERSION,
                "hologram_version": hologram.__version__,
                "tool_revision": _TOOL_REVISION,
                "requested_budget": budget, **map_info,
                # `accepted` is retained for schema compatibility; the explicit
                # name prevents consumers from mistaking a command pass for a
                # semantic correctness verdict.
                "accepted": accepted, "accept_cmd_ok": accepted,
                "acceptance_verdict": acceptance_verdict,
                "acceptance_infra_reason": acceptance_infra_reason,
                "semantic_verdict": semantic_verdict,
                "answer_ok": answer_ok,
                "scope_ok": scope_ok,
                "reuse_judged": bool(task.expect_reuse),
                "reused": verdict["reused"], "duplicated": verdict["duplicated"],
                "new_lines": len(verdict["new_lines"]),
                "runner_status": outcome.status,
                "runner_returncode": outcome.returncode,
                "runner_timed_out": outcome.timed_out,
                "runner_duration_seconds": outcome.duration_seconds,
                "acceptance_status": acceptance.status,
                "acceptance_returncode": acceptance.returncode,
                "acceptance_timed_out": acceptance.timed_out,
                "acceptance_duration_seconds": acceptance.duration_seconds,
                "runner": runner_info, "acceptance": acceptance_info,
                "review_findings": review_findings,
                **metrics}
    finally:
        if ws.exists():
            drop_workspace(corpus, ws)
        if review_capture_dir is not None:
            shutil.rmtree(review_capture_dir, ignore_errors=True)


def _legacy_cell_key(row: dict) -> tuple:
    return (row.get("task"), row.get("condition"), row.get("rep"),
            row.get("model"), row.get("effort"), row.get("max_turns"),
            row.get("requested_budget"), row.get("corpus_revision"),
            row.get("config_revision"), row.get("schema_version"),
            row.get("hologram_version"), row.get("tool_revision"),
            row.get("judge_config_revision"), row.get("runner_mode"))


def _latest_cells(rows: list[dict]) -> list[dict]:
    cells: dict[object, dict] = {}
    for row in rows:
        key: object = row.get("cell_id") or _legacy_cell_key(row)
        cells[key] = row
    return list(cells.values())


def _group_key(row: dict, *, condition: bool = True) -> tuple:
    prefix = ((row.get("condition"),) if condition else ())
    return prefix + (
        row.get("kind"), row.get("model") or "—", row.get("effort") or "—",
        str(row.get("requested_budget") or "full"),
        str(row.get("corpus_revision") or "—")[:8],
        str(row.get("config_revision") or "—")[:12],
        (f"{row.get('hologram_version')}/{row.get('tool_revision')}"
         f"/s{row.get('schema_version')}"
         if row.get("tool_revision") else "legacy"),
        row.get("runner_mode") or "legacy",
        row.get("experiment_id") or "legacy",
    )


def _numbers(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows
            if isinstance(row.get(key), (int, float))
            and not isinstance(row.get(key), bool)]


def _median_mad(values: list[float], *, decimals: int = 1,
                signed: bool = False) -> str:
    if not values:
        return "—"
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    sign = "+" if signed and median > 0 else ""
    if decimals == 0:
        return f"{sign}{median:,.0f} ± {mad:,.0f}"
    return f"{sign}{median:,.{decimals}f} ± {mad:,.{decimals}f}"


def _median_mad_n(values: list[float], *, decimals: int = 1,
                  signed: bool = False) -> str:
    summary = _median_mad(values, decimals=decimals, signed=signed)
    return f"{summary} (n={len(values)})" if values else summary


def _percent_cell(values: list[bool]) -> tuple[str, int]:
    if not values:
        return "—", 0
    return f"{100 * sum(values) / len(values):.0f}%", len(values)


def _automatic_acceptance_applicable(row: dict) -> bool:
    if row.get("manual_only") or row.get("runner_mode") == "dry-run":
        return False
    if row.get("schema_version") == _RESULT_SCHEMA_VERSION:
        return row.get("acceptance_verdict") in ("pass", "fail")
    return isinstance(row.get("accepted"), bool)


def _reuse_applicable(row: dict) -> bool:
    if row.get("manual_only"):
        return False
    if "reuse_judged" in row:
        return row.get("reuse_judged") is True
    return row.get("kind") == "reuse"


def _short_identity(value: object) -> str:
    text = str(value)
    return text.split("-", 1)[-1][:12] if "-" in text else text[:12]


def _anonymous_task_labels(rows: list[dict]) -> dict[str, str]:
    """Deterministic opaque labels for reports that must not expose task IDs."""
    names = sorted({str(row.get("task")) for row in rows
                    if row.get("task") is not None})
    return {name: f"task-{index:03d}"
            for index, name in enumerate(names, 1)}


def _task_label(row: dict, anon: bool,
                labels: dict[str, str]) -> str:
    raw = str(row.get("task"))
    return labels.get(raw, "task-unknown") if anon else raw


def _legacy_pair_key(row: dict) -> tuple:
    cell = _legacy_cell_key(row)
    return cell[:1] + cell[2:]


def _matched_section(rows: list[dict], *, anon: bool = False,
                     task_labels: dict[str, str] | None = None) -> list[str]:
    task_labels = task_labels or {}
    pair_cells: dict[object, dict[str, dict]] = {}
    for row in rows:
        if row.get("condition") not in _CONDITIONS:
            continue
        key: object = row.get("pair_id") or _legacy_pair_key(row)
        pair_cells.setdefault(key, {})[row["condition"]] = row
    if not pair_cells:
        return ["Matched treatment−B deltas:", "",
                "No treatment/control cells recorded.", ""]

    grouped: dict[tuple, list[tuple[dict, dict]]] = {}
    incomplete: list[tuple[dict, str, str]] = []
    for conditions in pair_cells.values():
        representative = conditions.get("A") or conditions.get("B")
        representative = representative or next(iter(conditions.values()))
        assert representative is not None
        planned = representative.get("experiment_conditions")
        has_planned_conditions = isinstance(planned, list)
        treatments = ([condition for condition in planned
                       if condition in _CONDITIONS and condition != "B"]
                      if has_planned_conditions else
                      [condition for condition in conditions if condition != "B"])
        # Legacy B-only rows came from an implicit A/B matrix.
        if not treatments and "B" in conditions and not has_planned_conditions:
            treatments = ["A"]
        for treatment in treatments:
            treatment_row = conditions.get(treatment)
            control = conditions.get("B")
            comparison = f"{treatment}−B"
            reasons = []
            if treatment_row is None:
                reasons.append(f"missing {treatment}")
            elif _infra_reason(treatment_row):
                reasons.append(
                    f"{treatment} {_infra_reason(treatment_row)}")
            if control is None:
                reasons.append("missing B")
            elif _infra_reason(control):
                reasons.append("B " + str(_infra_reason(control)))
            if treatment_row is not None and control is not None and not reasons:
                t_wave, b_wave = (treatment_row.get("wave_id"),
                                  control.get("wave_id"))
                if t_wave and b_wave and t_wave != b_wave:
                    reasons.append("different execution waves")
                t_model = treatment_row.get("resolved_model")
                b_model = control.get("resolved_model")
                if (t_model or b_model) and t_model != b_model:
                    reasons.append("different or missing resolved model")
            pair_representative = treatment_row or control or representative
            if reasons:
                incomplete.append(
                    (pair_representative, comparison, ", ".join(reasons)))
                continue
            key = (comparison,
                   *_group_key(pair_representative, condition=False))
            grouped.setdefault(key, []).append(
                (treatment_row, control))  # type: ignore[arg-type]

    lines = ["Matched treatment−B deltas "
             "(same immutable task and repetition):", "",
             "| comparison | kind | model | effort | budget | revision | "
             "config | tool | runner | experiment | pairs | accept cmd Δpp | semantic Δpp | "
             "answer match Δpp | scope match Δpp | duplication Δpp | "
             "reads Δ | files Δ | searches Δ | turns Δ | fresh input Δ | "
             "cache-created Δ | cache-read Δ | total input Δ | output Δ |",
             "|" + "|".join(["---"] * 25) + "|"]

    def numeric_deltas(pairs: list[tuple[dict, dict]], key: str) -> list[float]:
        return [float(a[key]) - float(b[key]) for a, b in pairs
                if isinstance(a.get(key), (int, float))
                and not isinstance(a.get(key), bool)
                and isinstance(b.get(key), (int, float))
                and not isinstance(b.get(key), bool)]

    def boolean_deltas(pairs: list[tuple[dict, dict]], key: str,
                       *, list_truth: bool = False,
                       applicable=None) -> list[float]:
        deltas = []
        for a, b in pairs:
            if applicable is not None and not (applicable(a) and applicable(b)):
                continue
            av = bool(a.get(key)) if list_truth else a.get(key)
            bv = bool(b.get(key)) if list_truth else b.get(key)
            if isinstance(av, bool) and isinstance(bv, bool):
                deltas.append(100.0 * (int(av) - int(bv)))
        return deltas

    def semantic_deltas(pairs: list[tuple[dict, dict]]) -> list[float]:
        values = []
        for a, b in pairs:
            av, bv = a.get("semantic_verdict"), b.get("semantic_verdict")
            if av in ("pass", "fail") and bv in ("pass", "fail"):
                values.append(100.0 * (int(av == "pass") - int(bv == "pass")))
        return values

    for key in sorted(grouped, key=lambda item: tuple(str(v) for v in item)):
        (comparison, kind, model, effort, budget, revision, config, tool,
         runner_mode, experiment) = key
        pairs = grouped[key]
        accept_deltas = boolean_deltas(
            pairs, "accepted", applicable=_automatic_acceptance_applicable)
        answer_deltas = boolean_deltas(
            pairs, "answer_ok",
            applicable=lambda row: not row.get("manual_only"))
        scope_deltas = boolean_deltas(
            pairs, "scope_ok",
            applicable=lambda row: not row.get("manual_only"))
        duplication_deltas = boolean_deltas(
            pairs, "duplicated", list_truth=True,
            applicable=_reuse_applicable)
        lines.append(
            f"| {comparison} | {kind} | {model} | {effort} | {budget} | "
            f"{revision} | {config} | {tool} | {runner_mode} | "
            f"{_short_identity(experiment)} | {len(pairs)} | "
            f"{_median_mad_n(accept_deltas, decimals=0, signed=True)} | "
            f"{_median_mad_n(semantic_deltas(pairs), decimals=0, signed=True)} | "
            f"{_median_mad_n(answer_deltas, decimals=0, signed=True)} | "
            f"{_median_mad_n(scope_deltas, decimals=0, signed=True)} | "
            f"{_median_mad_n(duplication_deltas, decimals=0, signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'reads'), signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'files_read'), signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'searches'), signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'turns'), signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'tokens_in_fresh'), decimals=0, signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'tokens_in_cache_created'), decimals=0, signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'tokens_in_cache_read'), decimals=0, signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'tokens_in'), decimals=0, signed=True)} | "
            f"{_median_mad_n(numeric_deltas(pairs, 'tokens_out'), decimals=0, signed=True)} |")
    if not grouped:
        lines.append("| " + " | ".join(["—"] * 10 + ["0"]
                                         + ["—"] * 14) + " |")
    lines.append("")
    lines.append(f"Incomplete treatment/B pairs: {len(incomplete)}")
    for row, comparison, reason in sorted(
            incomplete,
            key=lambda item: (str(item[0].get("task")),
                              int(item[0].get("rep", 0)), item[1])):
        lines.append(
            f"- {_task_label(row, anon, task_labels)} "
            f"[rep{row.get('rep')}] "
            f"{comparison}: {reason}")
    lines.append("")
    return lines


def _eligible_review_measurement(row: dict) -> dict | None:
    review = row.get("review_findings")
    if (row.get("condition") != "AR"
            or row.get("runner_mode") == "dry-run"
            or not isinstance(review, dict) or review.get("status") != "ok"
            or _infra_reason(row) is not None):
        return None
    keys = ("hook_events", "baseline_count", "final_count",
            "resolved_count", "persisting_count", "new_final_count")
    if any(not isinstance(review.get(key), int)
           or isinstance(review.get(key), bool) or review[key] < 0
           for key in keys):
        return None
    if (review["baseline_count"]
            != review["resolved_count"] + review["persisting_count"]
            or review["final_count"]
            != review["persisting_count"] + review["new_final_count"]):
        return None
    return review


def _review_section(rows: list[dict]) -> list[str]:
    candidates = [row for row in rows
                  if row.get("condition") == "AR"
                  and row.get("runner_mode") != "dry-run"]
    dry_count = sum(row.get("condition") == "AR"
                    and row.get("runner_mode") == "dry-run" for row in rows)
    eligible = [(row, review) for row in candidates
                if (review := _eligible_review_measurement(row)) is not None]
    lines = ["Structured review final-state counts "
             "(paid AR cells; aggregate counts use eligible status=ok runs only):",
             ""]
    statuses = {status: 0 for status in
                ("ok", "incomplete", "error", "not_run", "not_applicable")}
    other_status = 0
    for row in candidates:
        review = row.get("review_findings")
        status = review.get("status") if isinstance(review, dict) else None
        if status in statuses:
            statuses[status] += 1
        else:
            other_status += 1
    lines.append(
        "Measurement coverage: "
        f"cells={len(candidates)}, eligible={len(eligible)}, "
        f"ok={statuses['ok']}, incomplete={statuses['incomplete']}, "
        f"error={statuses['error']}, not_run={statuses['not_run']}, "
        f"not_applicable={statuses['not_applicable']}, other={other_status}; "
        f"dry-run AR excluded={dry_count}.")
    lines.append("")
    if not candidates:
        return lines + ["No paid AR cells recorded.", ""]
    grouped: dict[tuple, list[dict]] = {}
    for row in candidates:
        grouped.setdefault(_group_key(row), []).append(row)
    lines += [
        "| condition | kind | model | tool | runner | experiment | cells | eligible | excluded | hook events | "
        "baseline | final | resolved | persisting | new final |",
        "|" + "|".join(["---"] * 15) + "|",
    ]
    for key in sorted(grouped, key=lambda item: tuple(str(v) for v in item)):
        (condition, kind, model, _effort, _budget, _revision, _config,
         tool, runner_mode, experiment) = key
        group_rows = grouped[key]
        reviews = [review for row in group_rows
                   if (review := _eligible_review_measurement(row)) is not None]

        def total(field: str) -> str:
            return (str(sum(review[field] for review in reviews))
                    if reviews else "—")

        lines.append(
            f"| {condition} | {kind} | {model} | {tool} | {runner_mode} | "
            f"{_short_identity(experiment)} | {len(group_rows)} | "
            f"{len(reviews)} | {len(group_rows) - len(reviews)} | "
            f"{total('hook_events')} | {total('baseline_count')} | "
            f"{total('final_count')} | {total('resolved_count')} | "
            f"{total('persisting_count')} | {total('new_final_count')} |")
    lines.append("")
    return lines


def report(rows: list[dict], anon: bool = False) -> str:
    """Aggregate compatible, infrastructure-valid experiment cells.

    Repeated cells keep their newest attempt in aggregates. Every raw attempt,
    including failures, remains in runs.jsonl and failures remain listed here.
    `anon` replaces task identifiers and omits symbol names; it does not grant
    publication permission.
    """
    if not rows:
        return "no runs recorded\n"
    attempts = list(rows)
    rows = _latest_cells(rows)
    task_labels = _anonymous_task_labels(attempts) if anon else {}
    lines = ["Condition summaries (numeric cells are median ± MAD):", "",
             "| condition | kind | model | effort | budget | revision | config | "
             "tool | runner | experiment | valid | infra | accept cmd | accept n | "
             "semantic | semantic n | manual pending | answer match | answer n | "
             "scope match | scope n | duplication | duplication n | reads | "
             "files | searches | turns | fresh input | cache-created | cache-read | "
             "total input | output |",
             "|" + "|".join(["---"] * 32) + "|"]
    groups = sorted({_group_key(row) for row in rows},
                    key=lambda item: tuple(str(value) for value in item))
    for group in groups:
        (cond, kind, model, effort, budget, revision, config, tool,
         runner_mode, experiment) = group
        all_rs = [row for row in rows if _group_key(row) == group]
        valid = [row for row in all_rs if _infra_reason(row) is None]
        infra = len(all_rs) - len(valid)
        accepted_rows = [row for row in valid
                         if _automatic_acceptance_applicable(row)]
        accepted, accepted_n = _percent_cell(
            [bool(row["accepted"]) for row in accepted_rows])
        semantic_rows = [row for row in valid
                         if row.get("semantic_verdict") in ("pass", "fail")]
        semantic, semantic_n = _percent_cell(
            [row["semantic_verdict"] == "pass" for row in semantic_rows])
        manual_pending = sum(
            row.get("semantic_verdict") == "pending_manual" for row in valid)
        answered = [row for row in valid
                    if not row.get("manual_only")
                    and isinstance(row.get("answer_ok"), bool)]
        answer, answer_n = _percent_cell(
            [bool(row["answer_ok"]) for row in answered])
        scoped = [row for row in valid
                  if not row.get("manual_only")
                  and isinstance(row.get("scope_ok"), bool)]
        scope, scope_n = _percent_cell(
            [bool(row["scope_ok"]) for row in scoped])
        duplication_rows = [row for row in valid if _reuse_applicable(row)]
        duplication, duplication_n = _percent_cell(
            [bool(row.get("duplicated")) for row in duplication_rows])
        lines.append(
            f"| {cond} | {kind} | {model} | {effort} | {budget} | {revision} | "
            f"{config} | {tool} | {runner_mode} | "
            f"{_short_identity(experiment)} | {len(valid)} | "
            f"{infra} | {accepted} | {accepted_n} | {semantic} | {semantic_n} | "
            f"{manual_pending} | {answer} | {answer_n} | {scope} | {scope_n} | "
            f"{duplication} | {duplication_n} | "
            f"{_median_mad(_numbers(valid, 'reads'))} | "
            f"{_median_mad(_numbers(valid, 'files_read'))} | "
            f"{_median_mad(_numbers(valid, 'searches'))} | "
            f"{_median_mad(_numbers(valid, 'turns'))} | "
            f"{_median_mad(_numbers(valid, 'tokens_in_fresh'), decimals=0)} | "
            f"{_median_mad(_numbers(valid, 'tokens_in_cache_created'), decimals=0)} | "
            f"{_median_mad(_numbers(valid, 'tokens_in_cache_read'), decimals=0)} | "
            f"{_median_mad(_numbers(valid, 'tokens_in'), decimals=0)} | "
            f"{_median_mad(_numbers(valid, 'tokens_out'), decimals=0)} |")
    lines.append("")
    lines.extend(_matched_section(
        rows, anon=anon, task_labels=task_labels))
    lines.extend(_review_section(rows))

    failures = [(row, _infra_reason(row)) for row in attempts
                if _infra_reason(row)]
    if failures:
        lines.append("Infrastructure failure attempts (preserved):")
        for row, failure in sorted(
                failures,
                key=lambda item: (str(item[0].get("task")),
                                  str(item[0].get("condition")),
                                  int(item[0].get("rep", 0)))):
            lines.append(
                f"- {_task_label(row, anon, task_labels)} "
                f"[{row.get('condition')}#{row.get('rep')}] "
                f"infra:{failure}")
        lines.append("")
    if not anon:
        lines.append("Per-task duplication (reuse tasks):")
        for row in sorted(rows,
                          key=lambda item: (str(item.get("task")),
                                            str(item.get("condition")),
                                            int(item.get("rep", 0)))):
            if row.get("kind") == "reuse":
                failure = _infra_reason(row)
                mark = ("INFRA:" + str(failure) if failure else
                        "DUP:" + ",".join(row.get("duplicated", []))
                        if row.get("duplicated") else
                        "reused:" + ",".join(row.get("reused", []))
                        if row.get("reused") else "—")
                lines.append(
                    f"- {row.get('task')} [{row.get('condition')}#{row.get('rep')}] {mark}")
    else:
        lines.append("Per-task verdicts:")
        for row in sorted(rows,
                          key=lambda item: (str(item.get("task")),
                                            str(item.get("condition")),
                                            int(item.get("rep", 0)))):
            failure = _infra_reason(row)
            semantic = row.get("semantic_verdict")
            verdict = ("infra:" + str(failure) if failure else
                       "manual-pending" if semantic == "pending_manual" else
                       "semantic-pass" if semantic == "pass" else
                       "semantic-fail" if semantic == "fail" else
                       "dup" if row.get("duplicated") else
                       "scope-miss" if row.get("scope_ok") is False else
                       "reused" if row.get("reused") else
                       "scoped" if row.get("scope_ok") else
                       "answer-match" if row.get("answer_ok") else
                       "answer-miss" if row.get("answer_ok") is False else "—")
            action_proxy = row.get("review_action_proxy",
                                   row.get("acted_on_findings", False))
            review = ("rv+action" if action_proxy else
                      "rv" if row.get("review_seen") else "")
            lines.append(f"- {_task_label(row, True, task_labels)} "
                         f"[{row.get('condition')}#{row.get('rep')}] {verdict} "
                         f"turns={row.get('turns', 0)} reads={row.get('reads', 0)}"
                         + (f" {review}" if review else ""))
    return "\n".join(lines) + "\n"


def _dry_runner(prompt: str, ws: Path, model: str, max_turns: int,
                effort: str | None = None) -> str:
    """Zero-cost runner for harness testing: touches nothing, returns a
    minimal valid transcript."""
    return json.dumps({"type": "result", "num_turns": 0,
                       "result": "dry-run",
                       "usage": {"input_tokens": 0, "output_tokens": 0}})


def _setup_failure_row(*, task: Task, condition: str, rep: int,
                       config: Config, experiment: dict, cell: dict,
                       condition_order: list[str], order_index: int,
                       runner_mode: str, error: Exception,
                       wave_id: str, wave_started_at: str,
                       execution_index: int, block_index: int) -> dict:
    """Persist setup/harness exceptions as retryable result attempts."""
    message = repr(error)
    return {
        "run_id": uuid.uuid4().hex,
        "experiment_id": experiment["experiment_id"],
        "cell_id": cell["cell_id"], "pair_id": cell["pair_id"],
        "task_revision": cell["task_revision"],
        "judge_config_revision": cell["judge_config_revision"],
        "manual_only": task.manual_only,
        "semantic_judge": task.semantic_judge,
        "accept_pass_codes": task.accept_pass_codes,
        "accept_fail_codes": task.accept_fail_codes,
        "task": task.id, "kind": task.kind, "condition": condition,
        "rep": rep, "model": config.model,
        "effort": task.effort or config.effort,
        "max_turns": task.max_turns or config.max_turns,
        "runner_mode": runner_mode,
        "runner_provenance": experiment["runner_provenance"],
        "resolved_model": None,
        "host_execution_acknowledged": experiment[
            "host_execution_acknowledged"],
        "wave_id": wave_id, "wave_started_at": wave_started_at,
        "attempt_started_at": _utc_now(),
        "execution_index": execution_index, "block_index": block_index,
        "experiment_conditions": experiment["conditions"],
        "experiment_reps": experiment["reps"],
        "experiment_tasks": experiment["task_selection"],
        "order_seed": experiment["order_seed"],
        "condition_order": condition_order, "order_index": order_index,
        "corpus_revision": experiment["corpus_revision"],
        "config_revision": config.revision,
        "schema_version": _RESULT_SCHEMA_VERSION,
        "hologram_version": hologram.__version__,
        "tool_revision": _TOOL_REVISION,
        "requested_budget": config.budget,
        "effective_map_tokens": None, "effective_map_detail": None,
        "effective_map_adaptive": None,
        "effective_map_budget": None,
        "accepted": False, "accept_cmd_ok": False,
        "acceptance_verdict": None,
        "acceptance_infra_reason": "not_run",
        "semantic_verdict": "infra_error",
        "answer_ok": None, "scope_ok": None,
        "reuse_judged": bool(task.expect_reuse),
        "reused": [], "duplicated": [], "new_lines": 0,
        "runner_status": "setup_error", "runner_returncode": None,
        "runner_timed_out": False, "runner_duration_seconds": 0.0,
        "acceptance_status": "not_run", "acceptance_returncode": None,
        "acceptance_timed_out": False, "acceptance_duration_seconds": 0.0,
        "runner": {"status": "setup_error", "error": message},
        "acceptance": {"status": "not_run", "error": None},
        "reads": 0, "searches": 0, "edits": 0, "turns": 0,
        "files_read": 0, "input_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "tokens_in_fresh": 0,
        "tokens_in_cache_created": 0, "tokens_in_cache_read": 0,
        "tokens_in": 0, "tokens_out": 0, "review_seen": False,
        "review_action_proxy": False, "acted_on_findings": False,
        "review_findings": _empty_review_measurement("not_run"),
    }


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["_review-hook"]:
        # Benchmark-internal hook transport: parse it before constructing the
        # public command tree so it never appears in `bench --help`.
        internal = argparse.ArgumentParser(prog="bench _review-hook")
        internal.add_argument("rev")
        internal.add_argument("--root", type=Path, required=True)
        internal.add_argument("--capture", type=Path, required=True)
        internal.add_argument("--lang", action="append", default=None)
        hidden = internal.parse_args(raw_argv[1:])
        return _run_review_hook(
            hidden.root.resolve(), hidden.rev, hidden.capture, hidden.lang)

    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("taskfile", type=Path)
    p_run.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    p_run.add_argument("--conditions", nargs="+", default=["A", "B"])
    p_run.add_argument("--reps", type=int, default=1)
    p_run.add_argument("--only", nargs="*", default=None,
                       help="task ids to run (default: all)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="exercise setup/runner persistence without calling "
                            "claude or executing acceptance commands")
    p_run.add_argument("--model", default=None,
                       help="override the task file's model")
    p_run.add_argument("--effort", default=None,
                       choices=("low", "medium", "high"),
                       help="override the task file's reasoning effort")
    p_run.add_argument("--resume", action="store_true",
                       help="skip only complete same-wave task/rep blocks")
    p_run.add_argument("--seed", type=int, default=None,
                       help="condition-order seed (default: deterministic)")
    p_run.add_argument(
        "--allow-unsafe-host", action="store_true",
        help="acknowledge that real agent runs are not host-isolated")
    p_run.add_argument(
        "--max-consecutive-runner-failures", type=int,
        default=_DEFAULT_RUNNER_FAILURE_LIMIT,
        help="stop after this many consecutive infrastructure failures")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    p_rep.add_argument("--anon", action="store_true",
                       help="replace task IDs and omit symbol names for local "
                            "redacted inspection; "
                            "publication still requires corpus-owner approval")
    args = parser.parse_args(raw_argv)

    if args.cmd == "report":
        runs = args.results / "runs.jsonl"
        rows = _read_rows(runs)
        out = args.results / "report.md"
        _atomic_replace(out, report(rows, anon=args.anon).encode())
        print(out.read_text())
        return 0

    cfg = load_tasks(args.taskfile)
    if args.model:
        cfg.model = args.model
    if args.effort:
        cfg.effort = args.effort
    tasks = _validate_matrix(cfg, args.conditions, args.reps, args.only)
    if args.max_consecutive_runner_failures <= 0:
        raise SystemExit("--max-consecutive-runner-failures must be positive")
    if not args.dry_run and not args.allow_unsafe_host:
        raise SystemExit(
            "real benchmark sessions are not host-isolated; rerun with "
            "--allow-unsafe-host only inside an appropriately isolated host")
    runner = _dry_runner if args.dry_run else claude_runner
    runner_mode = "dry-run" if args.dry_run else "unsafe-host"
    runner_provenance = _preflight_runner(runner_mode)
    experiment = _experiment_spec(
        cfg, runner_mode, args.allow_unsafe_host, args.seed,
        conditions=args.conditions, reps=args.reps, tasks=tasks,
        runner_provenance=runner_provenance)
    schedule = _counterbalanced_schedule(
        tasks, args.conditions, args.reps, experiment["order_seed"])
    runs_path = args.results / "runs.jsonl"
    previous = _read_rows(runs_path) if args.resume else []
    latest_previous = _latest_cells(previous)
    latest_by_cell = {
        str(row["cell_id"]): row for row in latest_previous
        if row.get("cell_id")
    }
    completed_blocks: set[int] = set()
    if args.resume:
        scheduled_blocks: dict[int, list[dict]] = {}
        for item in schedule:
            scheduled_blocks.setdefault(item["block_index"], []).append(item)
        for block_index, items in scheduled_blocks.items():
            rows: list[dict] = []
            for item in items:
                task = item["task"]
                cell = _cell_spec(
                    experiment["experiment_id"], task, item["condition"],
                    item["rep"], task.max_turns or cfg.max_turns,
                    task.effort or cfg.effort)
                row = latest_by_cell.get(cell["cell_id"])
                if row is None:
                    break
                rows.append(row)
            # Resume is deliberately block-atomic: every planned condition
            # must be complete in the same wave, or the whole task/rep block
            # is rerun so matched comparisons cannot become permanently
            # cross-wave.
            if len(rows) == len(items) and _resumable_block(
                    rows, args.results):
                completed_blocks.add(block_index)
    args.results.mkdir(parents=True, exist_ok=True)
    wave_id = f"wave-{uuid.uuid4().hex}"
    wave_started_at = _utc_now()
    total = len(schedule)
    done = 0
    execution_index = 0
    had_infra_failure = False
    consecutive_infra_failures = 0
    infra_failures_by_condition = {
        condition: 0 for condition in args.conditions
    }
    print(f"experiment {experiment['experiment_id']} "
          f"seed={experiment['order_seed']}", flush=True)
    for item in schedule:
        task = item["task"]
        condition = item["condition"]
        rep = item["rep"]
        max_turns = task.max_turns or cfg.max_turns
        effort = task.effort or cfg.effort
        cell = _cell_spec(experiment["experiment_id"], task, condition, rep,
                          max_turns, effort)
        done += 1
        label = f"{task.id} {condition} rep{rep}"
        if args.resume and item["block_index"] in completed_blocks:
            print(f"[{done}/{total}] skip {label}", flush=True)
            continue
        print(f"[{done}/{total}] {label}", flush=True)
        execution_index += 1
        try:
            row = run_one(
                cfg.corpus, task, condition, rep, args.results,
                cfg.model, max_turns, runner=runner, lang=cfg.lang or None,
                budget=cfg.budget, effort=effort,
                config_revision=cfg.revision,
                experiment_id=experiment["experiment_id"],
                cell_id=cell["cell_id"], pair_id=cell["pair_id"],
                order_seed=experiment["order_seed"],
                condition_order=item["condition_order"],
                order_index=item["order_index"], runner_mode=runner_mode,
                host_execution_acknowledged=args.allow_unsafe_host,
                execute_acceptance=not args.dry_run,
                runner_provenance=runner_provenance,
                wave_id=wave_id, wave_started_at=wave_started_at,
                execution_index=execution_index,
                block_index=item["block_index"],
                expected_corpus_revision=experiment["corpus_revision"],
                experiment_conditions=experiment["conditions"],
                experiment_reps=experiment["reps"],
                experiment_tasks=experiment["task_selection"])
        except Exception as exc:
            row = _setup_failure_row(
                task=task, condition=condition, rep=rep, config=cfg,
                experiment=experiment, cell=cell,
                condition_order=item["condition_order"],
                order_index=item["order_index"], runner_mode=runner_mode,
                error=exc, wave_id=wave_id,
                wave_started_at=wave_started_at,
                execution_index=execution_index,
                block_index=item["block_index"])
        _append_jsonl_atomic(runs_path, row)
        infra_reason = _infra_reason(row)
        if infra_reason:
            had_infra_failure = True
        if infra_reason:
            consecutive_infra_failures += 1
            infra_failures_by_condition[condition] += 1
        else:
            consecutive_infra_failures = 0
            infra_failures_by_condition[condition] = 0
        failure_streak = max(
            consecutive_infra_failures,
            infra_failures_by_condition[condition])
        if failure_streak >= args.max_consecutive_runner_failures:
            print("infrastructure circuit breaker opened after "
                  f"{failure_streak} repeated failures; "
                  "resume after correcting the infrastructure problem",
                  file=sys.stderr, flush=True)
            break
    return 1 if had_infra_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
