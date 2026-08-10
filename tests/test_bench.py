import dataclasses
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))

import bench  # type: ignore[import-not-found]

from benchmark import corpus as benchmark_corpus
from benchmark.corpus import RunSpec, prepare_public_corpus, schedule_runs
from benchmark.schema import BenchmarkCorpus, Challenge
from benchmark.transcript import (
    ProcessResult,
    TranscriptSummary,
    parse_transcript,
    terminal_succeeded,
)
from hologram import (
    CONFIG_NAME,
    Language,
    SourceRole,
    SymbolId,
    SymbolKind,
    canonical_config_bytes,
    default_config,
)
from hologram.context import (
    CONTEXT_START,
    LEGACY_END,
    LEGACY_START,
    AtomicWriteError,
    render_managed_block,
)
from hologram.render import (
    RenderFile,
    RenderIR,
    RenderSymbol,
    render_project,
)


class TaskLoaderTest(unittest.TestCase):
    def _taskfile(self, tmp: Path) -> Path:
        corpus = tmp / "corpus"
        corpus.mkdir()
        p = tmp / "tasks.json"
        p.write_text(json.dumps({
            "schema_version": 2,
            "corpus": {
                "name": "example",
                "visibility": "public",
                "url": "https://example.com/example.git",
                "revision": "a" * 40,
                "path_env": "HOLOGRAM_BENCH_EXAMPLE",
            },
            "model": "claude-sonnet-5",
            "claude_code_version": "2.1.224",
            "max_turns": 40,
            "conditions": ["B", "C"],
            "reps": 1,
            "seed": 20260809,
            "tasks": [
                {"id": "weighted-avg", "tier": "simple",
                 "capability": "implementation", "kind": "reuse",
                 "visibility": "public",
                 "prompt": "Add a weighted average.",
                 "accept_cmd": "grep -rq weightedAverage {ws}",
                 "expect_reuse": ["normalize", "add"]},
                {"id": "find-lifecycle", "tier": "complex",
                 "capability": "orientation", "kind": "navigate",
                 "visibility": "public",
                 "prompt": "Where is record lifecycle handled?",
                 "accept_cmd": "verify-answer {ws} {answer}"},
            ],
        }))
        return p

    def test_loads_tasks_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = bench.load_tasks(
                self._taskfile(root),
                corpus_override=root / "corpus",
            )
        self.assertEqual(cfg.model, "claude-sonnet-5")
        self.assertEqual(cfg.max_turns, 40)
        self.assertEqual(len(cfg.tasks), 2)
        self.assertEqual(cfg.tasks[0].id, "weighted-avg")
        self.assertEqual(cfg.tasks[0].expect_reuse, ("normalize", "add"))
        self.assertEqual(cfg.corpus.name, "example")

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"schema_version": 2}))
            with self.assertRaises(ValueError):
                bench.load_tasks(p, corpus_override=Path(tmp))


MODEL = "claude-sonnet-5"
PASSING_VERIFIER = (
    "printf '%s\\n' "
    "'{\"passed\":true,\"score\":1.0,\"diagnostics\":[]}'"
)
TRANSCRIPT = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": MODEL}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.java"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "normalize"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "grep -rn hasText spring-core/src"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "sed -n '1,40p' StringUtils.java"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "grep -n trimToNull PROJECT_DIGEST.md"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": "/ws/PROJECT_DIGEST.md"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {
        "model": MODEL,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Completed the task."}],
    }}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "result": "Completed the task.", "num_turns": 7,
                "usage": {"input_tokens": 91000, "output_tokens": 4200,
                          "cache_creation_input_tokens": 30000,
                          "cache_read_input_tokens": 500000}}),
    "not-json-noise",
])


