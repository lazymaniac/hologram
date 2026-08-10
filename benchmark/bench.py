"""bench: measure agents with vs without a hologram digest.

Subcommands: run (execute the task matrix headlessly), report (aggregate results).
Every claude invocation goes through an injectable runner so the harness is
testable without spending tokens.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from hologram.config import CONFIG_NAME, canonical_config_bytes, default_config
from hologram.context import (
    AGENT_PATHS,
    ContextStatus,
    inspect_managed_block,
    read_target_bytes,
    render_managed_block,
)
from hologram.render import RenderSymbol, decode_render

if __package__:
    from .schema import Config, Task, load_tasks, resolve_corpus_path
else:
    from schema import (  # type: ignore[import-not-found,no-redef]
        Config,
        Task,
        load_tasks,
        resolve_corpus_path,
    )

__all__ = ("Config", "Task", "load_tasks")


def _hologram_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "hologram", *args]


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
    m = {"reads": 0, "searches": 0, "edits": 0, "turns": 0,
         "tokens_in": 0, "tokens_out": 0}
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
    return m


def _symbols(rendered: str) -> tuple[RenderSymbol, ...]:
    return tuple(
        symbol
        for file_ir in decode_render(rendered).files
        for symbol in file_ir.symbols
    )


def _short_display_name(value: str) -> str:
    return value.split("|", 1)[0].rsplit(".", 1)[-1].rsplit(":", 1)[-1].lower()


def judge_reuse(before: str, after: str, expect_reuse: Sequence[str]) -> dict:
    """Compare decoded canonical maps around one benchmark run."""

    old_ids = frozenset(symbol.symbol_id for symbol in _symbols(before))
    new_symbols = [
        symbol for symbol in _symbols(after) if symbol.symbol_id not in old_ids
    ]
    reused: list[str] = []
    duplicated: list[str] = []
    for target in expect_reuse:
        tshort = target.rsplit(".", 1)[-1].lower()
        hit = any(
            tshort == _short_display_name(call)
            for symbol in new_symbols
            for call in symbol.ordered_calls
        )
        if hit:
            reused.append(target)
            continue
        for symbol in new_symbols:
            name = symbol.symbol_id.name
            sim = difflib.SequenceMatcher(None, name.lower(), tshort).ratio()
            if sim >= 0.6 and name.lower() != tshort:
                duplicated.append(name)
                break
    return {"new_lines": new_symbols,
            "reused": sorted(set(reused)),
            "duplicated": sorted(set(duplicated))}


_BASE_CLAUDE_MD = """# Working notes

