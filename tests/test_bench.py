import dataclasses
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))

import bench

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
        p = tmp / "tasks.json"
        p.write_text(json.dumps({
            "corpus": "~/workspace/some-private-repo",
            "model": "sonnet",
            "max_turns": 40,
            "tasks": [
                {"id": "weighted-avg", "kind": "reuse",
                 "prompt": "Add a weighted average.",
                 "accept_cmd": "grep -rq weightedAverage {ws}/src",
                 "expect_reuse": ["normalize", "add"]},
                {"id": "find-lifecycle", "kind": "navigate",
                 "prompt": "Where is record lifecycle handled?",
                 "accept_cmd": "true",
                 "expect_reuse": []},
            ],
        }))
        return p

    def test_loads_tasks_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = bench.load_tasks(self._taskfile(Path(tmp)))
        self.assertEqual(cfg.model, "sonnet")
        self.assertEqual(cfg.max_turns, 40)
        self.assertEqual(len(cfg.tasks), 2)
        self.assertEqual(cfg.tasks[0].id, "weighted-avg")
        self.assertEqual(cfg.tasks[0].expect_reuse, ["normalize", "add"])
        self.assertTrue(str(cfg.corpus).startswith("/"))  # ~ expanded

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"corpus": ".", "tasks": [{"id": "x"}]}))
            with self.assertRaises(SystemExit):
                bench.load_tasks(p)


TRANSCRIPT = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
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
    json.dumps({"type": "result", "num_turns": 7,
                "usage": {"input_tokens": 91000, "output_tokens": 4200,
                          "cache_creation_input_tokens": 30000,
                          "cache_read_input_tokens": 500000}}),
    "not-json-noise",
])


class TranscriptMetricsTest(unittest.TestCase):
    def test_counts_and_usage(self):
        m = bench.parse_transcript(TRANSCRIPT)
        self.assertEqual(m["reads"], 3)      # Read ×2 + bash sed -n
        self.assertEqual(m["searches"], 3)   # Grep + bash grep ×2
        self.assertEqual(m["edits"], 2)      # Edit + Write
        self.assertNotIn("digest_hits", m)
        self.assertEqual(m["turns"], 7)
        self.assertEqual(m["tokens_in"], 91000 + 30000 + 500000)
        self.assertEqual(m["tokens_out"], 4200)

    def test_empty_transcript_gives_zeroes(self):
        m = bench.parse_transcript("")
        self.assertEqual(m, {"reads": 0, "searches": 0, "edits": 0,
                             "turns": 0, "tokens_in": 0, "tokens_out": 0})


def _render_symbol(name: str, calls: tuple[str, ...] = ()) -> RenderSymbol:
    return RenderSymbol(
        SymbolId(Language.PYTHON, "svc.py", (), SymbolKind.FUNCTION, name, "()"),
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
    return repo


class WorkspaceTest(unittest.TestCase):
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
                        bench,
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


class RunOneTest(unittest.TestCase):
    def test_full_cycle_with_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="avg", kind="reuse",
                prompt="Add average() that reuses normalize.",
                accept_cmd="grep -q average {ws}/svc.py",
                expect_reuse=["normalize"])

            def fake_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
                # the "agent" appends a function that calls normalize
                (ws / "svc.py").write_text(
                    (ws / "svc.py").read_text()
                    + "\ndef average(xs: list) -> float:\n"
                      "    return sum(normalize(xs)) / len(xs)\n")
                return TRANSCRIPT

            row = bench.run_one(repo, task, "C", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40,
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
                id="newfile", kind="reuse",
                prompt="Add a helper in a new module.",
                accept_cmd="git -C {ws} diff --stat | grep -q .",
                expect_reuse=[])

            def fake_runner(prompt, ws, model, max_turns):
                (ws / "helper.py").write_text("def helper() -> int:\n    return 1\n")
                return TRANSCRIPT

            row = bench.run_one(repo, task, "B", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40, runner=fake_runner)
        self.assertTrue(row["accepted"])   # untracked new file must count

    def test_transcript_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="noop", kind="navigate", prompt="look around",
                              accept_cmd="true", expect_reuse=[])
            results = Path(tmp) / "results"
            bench.run_one(repo, task, "B", rep=1, results_dir=results,
                          model="sonnet", max_turns=40,
                          runner=lambda *a, **k: TRANSCRIPT)
            self.assertTrue((results / "noop-B-1.jsonl").exists())


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
        # duplication rate: A 0%, B 100% of reuse-kind runs
        self.assertIn("0%", md)
        self.assertIn("100%", md)
        self.assertIn("reads", md)
        self.assertNotIn("digest hits", md)

    def test_empty_rows(self):
        self.assertIn("no runs", bench.report([]))


class CliTest(unittest.TestCase):
    def test_active_condition_defaults_are_b_and_c_and_a_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            taskfile.write_text(
                json.dumps(
                    {
                        "corpus": str(repo),
                        "tasks": [
                            {
                                "id": "noop",
                                "kind": "navigate",
                                "prompt": "count files",
                                "accept_cmd": "true",
                            }
                        ],
                    }
                )
            )
            results = Path(tmp) / "results"
            row = {
                "task": "noop",
                "kind": "navigate",
                "condition": "unused",
                "rep": 0,
                "accepted": True,
                "reused": [],
                "duplicated": [],
                "new_lines": 0,
            }
            with mock.patch.object(bench, "run_one", return_value=row) as run:
                self.assertEqual(
                    bench.main(
                        [
                            "run",
                            str(taskfile),
                            "--results",
                            str(results),
                            "--dry-run",
                        ]
                    ),
                    0,
                )
            self.assertEqual([call.args[2] for call in run.call_args_list], ["B", "C"])

            with self.assertRaises(SystemExit) as caught:
                bench.main(
                    [
                        "run",
                        str(taskfile),
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
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "noop", "kind": "navigate",
                           "prompt": "count files",
                           "accept_cmd": "true", "expect_reuse": []}],
            }))
            results = Path(tmp) / "results"
            code = bench.main(["run", str(taskfile),
                               "--results", str(results),
                               "--conditions", "C", "--reps", "1",
                               "--dry-run"])
            self.assertEqual(code, 0)
            rows = [json.loads(l) for l in
                    (results / "runs.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["condition"], "C")

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