class TranscriptMetricsTest(unittest.TestCase):
    def test_transcript_records_are_frozen_with_exact_fields(self):
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(ProcessResult)),
            ("stdout", "stderr", "returncode", "timed_out"),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(TranscriptSummary)),
            (
                "terminal_status", "terminal_count", "is_error", "stop_reason",
                "final_answer", "reported_model", "reads", "searches", "edits",
                "map_hits", "turns", "tokens_in", "tokens_out",
            ),
        )
        result = ProcessResult("", "", 0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.returncode = 1  # type: ignore[misc]

    def test_counts_and_usage(self):
        summary = parse_transcript(TRANSCRIPT, requested_model=MODEL)
        self.assertIsInstance(summary, TranscriptSummary)
        self.assertEqual(summary.terminal_status, "success")
        self.assertEqual(summary.terminal_count, 1)
        self.assertFalse(summary.is_error)
        self.assertEqual(summary.stop_reason, "end_turn")
        self.assertEqual(summary.final_answer, "Completed the task.")
        self.assertEqual(summary.reported_model, MODEL)
        self.assertEqual(summary.reads, 3)      # Read ×2 + bash sed -n
        self.assertEqual(summary.searches, 3)   # Grep + bash grep ×2
        self.assertEqual(summary.edits, 2)      # Edit + Write
        self.assertEqual(summary.map_hits, 2)   # PROJECT_DIGEST grep + Read
        self.assertEqual(summary.turns, 7)
        self.assertEqual(summary.tokens_in, 91000 + 30000 + 500000)
        self.assertEqual(summary.tokens_out, 4200)
        self.assertTrue(terminal_succeeded(ProcessResult(TRANSCRIPT, "", 0), summary))

    def test_empty_transcript_gives_zeroes(self):
        summary = parse_transcript("", requested_model=MODEL)
        self.assertEqual(summary.terminal_status, "missing_result")
        self.assertEqual(summary.terminal_count, 0)
        self.assertEqual(summary.reads, 0)
        self.assertEqual(summary.searches, 0)
        self.assertEqual(summary.edits, 0)
        self.assertEqual(summary.map_hits, 0)
        self.assertEqual(summary.turns, 0)
        self.assertEqual(summary.tokens_in, 0)
        self.assertEqual(summary.tokens_out, 0)

    def _terminal(
        self,
        *,
        subtype: str = "success",
        is_error: bool = False,
        answer: str = "done",
        stop_reason: str = "end_turn",
        model: str = MODEL,
    ) -> str:
        return "\n".join(
            (
                json.dumps({"type": "system", "subtype": "init", "model": model}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "model": model,
                            "stop_reason": stop_reason,
                            "content": [{"type": "text", "text": answer}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": subtype,
                        "is_error": is_error,
                        "result": answer,
                        "num_turns": 1,
                        "usage": {},
                    }
                ),
            )
        )

    def test_terminal_failures_are_stable_and_fail_closed(self) -> None:
        success = self._terminal()
        cases = {
            "missing_result": "",
            "multiple_results": success + "\n" + success.splitlines()[-1],
            "permission_error": self._terminal(
                subtype="permission_error", is_error=True
            ),
            "error_max_turns": self._terminal(
                subtype="error_max_turns", is_error=True
            ),
            "context_overflow": self._terminal(
                subtype="context_overflow", is_error=True
            ),
            "empty_answer": self._terminal(answer="   "),
            "stop_reason_tool_use": self._terminal(stop_reason="tool_use"),
            "model_mismatch": self._terminal(model="claude-sonnet-4"),
        }
        for expected, transcript in cases.items():
            with self.subTest(expected=expected):
                summary = parse_transcript(transcript, requested_model=MODEL)
                self.assertEqual(summary.terminal_status, expected)
                self.assertFalse(
                    terminal_succeeded(ProcessResult(transcript, "", 0), summary)
                )

        summary = parse_transcript(success, requested_model=MODEL)
        self.assertFalse(
            terminal_succeeded(ProcessResult(success, "failed", 9), summary)
        )
        self.assertFalse(
            terminal_succeeded(
                ProcessResult(success, "timeout", 0, timed_out=True), summary
            )
        )


def _render_symbol(name: str, calls: tuple[str, ...] = ()) -> RenderSymbol:
    symbol_id = SymbolId(
        Language.PYTHON,
        "svc.py",
        (),
        SymbolKind.FUNCTION,
        name,
        "()",
    )
    return RenderSymbol(
        symbol_id,
        1,
        0,
        "pub",
        f"{name}()",
        (),
        None,
        (),
        (),
        (),
        (),
        (),
        calls,
        (),
        (),
        2,
        (),
        call_targets=tuple(
            SymbolId(
                Language.PYTHON,
                "svc.py",
                (),
                SymbolKind.FUNCTION,
                target,
                "()",
            )
            for target in calls
        ),
    )


def _canonical_map(*symbols: RenderSymbol) -> str:
    ordered = tuple(sorted(symbols, key=lambda symbol: symbol.symbol_id.name))
    return render_project(
        RenderIR(
            2,
            "a" * 64,
            (),
            (),
            (
                RenderFile(
                    "svc.py",
                    Language.PYTHON.value,
                    SourceRole.PRODUCTION.value,
                    "svc",
                    (),
                    ordered,
                ),
            ),
        )
    )


NORMALIZE = _render_symbol("normalize")
ADD = _render_symbol("add")
WEIGHTED = _render_symbol("weightedAverage", ("add", "normalize"))
NORMALIZE_WEIGHTS = _render_symbol("normalizeWeights")
WEIGHTED_DUPLICATE = _render_symbol("weightedAverage", ("normalizeWeights",))
BEFORE_DIGEST = _canonical_map(ADD, NORMALIZE)
AFTER_REUSED = _canonical_map(ADD, NORMALIZE, WEIGHTED)
AFTER_DUPLICATED = _canonical_map(
    ADD,
    NORMALIZE,
    NORMALIZE_WEIGHTS,
    WEIGHTED_DUPLICATE,
)


class DuplicationDetectorTest(unittest.TestCase):
    def test_reuse_detected(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED, ["normalize", "add"])
        self.assertEqual(sorted(v["reused"]), ["add", "normalize"])
        self.assertEqual(v["duplicated"], [])

    def test_duplicate_detected(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_DUPLICATED, ["normalize"])
        self.assertEqual(v["reused"], [])
        self.assertEqual(v["duplicated"], ["normalizeWeights"])

    def test_new_lines_listed(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED, [])
        self.assertEqual(
            [symbol.symbol_id.name for symbol in v["new_lines"]],
            ["weightedAverage"],
        )

    def test_no_change_is_clean(self):
        v = bench.judge_reuse(BEFORE_DIGEST, BEFORE_DIGEST, ["normalize"])
        self.assertEqual(v, {"new_lines": [], "reused": [], "duplicated": []})

    def test_changed_existing_symbol_is_not_new(self):
        changed = dataclasses.replace(
            NORMALIZE,
            signature="normalize(value)",
            parameters=("value",),
            body_lines=9,
        )

        verdict = bench.judge_reuse(
            BEFORE_DIGEST,
            _canonical_map(ADD, changed),
            ["normalize"],
        )

        self.assertEqual(verdict, {"new_lines": [], "reused": [], "duplicated": []})


def _mini_corpus(tmp: Path) -> Path:
    repo = tmp / "corpus"
    repo.mkdir()
    (repo / "svc.py").write_text(
        "def normalize(xs: list) -> list:\n    return xs\n")
    (repo / "CLAUDE.md").write_text("# Corpus conventions\nUse tabs. Just kidding.\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b",
                    "commit", "-qm", "seed"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/example.git"],
        cwd=repo,
        check=True,
    )
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _challenge(repo: Path, root: Path) -> Challenge:
    source = repo / "svc.py"
    original = source.read_text()
    source.write_text(
        original + "\ndef challenged(value: int) -> int:\n    return value + 1\n"
    )
    patch_bytes = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary"],
        check=True,
        capture_output=True,
    ).stdout
    source.write_text(original)
    patch = root / "challenge.patch"
    patch.write_bytes(patch_bytes)
    return Challenge(patch, hashlib.sha256(patch_bytes).hexdigest())


