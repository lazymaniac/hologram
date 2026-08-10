"""bench: measure agents with vs without a hologram digest.

Subcommands: run (execute the task matrix headlessly), report (aggregate results).
Every claude invocation goes through an injectable runner so the harness is
testable without spending tokens.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from hologram.config import CONFIG_NAME, canonical_config_bytes, default_config
from hologram.render import RenderSymbol, decode_render

if __package__:
    from .corpus import (
        drop_workspace,
        make_workspace,
        prepare_public_corpus,
        schedule_runs,
        verify_prepared_corpus,
        workspace_provenance,
    )
    from .corpus import workspace_asset_sha256 as workspace_asset_digest
    from .reporting import report, require_outside_worktree
    from .schema import (
        Config,
        Task,
        load_tasks,
        resolve_corpus_path,
        task_asset_paths,
    )
    from .transcript import ProcessResult, parse_transcript, terminal_succeeded
    from .verifiers.common import Verification, parse_verifier_output
else:
    def _shared_module(short_name: str):
        package_name = f"benchmark.{short_name}"
        if package_name in sys.modules:
            module = sys.modules[package_name]
            sys.modules.setdefault(short_name, module)
            return module
        module = importlib.import_module(short_name)
        sys.modules.setdefault(package_name, module)
        return module

    _schema = _shared_module("schema")
    _corpus = _shared_module("corpus")
    _reporting = _shared_module("reporting")
    _transcript = _shared_module("transcript")
    if "benchmark.verifiers" in sys.modules:
        sys.modules.setdefault("verifiers", sys.modules["benchmark.verifiers"])
    _verifier_common = _shared_module("verifiers.common")
    from corpus import (  # type: ignore[import-not-found,no-redef]
        drop_workspace,
        make_workspace,
        prepare_public_corpus,
        schedule_runs,
        verify_prepared_corpus,
        workspace_provenance,
    )
    from corpus import (  # type: ignore[import-not-found,no-redef]
        workspace_asset_sha256 as workspace_asset_digest,
    )
    from reporting import (  # type: ignore[import-not-found,no-redef]
        report,
        require_outside_worktree,
    )
    from schema import (  # type: ignore[import-not-found,no-redef]
        Config,
        Task,
        load_tasks,
        resolve_corpus_path,
        task_asset_paths,
    )
    from transcript import (  # type: ignore[import-not-found,no-redef]
        ProcessResult,
        parse_transcript,
        terminal_succeeded,
    )
    from verifiers.common import (  # type: ignore[import-not-found,no-redef]
        Verification,
        parse_verifier_output,
    )

__all__ = ("Config", "Task", "load_tasks")

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_WORKTREE = Path(__file__).resolve().parents[1]
_DEFAULT_RESULTS = Path(__file__).parent / "results"


def _manifest_path(path: Path) -> Path:
    """Resolve a manifest without following an outside symlink into this tree."""

    selected = Path(path).expanduser()
    lexical = Path(os.path.abspath(selected))
    resolved = selected.resolve(strict=False)
    worktree = _WORKTREE.resolve(strict=True)
    if resolved.is_relative_to(worktree):
        public_manifest = worktree / "benchmark" / "tasks" / "codecompanion.json"
        if lexical != resolved or resolved != public_manifest:
            raise ValueError("benchmark manifest must be outside the Hologram worktree")
    return resolved


def _private_run_paths(
    config: Config,
    *,
    manifest: Path,
    corpus: Path,
    results: Path | None,
) -> Path:
    if results is None:
        raise ValueError("private run requires an explicit external --results path")
    require_outside_worktree(
        manifest,
        worktree=_WORKTREE,
        label="private manifest",
    )
    require_outside_worktree(
        corpus,
        worktree=_WORKTREE,
        label="private corpus",
    )
    selected_results = require_outside_worktree(
        results,
        worktree=_WORKTREE,
        label="private results",
    )
    for task in config.tasks:
        if task.challenge is not None:
            require_outside_worktree(
                task.challenge.patch,
                worktree=_WORKTREE,
                label="private challenge",
            )
        for asset in task_asset_paths(task, manifest=manifest):
            require_outside_worktree(
                asset,
                worktree=_WORKTREE,
                label="private verifier or hidden-test asset",
            )
    for relative in config.corpus.workspace_assets:
        require_outside_worktree(
            corpus / relative,
            worktree=_WORKTREE,
            label="private workspace asset",
        )
    return selected_results


def _hologram_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "hologram", *args]


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


def claude_version(run=subprocess.run) -> str:
    completed = run(
        ["claude", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"claude --version failed: {(completed.stderr or '').strip()}"
        )
    raw = (completed.stdout or completed.stderr or "").strip()
    match = re.fullmatch(
        r"(?:Claude Code\s+)?(\d+\.\d+\.\d+)(?:\s+\(Claude Code\))?",
        raw,
    )
    if match is None:
        raise ValueError(f"unrecognized claude --version output: {raw!r}")
    return match.group(1)


def _process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def claude_runner(
    prompt: str,
    workspace: Path,
    model: str,
    max_turns: int,
    *,
    config_dir: Path,
) -> ProcessResult:
    """The only function that spends tokens. Runs claude headless in the
    workspace; returns the raw stream-json transcript."""
    selected_config = config_dir.resolve()
    if not selected_config.is_dir() or any(selected_config.iterdir()):
        raise ValueError("Claude configuration directory must be fresh and empty")
    child_environment = os.environ.copy()
    child_environment["CLAUDE_CONFIG_DIR"] = str(selected_config)
    child_environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    try:
        completed = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
             "--max-turns", str(max_turns), "--model", model,
             "--dangerously-skip-permissions"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env=child_environment,
        )
    except subprocess.TimeoutExpired as error:
        return ProcessResult(
            _process_text(error.stdout),
            _process_text(error.stderr),
            124,
            timed_out=True,
        )
    return ProcessResult(completed.stdout, completed.stderr, completed.returncode)


def _digest_of(ws: Path, workspace_assets: Sequence[str] = ()) -> str:
    out = ws / ".bench-digest.md"
    base = default_config()
    asset_exclusions = tuple(
        f"**/{asset.strip('/')}/**" for asset in workspace_assets
    )
    config = replace(
        base,
        agents=(),
        output=out.name,
        exclude=(*base.exclude, *asset_exclusions),
    )
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


def _run_task_verifier(
    task: Task,
    workspace: Path,
    answer: Path,
    log_path: Path,
) -> Verification:
    command = task.accept_cmd.replace(
        "{ws}", shlex.quote(str(workspace.resolve()))
    ).replace(
        "{answer}", shlex.quote(str(answer.resolve()))
    )
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = _process_text(error.stdout)
        stderr = _process_text(error.stderr)
        returncode = 124
    log_path.write_text(
        "stdout:\n" + stdout + "\nstderr:\n" + stderr,
        encoding="utf-8",
    )
    return parse_verifier_output(stdout, returncode)


def run_one(corpus: Path, task: Task, condition: str, rep: int,
            results_dir: Path, model: str, max_turns: int,
            runner=claude_runner, *, claude_code_version: str = "",
            corpus_revision: str = "", seed: int = 0, pair_index: int = 0,
            challenged_tree_sha256: str = "", workspace_asset_sha256: str = "",
            workspace_assets: Sequence[str] = ()) -> dict:
    results_dir.mkdir(parents=True, exist_ok=True)
    ws = results_dir / f"ws-{task.id}-{condition}-{rep}"
    make_workspace(
        corpus,
        ws,
        condition,
        challenge=task.challenge,
        workspace_assets=workspace_assets,
    )
    try:
        actual_tree_sha256, actual_asset_sha256 = workspace_provenance(ws)
        row_tree_sha256 = challenged_tree_sha256 or actual_tree_sha256
        row_asset_sha256 = workspace_asset_sha256 or actual_asset_sha256
        before = _digest_of(ws, workspace_assets)
        config_dir = results_dir / f"{task.id}-{condition}-{rep}.claude-config"
        config_dir.mkdir(mode=0o700)
        process = runner(
            task.prompt,
            ws,
            model,
            max_turns,
            config_dir=config_dir,
        )
        if type(process) is not ProcessResult:
            raise TypeError("benchmark runner must return ProcessResult")
        transcript_path = results_dir / f"{task.id}-{condition}-{rep}.jsonl"
        transcript_path.write_text(process.stdout, encoding="utf-8")
        if process.stderr:
            (results_dir / f"{task.id}-{condition}-{rep}.stderr.txt").write_text(
                process.stderr,
                encoding="utf-8",
            )
        summary = parse_transcript(process.stdout, requested_model=model)
        answer_path = results_dir / f"{task.id}-{condition}-{rep}.answer.txt"
        answer_path.write_text(summary.final_answer, encoding="utf-8")
        after = _digest_of(ws, workspace_assets)
        if workspace_asset_digest(ws, workspace_assets) != actual_asset_sha256:
            raise ValueError("workspace asset changed during the benchmark run")
        verdict = judge_reuse(before, after, task.expect_reuse)
        # intent-to-add so brand-new files show up in `git diff`-based acceptance
        subprocess.run(["git", "-C", str(ws), "add", "-N", "."],
                       capture_output=True, check=False)
        verification = _run_task_verifier(
            task,
            ws,
            answer_path,
            results_dir / f"{task.id}-{condition}-{rep}.verifier.log",
        )
        verifier_passed = verification.passed
        completed = terminal_succeeded(process, summary)
        accepted = completed and verifier_passed
        terminal_status = (
            "timeout"
            if process.timed_out
            else f"process_exit_{process.returncode}"
            if process.returncode != 0
            else summary.terminal_status
        )
        return {"task": task.id, "kind": task.kind, "condition": condition,
                "rep": rep, "terminal_status": terminal_status,
                "completed": completed, "verifier_passed": verifier_passed,
                "accepted": accepted, "model": model,
                "claude_code_version": claude_code_version,
                "max_turns": max_turns, "corpus_revision": corpus_revision,
                "seed": seed, "pair_index": pair_index,
                "challenged_tree_sha256": row_tree_sha256,
                "workspace_asset_sha256": row_asset_sha256,
                "tier": task.tier, "capability": task.capability,
                "visibility": task.visibility,
                "rubric_score": verification.score,
                "reused": verdict["reused"], "duplicated": verdict["duplicated"],
                "new_lines": len(verdict["new_lines"]),
                "reads": summary.reads, "searches": summary.searches,
                "edits": summary.edits, "map_hits": summary.map_hits,
                "turns": summary.turns, "tokens_in": summary.tokens_in,
                "tokens_out": summary.tokens_out}
    finally:
        drop_workspace(corpus, ws)


def _dry_run_row(
    item,
    *,
    config: Config,
) -> dict[str, object]:
    return {
        "task": item.task.id,
        "kind": item.task.kind,
        "condition": item.condition,
        "rep": item.rep,
        "terminal_status": "dry_run",
        "completed": False,
        "verifier_passed": False,
        "accepted": False,
        "model": config.model,
        "claude_code_version": config.claude_code_version,
        "max_turns": config.max_turns,
        "corpus_revision": config.corpus.revision,
        "seed": config.seed,
        "pair_index": item.pair_index,
        "challenged_tree_sha256": "0" * 64,
        "workspace_asset_sha256": "0" * 64,
        "tier": item.task.tier,
        "capability": item.task.capability,
        "visibility": item.task.visibility,
        "rubric_score": 0.0,
        "reused": [],
        "duplicated": [],
        "new_lines": 0,
        "reads": 0,
        "searches": 0,
        "edits": 0,
        "map_hits": 0,
        "turns": 0,
        "tokens_in": 0,
        "tokens_out": 0,
    }


def _empty_results_directory(path: Path) -> Path:
    selected = path.resolve()
    selected.mkdir(parents=True, exist_ok=True)
    if not selected.is_dir():
        raise ValueError(f"results path is not a directory: {selected}")
    if any(selected.iterdir()):
        raise ValueError(f"results directory must be empty: {selected}")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("taskfile", type=Path)
    p_run.add_argument("--corpus", type=Path)
    p_run.add_argument("--results", type=Path)
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
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("taskfile", type=Path)
    p_prepare.add_argument("--corpus", type=Path, required=True)
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--results", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "report":
        explicit_results = args.results is not None
        selected_results = _DEFAULT_RESULTS if args.results is None else args.results
        if explicit_results:
            selected_results = require_outside_worktree(
                selected_results,
                worktree=_WORKTREE,
                label="explicit report results",
            )
        runs = selected_results / "runs.jsonl"
        rows = [json.loads(l) for l in runs.read_text().splitlines()] \
            if runs.exists() else []
        if any(row.get("visibility") == "private" for row in rows):
            if not explicit_results:
                raise ValueError(
                    "private report requires an explicit external raw-results path"
                )
            require_outside_worktree(
                selected_results,
                worktree=_WORKTREE,
                label="private report raw results",
            )
        out = selected_results / "report.md"
        rendered = report(rows)
        out.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0

    if args.cmd == "prepare":
        destination = args.corpus
        created = not destination.exists()
        if created:
            destination.mkdir(parents=True)
        try:
            cfg = load_tasks(
                _manifest_path(args.taskfile),
                corpus_override=destination,
                environ=os.environ,
            )
            prepared = prepare_public_corpus(cfg.corpus, destination)
        except BaseException:
            if created:
                try:
                    destination.rmdir()
                except OSError:
                    pass
            raise
        print(prepared)
        return 0

    manifest = _manifest_path(args.taskfile)
    cfg = load_tasks(
        manifest,
        corpus_override=args.corpus,
        environ=os.environ,
    )
    corpus = resolve_corpus_path(
        cfg.corpus,
        corpus_override=args.corpus,
        environ=os.environ,
    )
    selected_results = (
        _private_run_paths(
            cfg,
            manifest=manifest,
            corpus=corpus,
            results=args.results,
        )
        if cfg.corpus.visibility == "private"
        else (_DEFAULT_RESULTS if args.results is None else args.results)
    )
    verify_prepared_corpus(cfg.corpus, corpus)
    results_dir = _empty_results_directory(selected_results)
    tasks = [t for t in cfg.tasks if args.only is None or t.id in args.only]
    if not tasks:
        raise ValueError("benchmark selection contains no tasks")
    conditions = tuple(args.conditions)
    if len(conditions) != 2 or set(conditions) != {"B", "C"}:
        raise ValueError("benchmark runs require one B and one C condition")
    if args.reps != cfg.reps:
        raise ValueError(f"benchmark reps must equal manifest reps {cfg.reps}")
    planned = schedule_runs(
        tasks,
        conditions=conditions,
        reps=args.reps,
        seed=cfg.seed,
    )
    identities = tuple(
        (item.task.id, item.condition, item.rep) for item in planned
    )
    pair_keys = {(item.task.id, item.rep) for item in planned}
    complete_pairs = {
        pair
        for pair in pair_keys
        if {
            item.condition
            for item in planned
            if (item.task.id, item.rep) == pair
        }
        == {"B", "C"}
    }
    expected_runs = len(tasks) * args.reps * 2
    expected_pairs = len(tasks) * args.reps
    if (
        len(planned) != expected_runs
        or len(set(identities)) != expected_runs
        or len(complete_pairs) != expected_pairs
    ):
        raise ValueError("benchmark schedule is not a complete unique B/C matrix")
    if not args.dry_run:
        installed_version = claude_version()
        if installed_version != cfg.claude_code_version:
            raise ValueError(
                f"Claude Code {cfg.claude_code_version} required; "
                f"found {installed_version}"
            )
    runs_path = results_dir / "runs.jsonl"
    pair_provenance: dict[tuple[str, int], tuple[str, str]] = {}
    total = len(planned)
    for done, item in enumerate(planned, start=1):
        print(
            f"[{done}/{total}] {item.task.id} {item.condition} rep{item.rep}",
            flush=True,
        )
        row = (
            _dry_run_row(item, config=cfg)
            if args.dry_run
            else run_one(
                corpus,
                item.task,
                item.condition,
                item.rep,
                results_dir,
                cfg.model,
                cfg.max_turns,
                runner=claude_runner,
                claude_code_version=cfg.claude_code_version,
                corpus_revision=cfg.corpus.revision,
                seed=cfg.seed,
                pair_index=item.pair_index,
                workspace_assets=cfg.corpus.workspace_assets,
            )
        )
        tree_hash = row.get("challenged_tree_sha256")
        asset_hash = row.get("workspace_asset_sha256")
        if (
            type(tree_hash) is not str
            or _HEX64.fullmatch(tree_hash) is None
            or type(asset_hash) is not str
            or _HEX64.fullmatch(asset_hash) is None
        ):
            raise ValueError("benchmark pair provenance is missing or malformed")
        pair_key = (item.task.id, item.rep)
        provenance = (tree_hash, asset_hash)
        expected_provenance = pair_provenance.setdefault(pair_key, provenance)
        if provenance != expected_provenance:
            raise ValueError("benchmark B/C pair provenance does not match")
        with runs_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
