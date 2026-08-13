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
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402

HOLOGRAM = Path(__file__).resolve().parents[1] / "hologram.py"


@dataclass
class Task:
    id: str
    kind: str                 # "reuse" | "navigate"
    prompt: str
    accept_cmd: str           # shell; {ws} is replaced with the workspace path
    expect_reuse: list[str] = field(default_factory=list)
    expect_answer: list[str] = field(default_factory=list)  # regexes vs result text
    max_turns: int | None = None  # per-task override: the session-length dial


@dataclass
class Config:
    corpus: Path
    tasks: list[Task]
    model: str = "sonnet"
    max_turns: int = 40
    lang: list[str] = field(default_factory=list)  # map filter for condition A


def load_tasks(path: Path) -> Config:
    data = json.loads(path.read_text())
    try:
        tasks = [Task(id=t["id"], kind=t["kind"], prompt=t["prompt"],
                      accept_cmd=t["accept_cmd"],
                      expect_reuse=t.get("expect_reuse", []),
                      expect_answer=t.get("expect_answer", []),
                      max_turns=(int(t["max_turns"])
                                 if "max_turns" in t else None))
                 for t in data["tasks"]]
        return Config(corpus=Path(data["corpus"]).expanduser().resolve(),
                      tasks=tasks,
                      model=data.get("model", "sonnet"),
                      max_turns=int(data.get("max_turns", 40)),
                      lang=list(data.get("lang", [])))
    except KeyError as e:
        raise SystemExit(f"task file {path}: missing field {e}")