Complete the requested task directly. Keep changes minimal and idiomatic.
"""
_EMPTY_MANAGED_BLOCK = render_managed_block("")


def _declared_corpus_output(ws: Path) -> Path | None:
    manifest = ws / CONFIG_NAME
    try:
        manifest_stat = manifest.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(manifest_stat.st_mode):
        return None

    raw = read_target_bytes(manifest)
    if raw is None:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    schema = data.get("schema_version")
    output = data.get("output")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != 2
        or not isinstance(output, str)
    ):
        return None

    relative = PurePosixPath(output)
    if (
        not output
        or not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in output
        or re.match(r"^[A-Za-z]:", output)
        or output != relative.as_posix()
        or relative.suffix != ".md"
        or any(character in output for character in "*?[]")
    ):
        return None
    return ws.joinpath(*relative.parts)


def _benchmark_claude_bytes(ws: Path) -> bytes:
    claude = b""
    for agent, relative in AGENT_PATHS.items():
        existing = read_target_bytes(ws / relative)
        if existing is None:
            continue
        if inspect_managed_block(existing, _EMPTY_MANAGED_BLOCK) is not ContextStatus.MISSING:
            raise ValueError(f"preexisting Hologram context in {relative}")
        if agent == "claude":
            claude = existing

    standalone = ws / "PROJECT_DIGEST.md"
    try:
        standalone.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("preexisting Hologram standalone map in PROJECT_DIGEST.md")

    declared_output = _declared_corpus_output(ws)
    if (
        declared_output is not None
        and read_target_bytes(declared_output, root=ws) is not None
    ):
        raise ValueError(f"preexisting Hologram standalone map in {declared_output}")
    return claude


def _append_benchmark_instructions(authored: bytes) -> bytes:
    instructions = _BASE_CLAUDE_MD.encode("utf-8")
    if not authored:
        return instructions
    separator = b"\n" if authored.endswith((b"\n", b"\r")) else b"\n\n"
    return authored + separator + instructions


def make_workspace(corpus: Path, ws: Path, condition: str) -> Path:
    """Detached git worktree of the corpus, prepared for one condition.
    B = control; C = a managed canonical map in CLAUDE.md. The corpus's own
    CLAUDE.md is preserved, and the setup is committed in the detached worktree
    so that any later `git diff` shows exactly what the agent changed."""
    if condition not in {"B", "C"}:
        raise ValueError("benchmark condition must be one of conditions B and C")
    subprocess.run(["git", "-C", str(corpus), "worktree", "add", "--detach",
                    "-f", str(ws), "HEAD"], check=True, capture_output=True)
    try:
        claude_path = ws / "CLAUDE.md"
        authored = _benchmark_claude_bytes(ws)
        claude_path.write_bytes(_append_benchmark_instructions(authored))
        if condition == "C":
            config = replace(default_config(), agents=("claude",), output=None)
            with tempfile.TemporaryDirectory(
                prefix="hologram-bench-condition-"
            ) as temporary:
                config_path = Path(temporary) / CONFIG_NAME
                config_path.write_bytes(canonical_config_bytes(config))
                subprocess.run(
                    _hologram_command("build")
                    + [
                        "--root",
                        str(ws),
                        "--config",
                        str(config_path),
                        "--quiet",
                    ],
                    check=True,
                )
        subprocess.run(["git", "-C", str(ws), "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "-c", "user.email=bench@bench",
                        "-c", "user.name=bench", "commit", "-qm", "bench setup"],
                       check=True, capture_output=True)
        return ws
    except BaseException:
        drop_workspace(corpus, ws)
        raise


def drop_workspace(corpus: Path, ws: Path) -> None:
    subprocess.run(["git", "-C", str(corpus), "worktree", "remove", "--force",
                    str(ws)], capture_output=True, check=False)
    shutil.rmtree(ws, ignore_errors=True)


def claude_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
    """The only function that spends tokens. Runs claude headless in the
    workspace; returns the raw stream-json transcript."""
    r = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
         "--max-turns", str(max_turns), "--model", model,
         "--dangerously-skip-permissions"],
        cwd=ws, capture_output=True, text=True, timeout=1800, check=False)
    return r.stdout


def _digest_of(ws: Path) -> str:
    out = ws / ".bench-digest.md"
    config = replace(default_config(), agents=(), output=out.name)
    with tempfile.TemporaryDirectory(prefix="hologram-bench-config-") as temporary:
        config_path = Path(temporary) / CONFIG_NAME
        config_path.write_bytes(canonical_config_bytes(config))
        try:
            subprocess.run(
                _hologram_command("build")
                + [
                    "--root",
                    str(ws),
                    "--config",
                    str(config_path),
                    "--quiet",
                ],
                check=True,
            )
            return out.read_text(encoding="utf-8")
        finally:
            out.unlink(missing_ok=True)


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
        # intent-to-add so brand-new files show up in `git diff`-based acceptance
        subprocess.run(["git", "-C", str(ws), "add", "-N", "."],
                       capture_output=True, check=False)
        accepted = subprocess.run(
            task.accept_cmd.format(ws=ws), shell=True,
            capture_output=True, check=False).returncode == 0
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
    lines = [("| condition | runs | accepted | duplication (reuse tasks) | "
              "reads | searches | turns | tokens in | tokens out |"),
             "|---|---|---|---|---|---|---|---|---|"]
    for cond in sorted({r["condition"] for r in rows}):
        rs = [r for r in rows if r["condition"] == cond]
        reuse = [r for r in rs if r["kind"] == "reuse"]
        dup_rate = (100 * sum(1 for r in reuse if r["duplicated"]) / len(reuse)
                    if reuse else 0)
        acc = 100 * sum(1 for r in rs if r["accepted"]) / len(rs)

        def mean(key, samples=rs):
            return statistics.fmean(r[key] for r in samples)

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
    p_run.add_argument("--corpus", type=Path)
    p_run.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    p_run.add_argument(
        "--conditions",
        nargs="+",
        choices=("B", "C"),
        default=["B", "C"],
    )
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

    cfg = load_tasks(
        args.taskfile,
        corpus_override=args.corpus,
        environ=os.environ,
    )
    corpus = resolve_corpus_path(
        cfg.corpus,
        corpus_override=args.corpus,
        environ=os.environ,
    )
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
                row = run_one(corpus, task, cond, rep, args.results,
                              cfg.model, cfg.max_turns, runner=runner)
                with runs_path.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
