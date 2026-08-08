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
    A = digest + agent instructions; B = control. The corpus's own CLAUDE.md is
    preserved (appended to, identically in both conditions except the snippet),
    and the setup is committed in the detached worktree so that any later
    `git diff` shows exactly what the agent changed."""
    subprocess.run(["git", "-C", str(corpus), "worktree", "add", "--detach",
                    "-f", str(ws), "HEAD"], check=True, capture_output=True)
    claude_path = ws / "CLAUDE.md"
    existing = claude_path.read_text() if claude_path.exists() else ""
    claude_md = (existing.rstrip("\n") + "\n\n" if existing else "") + _BASE_CLAUDE_MD
    if condition == "A":
        subprocess.run([sys.executable, str(HOLOGRAM), "build",
                        "--root", str(ws), "--quiet"], check=True)
        claude_md += "\n" + _AGENT_SNIPPET
    claude_path.write_text(claude_md)
    subprocess.run(["git", "-C", str(ws), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "-c", "user.email=bench@bench",
                    "-c", "user.name=bench", "commit", "-qm", "bench setup"],
                   check=True, capture_output=True)
    return ws


def drop_workspace(corpus: Path, ws: Path) -> None:
    subprocess.run(["git", "-C", str(corpus), "worktree", "remove", "--force",
                    str(ws)], capture_output=True)
    shutil.rmtree(ws, ignore_errors=True)


def claude_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
    """The only function that spends tokens. Runs claude headless in the
    workspace; returns the raw stream-json transcript."""
    r = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
         "--max-turns", str(max_turns), "--model", model,
         "--dangerously-skip-permissions"],
        cwd=ws, capture_output=True, text=True, timeout=1800)
    return r.stdout


def _digest_of(ws: Path) -> str:
    out = ws / ".bench-digest.md"
    subprocess.run([sys.executable, str(HOLOGRAM), "build", "--root", str(ws),
                    "--out", str(out), "--quiet"], check=True)
    text = out.read_text()
    out.unlink()
    return text


def run_one(corpus: Path, task: Task, condition: str, rep: int,
            results_dir: Path, model: str, max_turns: int,
            runner=claude_runner) -> dict:
    results_dir.mkdir(parents=True, exist_ok=True)
    ws = results_dir / f"ws-{task.id}-{condition}-{rep}"
    make_workspace(corpus, ws, condition)
    try:
        before = _digest_of(ws)
        transcript = runner(task.prompt, ws, model, max_turns)
        (results_dir / f"{task.id}-{condition}-{rep}.jsonl").write_text(transcript)
        after = _digest_of(ws)
        verdict = judge_reuse(before, after, task.expect_reuse)
        accepted = subprocess.run(
            task.accept_cmd.format(ws=ws), shell=True,
            capture_output=True).returncode == 0
        metrics = parse_transcript(transcript)
        return {"task": task.id, "kind": task.kind, "condition": condition,
                "rep": rep, "accepted": accepted,
                "reused": verdict["reused"], "duplicated": verdict["duplicated"],
                "new_lines": len(verdict["new_lines"]), **metrics}
    finally:
        drop_workspace(corpus, ws)


def report(rows: list[dict]) -> str:
    if not rows:
        return "no runs recorded\n"
    lines = ["| condition | runs | accepted | duplication (reuse tasks) | "
             "reads | searches | turns | tokens in | tokens out |",
             "|---|---|---|---|---|---|---|---|---|"]
    for cond in sorted({r["condition"] for r in rows}):
        rs = [r for r in rows if r["condition"] == cond]
        reuse = [r for r in rs if r["kind"] == "reuse"]
        dup_rate = (100 * sum(1 for r in reuse if r["duplicated"]) / len(reuse)
                    if reuse else 0)
        acc = 100 * sum(1 for r in rs if r["accepted"]) / len(rs)

        def mean(key):
            return statistics.fmean(r[key] for r in rs)

        lines.append(
            f"| {cond} | {len(rs)} | {acc:.0f}% | {dup_rate:.0f}% | "
            f"{mean('reads'):.1f} | {mean('searches'):.1f} | "
            f"{mean('turns'):.1f} | {mean('tokens_in'):,.0f} | "
            f"{mean('tokens_out'):,.0f} |")
    lines.append("")
    lines.append("Per-task duplication (reuse tasks):")
    for r in sorted(rows, key=lambda r: (r["task"], r["condition"], r["rep"])):
        if r["kind"] == "reuse":
            mark = "DUP:" + ",".join(r["duplicated"]) if r["duplicated"] \
                else ("reused:" + ",".join(r["reused"]) if r["reused"] else "—")
            lines.append(f"- {r['task']} [{r['condition']}#{r['rep']}] {mark}")
    return "\n".join(lines) + "\n"


def _dry_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
    """Zero-cost runner for harness testing: touches nothing, returns a
    minimal valid transcript."""
    return json.dumps({"type": "result", "num_turns": 0,
                       "usage": {"input_tokens": 0, "output_tokens": 0}})


def main(argv: list[str] | None = None) -> int:
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
                       help="exercise the harness without calling claude")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    args = parser.parse_args(argv)

    if args.cmd == "report":
        runs = args.results / "runs.jsonl"
        rows = [json.loads(l) for l in runs.read_text().splitlines()] \
            if runs.exists() else []
        out = args.results / "report.md"
        out.write_text(report(rows))
        print(out.read_text())
        return 0

    cfg = load_tasks(args.taskfile)
    runner = _dry_runner if args.dry_run else claude_runner
    runs_path = args.results / "runs.jsonl"
    args.results.mkdir(parents=True, exist_ok=True)
    tasks = [t for t in cfg.tasks if args.only is None or t.id in args.only]
    total = len(tasks) * len(args.conditions) * args.reps
    done = 0
    for task in tasks:
        for cond in args.conditions:
            for rep in range(args.reps):
                done += 1
                print(f"[{done}/{total}] {task.id} {cond} rep{rep}", flush=True)
                row = run_one(cfg.corpus, task, cond, rep, args.results,
                              cfg.model, cfg.max_turns, runner=runner)
                with runs_path.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