class WorkspaceTest(unittest.TestCase):
    def test_prepare_public_corpus_pins_revision_and_rejects_dirty_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = _mini_corpus(root)
            destination = root / "prepared"
            destination.mkdir()
            spec = BenchmarkCorpus(
                "example", "public", origin.as_uri(), _head(origin),
                "HOLOGRAM_BENCH_EXAMPLE",
            )
            prepared = prepare_public_corpus(spec, destination)
            self.assertEqual(prepared, destination.resolve())
            self.assertEqual(_head(prepared), spec.revision)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(prepared), "status", "--porcelain=v1",
                     "--untracked-files=all"],
                    check=True, capture_output=True,
                ).stdout,
                b"",
            )
            (prepared / "dirty.txt").write_text("dirty")
            with self.assertRaises(ValueError):
                bench.verify_prepared_corpus(spec, prepared)

    def test_prepare_runs_bootstrap_and_requires_declared_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = _mini_corpus(root)
            (origin / ".gitignore").write_text("deps/\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=origin, check=True)
            subprocess.run(
                ["git", "-c", "user.email=b@b", "-c", "user.name=b",
                 "commit", "-qm", "ignore dependencies"],
                cwd=origin, check=True,
            )
            spec = BenchmarkCorpus(
                "example",
                "public",
                origin.as_uri(),
                _head(origin),
                "HOLOGRAM_BENCH_EXAMPLE",
                "mkdir -p deps && printf 'ready\\n' > deps/tool",
                ("deps",),
            )

            prepared = prepare_public_corpus(spec, root / "prepared")

            self.assertEqual((prepared / "deps/tool").read_text(), "ready\n")
            self.assertEqual(
                bench.verify_prepared_corpus(spec, prepared),
                prepared,
            )

    def test_challenge_sha_mismatch_fails_before_workspace_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            challenge = dataclasses.replace(_challenge(repo, root), sha256="0" * 64)
            destination = root / "workspace"

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                bench.make_workspace(
                    repo,
                    destination,
                    "C",
                    challenge=challenge,
                )

            self.assertFalse(destination.exists())

    def test_challenge_and_assets_are_identical_but_physically_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            (repo / ".gitignore").write_text("deps/\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=b@b", "-c", "user.name=b",
                 "commit", "-qm", "ignore dependencies"],
                cwd=repo, check=True,
            )
            deps = repo / "deps"
            deps.mkdir()
            (deps / "tool.lua").write_text("return 1\n")
            challenge = _challenge(repo, root)
            workspaces: dict[str, Path] = {}
            try:
                for condition in ("B", "C"):
                    workspaces[condition] = bench.make_workspace(
                        repo,
                        root / f"workspace-{condition}",
                        condition,
                        challenge=challenge,
                        workspace_assets=("deps",),
                    )
                before = workspaces["B"]
                after = workspaces["C"]
                self.assertEqual(
                    (before / "svc.py").read_bytes(),
                    (after / "svc.py").read_bytes(),
                )
                self.assertIn(b"def challenged", (after / "svc.py").read_bytes())
                self.assertIn(b"challenged", (after / "CLAUDE.md").read_bytes())
                self.assertEqual(
                    (before / "deps/tool.lua").read_bytes(),
                    (after / "deps/tool.lua").read_bytes(),
                )
                self.assertNotEqual(
                    (deps / "tool.lua").stat().st_ino,
                    (before / "deps/tool.lua").stat().st_ino,
                )
                self.assertNotEqual(
                    (before / "deps/tool.lua").stat().st_ino,
                    (after / "deps/tool.lua").stat().st_ino,
                )
                for workspace in (before, after):
                    self.assertFalse(
                        (workspace / ".git/objects/info/alternates").exists()
                    )
                self.assertEqual(
                    bench.workspace_provenance(before),
                    bench.workspace_provenance(after),
                )
                (before / "deps/tool.lua").write_text("mutated\n")
                self.assertEqual((deps / "tool.lua").read_text(), "return 1\n")
                self.assertEqual(
                    (after / "deps/tool.lua").read_text(), "return 1\n"
                )
            finally:
                for workspace in workspaces.values():
                    bench.drop_workspace(repo, workspace)

    def test_nested_asset_symlink_is_materialized_as_independent_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            (repo / ".gitignore").write_text("deps/\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=b@b", "-c", "user.name=b",
                 "commit", "-qm", "ignore dependencies"],
                cwd=repo, check=True,
            )
            deps = repo / "deps"
            deps.mkdir()
            (deps / "payload").write_bytes(b"stable\x00bytes\n")
            (deps / "alias").symlink_to("payload")

            workspace = bench.make_workspace(
                repo,
                root / "workspace",
                "B",
                workspace_assets=("deps",),
            )
            try:
                alias = workspace / "deps/alias"
                self.assertFalse(alias.is_symlink())
                self.assertEqual(alias.read_bytes(), b"stable\x00bytes\n")
                self.assertNotEqual(
                    alias.stat().st_ino,
                    (deps / "payload").stat().st_ino,
                )
            finally:
                bench.drop_workspace(repo, workspace)

    def test_asset_symlink_must_remain_inside_its_declared_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            (repo / ".gitignore").write_text("deps/\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=b@b", "-c", "user.name=b",
                 "commit", "-qm", "ignore dependencies"],
                cwd=repo, check=True,
            )
            deps = repo / "deps"
            deps.mkdir()
            outside = repo / "outside.txt"
            outside.write_text("outside\n")
            (deps / "escape").symlink_to(outside)
            with self.assertRaises(ValueError):
                bench.make_workspace(
                    repo,
                    root / "unsafe-asset",
                    "B",
                    workspace_assets=("deps",),
                )
            self.assertEqual(outside.read_text(), "outside\n")

    def test_agent_asset_mutation_rejects_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            (repo / ".gitignore").write_text("deps/\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=b@b", "-c", "user.name=b",
                 "commit", "-qm", "ignore dependencies"],
                cwd=repo, check=True,
            )
            (repo / "deps").mkdir()
            (repo / "deps/data").write_text("stable\n")
            task = bench.Task(
                id="asset-mutation", tier="simple",
                capability="implementation", kind="reuse",
                visibility="public", prompt="Do not mutate assets.",
                accept_cmd="test -d {ws}", expect_reuse=("normalize",),
            )

            def runner(prompt, workspace, model, max_turns, *, config_dir):
                (workspace / "deps/data").write_text("changed\n")
                return ProcessResult(TRANSCRIPT, "", 0)

            with self.assertRaisesRegex(ValueError, "workspace asset"):
                bench.run_one(
                    repo, task, "B", 0, root / "results", MODEL, 40,
                    runner=runner, workspace_assets=("deps",),
                )

    def test_active_conditions_have_exact_controlled_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _mini_corpus(tmp_path)
            for condition in ("B", "C"):
                with self.subTest(condition=condition):
                    ws = bench.make_workspace(
                        repo,
                        tmp_path / f"ws{condition}",
                        condition,
                    )
                    try:
                        self.assertFalse((ws / CONFIG_NAME).exists())
                    finally:
                        bench.drop_workspace(repo, ws)

    def test_corpus_manifest_is_preserved_byte_for_byte(self):
        provided = (
            b"# Corpus-owned formatting must survive.\n"
            b"schema_version = 2\n"
            b'agents = ["claude"]\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _mini_corpus(tmp_path)
            (repo / CONFIG_NAME).write_bytes(provided)
            subprocess.run(["git", "add", CONFIG_NAME], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=b@b",
                    "-c",
                    "user.name=b",
                    "commit",
                    "-qm",
                    "add manifest",
                ],
                cwd=repo,
                check=True,
            )
            ws = bench.make_workspace(repo, tmp_path / "wsC", "C")
            try:
                self.assertEqual((repo / CONFIG_NAME).read_bytes(), provided)
                self.assertEqual((ws / CONFIG_NAME).read_bytes(), provided)
                self.assertIn(
                    b"hologram:v2:start",
                    (ws / "CLAUDE.md").read_bytes(),
                )
            finally:
                bench.drop_workspace(repo, ws)

    def test_dangling_corpus_manifest_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _mini_corpus(tmp_path)
            manifest = repo / CONFIG_NAME
            manifest.symlink_to("missing-manifest.toml")
            subprocess.run(["git", "add", CONFIG_NAME], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=b@b",
                    "-c",
                    "user.name=b",
                    "commit",
                    "-qm",
                    "add manifest symlink",
                ],
                cwd=repo,
                check=True,
            )
            for condition in ("B", "C"):
                with self.subTest(condition=condition):
                    ws = bench.make_workspace(
                        repo,
                        tmp_path / f"ws{condition}",
                        condition,
                    )
                    try:
                        workspace_manifest = ws / CONFIG_NAME
                        self.assertTrue(workspace_manifest.is_symlink())
                        self.assertEqual(
                            workspace_manifest.readlink(),
                            Path("missing-manifest.toml"),
                        )
                        self.assertTrue(manifest.is_symlink())
                        self.assertFalse((ws / "missing-manifest.toml").exists())
                        self.assertFalse((ws / "PROJECT_DIGEST.md").exists())
                    finally:
                        bench.drop_workspace(repo, ws)

    def test_condition_c_has_managed_map_and_preserves_authored_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsC", "C")
            try:
                context = (ws / "CLAUDE.md").read_bytes()
                self.assertIn(b"Corpus conventions", context)
                self.assertIn(b"hologram:v2:start", context)
                self.assertIn(b"# hologram:2 state=", context)
                self.assertFalse((ws / "PROJECT_DIGEST.md").exists())
                self.assertTrue((ws / "svc.py").exists())
            finally:
                bench.drop_workspace(repo, ws)

    def test_authored_context_bytes_are_preserved_for_b_and_c(self):
        authored = b"\xfffirst\r\nsecond\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _mini_corpus(tmp_path)
            (repo / "CLAUDE.md").write_bytes(authored)
            subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=b@b",
                    "-c",
                    "user.name=b",
                    "commit",
                    "-qm",
                    "binary authored context",
                ],
                cwd=repo,
                check=True,
            )

            for condition in ("B", "C"):
                with self.subTest(condition=condition):
                    ws = bench.make_workspace(
                        repo,
                        tmp_path / f"binary-{condition}",
                        condition,
                    )
                    try:
                        self.assertTrue(
                            (ws / "CLAUDE.md").read_bytes().startswith(authored)
                        )
                    finally:
                        bench.drop_workspace(repo, ws)

    def test_authored_context_symlink_is_rejected_without_following(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _mini_corpus(tmp_path)
            outside = tmp_path / "outside.md"
            outside.write_bytes(b"outside authored bytes\n")
            context = repo / "CLAUDE.md"
            context.unlink()
            context.symlink_to(outside)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=b@b",
                    "-c",
                    "user.name=b",
                    "commit",
                    "-qm",
                    "symlink authored context",
                ],
                cwd=repo,
                check=True,
            )

            with self.assertRaises(AtomicWriteError):
                bench.make_workspace(repo, tmp_path / "unsafe", "B")
            self.assertEqual(outside.read_bytes(), b"outside authored bytes\n")

    def test_preexisting_hologram_context_and_map_are_rejected(self):
        artifacts = {
            "claude": ("CLAUDE.md", render_managed_block("old map\n")),
            "agent": (
                "AGENTS.md",
                LEGACY_START + b"\nold map\n" + LEGACY_END + b"\n",
            ),
            "malformed": ("GEMINI.md", CONTEXT_START + b"\nunterminated\n"),
            "standalone": ("PROJECT_DIGEST.md", b"# hologram:2 stale\n"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for artifact, (name, content) in artifacts.items():
                for condition in ("B", "C"):
                    with self.subTest(artifact=artifact, condition=condition):
                        case = tmp_path / f"{artifact}-{condition}"
                        case.mkdir()
                        repo = _mini_corpus(case)
                        (repo / name).write_bytes(content)
                        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
                        subprocess.run(
                            [
                                "git",
                                "-c",
                                "user.email=b@b",
                                "-c",
                                "user.name=b",
                                "commit",
                                "-qm",
                                "preexisting hologram context",
                            ],
                            cwd=repo,
                            check=True,
                        )
                        ws = case / "workspace"
                        with self.assertRaisesRegex(
                            ValueError,
                            "preexisting Hologram",
                        ):
                            bench.make_workspace(repo, ws, condition)
                        self.assertFalse(ws.exists())

    def test_manifest_declared_custom_output_is_rejected_without_following(self):
        manifest_bytes = canonical_config_bytes(
            dataclasses.replace(
                default_config(),
                agents=(),
                output="docs/MAP.md",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for target_kind in ("regular", "symlink"):
                for condition in ("B", "C"):
                    with self.subTest(target_kind=target_kind, condition=condition):
                        case = tmp_path / f"{target_kind}-{condition}"
                        case.mkdir()
                        repo = _mini_corpus(case)
                        (repo / CONFIG_NAME).write_bytes(manifest_bytes)
                        docs = repo / "docs"
                        docs.mkdir()
                        target = docs / "MAP.md"
                        if target_kind == "regular":
                            target.write_text(BEFORE_DIGEST, encoding="utf-8")
                        else:
                            outside = case / "outside-map.md"
                            outside.write_bytes(b"outside map bytes\n")
                            target.symlink_to("../../outside-map.md")
                        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
                        subprocess.run(
                            [
                                "git",
                                "-c",
                                "user.email=b@b",
                                "-c",
                                "user.name=b",
                                "commit",
                                "-qm",
                                "custom standalone map",
                            ],
                            cwd=repo,
                            check=True,
                        )

                        error = ValueError if target_kind == "regular" else AtomicWriteError
                        with self.assertRaises(error):
                            bench.make_workspace(repo, case / "workspace", condition)
                        self.assertEqual((repo / CONFIG_NAME).read_bytes(), manifest_bytes)
                        if target_kind == "symlink":
                            self.assertEqual(
                                (case / "outside-map.md").read_bytes(),
                                b"outside map bytes\n",
                            )

    def test_failed_workspace_setup_removes_registered_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _mini_corpus(tmp_path)
            ws = tmp_path / "failed-workspace"
            try:
                with (
                    mock.patch.object(
                        benchmark_corpus,
                        "canonical_config_bytes",
                        side_effect=RuntimeError("setup failed"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "setup failed"),
                ):
                    bench.make_workspace(repo, ws, "C")

                self.assertFalse(ws.exists())
                worktrees = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertNotIn(str(ws), worktrees)
            finally:
                if ws.exists():
                    bench.drop_workspace(repo, ws)

    def test_condition_b_is_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsB", "B")
            try:
                self.assertFalse((ws / "PROJECT_DIGEST.md").exists())
                context = (ws / "CLAUDE.md").read_bytes()
                self.assertNotIn(b"PROJECT_DIGEST", context)
                self.assertNotIn(b"hologram:v2:start", context)
                self.assertFalse((ws / CONFIG_NAME).exists())
            finally:
                bench.drop_workspace(repo, ws)

    def test_historical_condition_a_is_rejected_before_worktree_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            with self.assertRaisesRegex(ValueError, "conditions B and C"):
                bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            self.assertFalse((Path(tmp) / "wsA").exists())

    def test_workspace_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsC", "C")
            try:
                (ws / "svc.py").write_text("changed")
                self.assertIn("normalize", (repo / "svc.py").read_text())
            finally:
                bench.drop_workspace(repo, ws)

    def test_corpus_claude_md_preserved_and_setup_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsC", "C")
            try:
                text = (ws / "CLAUDE.md").read_text()
                self.assertIn("Corpus conventions", text)      # corpus part kept
                self.assertIn("hologram:v2:start", text)
                diff = subprocess.run(["git", "-C", str(ws), "diff", "--stat"],
                                      capture_output=True, text=True,
                                      check=False).stdout
                self.assertEqual(diff, "")   # setup committed -> clean slate
            finally:
                bench.drop_workspace(repo, ws)


class ScheduleTest(unittest.TestCase):
    def _tasks(self) -> tuple[bench.Task, ...]:
        return tuple(
            bench.Task(
                id=f"task-{name}",
                tier="simple" if index < 2 else "complex",
                capability="orientation",
                kind="navigate",
                visibility="public",
                prompt=f"Inspect {name}.",
                accept_cmd="verify {ws} {answer}",
            )
            for index, name in enumerate(("alpha", "beta", "gamma", "delta"))
        )

    def test_run_spec_is_frozen_with_exact_fields(self):
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(RunSpec)),
            ("task", "condition", "rep", "pair_index"),
        )
        schedule = schedule_runs(
            self._tasks(), conditions=("B", "C"), reps=1, seed=20260809
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            schedule[0].rep = 2  # type: ignore[misc]

    def test_schedule_is_permutation_stable_paired_and_alternating(self):
        tasks = self._tasks()
        first = schedule_runs(
            tasks, conditions=("B", "C"), reps=1, seed=20260809
        )
        second = schedule_runs(
            tuple(reversed(tasks)),
            conditions=("C", "B"),
            reps=1,
            seed=20260809,
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(tasks) * 2)
        pairs: dict[int, list[RunSpec]] = {}
        for item in first:
            pairs.setdefault(item.pair_index, []).append(item)
        self.assertEqual(set(pairs), set(range(len(tasks))))
        for pair_index, pair in pairs.items():
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0].task, pair[1].task)
            self.assertEqual(pair[0].rep, pair[1].rep)
            expected = ("B", "C") if pair_index % 2 == 0 else ("C", "B")
            self.assertEqual(tuple(item.condition for item in pair), expected)
        self.assertNotEqual(
            first,
            schedule_runs(tasks, conditions=("B", "C"), reps=1, seed=17),
        )

    def test_schedule_rejects_asymmetry_duplicates_and_invalid_reps(self):
        tasks = self._tasks()
        for conditions, reps in ((('B',), 1), (("B", "B"), 1), (("B", "C"), 0)):
            with self.subTest(conditions=conditions, reps=reps), self.assertRaises(
                ValueError
            ):
                schedule_runs(tasks, conditions=conditions, reps=reps, seed=1)
        with self.assertRaises(ValueError):
            schedule_runs(
                (tasks[0], tasks[0]),
                conditions=("B", "C"),
                reps=1,
                seed=1,
            )


class RunOneTest(unittest.TestCase):
    def test_full_cycle_with_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="avg", tier="simple", capability="implementation",
                kind="reuse", visibility="public",
                prompt="Add average() that reuses normalize.",
                accept_cmd=f"grep -q average {{ws}}/svc.py && {PASSING_VERIFIER}",
                expect_reuse=("normalize",))

            def fake_runner(
                prompt: str, ws: Path, model: str, max_turns: int, *, config_dir: Path
            ) -> ProcessResult:
                # the "agent" appends a function that calls normalize
                (ws / "svc.py").write_text(
                    (ws / "svc.py").read_text()
                    + "\ndef average(xs: list) -> float:\n"
                      "    return sum(normalize(xs)) / len(xs)\n")
                return ProcessResult(TRANSCRIPT, "", 0)

            row = bench.run_one(repo, task, "C", rep=0,
                                results_dir=Path(tmp) / "results",
                                model=MODEL, max_turns=40,
                                runner=fake_runner)
        self.assertEqual(row["task"], "avg")
        self.assertEqual(row["condition"], "C")
        self.assertTrue(row["accepted"])
        self.assertEqual(row["reused"], ["normalize"])
        self.assertEqual(row["duplicated"], [])
        self.assertEqual(row["reads"], 3)   # Read ×2 + bash `sed -n` (see TRANSCRIPT)
        self.assertEqual(row["tokens_in"], 621000)

    def test_new_file_counts_as_change_for_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="newfile", tier="simple", capability="implementation",
                kind="reuse", visibility="public",
                prompt="Add a helper in a new module.",
                accept_cmd=(
                    "git -C {ws} diff --stat | grep -q . && " + PASSING_VERIFIER
                ),
                expect_reuse=("normalize",))

            def fake_runner(prompt, ws, model, max_turns, *, config_dir):
                (ws / "helper.py").write_text("def helper() -> int:\n    return 1\n")
                return ProcessResult(TRANSCRIPT, "", 0)

            row = bench.run_one(repo, task, "B", rep=0,
                                results_dir=Path(tmp) / "results",
                                model=MODEL, max_turns=40, runner=fake_runner)
        self.assertTrue(row["accepted"])   # untracked new file must count

    def test_transcript_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="noop", tier="simple", capability="orientation",
                kind="navigate", visibility="public", prompt="look around",
                accept_cmd=f"test -d {{ws}} && {PASSING_VERIFIER}",
            )
            results = Path(tmp) / "results"
            bench.run_one(repo, task, "B", rep=1, results_dir=results,
                          model=MODEL, max_turns=40,
                          runner=lambda *a, **k: ProcessResult(TRANSCRIPT, "", 0))
            self.assertTrue((results / "noop-B-1.jsonl").exists())

    def test_verifier_passing_max_turn_partial_edit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="partial", tier="simple", capability="implementation",
                kind="reuse", visibility="public", prompt="Add partial().",
                accept_cmd=f"grep -q partial {{ws}}/svc.py && {PASSING_VERIFIER}",
                expect_reuse=("normalize",),
            )
            transcript = TranscriptMetricsTest()._terminal(
                subtype="error_max_turns",
                is_error=True,
                answer="Partial edit only.",
            )

            def fake_runner(prompt, ws, model, max_turns, *, config_dir):
                (ws / "svc.py").write_text(
                    (ws / "svc.py").read_text()
                    + "\ndef partial() -> None:\n    pass\n"
                )
                return ProcessResult(transcript, "", 0)

            row = bench.run_one(
                repo,
                task,
                "B",
                rep=0,
                results_dir=Path(tmp) / "results",
                model=MODEL,
                max_turns=40,
                runner=fake_runner,
            )
        self.assertEqual(row["terminal_status"], "error_max_turns")
        self.assertFalse(row["completed"])
        self.assertTrue(row["verifier_passed"])
        self.assertFalse(row["accepted"])

    def test_final_answer_is_saved_and_available_to_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="answer", tier="simple", capability="orientation",
                kind="navigate", visibility="public", prompt="Answer.",
                accept_cmd=(
                    "grep -q 'Completed the task' {answer} && test -d {ws} && "
                    + PASSING_VERIFIER
                ),
            )
            results = Path(tmp) / "results"
            row = bench.run_one(
                repo,
                task,
                "B",
                rep=0,
                results_dir=results,
                model=MODEL,
                max_turns=40,
                runner=lambda *args, **kwargs: ProcessResult(TRANSCRIPT, "", 0),
                claude_code_version="2.1.224",
                corpus_revision="a" * 40,
                seed=20260809,
                pair_index=7,
                challenged_tree_sha256="b" * 64,
                workspace_asset_sha256="c" * 64,
            )
            answer = results / "answer-B-0.answer.txt"
            self.assertEqual(answer.read_text(), "Completed the task.")
            verifier_log = (results / "answer-B-0.verifier.log").read_text()
        self.assertTrue(row["completed"])
        self.assertTrue(row["verifier_passed"])
        self.assertTrue(row["accepted"])
        self.assertEqual(row["model"], MODEL)
        self.assertEqual(row["claude_code_version"], "2.1.224")
        self.assertEqual(row["corpus_revision"], "a" * 40)
        self.assertEqual(row["seed"], 20260809)
        self.assertEqual(row["pair_index"], 7)
        self.assertEqual(row["challenged_tree_sha256"], "b" * 64)
        self.assertEqual(row["workspace_asset_sha256"], "c" * 64)
        self.assertEqual(row["tier"], "simple")
        self.assertEqual(row["capability"], "orientation")
        self.assertEqual(row["visibility"], "public")
        self.assertEqual(row["rubric_score"], 1.0)
        self.assertIn('"passed":true', verifier_log)


