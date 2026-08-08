#!/usr/bin/env python3
"""bench: measure agents with vs without a hologram digest.

Subcommands: run (execute the task matrix headlessly), report (aggregate results).
Every claude invocation goes through an injectable runner so the harness is
testable without spending tokens.
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HOLOGRAM = Path(__file__).resolve().parents[1] / "hologram.py"


@dataclass
class Task:
    id: str
    kind: str                 # "reuse" | "navigate"
    prompt: str
    accept_cmd: str           # shell; {ws} is replaced with the workspace path
    expect_reuse: list[str] = field(default_factory=list)


@dataclass
class Config:
    corpus: Path
    tasks: list[Task]
    model: str = "sonnet"
    max_turns: int = 40


def load_tasks(path: Path) -> Config:
    data = json.loads(path.read_text())
    try:
        tasks = [Task(id=t["id"], kind=t["kind"], prompt=t["prompt"],
                      accept_cmd=t["accept_cmd"],
                      expect_reuse=t.get("expect_reuse", []))
                 for t in data["tasks"]]
        return Config(corpus=Path(data["corpus"]).expanduser().resolve(),
                      tasks=tasks,
                      model=data.get("model", "sonnet"),
                      max_turns=int(data.get("max_turns", 40)))
    except KeyError as e:
        raise SystemExit(f"task file {path}: missing field {e}")


_READ_TOOLS = {"Read"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


def parse_transcript(text: str) -> dict:
    """Tool-call counts and usage from a claude stream-json transcript.
    Tolerant of non-JSON noise lines."""
    m = {"reads": 0, "searches": 0, "edits": 0,
         "turns": 0, "tokens_in": 0, "tokens_out": 0}
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
                elif name in _SEARCH_TOOLS:
                    m["searches"] += 1
                elif name in _EDIT_TOOLS:
                    m["edits"] += 1
        elif ev.get("type") == "result":
            usage = ev.get("usage") or {}
            m["turns"] = int(ev.get("num_turns", 0))
            m["tokens_in"] = int(usage.get("input_tokens", 0))
            m["tokens_out"] = int(usage.get("output_tokens", 0))
    return m


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


_AGENT_SNIPPET = """## Project map: PROJECT_DIGEST.md

`PROJECT_DIGEST.md` at the repo root is a generated inventory of this codebase:
every public signature, type relation, and call chain, plus private member names.
Line 2 is the legend.

Consult it BEFORE:
- writing any new function, class, or helper — search for an existing one first
  and reuse it (`×N` marks widely-used utilities, `✓` marks test-exercised ones)
- placing new code — the package tree, the `· deps a→b` lines, and grouped
  families are the house structure; extend it rather than inventing a parallel one
- exploratory grepping — look here first, then grep for the specific thing this
  file says exists
"""

_BASE_CLAUDE_MD = """# Working notes

Complete the requested task directly. Keep changes minimal and idiomatic.
"""


def make_workspace(corpus: Path, ws: Path, condition: str) -> Path:
    """Detached git worktree of the corpus, prepared for one condition.
    A = digest + agent instructions; B = control."""
    subprocess.run(["git", "-C", str(corpus), "worktree", "add", "--detach",
                    "-f", str(ws), "HEAD"], check=True, capture_output=True)
    claude_md = _BASE_CLAUDE_MD
    if condition == "A":
        subprocess.run([sys.executable, str(HOLOGRAM), "build",
                        "--root", str(ws), "--quiet"], check=True)
        claude_md += "\n" + _AGENT_SNIPPET
    (ws / "CLAUDE.md").write_text(claude_md)
    return ws


def drop_workspace(corpus: Path, ws: Path) -> None:
    subprocess.run(["git", "-C", str(corpus), "worktree", "remove", "--force",
                    str(ws)], capture_output=True)
    shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(0)