_READ_TOOLS = {"Read"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
_BASH_SEARCH = re.compile(r"\b(grep|rg|find|fd|ag)\b")
_BASH_READ = re.compile(r"\b(cat|head|tail|sed -n|less|more)\b")


def parse_transcript(text: str) -> dict:
    """Tool-call counts and usage from a claude stream-json transcript.
    Agents search/read through Bash as often as through dedicated tools, so
    Bash commands are classified too. tokens_in sums fresh input + cache
    creation + cache reads — the actual context consumption. Tolerant of
    non-JSON noise lines."""
    m = {"reads": 0, "searches": 0, "edits": 0,
         "turns": 0, "tokens_in": 0, "tokens_out": 0,
         "files_read": 0, "result_text": ""}
    read_paths: set[str] = set()
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
                elif name == "Bash":
                    cmd = (block.get("input") or {}).get("command", "")
                    if _BASH_SEARCH.search(cmd):
                        m["searches"] += 1
                    elif _BASH_READ.search(cmd):
                        m["reads"] += 1
        elif ev.get("type") == "result":
            usage = ev.get("usage") or {}
            m["turns"] = int(ev.get("num_turns", 0))
            m["tokens_in"] = (int(usage.get("input_tokens", 0))
                              + int(usage.get("cache_creation_input_tokens", 0))
                              + int(usage.get("cache_read_input_tokens", 0)))
            m["tokens_out"] = int(usage.get("output_tokens", 0))
            m["result_text"] = str(ev.get("result", ""))
    m["files_read"] = len(read_paths)
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


_BASE_CLAUDE_MD = """# Working notes

Complete the requested task directly. Keep changes minimal and idiomatic.
"""


def make_workspace(corpus: Path, ws: Path, condition: str,
                   lang: list[str] | None = None) -> Path:
    """Detached git worktree of the corpus, prepared for one condition.
    A = the map embedded in the agent's context file (the whole map in context
    from turn zero); B = control. The corpus's own CLAUDE.md is preserved, and
    the setup is committed in the detached worktree so that any later
    `git diff` shows exactly what the agent changed."""
    subprocess.run(["git", "-C", str(corpus), "worktree", "add", "--detach",
                    "-f", str(ws), "HEAD"], check=True, capture_output=True)
    # A corpus whose committed context files already carry an embedded map
    # would contaminate the control condition — strip any pre-existing
    # blocks in both conditions; A rebuilds its own below.
    for target in hologram.context_targets(ws):
        if not target.is_file():
            continue
        text = target.read_text()
        span = hologram.embed._block_span(text)
        if span is not None:
            cleaned = (text[:span[0]] + text[span[1]:]).strip("\n")
            target.write_text(cleaned + "\n" if cleaned else "")
    claude_path = ws / "CLAUDE.md"
    existing = claude_path.read_text() if claude_path.exists() else ""
    claude_md = (existing.rstrip("\n") + "\n\n" if existing else "") + _BASE_CLAUDE_MD
    claude_path.write_text(claude_md)
    if condition == "A":
        cmd = [sys.executable, str(HOLOGRAM), "build", "--root", str(ws),
               "--quiet", "--warn-tokens", "0"]
        for l in (lang or []):
            cmd += ["--lang", l]
        subprocess.run(cmd, check=True)
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
    return hologram.build_digest(ws)


def run_one(corpus: Path, task: Task, condition: str, rep: int,
            results_dir: Path, model: str, max_turns: int,
            runner=claude_runner, lang: list[str] | None = None) -> dict:
    results_dir.mkdir(parents=True, exist_ok=True)
    ws = results_dir / f"ws-{task.id}-{condition}-{rep}"
    make_workspace(corpus, ws, condition, lang=lang)
    try:
        before = _digest_of(ws)
        transcript = runner(task.prompt, ws, model, max_turns)
        (results_dir / f"{task.id}-{condition}-{rep}.jsonl").write_text(transcript)
        after = _digest_of(ws)
        verdict = judge_reuse(before, after, task.expect_reuse)
        # intent-to-add so brand-new files show up in `git diff`-based acceptance
        subprocess.run(["git", "-C", str(ws), "add", "-N", "."],
                       capture_output=True)
        accepted = subprocess.run(
            task.accept_cmd.format(ws=ws), shell=True,
            capture_output=True).returncode == 0
        metrics = parse_transcript(transcript)
        result_text = metrics.pop("result_text")
        answer_ok = (all(re.search(rx, result_text, re.I | re.S)
                         for rx in task.expect_answer)
                     if task.expect_answer else None)
        return {"task": task.id, "kind": task.kind, "condition": condition,
                "rep": rep, "model": model,
                "accepted": accepted, "answer_ok": answer_ok,
                "reused": verdict["reused"], "duplicated": verdict["duplicated"],
                "new_lines": len(verdict["new_lines"]), **metrics}
    finally:
        drop_workspace(corpus, ws)


def report(rows: list[dict], anon: bool = False) -> str:
    """Aggregate by (condition, kind). `anon` prints only ids, conditions and
    numeric metrics — no symbol names — so results over private corpora can be
    shared without leaking code details."""
    if not rows:
        return "no runs recorded\n"
    lines = ["| condition | kind | runs | accepted | answer ok | "
             "duplication | reads | files | searches | turns | "
             "tokens in | tokens out |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    groups = sorted({(r["condition"], r["kind"]) for r in rows})
    for cond, kind in groups:
        rs = [r for r in rows if r["condition"] == cond and r["kind"] == kind]
        dup_rate = (100 * sum(1 for r in rs if r["duplicated"]) / len(rs)
                    if kind == "reuse" else 0)
        acc = 100 * sum(1 for r in rs if r["accepted"]) / len(rs)
        answered = [r for r in rs if r.get("answer_ok") is not None]
        ans = (f"{100 * sum(1 for r in answered if r['answer_ok']) / len(answered):.0f}%"
               if answered else "—")

        def mean(key, rows=rs):
            return statistics.fmean(r.get(key, 0) for r in rows)

        lines.append(
            f"| {cond} | {kind} | {len(rs)} | {acc:.0f}% | {ans} | "
            f"{dup_rate:.0f}% | {mean('reads'):.1f} | {mean('files_read'):.1f} | "
            f"{mean('searches'):.1f} | {mean('turns'):.1f} | "
            f"{mean('tokens_in'):,.0f} | {mean('tokens_out'):,.0f} |")
    lines.append("")
    if not anon:
        lines.append("Per-task duplication (reuse tasks):")
        for r in sorted(rows,
                        key=lambda r: (r["task"], r["condition"], r["rep"])):
            if r["kind"] == "reuse":
                mark = "DUP:" + ",".join(r["duplicated"]) if r["duplicated"] \
                    else ("reused:" + ",".join(r["reused"])
                          if r["reused"] else "—")
                lines.append(f"- {r['task']} [{r['condition']}#{r['rep']}] {mark}")
    else:
        lines.append("Per-task verdicts:")
        for r in sorted(rows,
                        key=lambda r: (r["task"], r["condition"], r["rep"])):
            verdict = ("dup" if r["duplicated"] else
                       "reused" if r["reused"] else
                       "ok" if r.get("answer_ok") else
                       "miss" if r.get("answer_ok") is False else "—")
            lines.append(f"- {r['task']} [{r['condition']}#{r['rep']}] "
                         f"{verdict} turns={r['turns']} reads={r['reads']}")
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
    p_run.add_argument("--model", default=None,
                       help="override the task file's model")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    p_rep.add_argument("--anon", action="store_true",
                       help="numeric metrics and ids only — safe to share "
                            "for runs over private corpora")
    args = parser.parse_args(argv)

    if args.cmd == "report":
        runs = args.results / "runs.jsonl"
        rows = [json.loads(l) for l in runs.read_text().splitlines()] \
            if runs.exists() else []
        out = args.results / "report.md"
        out.write_text(report(rows, anon=args.anon))
        print(out.read_text())
        return 0

    cfg = load_tasks(args.taskfile)
    if args.model:
        cfg.model = args.model
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
                              cfg.model, task.max_turns or cfg.max_turns,
                              runner=runner, lang=cfg.lang or None)
                with runs_path.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