class RunnerIsolationTest(unittest.TestCase):
    def test_runner_api_signatures_are_exact(self):
        self.assertEqual(
            tuple(inspect.signature(bench.claude_version).parameters),
            ("run",),
        )
        self.assertEqual(
            tuple(inspect.signature(bench.claude_runner).parameters),
            ("prompt", "workspace", "model", "max_turns", "config_dir"),
        )

    def test_version_is_normalized_and_invocation_is_argv_only(self):
        completed = subprocess.CompletedProcess(
            ["claude", "--version"],
            0,
            "2.1.224 (Claude Code)\n",
            "",
        )
        run = mock.Mock(return_value=completed)
        self.assertEqual(bench.claude_version(run=run), "2.1.224")
        self.assertEqual(run.call_args.args[0], ["claude", "--version"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

        run.return_value = subprocess.CompletedProcess(
            ["claude", "--version"], 9, "", "broken"
        )
        with self.assertRaises(ValueError):
            bench.claude_version(run=run)

    def test_runner_pins_arguments_and_isolates_only_child_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            config = root / "config"
            config.mkdir()
            completed = subprocess.CompletedProcess([], 0, TRANSCRIPT, "warning")
            with (
                mock.patch.dict(
                    os.environ,
                    {"BENCH_CREDENTIAL_SENTINEL": "inherited"},
                    clear=False,
                ),
                mock.patch.object(bench.subprocess, "run", return_value=completed) as run,
            ):
                result = bench.claude_runner(
                    "Do the task",
                    workspace,
                    MODEL,
                    40,
                    config_dir=config,
                )
            self.assertEqual(result, ProcessResult(TRANSCRIPT, "warning", 0))
            argv = run.call_args.args[0]
            self.assertEqual(argv[0:3], ["claude", "-p", "Do the task"])
            self.assertEqual(argv[argv.index("--model") + 1], MODEL)
            self.assertEqual(argv[argv.index("--max-turns") + 1], "40")
            self.assertEqual(run.call_args.kwargs["cwd"], workspace)
            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env["BENCH_CREDENTIAL_SENTINEL"], "inherited")
            self.assertEqual(child_env["CLAUDE_CONFIG_DIR"], str(config.resolve()))
            self.assertEqual(
                child_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1"
            )
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)
            self.assertEqual(tuple(config.iterdir()), ())

    def test_timeout_preserves_partial_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            config = root / "config"
            workspace.mkdir()
            config.mkdir()
            timeout = subprocess.TimeoutExpired(
                ["claude"],
                1800,
                output=b"partial stdout",
                stderr="partial stderr",
            )
            with mock.patch.object(bench.subprocess, "run", side_effect=timeout):
                result = bench.claude_runner(
                    "Do the task",
                    workspace,
                    MODEL,
                    40,
                    config_dir=config,
                )
        self.assertEqual(result.stdout, "partial stdout")
        self.assertEqual(result.stderr, "partial stderr")
        self.assertEqual(result.returncode, 124)
        self.assertTrue(result.timed_out)

    def test_pair_members_receive_distinct_fresh_config_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            results = root / "results"
            task = bench.Task(
                id="isolated", tier="simple", capability="implementation",
                kind="reuse", visibility="public", prompt="Inspect isolation.",
                accept_cmd=f"test -d {{ws}} && {PASSING_VERIFIER}",
                expect_reuse=("normalize",),
            )
            seen: list[Path] = []

            def runner(prompt, workspace, model, max_turns, *, config_dir):
                seen.append(config_dir)
                self.assertTrue(config_dir.is_dir())
                self.assertEqual(tuple(config_dir.iterdir()), ())
                return ProcessResult(TRANSCRIPT, "", 0)

            for condition in ("B", "C"):
                bench.run_one(
                    repo,
                    task,
                    condition,
                    rep=0,
                    results_dir=results,
                    model=MODEL,
                    max_turns=40,
                    runner=runner,
                )
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0], seen[1])
            self.assertTrue(all(path.is_relative_to(results) for path in seen))

    def test_run_rejects_nonempty_results_and_incomplete_pair_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            _write_tiered_taskfile(taskfile, repo)
            results = root / "results"
            results.mkdir()
            (results / "prior.jsonl").write_text("old\n")
            with (
                mock.patch.object(bench, "run_one") as run,
                self.assertRaises(ValueError),
            ):
                bench.main(
                    [
                        "run", str(taskfile), "--corpus", str(repo),
                        "--results", str(results), "--dry-run",
                    ]
                )
            run.assert_not_called()

            (results / "prior.jsonl").unlink()
            with (
                mock.patch.object(bench, "run_one") as run,
                self.assertRaises(ValueError),
            ):
                bench.main(
                    [
                        "run", str(taskfile), "--corpus", str(repo),
                        "--results", str(results), "--conditions", "B",
                        "--dry-run",
                    ]
                )
            run.assert_not_called()

    def test_paid_matrix_requires_exact_cli_version_before_any_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            _write_tiered_taskfile(taskfile, repo)
            with (
                mock.patch.object(bench, "claude_version", return_value="2.1.223"),
                mock.patch.object(bench, "run_one") as run,
                self.assertRaises(ValueError),
            ):
                bench.main(
                    [
                        "run", str(taskfile), "--corpus", str(repo),
                        "--results", str(root / "results"),
                    ]
                )
            run.assert_not_called()


class ReportTest(unittest.TestCase):
    def test_aggregates_by_condition(self):
        rows = [
            {"task": "t1", "kind": "reuse", "condition": "A", "rep": 0,
             "accepted": True, "reused": ["normalize"], "duplicated": [],
             "new_lines": 1, "reads": 3, "searches": 2, "edits": 2,
             "turns": 9, "tokens_in": 100, "tokens_out": 10},
            {"task": "t1", "kind": "reuse", "condition": "B", "rep": 0,
             "accepted": True, "reused": [], "duplicated": ["normalize2"],
             "new_lines": 2, "reads": 9, "searches": 8, "edits": 2,
             "turns": 15, "tokens_in": 300, "tokens_out": 30},
        ]
        md = bench.report(rows)
        self.assertIn("| A |", md)
        self.assertIn("| B |", md)
        self.assertIn("legacy / unclassified", md)
        self.assertIn("unique tasks", md)
        self.assertNotIn("normalize", md)
        self.assertNotIn("digest hits", md)

    def test_empty_rows(self):
        self.assertIn("no runs", bench.report([]))


def _write_tiered_taskfile(path: Path, repo: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus": {
                    "name": "example",
                    "visibility": "public",
                    "url": "https://example.com/example.git",
                    "revision": _head(repo),
                    "path_env": "HOLOGRAM_BENCH_EXAMPLE",
                },
                "tasks": [
                    {
                        "id": "noop-simple",
                        "tier": "simple",
                        "capability": "implementation",
                        "kind": "reuse",
                        "visibility": "public",
                        "prompt": "Exercise the simple harness path.",
                        "accept_cmd": "test -d {ws}",
                        "expect_reuse": ["normalize"],
                    },
                    {
                        "id": "noop-complex",
                        "tier": "complex",
                        "capability": "implementation",
                        "kind": "reuse",
                        "visibility": "public",
                        "prompt": "Exercise the complex harness path.",
                        "accept_cmd": "test -d {ws}",
                        "expect_reuse": ["normalize"],
                    },
                ],
                "model": "claude-sonnet-5",
                "claude_code_version": "2.1.224",
                "max_turns": 40,
                "conditions": ["B", "C"],
                "reps": 1,
                "seed": 20260809,
            }
        ),
        encoding="utf-8",
    )


class CliTest(unittest.TestCase):
    def test_prepare_accepts_an_absent_destination_before_cloning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            _write_tiered_taskfile(taskfile, repo)
            destination = root / "prepared"
            self.assertFalse(destination.exists())

            with mock.patch.object(
                bench,
                "prepare_public_corpus",
                return_value=destination.resolve(),
            ) as prepare:
                self.assertEqual(
                    bench.main(
                        ["prepare", str(taskfile), "--corpus", str(destination)]
                    ),
                    0,
                )

            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.args[1], destination)

    def test_run_rejects_mismatched_pair_asset_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            _write_tiered_taskfile(taskfile, repo)
            results = root / "results"
            common = {
                "task": "noop-simple",
                "rep": 0,
                "pair_index": 0,
                "challenged_tree_sha256": "a" * 64,
            }
            rows = (
                {**common, "condition": "B", "workspace_asset_sha256": "b" * 64},
                {**common, "condition": "C", "workspace_asset_sha256": "c" * 64},
            )

            with (
                mock.patch.object(bench, "run_one", side_effect=rows),
                mock.patch.object(bench, "claude_version", return_value="2.1.224"),
                self.assertRaisesRegex(ValueError, "pair provenance"),
            ):
                bench.main(
                    [
                        "run",
                        str(taskfile),
                        "--corpus",
                        str(repo),
                        "--results",
                        str(results),
                        "--only",
                        "noop-simple",
                    ]
                )

    def test_active_condition_defaults_are_b_and_c_and_a_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            _write_tiered_taskfile(taskfile, repo)
            results = Path(tmp) / "results"
            with mock.patch.object(
                bench,
                "run_one",
                side_effect=AssertionError("dry run executed a session"),
            ) as run:
                self.assertEqual(
                    bench.main(
                        [
                            "run",
                            str(taskfile),
                            "--corpus",
                            str(repo),
                            "--only",
                            "noop-simple",
                            "--results",
                            str(results),
                            "--dry-run",
                        ]
                    ),
                    0,
                )
            run.assert_not_called()
            rows = tuple(
                json.loads(line)
                for line in (results / "runs.jsonl").read_text().splitlines()
            )
            self.assertEqual([row["condition"] for row in rows], ["B", "C"])

            with self.assertRaises(SystemExit) as caught:
                bench.main(
                    [
                        "run",
                        str(taskfile),
                        "--corpus",
                        str(repo),
                        "--results",
                        str(results),
                        "--conditions",
                        "A",
                        "--dry-run",
                    ]
                )
            self.assertEqual(caught.exception.code, 2)

    def test_run_writes_jsonl_and_report_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            _write_tiered_taskfile(taskfile, repo)
            results = Path(tmp) / "results"
            code = bench.main(["run", str(taskfile),
                               "--corpus", str(repo),
                               "--results", str(results),
                               "--reps", "1",
                               "--only", "noop-simple",
                               "--dry-run"])
            self.assertEqual(code, 0)
            rows = [json.loads(l) for l in
                    (results / "runs.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["condition"] for row in rows}, {"B", "C"})

            code = bench.main(["report", "--results", str(results)])
            self.assertEqual(code, 0)
            self.assertTrue((results / "report.md").exists())


class V2ConsumerMigrationTest(unittest.TestCase):
    def test_benchmark_reads_symbols_through_decoder(self) -> None:
        verdict = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED, ["normalize"])

        self.assertEqual(verdict["reused"], ["normalize"])
        self.assertEqual(
            [symbol.symbol_id.name for symbol in verdict["new_lines"]],
            ["weightedAverage"],
        )

    def test_condition_c_uses_managed_claude_block_without_legacy_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsC", "C")
            try:
                context = (ws / "CLAUDE.md").read_bytes()
                self.assertIn(b"hologram:v2:start", context)
                self.assertNotIn(b"--embed", context)
                self.assertFalse((ws / "PROJECT_DIGEST.md").exists())
            finally:
                bench.drop_workspace(repo, ws)

    def test_benchmark_readme_labels_a_historical_and_b_c_current(self) -> None:
        readme = (
            Path(__file__).resolve().parents[1] / "benchmark" / "README.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Historical condition A", readme)
        self.assertIn("current conditions B and C", readme)
        self.assertIn("managed canonical v2 block", readme)
        for caveat in (
            "legacy, exploratory, and pre-tier",
            "`sonnet` is a mutable model alias",
            "reuse acceptance commands often verify only that a change occurred",
            "navigation correctness is not automated (`true`)",
            "40-turn ceiling is not outcome-gated",
            "n=1 per cell",
        ):
            with self.subTest(caveat=caveat):
                self.assertIn(caveat, readme)


if __name__ == "__main__":
    unittest.main()
