import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))

import bench  # noqa: E402


class TaskLoaderTest(unittest.TestCase):
    def _taskfile(self, tmp: Path) -> Path:
        p = tmp / "tasks.json"
        p.write_text(json.dumps({
            "corpus": "~/workspace/demo-repo",
            "model": "sonnet",
            "max_turns": 40,
            "tasks": [
                {"id": "format-title", "kind": "reuse",
                 "prompt": "Add title formatting.",
                 "accept_cmd": "grep -rq formatTitle {ws}/src",
                 "expect_reuse": ["trim_spaces", "join_parts"]},
                {"id": "find-renderer", "kind": "fix",
                 "prompt": "Where is theme rendering handled?",
                 "accept_cmd": "true",
                 "expect_reuse": [],
                 "expect_answer": ["ThemeRenderer", r"themes\.py"],
                 "max_turns": 8},
            ],
        }))
        return p

    def test_loads_tasks_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = bench.load_tasks(self._taskfile(Path(tmp)))
        self.assertEqual(cfg.model, "sonnet")
        self.assertEqual(cfg.max_turns, 40)
        self.assertEqual(len(cfg.tasks), 2)
        self.assertEqual(cfg.tasks[0].id, "format-title")
        self.assertEqual(cfg.tasks[0].expect_reuse,
                         ["trim_spaces", "join_parts"])
        self.assertEqual(cfg.tasks[0].expect_answer, [])
        self.assertIsNone(cfg.tasks[0].max_turns)
        self.assertEqual(cfg.tasks[1].expect_answer,
                         ["ThemeRenderer", r"themes\.py"])
        self.assertEqual(cfg.tasks[1].max_turns, 8)  # session-length dial
        self.assertFalse(cfg.tasks[1].manual_only)
        self.assertEqual(cfg.tasks[1].accept_pass_codes, [0])
        self.assertEqual(cfg.tasks[1].accept_fail_codes, [1])
        self.assertFalse(cfg.tasks[1].semantic_judge)
        self.assertEqual(cfg.tasks[1].judge, {})
        self.assertTrue(str(cfg.corpus).startswith("/"))  # ~ expanded

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"corpus": ".", "tasks": [{"id": "x"}]}))
            with self.assertRaises(SystemExit):
                bench.load_tasks(p)

    def test_rejects_duplicate_unsafe_ids_and_bad_regex_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({
                "corpus": ".", "budget": 0,
                "tasks": [
                    {"id": "../same", "kind": "wrong", "prompt": "x",
                     "accept_cmd": "echo {unknown}", "expect_answer": ["("]},
                    {"id": "../same", "kind": "reuse", "prompt": "y",
                     "accept_cmd": "true"},
                ],
            }))
            with self.assertRaisesRegex(SystemExit, "unsafe task id"):
                bench.load_tasks(p)

    def test_rejects_malformed_expectation_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad-shapes.json"
            p.write_text(json.dumps({
                "corpus": ".", "lang": "python",
                "tasks": [{
                    "id": "shape", "kind": "reuse", "prompt": "x",
                    "accept_cmd": "true", "expect_reuse": [7],
                    "expect_answer": "ok", "expect_in_new_code": [8],
                    "scope_in_tests": "yes",
                }],
            }))
            with self.assertRaisesRegex(SystemExit, "expect_reuse must be"):
                bench.load_tasks(p)

    def test_rejects_task_id_too_long_for_artifact_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "long-id.json"
            p.write_text(json.dumps({
                "corpus": ".",
                "tasks": [{"id": "x" * 129, "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            with self.assertRaisesRegex(SystemExit, "unsafe task id"):
                bench.load_tasks(p)

    def test_rejects_unknown_fields_instead_of_silently_dropping_typos(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "typo.json"
            p.write_text(json.dumps({
                "corpus": ".",
                "tasks": [{"id": "inspect", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true",
                           "manual_ony": True}],
            }))
            with self.assertRaisesRegex(SystemExit, "manual_ony"):
                bench.load_tasks(p)

    def test_rejects_overlapping_or_malformed_acceptance_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "codes.json"
            p.write_text(json.dumps({
                "corpus": ".",
                "tasks": [{"id": "inspect", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true",
                           "accept_pass_codes": [0, 1],
                           "accept_fail_codes": [1]}],
            }))
            with self.assertRaisesRegex(SystemExit, "must be disjoint"):
                bench.load_tasks(p)

    def test_loads_manual_and_judge_metadata_for_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "manual.json"
            p.write_text(json.dumps({
                "corpus": ".",
                "tasks": [{"id": "inspect", "kind": "navigate",
                           "prompt": "Inspect the public demo.",
                           "accept_cmd": "true", "manual_only": True,
                           "judge": {"rubric": "authorized-demo-v1"}}],
            }))
            cfg = bench.load_tasks(p)
        self.assertTrue(cfg.tasks[0].manual_only)
        self.assertEqual(cfg.tasks[0].judge["rubric"], "authorized-demo-v1")
        self.assertRegex(bench._judge_config_revision(cfg.tasks[0]),
                         r"^judge-[0-9a-f]{20}$")

    def test_every_publishable_task_file_validates(self):
        task_dir = Path(__file__).resolve().parents[1] / "benchmark" / "tasks"
        paths = [path for path in sorted(task_dir.glob("*.json"))
                 if not path.name.startswith("local-")]
        configs = [bench.load_tasks(path) for path in paths]
        self.assertTrue(configs)


TRANSCRIPT = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.java"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "trim_spaces"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "grep -rn hasText spring-core/src"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "sed -n '1,40p' StringUtils.java"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "grep -n trimToNull spring-core/src"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": "/ws/StringUtils.java"}}]}}),
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
        self.assertEqual(m["turns"], 7)
        self.assertEqual(m["input_tokens"], 91000)
        self.assertEqual(m["cache_creation_input_tokens"], 30000)
        self.assertEqual(m["cache_read_input_tokens"], 500000)
        self.assertEqual(m["tokens_in_fresh"], 91000)
        self.assertEqual(m["tokens_in_cache_created"], 30000)
        self.assertEqual(m["tokens_in_cache_read"], 500000)
        self.assertEqual(m["tokens_in"], 91000 + 30000 + 500000)
        self.assertEqual(m["tokens_out"], 4200)

    def test_empty_transcript_gives_zeroes(self):
        m = bench.parse_transcript("")
        self.assertEqual(m, {"reads": 0, "searches": 0, "edits": 0,
                             "turns": 0, "input_tokens": 0,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0,
                             "tokens_in_fresh": 0,
                             "tokens_in_cache_created": 0,
                             "tokens_in_cache_read": 0,
                             "tokens_in": 0, "tokens_out": 0,
                             "files_read": 0, "result_text": "",
                             "review_seen": False,
                             "review_action_proxy": False,
                             "acted_on_findings": False})

    def test_result_text_and_distinct_files_read(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/a.py"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/a.py"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/b.py"}}]}}),
            json.dumps({"type": "result", "num_turns": 3,
                        "result": "The value is 42.",
                        "usage": {"input_tokens": 1, "output_tokens": 2}}),
        ]
        m = bench.parse_transcript("\n".join(lines))
        self.assertEqual(m["reads"], 3)
        self.assertEqual(m["files_read"], 2)   # distinct paths
        self.assertEqual(m["result_text"], "The value is 42.")


BEFORE_DIGEST = """# hologram · 100 LOC · state aaa
· C/R/I{fields} E{values} T:target · f(args):Ret > project calls
src
 text
  TextTools(C)
   join_parts(left,right):str
   trim_spaces(values):list[str] > join_parts
"""

AFTER_REUSED = BEFORE_DIGEST + """\
  LabelBuilder(C)
   render_label(values):str > TextTools.join_parts,trim_spaces
"""

AFTER_DUPLICATED = BEFORE_DIGEST + """\
  LabelBuilder(C)
   trim_spaces_copy(values):list[str]
   render_label(values):str > trim_spaces_copy
"""


class DuplicationDetectorTest(unittest.TestCase):
    def test_reuse_detected(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED,
                              ["trim_spaces", "join_parts"])
        self.assertEqual(sorted(v["reused"]), ["join_parts", "trim_spaces"])
        self.assertEqual(v["duplicated"], [])

    def test_duplicate_detected(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_DUPLICATED, ["trim_spaces"])
        self.assertEqual(v["reused"], [])
        self.assertEqual(v["duplicated"], ["trim_spaces_copy"])

    def test_new_lines_listed(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED, [])
        self.assertTrue(any("render_label" in ln for ln in v["new_lines"]))

    def test_no_change_is_clean(self):
        v = bench.judge_reuse(BEFORE_DIGEST, BEFORE_DIGEST, ["trim_spaces"])
        self.assertEqual(v, {"new_lines": [], "reused": [], "duplicated": []})


import subprocess  # noqa: E402


def _mini_corpus(tmp: Path) -> Path:
    repo = tmp / "corpus"
    repo.mkdir()
    (repo / "svc.py").write_text(
        "def trim_spaces(items: list) -> list:\n    return items\n")
    (repo / "CLAUDE.md").write_text("# Corpus conventions\nUse tabs. Just kidding.\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b",
                    "commit", "-qm", "seed"], cwd=repo, check=True)
    return repo


class ExperimentIdentityTest(unittest.TestCase):
    def test_setup_failure_marks_structured_review_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="setup", kind="navigate", prompt="x",
                              accept_cmd="true")
            config = bench.Config(corpus=repo, tasks=[task], revision="cfg")
            experiment = bench._experiment_spec(
                config, "dry-run", False, 1, conditions=["AR"], reps=1,
                tasks=[task])
            cell = bench._cell_spec(
                experiment["experiment_id"], task, "AR", 0, 40, None)
            row = bench._setup_failure_row(
                task=task, condition="AR", rep=0, config=config,
                experiment=experiment, cell=cell,
                condition_order=["AR"], order_index=0,
                runner_mode="dry-run", error=RuntimeError("setup"),
                wave_id="wave-test",
                wave_started_at="2026-01-01T00:00:00+00:00",
                execution_index=1, block_index=0)
        self.assertEqual(row["review_findings"]["status"], "not_run")
        self.assertIsNone(row["review_findings"]["final_count"])

    def test_identity_is_exact_and_dry_runs_cannot_match_host_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="inspect", kind="navigate", prompt="Inspect.",
                              accept_cmd="true")
            cfg = bench.Config(corpus=repo, tasks=[task], revision="cfg-demo")
            dry = bench._experiment_spec(cfg, "dry-run", False, 17)
            same = bench._experiment_spec(cfg, "dry-run", False, 17)
            host = bench._experiment_spec(cfg, "unsafe-host", True, 17)
            other_runtime = bench._experiment_spec(
                cfg, "dry-run", False, 17,
                runner_provenance={"runner": "dry", "version": "other"})
            matrix = bench._experiment_spec(
                cfg, "dry-run", False, 17, conditions=["A", "B"],
                reps=1, tasks=[task])
            a = bench._cell_spec(dry["experiment_id"], task, "A", 0, 40,
                                 None)
            b = bench._cell_spec(dry["experiment_id"], task, "B", 0, 40,
                                 None)
        self.assertEqual(dry["experiment_id"], same["experiment_id"])
        self.assertNotEqual(dry["experiment_id"], host["experiment_id"])
        self.assertNotEqual(dry["experiment_id"],
                            other_runtime["experiment_id"])
        self.assertNotEqual(dry["experiment_id"], matrix["experiment_id"])
        self.assertNotEqual(a["cell_id"], b["cell_id"])
        self.assertEqual(a["pair_id"], b["pair_id"])

    def test_counterbalanced_schedule_records_rotating_orders(self):
        tasks = [bench.Task(id="one", kind="navigate", prompt="x",
                            accept_cmd="true"),
                 bench.Task(id="two", kind="navigate", prompt="x",
                            accept_cmd="true")]
        schedule = bench._counterbalanced_schedule(tasks, ["A", "B"], 2, 9)
        blocks = [schedule[index:index + 2]
                  for index in range(0, len(schedule), 2)]
        self.assertEqual({tuple(item[0]["condition_order"]) for item in blocks},
                         {("A", "B"), ("B", "A")})
        self.assertTrue(all(item[entry["order_index"]]["condition"]
                            == entry["condition"]
                            for item in blocks for entry in item))

    def test_atomic_jsonl_append_preserves_complete_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.jsonl"
            bench._append_jsonl_atomic(path, {"run_id": "first", "value": 1})
            bench._append_jsonl_atomic(path, {"run_id": "second", "value": 2})
            rows = bench._read_rows(path)
        self.assertEqual([row["run_id"] for row in rows], ["first", "second"])

    def test_atomic_append_rejects_preexisting_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.jsonl"
            path.write_text('{"run_id":"partial"}')
            with self.assertRaisesRegex(RuntimeError, "partial JSONL"):
                bench._append_jsonl_atomic(path, {"run_id": "next"})
            self.assertEqual(path.read_text(), '{"run_id":"partial"}')

    def test_only_fully_observed_cells_are_terminal_for_resume(self):
        self.assertTrue(bench._terminal_cell({
            "schema_version": 2, "runner_status": "ok",
            "acceptance_status": "nonzero"}))
        self.assertFalse(bench._terminal_cell({
            "schema_version": 2, "runner_status": "timeout",
            "acceptance_status": "ok"}))
        self.assertFalse(bench._terminal_cell({"schema_version": 2}))
        self.assertTrue(bench._terminal_cell({
            "schema_version": 3, "runner_status": "ok",
            "acceptance_status": "nonzero", "acceptance_verdict": "fail"}))
        self.assertFalse(bench._terminal_cell({
            "schema_version": 3, "runner_status": "ok",
            "acceptance_status": "nonzero", "acceptance_verdict": None,
            "acceptance_infra_reason": "unexpected_exit:127"}))
        self.assertFalse(bench._terminal_cell({
            "schema_version": 3, "condition": "AR",
            "runner_mode": "unsafe-host", "runner_status": "ok",
            "acceptance_status": "ok", "acceptance_verdict": "pass",
            "review_findings": {"status": "incomplete"},
        }))
        self.assertTrue(bench._terminal_cell({
            "schema_version": 3, "condition": "AR",
            "runner_mode": "dry-run", "runner_status": "ok",
            "acceptance_status": "skipped_dry_run",
            "review_findings": {"status": "not_applicable"},
        }))

    def test_resumable_block_requires_complete_same_wave_observations(self):
        base = {
            "schema_version": 3, "runner_status": "ok",
            "runner_mode": "unsafe-host", "acceptance_status": "ok",
            "acceptance_verdict": "pass", "wave_id": "wave-one",
            "resolved_model": "fixed-model",
        }
        treatment = {
            **base, "condition": "AR",
            "review_findings": {"status": "ok"},
        }
        control = {**base, "condition": "B"}
        self.assertTrue(bench._resumable_block([treatment, control]))
        self.assertFalse(bench._resumable_block([
            {**treatment, "review_findings": {"status": "incomplete"}},
            control,
        ]))
        self.assertFalse(bench._resumable_block([
            treatment, {**control, "wave_id": "wave-two"},
        ]))


class WorkspaceTest(unittest.TestCase):
    def test_condition_a_embeds_the_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                self.assertIn("hologram:start", (ws / "CLAUDE.md").read_text())
                self.assertTrue((ws / "svc.py").exists())
            finally:
                bench.drop_workspace(repo, ws)

    def test_condition_b_is_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsB", "B")
            try:
                self.assertNotIn("hologram:start", (ws / "CLAUDE.md").read_text())
            finally:
                bench.drop_workspace(repo, ws)

    def test_workspace_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                (ws / "svc.py").write_text("changed")
                self.assertIn("trim_spaces", (repo / "svc.py").read_text())
            finally:
                bench.drop_workspace(repo, ws)

    def test_outside_paths_in_context_files_are_confined(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            (repo / "CLAUDE.md").write_text(
                "# Corpus conventions\n"
                "Work directly in /Users/somebody/workspace/realproj on main.\n"
                "Inside ref: /Users/somebody/workspace/realproj/docs/x.md\n")
            subprocess.run(["git", "-c", "user.email=b@b", "-c",
                            "user.name=b", "commit", "-aqm", "paths"],
                           cwd=repo, check=True)
            ws = bench.make_workspace(repo, Path(tmp) / "wsP", "B")
            try:
                text = (ws / "CLAUDE.md").read_text()
                self.assertNotIn("/Users/somebody", text)
                self.assertIn(str(ws), text)
                self.assertIn("Work ONLY inside", text)  # confinement note
            finally:
                bench.drop_workspace(repo, ws)

    def test_context_symlink_cannot_escape_throwaway_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            outside = root / "outside.md"
            outside.write_text("do not touch\n")
            (repo / "CLAUDE.md").unlink()
            (repo / "CLAUDE.md").symlink_to(outside)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.email=b@b", "-c",
                            "user.name=b", "commit", "-qm", "symlink"],
                           cwd=repo, check=True)
            ws = root / "ws-link"
            with self.assertRaisesRegex(RuntimeError, "symlink|escapes"):
                bench.make_workspace(repo, ws, "B")
            self.assertEqual(outside.read_text(), "do not touch\n")
            if ws.exists():
                bench.drop_workspace(repo, ws)

    def test_dangling_claude_symlink_cannot_create_outside_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            outside = root / "not-created.md"
            (repo / "CLAUDE.md").unlink()
            (repo / "CLAUDE.md").symlink_to(outside)
            (repo / "AGENTS.md").write_text("regular context\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.email=b@b", "-c",
                            "user.name=b", "commit", "-qm", "dangling"],
                           cwd=repo, check=True)
            ws = root / "ws-dangling"
            with self.assertRaisesRegex(RuntimeError, "symlink|escapes"):
                bench.make_workspace(repo, ws, "B")
            self.assertFalse(outside.exists())
            if ws.exists():
                bench.drop_workspace(repo, ws)

    def test_corpus_claude_md_preserved_and_setup_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                text = (ws / "CLAUDE.md").read_text()
                self.assertIn("Corpus conventions", text)      # corpus part kept
                self.assertIn("hologram:start", text)          # map embedded
                diff = subprocess.run(["git", "-C", str(ws), "diff", "--stat"],
                                      capture_output=True, text=True).stdout
                self.assertEqual(diff, "")   # setup committed -> clean slate
                status = subprocess.run(
                    ["git", "-C", str(ws), "status", "--porcelain=v1",
                     "--untracked-files=all"], capture_output=True,
                    text=True).stdout
                self.assertEqual(status, "")
                self.assertFalse((ws / ".bench-setup-sha").exists())
                self.assertEqual(
                    bench._setup_sha(ws),
                    subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip())
            finally:
                bench.drop_workspace(repo, ws)


class BudgetConditionTest(unittest.TestCase):
    def test_workspace_map_carries_budget_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _mini_corpus(Path(tmp))
            ws = Path(tmp) / "wsB"
            bench.make_workspace(corpus, ws, "A", budget=1)
            text = (ws / "CLAUDE.md").read_text()
            self.assertIn("· budget 1", text)
            bench.drop_workspace(corpus, ws)

    def test_adaptive_budget_stamp_is_recorded_as_adaptive_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _mini_corpus(Path(tmp))
            ws = Path(tmp) / "wsA"
            bench.make_workspace(corpus, ws, "A", budget=60)
            info = bench._embedded_map_info(ws)
            header = bench.hologram.embedded_digest(
                ws / "CLAUDE.md").splitlines()[0]
            self.assertEqual(info["effective_map_budget"], 60)
            self.assertEqual(info["effective_map_adaptive"], " A" in header)
            if " A" in header:
                self.assertGreater(info["effective_map_detail"], 0)
            bench.drop_workspace(corpus, ws)


class ActedOnFindingsTest(unittest.TestCase):
    def _t(self, *events, revision="HEAD"):
        lines = []
        for index, (kind, payload) in enumerate(events):
            if kind == "review":
                tool_id = f"commit-{index}"
                lines.append(json.dumps({
                    "type": "assistant", "message": {"content": [
                        {"type": "tool_use", "id": tool_id, "name": "Bash",
                         "input": {"command": "git commit -m before-review"}}]}}))
                lines.append(json.dumps({
                    "type": "user", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": tool_id,
                         "content": f"hologram review vs {revision}: 1 finding(s)\n"
                                    + payload}]}}))
            elif kind == "edit":
                lines.append(json.dumps({
                    "type": "assistant", "message": {"content": [
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": payload}}]}}))
            elif kind == "commit":
                lines.append(json.dumps({
                    "type": "assistant", "message": {"content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "git commit --amend"}}]}}))
        return "\n".join(lines)

    def test_edit_of_named_file_then_commit_counts(self):
        t = self._t(("review", "- dup: x in money.py is similar"),
                    ("edit", "/ws/money.py"), ("commit", ""))
        self.assertTrue(bench.parse_transcript(t)["acted_on_findings"])

    def test_findings_after_last_commit_do_not_count(self):
        t = self._t(("commit", ""),
                    ("review", "- dup: x in money.py is similar"))
        self.assertFalse(bench.parse_transcript(t)["acted_on_findings"])

    def test_seen_without_edit_does_not_count(self):
        t = self._t(("review", "- dup: x in money.py is similar"),
                    ("commit", ""))
        self.assertFalse(bench.parse_transcript(t)["acted_on_findings"])

    def test_pathless_findings_fall_back_to_any_edit(self):
        t = self._t(("review", "- recover: T re-covers x"),
                    ("edit", "/ws/whatever.java"), ("commit", ""))
        self.assertTrue(bench.parse_transcript(t)["acted_on_findings"])

    def test_edit_of_unrelated_file_does_not_count_when_files_named(self):
        t = self._t(("review", "- dup: x in money.py is similar"),
                    ("edit", "/ws/other.py"), ("commit", ""))
        self.assertFalse(bench.parse_transcript(t)["acted_on_findings"])

    def test_shipped_head_parent_revision_counts_as_real_event(self):
        t = self._t(("review", "- dup: x in money.py is similar"),
                    ("edit", "/ws/money.py"), ("commit", ""),
                    revision="HEAD~1")
        metrics = bench.parse_transcript(t)
        self.assertTrue(metrics["review_seen"])
        self.assertTrue(metrics["review_action_proxy"])
        self.assertTrue(metrics["acted_on_findings"])  # compatibility alias


class ReviewConditionTest(unittest.TestCase):
    def test_ar_workspace_has_review_hook_corpus_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _mini_corpus(Path(tmp))
            ws = Path(tmp) / "wsAR"
            bench.make_workspace(corpus, ws, "AR")
            hook = ws / ".git" / "hooks" / "post-commit"
            self.assertTrue(hook.exists())
            self.assertIn("review HEAD~1", hook.read_text())
            self.assertIn("hologram:start", (ws / "CLAUDE.md").read_text())
            self.assertFalse(
                (corpus / ".git" / "hooks" / "post-commit").exists())
            bench.drop_workspace(corpus, ws)

    def test_ar_capture_replaces_only_review_half_of_managed_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _mini_corpus(root)
            ws = root / "wsAR"
            capture = root / "private" / "events.jsonl"
            bench.make_workspace(corpus, ws, "AR")
            hook = ws / ".git" / "hooks" / "post-commit"
            before = hook.read_text()

            bench._install_review_capture(ws, capture, lang=["python"])
            after = hook.read_text()

            self.assertIn(" build --root ", before)
            self.assertIn(" build --root ", after)
            self.assertIn("_review-hook", after)
            self.assertIn("HEAD~1", after)
            self.assertIn("--lang python", after)
            self.assertNotIn(" review HEAD~1 ", after)
            self.assertTrue(after.rstrip().endswith(
                "|| true # hologram:managed"))
            bench.drop_workspace(corpus, ws)

    def test_assistant_mention_is_not_a_review_event(self):
        t = "\n".join([
            '{"type": "assistant", "message": {"content": [{"type": "text",'
            ' "text": "tool said: hologram review vs HEAD~1: 2 finding(s)"}]}}'])
        self.assertFalse(bench.parse_transcript(t)["review_seen"])
        self.assertFalse(bench.parse_transcript("nothing")["review_seen"])

    def test_user_tool_result_is_a_review_event(self):
        t = "\n".join([
            json.dumps({
                "type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": "commit-1", "name": "Bash",
                    "input": {"command": "git commit -m change"},
                }]},
            }),
            json.dumps({
                "type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "commit-1",
                    "content": "hologram review vs HEAD~1: 0 finding(s)",
                }]},
            }),
        ])
        metrics = bench.parse_transcript(t)
        self.assertTrue(metrics["review_seen"])
        self.assertFalse(metrics["review_action_proxy"])

    def test_read_result_containing_fixture_is_not_a_review_event(self):
        t = "\n".join([
            json.dumps({
                "type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": "read-1", "name": "Read",
                    "input": {"file_path": "fixture.txt"},
                }]},
            }),
            json.dumps({
                "type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "read-1",
                    "content": "hologram review vs HEAD~1: 1 finding(s)\n"
                               "- dup: x in fixture.py is similar",
                }]},
            }),
        ])
        self.assertFalse(bench.parse_transcript(t)["review_seen"])


class StructuredReviewMeasurementTest(unittest.TestCase):
    def test_hook_human_output_is_unchanged_and_capture_is_sanitized(self):
        import contextlib
        import io
        from hologram.review import Finding, render_report

        finding = Finding(
            "dead", "synthetic_helper", "dead: synthetic detail",
            kind="fn", path="src/synthetic.py")
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            capture = Path(tmp) / "events.jsonl"
            out = io.StringIO()
            with (mock.patch("hologram.review.run_review_findings",
                             return_value=[finding]),
                  contextlib.redirect_stdout(out)):
                code = bench._run_review_hook(
                    repo, "HEAD~1", capture, ["python"])
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True).stdout.strip()
            raw = capture.read_text()
            rows = bench._read_rows(capture)

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), render_report([finding], "HEAD~1"))
        self.assertEqual(rows, [{
            "schema_version": 1,
            "head": head,
            "findings": [{"id": finding.id, "check": "dead"}],
        }])
        self.assertNotIn("synthetic_helper", raw)
        self.assertNotIn("synthetic.py", raw)
        self.assertNotIn("synthetic detail", raw)

    def test_final_state_is_deduplicated_and_keeps_new_findings_separate(self):
        baseline_resolved = {"id": "hr1-00000000000000000001",
                             "check": "dup"}
        baseline_persisting = {"id": "hr1-00000000000000000002",
                               "check": "dead"}
        final_new = mock.Mock(id="hr1-00000000000000000003", check="place")
        final_persisting = mock.Mock(
            id=baseline_persisting["id"], check="dead")
        first_head = "a" * 40
        second_head = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "events.jsonl"
            bench._append_jsonl_atomic(capture, {
                "schema_version": 1,
                "head": first_head,
                "findings": [baseline_resolved, baseline_persisting],
            })
            bench._append_jsonl_atomic(capture, {
                "schema_version": 1,
                "head": second_head,
                "findings": [baseline_persisting],
            })
            with (mock.patch.object(bench, "_agent_commit_ids",
                                    return_value=[first_head, second_head]),
                  mock.patch("hologram.review.run_review_findings",
                             return_value=[final_new, final_persisting])):
                measured = bench._review_final_state(
                    "AR", Path(tmp), "setup", capture)

        self.assertEqual(measured["status"], "ok")
        self.assertEqual(measured["hook_events"], 2)
        self.assertEqual(measured["baseline_count"], 2)
        self.assertEqual(measured["final_count"], 2)
        self.assertEqual(measured["resolved_count"], 1)
        self.assertEqual(measured["persisting_count"], 1)
        self.assertEqual(measured["new_final_count"], 1)
        self.assertEqual(
            {item["state"] for item in measured["items"]},
            {"resolved", "persisting"})
        self.assertNotIn("attempted", json.dumps(measured))
        self.assertEqual(measured["new_final"], [{
            "id": final_new.id, "check": "place",
        }])

    def test_missing_hook_event_is_incomplete_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "events.jsonl"
            with mock.patch.object(bench, "_agent_commit_ids",
                                   return_value=["a" * 40]):
                measured = bench._review_final_state(
                    "AR", Path(tmp), "setup", capture)
        self.assertEqual(measured["status"], "incomplete")
        self.assertIsNone(measured["baseline_count"])
        self.assertEqual(measured["items"], [])

    def test_stale_event_cannot_cover_a_rewritten_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "events.jsonl"
            bench._append_jsonl_atomic(capture, {
                "schema_version": 1,
                "head": "a" * 40,
                "findings": [],
            })
            with mock.patch.object(
                    bench, "_agent_commit_ids", return_value=["b" * 40]):
                measured = bench._review_final_state(
                    "AR", Path(tmp), "setup", capture)

        self.assertEqual(measured["status"], "incomplete")
        self.assertEqual(measured["hook_events"], 0)

    def test_ar_no_commit_run_has_complete_zero_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            task = bench.Task(id="review-clean", kind="navigate", prompt="x",
                              accept_cmd="true")
            row = bench.run_one(
                repo, task, "AR", 0, root / "results", "sonnet", 4,
                runner=lambda *args, **kwargs: TRANSCRIPT)
        self.assertEqual(row["review_findings"], {
            "schema_version": 1, "status": "ok", "hook_events": 0,
            "baseline_count": 0, "final_count": 0,
            "resolved_count": 0, "persisting_count": 0,
            "new_final_count": 0, "items": [], "new_final": [],
        })

    def test_ar_capture_install_failure_stops_before_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            task = bench.Task(id="review-setup", kind="navigate", prompt="x",
                              accept_cmd="true")
            runner = mock.Mock(return_value=TRANSCRIPT)
            with (mock.patch.object(
                    bench, "_install_review_capture",
                    side_effect=RuntimeError("synthetic install failure")),
                  self.assertRaisesRegex(RuntimeError,
                                        "before runner execution")):
                bench.run_one(
                    repo, task, "AR", 0, root / "results", "sonnet", 4,
                    runner=runner)
        runner.assert_not_called()

    def test_ar_commit_is_captured_by_the_installed_single_pass_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            task = bench.Task(id="review-commit", kind="fix", prompt="x",
                              accept_cmd="true")

            def commit_finding(prompt, ws, model, max_turns, effort=None):
                (ws / "new_helper.py").write_text(
                    "def isolated_widget():\n    return 7\n")
                subprocess.run(
                    ["git", "-C", str(ws), "add", "new_helper.py"],
                    check=True, capture_output=True)
                committed = subprocess.run(
                    ["git", "-C", str(ws), "-c", "user.email=t@t",
                     "-c", "user.name=t", "commit", "-m", "add helper"],
                    check=True, capture_output=True, text=True)
                self.assertIn("hologram review vs HEAD~1", committed.stdout)
                return TRANSCRIPT

            row = bench.run_one(
                repo, task, "AR", 0, root / "results", "sonnet", 4,
                runner=commit_finding)

        measured = row["review_findings"]
        self.assertEqual(measured["status"], "ok")
        self.assertEqual(measured["hook_events"], 1)
        self.assertGreaterEqual(measured["baseline_count"], 1)
        self.assertEqual(measured["resolved_count"], 0)
        self.assertEqual(measured["persisting_count"],
                         measured["baseline_count"])
        self.assertEqual(measured["new_final_count"], 0)

    def test_final_review_runs_before_acceptance_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            marker = root / "acceptance-side-effect"
            task = bench.Task(id="review-order", kind="navigate", prompt="x",
                              accept_cmd=f"touch {marker}")

            def observe(*args, **kwargs):
                self.assertFalse(marker.exists())
                return bench._empty_review_measurement("not_applicable")

            with mock.patch.object(bench, "_review_final_state",
                                   side_effect=observe):
                row = bench.run_one(
                    repo, task, "B", 0, root / "results", "sonnet", 4,
                    runner=lambda *args, **kwargs: TRANSCRIPT)
            marker_exists = marker.exists()
        self.assertTrue(marker_exists)
        self.assertTrue(row["accepted"])

    def test_final_review_failure_is_measurement_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            task = bench.Task(id="review-error", kind="navigate", prompt="x",
                              accept_cmd="true")
            with mock.patch.object(
                    bench, "_review_final_state",
                    side_effect=RuntimeError("synthetic measurement failure")):
                row = bench.run_one(
                    repo, task, "B", 0, root / "results", "sonnet", 4,
                    runner=lambda *args, **kwargs: TRANSCRIPT)
        self.assertTrue(row["accepted"])
        self.assertEqual(row["acceptance_verdict"], "pass")
        self.assertEqual(row["review_findings"]["status"], "error")

    def test_real_ar_review_failure_is_infrastructure_not_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            task = bench.Task(id="review-infra", kind="navigate", prompt="x",
                              accept_cmd="true")
            with mock.patch.object(
                    bench, "_review_final_state",
                    return_value=bench._empty_review_measurement("error")):
                row = bench.run_one(
                    repo, task, "AR", 0, root / "results", "sonnet", 4,
                    runner=lambda *args, **kwargs: TRANSCRIPT,
                    runner_mode="unsafe-host")
        self.assertTrue(row["accepted"])
        self.assertEqual(bench._infra_reason(row), "review:error")
        self.assertFalse(bench._terminal_cell(row))


class CoachConditionTest(unittest.TestCase):
    def test_ac_keeps_coaching_a_strips_it(self):
        from hologram.embed import _COACH_SENTENCE
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _mini_corpus(Path(tmp))
            ws_a = Path(tmp) / "wsA"
            bench.make_workspace(corpus, ws_a, "A")
            a_text = (ws_a / "CLAUDE.md").read_text()
            bench.drop_workspace(corpus, ws_a)
            ws_ac = Path(tmp) / "wsAC"
            bench.make_workspace(corpus, ws_ac, "AC")
            ac_text = (ws_ac / "CLAUDE.md").read_text()
            bench.drop_workspace(corpus, ws_ac)
        self.assertIn("hologram:start", a_text)
        self.assertNotIn(_COACH_SENTENCE.strip(), a_text)
        self.assertIn("hologram:start", ac_text)
        self.assertIn(_COACH_SENTENCE.strip(), ac_text)


class RunOneTest(unittest.TestCase):
    def test_full_cycle_with_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="label", kind="reuse",
                prompt="Add format_label() that reuses trim_spaces.",
                accept_cmd="grep -q format_label {ws}/svc.py",
                expect_reuse=["trim_spaces"])

            def fake_runner(prompt: str, ws: Path, model: str, max_turns: int,
                            effort: str | None = None) -> str:
                # the "agent" appends a function that calls trim_spaces
                (ws / "svc.py").write_text(
                    (ws / "svc.py").read_text()
                    + "\ndef format_label(items: list) -> str:\n"
                      "    return ' '.join(trim_spaces(items))\n")
                return TRANSCRIPT

            row = bench.run_one(repo, task, "A", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40,
                                runner=fake_runner)
        self.assertEqual(row["task"], "label")
        self.assertEqual(row["condition"], "A")
        self.assertTrue(row["accepted"])
        self.assertEqual(row["reused"], ["trim_spaces"])
        self.assertEqual(row["duplicated"], [])
        self.assertEqual(row["reads"], 3)   # Read ×2 + bash `sed -n` (see TRANSCRIPT)
        self.assertEqual(row["tokens_in"], 621000)
        self.assertEqual(row["runner_status"], "ok")
        self.assertEqual(row["acceptance_status"], "ok")
        self.assertIsNone(row["requested_budget"])
        self.assertGreater(row["effective_map_tokens"], 0)
        self.assertEqual(row["schema_version"], 3)
        self.assertEqual(row["hologram_version"], bench.hologram.__version__)
        self.assertRegex(row["tool_revision"], r"^[0-9a-f]{12}$")
        self.assertTrue(row["config_revision"].startswith("adhoc-"))
        self.assertRegex(row["experiment_id"], r"^experiment-[0-9a-f]{20}$")
        self.assertRegex(row["cell_id"], r"^cell-[0-9a-f]{20}$")
        self.assertRegex(row["pair_id"], r"^pair-[0-9a-f]{20}$")
        self.assertEqual(row["input_tokens"], 91000)
        self.assertEqual(row["experiment_conditions"], ["A"])
        self.assertEqual(row["experiment_reps"], 1)
        self.assertEqual(row["experiment_tasks"], ["label"])
        self.assertEqual(row["acceptance_verdict"], "pass")
        self.assertEqual(row["semantic_verdict"], "not_judged")
        self.assertRegex(row["runner"]["stdout_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(row["runner"]["stdout_size"], len(TRANSCRIPT.encode()))
        self.assertEqual(row["review_findings"]["status"], "not_applicable")
        self.assertIsNone(row["review_findings"]["baseline_count"])

    def test_noop_does_not_satisfy_any_diff_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="noop-diff", kind="reuse", prompt="Do nothing.",
                accept_cmd="git -C {ws} diff --stat {sha} | grep -q .")
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: TRANSCRIPT)
        self.assertFalse(row["accepted"])
        self.assertEqual(row["acceptance_status"], "nonzero")

    def test_new_file_counts_as_change_for_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="newfile", kind="reuse",
                prompt="Add a helper in a new module.",
                accept_cmd="git -C {ws} diff --stat | grep -q .",
                expect_reuse=[])

            def fake_runner(prompt, ws, model, max_turns, effort=None):
                (ws / "helper.py").write_text("def helper() -> int:\n    return 1\n")
                return TRANSCRIPT

            row = bench.run_one(repo, task, "B", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40, runner=fake_runner)
        self.assertTrue(row["accepted"])   # untracked new file must count

    def test_runner_nonzero_never_accepts_and_persists_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            results = Path(tmp) / "results"
            task = bench.Task(id="bad-runner", kind="navigate", prompt="x",
                              accept_cmd="true")

            def bad_runner(*args, **kwargs):
                return bench.RunnerOutcome(
                    stdout=TRANSCRIPT, stderr="provider unavailable",
                    returncode=23)

            row = bench.run_one(repo, task, "B", rep=0, results_dir=results,
                                model="sonnet", max_turns=40,
                                runner=bad_runner)
            self.assertFalse(row["accepted"])
            self.assertEqual(row["runner_status"], "nonzero")
            self.assertEqual(row["runner_returncode"], 23)
            self.assertEqual(row["runner"]["stderr"], "provider unavailable")
            self.assertEqual(
                (results / row["runner"]["stderr_artifact"]).read_text(),
                "provider unavailable")

    def test_runner_timeout_never_accepts_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            results = Path(tmp) / "results"
            task = bench.Task(id="timeout", kind="navigate", prompt="x",
                              accept_cmd="true")

            def timeout_runner(*args, **kwargs):
                raise subprocess.TimeoutExpired(
                    "claude", 1, output=TRANSCRIPT, stderr="deadline")

            row = bench.run_one(repo, task, "B", rep=0, results_dir=results,
                                model="sonnet", max_turns=40,
                                runner=timeout_runner)
            self.assertFalse(row["accepted"])
            self.assertEqual(row["runner_status"], "timeout")
            self.assertTrue(row["runner_timed_out"])
            self.assertEqual(row["runner"]["stderr"], "deadline")
            self.assertTrue((results / row["runner"]["stdout_artifact"]).exists())

    def test_terminal_runner_error_never_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="provider-error", kind="navigate", prompt="x",
                              accept_cmd="true")
            transcript = json.dumps({
                "type": "result", "subtype": "error_during_execution",
                "is_error": True, "result": "provider failed",
            })
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: transcript)
        self.assertFalse(row["accepted"])
        self.assertEqual(row["runner_status"], "result_error")
        self.assertEqual(row["runner"]["error"], "error_during_execution")
        self.assertEqual(bench._infra_reason(row), "runner:result_error")

    def test_turn_limit_is_an_observation_not_an_infra_failure(self):
        # Exhausting --max-turns is the session-length dial doing its job. The
        # cell must reach the judges and the aggregates, or the harness cannot
        # measure whether the map lets an agent finish inside the turn budget.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="max-turns", kind="navigate", prompt="x",
                              accept_cmd="true")
            transcript = json.dumps({
                "type": "result", "subtype": "error_max_turns",
                "is_error": True, "num_turns": 40,
                "usage": {"input_tokens": 5, "output_tokens": 7},
            })
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: transcript)
        self.assertEqual(row["runner_status"], "ok")
        self.assertIsNone(bench._infra_reason(row))
        self.assertEqual(row["turns"], 40)
        self.assertEqual(row["acceptance_verdict"], "pass")

    def test_structured_runner_requires_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="empty-runner", kind="navigate", prompt="x",
                              accept_cmd="true")
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: bench.RunnerOutcome(stdout=""))
        self.assertFalse(row["accepted"])
        self.assertEqual(row["runner_status"], "invalid_transcript")
        self.assertIn("missing terminal", row["runner"]["error"])

    def test_inconsistent_structured_runner_cannot_claim_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="runner-error", kind="navigate", prompt="x",
                              accept_cmd="true")
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: bench.RunnerOutcome(
                    stdout=TRANSCRIPT, returncode=0, status="ok",
                    error="provider reported failure"))
        self.assertEqual(row["runner_status"], "error")
        self.assertFalse(bench._terminal_cell(row))

    def test_legacy_string_runner_also_requires_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="empty-string", kind="navigate", prompt="x",
                              accept_cmd="true")
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: "not stream json")
        self.assertFalse(row["accepted"])
        self.assertEqual(row["runner_status"], "invalid_transcript")

    def test_terminal_result_requires_usage_and_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="bad-protocol", kind="navigate", prompt="x",
                              accept_cmd="true")
            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40,
                runner=lambda *a, **k: json.dumps({"type": "result"}))
        self.assertFalse(row["accepted"])
        self.assertEqual(row["runner_status"], "invalid_transcript")
        self.assertIn("num_turns", row["runner"]["error"])

    def test_agent_cannot_move_the_captured_acceptance_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="move-ref", kind="navigate", prompt="x",
                accept_cmd="git -C {ws} diff --stat {sha} | grep -q .")

            def move_ref(prompt, ws, model, max_turns, effort=None):
                subprocess.run(
                    ["git", "-C", str(ws), "update-ref", "refs/bench/setup",
                     "HEAD^"], check=True)
                return TRANSCRIPT

            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40, runner=move_ref)

        self.assertFalse(row["accepted"])
        self.assertEqual(row["new_lines"], 0)

    def test_new_source_file_participates_in_reuse_judging(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="new-reuse", kind="reuse", prompt="x",
                              accept_cmd="true", expect_reuse=["trim_spaces"])

            def add_module(prompt, ws, model, max_turns, effort=None):
                (ws / "newmod.py").write_text(
                    "def clean(items):\n    return trim_spaces(items)\n")
                return TRANSCRIPT

            row = bench.run_one(
                repo, task, "B", rep=0, results_dir=Path(tmp) / "results",
                model="sonnet", max_turns=40, runner=add_module)

        self.assertTrue(row["accepted"])
        self.assertEqual(row["reused"], ["trim_spaces"])
        self.assertGreater(row["new_lines"], 0)

    def test_rerun_artifacts_are_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            results = Path(tmp) / "results"
            task = bench.Task(id="rerun", kind="navigate", prompt="x",
                              accept_cmd="true")
            first = bench.run_one(repo, task, "B", rep=0,
                                  results_dir=results, model="sonnet",
                                  max_turns=40,
                                  runner=lambda *a, **k: TRANSCRIPT)
            second = bench.run_one(repo, task, "B", rep=0,
                                   results_dir=results, model="sonnet",
                                   max_turns=40,
                                   runner=lambda *a, **k: TRANSCRIPT)

        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["runner"]["stdout_artifact"],
                            second["runner"]["stdout_artifact"])

    def test_acceptance_nonzero_captures_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            results = Path(tmp) / "results"
            task = bench.Task(
                id="bad-accept", kind="navigate", prompt="x",
                accept_cmd="sh -c 'echo accept-out; echo accept-err >&2; exit 7'",
                accept_fail_codes=[7])
            row = bench.run_one(repo, task, "B", rep=0, results_dir=results,
                                model="sonnet", max_turns=40,
                                runner=lambda *a, **k: TRANSCRIPT)
            self.assertFalse(row["accepted"])
            self.assertEqual(row["acceptance_status"], "nonzero")
            self.assertEqual(row["acceptance_returncode"], 7)
            self.assertEqual(row["acceptance"]["stdout"], "accept-out\n")
            self.assertEqual(row["acceptance"]["stderr"], "accept-err\n")
            self.assertEqual(row["acceptance_verdict"], "fail")
            self.assertIsNone(bench._infra_reason(row))

    def test_undeclared_acceptance_exit_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="missing-judge", kind="navigate", prompt="x",
                accept_cmd="command-that-does-not-exist-hologram-bench")
            row = bench.run_one(
                repo, task, "B", rep=0,
                results_dir=Path(tmp) / "results", model="sonnet",
                max_turns=40, runner=lambda *a, **k: TRANSCRIPT)
        self.assertEqual(row["acceptance_returncode"], 127)
        self.assertIsNone(row["acceptance_verdict"])
        self.assertEqual(row["acceptance_infra_reason"],
                         "unexpected_exit:127")
        self.assertFalse(bench._terminal_cell(row))

    def test_manual_and_semantic_verdicts_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            manual = bench.Task(
                id="manual", kind="navigate", prompt="x", accept_cmd="true",
                manual_only=True)
            manual_transcript = json.dumps({
                "type": "result", "num_turns": 1, "result": "inspect me",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            manual_row = bench.run_one(
                repo, manual, "B", 0, root / "manual-results", "sonnet", 4,
                runner=lambda *a, **k: manual_transcript)
            semantic = bench.Task(
                id="semantic", kind="fix", prompt="x", accept_cmd="false",
                semantic_judge=True)
            semantic_row = bench.run_one(
                repo, semantic, "B", 0, root / "semantic-results", "sonnet", 4,
                runner=lambda *a, **k: TRANSCRIPT)
        self.assertTrue(manual_row["accepted"])
        self.assertEqual(manual_row["semantic_verdict"], "pending_manual")
        self.assertFalse(semantic_row["accepted"])
        self.assertEqual(semantic_row["semantic_verdict"], "fail")

    def test_acceptance_timeout_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            results = Path(tmp) / "results"
            task = bench.Task(id="slow-accept", kind="navigate", prompt="x",
                              accept_cmd="sleep 1")
            old_timeout = bench._ACCEPT_TIMEOUT_SECONDS
            bench._ACCEPT_TIMEOUT_SECONDS = 0.01
            try:
                row = bench.run_one(
                    repo, task, "B", rep=0, results_dir=results,
                    model="sonnet", max_turns=40,
                    runner=lambda *a, **k: TRANSCRIPT)
            finally:
                bench._ACCEPT_TIMEOUT_SECONDS = old_timeout
            self.assertFalse(row["accepted"])
            self.assertEqual(row["acceptance_status"], "timeout")
            self.assertTrue(row["acceptance_timed_out"])
            self.assertIsNone(row["acceptance_returncode"])

    def test_acceptance_timeout_kills_background_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            marker = ws / "late-marker"
            old_timeout = bench._ACCEPT_TIMEOUT_SECONDS
            bench._ACCEPT_TIMEOUT_SECONDS = 0.02
            try:
                outcome = bench._run_acceptance(
                    f"sh -c 'sleep 0.2; touch {marker}' & sleep 5", ws)
            finally:
                bench._ACCEPT_TIMEOUT_SECONDS = old_timeout
            self.assertTrue(outcome.timed_out)
            import time
            time.sleep(0.3)
            self.assertFalse(marker.exists())

    def test_transcript_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="noop", kind="navigate", prompt="look around",
                              accept_cmd="true", expect_reuse=[])
            results = Path(tmp) / "results"
            bench.run_one(repo, task, "B", rep=1, results_dir=results,
                          model="sonnet", max_turns=40,
                          runner=lambda *a, **k: TRANSCRIPT)
            self.assertEqual(len(list(results.glob("noop-B-1-*.jsonl"))), 1)


class ScopeJudgeTest(unittest.TestCase):
    def _repo(self, tmp: Path) -> Path:
        import subprocess
        repo = tmp / "ws"
        repo.mkdir()
        (repo / "base.py").write_text("class NeutralDriver:\n    pass\n")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "setup"]):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
        return repo

    def test_name_in_new_file_matches(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            (repo / "tests").mkdir()
            (repo / "tests" / "test_new.py").write_text(
                "def test_x():\n    NeutralDriver()\n")
            subprocess.run(["git", "add", "-N", "."], cwd=repo,
                           capture_output=True)
            self.assertTrue(bench.judge_scope(repo, ["NeutralDriver"]))
            self.assertTrue(bench.judge_scope(repo, ["NeutralDriver"],
                                              test_only=True))

    def test_unchanged_line_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self.assertFalse(bench.judge_scope(repo, ["NeutralDriver"]))

    def test_empty_expectation_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self.assertIsNone(bench.judge_scope(repo, []))

    def test_test_only_excludes_prod_additions(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            (repo / "prod_new.py").write_text("NeutralDriver()\n")
            subprocess.run(["git", "add", "-N", "."], cwd=repo,
                           capture_output=True)
            self.assertTrue(bench.judge_scope(repo, ["NeutralDriver"]))
            self.assertFalse(bench.judge_scope(repo, ["NeutralDriver"],
                                               test_only=True))


class ReportTest(unittest.TestCase):
    def test_structured_review_section_uses_only_eligible_counts_not_ids(self):
        eligible = {
            "task": "inspect", "kind": "fix", "condition": "AR", "rep": 0,
            "model": "m", "schema_version": 3,
            "runner_mode": "unsafe-host", "runner_status": "ok",
            "acceptance_status": "ok", "acceptance_verdict": "pass",
            "experiment_id": "experiment-demo", "duplicated": [],
            "review_findings": {
                "schema_version": 1, "status": "ok", "hook_events": 2,
                "baseline_count": 3, "final_count": 3,
                "resolved_count": 1, "persisting_count": 2,
                "new_final_count": 1,
                "items": [{"id": "hr1-00000000000000000001",
                           "check": "dup", "state": "resolved"}],
                "new_final": [{"id": "hr1-00000000000000000004",
                               "check": "dead"}],
            },
        }
        excluded = {
            **eligible, "cell_id": "excluded", "review_findings": {
                **eligible["review_findings"], "status": "error",
                "resolved_count": 99,
            },
        }

        section = "\n".join(bench._review_section([eligible, excluded]))

        self.assertIn("eligible status=ok", section)
        self.assertIn("| AR | fix | m |", section)
        self.assertIn("cells=2, eligible=1, ok=1", section)
        self.assertIn("| 2 | 1 | 1 | 2 | 3 | 3 | 1 | 2 | 1 |", section)
        self.assertNotIn("hr1-", section)
        self.assertNotIn("99", section)

    def test_dry_ar_is_excluded_from_review_measurements(self):
        row = {
            "task": "dry", "kind": "fix", "condition": "AR", "rep": 0,
            "model": "m", "schema_version": 3,
            "runner_mode": "dry-run", "runner_status": "ok",
            "acceptance_status": "skipped_dry_run",
            "review_findings": {
                "schema_version": 1, "status": "ok", "hook_events": 0,
                "baseline_count": 0, "final_count": 0,
                "resolved_count": 0, "persisting_count": 0,
                "new_final_count": 0, "items": [], "new_final": [],
            },
        }

        section = "\n".join(bench._review_section([row]))

        self.assertIn("cells=0, eligible=0", section)
        self.assertIn("dry-run AR excluded=1", section)

    def test_aggregates_by_condition(self):
        rows = [
            {"task": "t1", "kind": "reuse", "condition": "A", "rep": 0,
             "accepted": True, "reused": ["trim_spaces"], "duplicated": [],
             "new_lines": 1, "reads": 3, "searches": 2, "edits": 2,
             "turns": 9, "tokens_in": 100, "tokens_out": 10},
            {"task": "t1", "kind": "reuse", "condition": "B", "rep": 0,
             "accepted": True, "reused": [], "duplicated": ["trim_spaces_copy"],
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
        self.assertIn("searches", md)

    def test_empty_rows(self):
        self.assertIn("no runs", bench.report([]))

    def test_kind_split_and_answer_column(self):
        rows = [
            {"task": "nav-t1", "kind": "navigate", "condition": "A", "rep": 0,
             "accepted": True, "answer_ok": True, "reused": [],
             "duplicated": [], "new_lines": 0, "reads": 0, "searches": 0,
             "edits": 0, "turns": 2, "tokens_in": 10, "tokens_out": 1,
             "files_read": 0},
            {"task": "nav-t1", "kind": "navigate", "condition": "B", "rep": 0,
             "accepted": True, "answer_ok": False, "reused": [],
             "duplicated": [], "new_lines": 0, "reads": 6, "searches": 4,
             "edits": 0, "turns": 9, "tokens_in": 90, "tokens_out": 9,
             "files_read": 5},
            {"task": "t2", "kind": "reuse", "condition": "A", "rep": 0,
             "accepted": True, "answer_ok": None, "reused": ["x"],
             "duplicated": [], "new_lines": 1, "reads": 3, "searches": 1,
             "edits": 2, "turns": 8, "tokens_in": 50, "tokens_out": 5,
             "files_read": 3},
        ]
        md = bench.report(rows)
        self.assertIn("| A | navigate |", md)
        self.assertIn("| B | navigate |", md)
        self.assertIn("| A | reuse |", md)
        self.assertIn("100%", md)   # A navigate answers
        self.assertIn("0%", md)     # B navigate answers

    def test_anon_report_has_no_symbol_names(self):
        rows = [
            {"task": "t2", "kind": "reuse", "condition": "A", "rep": 0,
             "accepted": True, "answer_ok": None,
             "reused": ["SecretPricingEngine.evaluate"],
             "duplicated": [], "new_lines": 1, "reads": 3, "searches": 1,
             "edits": 2, "turns": 8, "tokens_in": 50, "tokens_out": 5,
             "files_read": 3},
        ]
        md = bench.report(rows, anon=True)
        self.assertNotIn("SecretPricingEngine", md)
        self.assertNotIn("t2", md)
        self.assertIn("task-001", md)
        self.assertIn("reused", md)

    def test_infrastructure_failures_are_not_averaged(self):
        rows = [
            {"task": "t1", "kind": "navigate", "condition": "A", "rep": 0,
             "model": "m", "accepted": False, "reused": [], "duplicated": [],
             "runner_status": "timeout", "acceptance_status": "ok",
             "reads": 0, "files_read": 0, "searches": 0, "turns": 0,
             "tokens_in": 0, "tokens_out": 0},
            {"task": "t2", "kind": "navigate", "condition": "A", "rep": 0,
             "model": "m", "accepted": True, "reused": [], "duplicated": [],
             "runner_status": "ok", "acceptance_status": "ok",
             "reads": 4, "files_read": 3, "searches": 2, "turns": 8,
             "tokens_in": 100, "tokens_out": 10},
        ]

        md = bench.report(rows)

        self.assertIn("| A | navigate | m |", md)
        self.assertIn("| 1 | 1 | 100% |", md)
        self.assertIn("| 4.0 ± 0.0 | 3.0 ± 0.0 | 2.0 ± 0.0 | 8.0 ± 0.0 |", md)
        self.assertIn("| 100 ± 0 | 10 ± 0 |", md)
        self.assertIn("infra:runner:timeout", md)

    def test_zero_valid_and_inapplicable_rates_are_not_reported_as_zero(self):
        row = {
            "task": "broken", "kind": "navigate", "condition": "A",
            "rep": 0, "model": "m", "runner_status": "timeout",
            "acceptance_status": "not_run", "accepted": False,
            "duplicated": [],
        }
        md = bench.report([row])
        summary = next(line for line in md.splitlines()
                       if line.startswith("| A | navigate"))
        self.assertIn("| 0 | 1 | — | 0 | ", summary)
        self.assertNotIn("| 0% |", summary)

    def test_manual_rows_are_pending_not_automatic_successes(self):
        row = {
            "task": "inspect", "kind": "navigate", "condition": "A",
            "rep": 0, "model": "m", "schema_version": 3,
            "runner_mode": "unsafe-host", "runner_status": "ok",
            "acceptance_status": "ok", "acceptance_verdict": "pass",
            "accepted": True, "manual_only": True,
            "semantic_verdict": "pending_manual", "duplicated": [],
        }
        summary = next(line for line in bench.report([row]).splitlines()
                       if line.startswith("| A | navigate"))
        self.assertIn("| — | 0 | — | 0 | 1 |", summary)

    def test_pairs_from_different_execution_waves_are_not_combined(self):
        base = {
            "task": "inspect", "kind": "navigate", "rep": 0,
            "model": "m", "schema_version": 3,
            "runner_mode": "unsafe-host", "runner_status": "ok",
            "acceptance_status": "ok", "acceptance_verdict": "pass",
            "accepted": True, "manual_only": False, "duplicated": [],
            "pair_id": "pair-demo", "reads": 1,
        }
        md = bench.report([
            {**base, "condition": "A", "cell_id": "cell-a",
             "wave_id": "wave-one"},
            {**base, "condition": "B", "cell_id": "cell-b",
             "wave_id": "wave-two"},
        ])
        self.assertIn("Incomplete treatment/B pairs: 1", md)
        self.assertIn("different execution waves", md)

    def test_latest_duplicate_cell_is_reported_once(self):
        base = {"task": "t", "kind": "navigate", "condition": "A", "rep": 0,
                "model": "m", "reused": [], "duplicated": [],
                "runner_status": "ok", "acceptance_status": "ok",
                "reads": 1, "files_read": 1, "searches": 1, "turns": 1,
                "tokens_in": 10, "tokens_out": 1}
        rows = [{**base, "run_id": "old", "accepted": False},
                {**base, "run_id": "new", "accepted": True}]

        md = bench.report(rows)

        self.assertIn("| 1 | 0 | 100% |", md)

    def test_matched_deltas_use_median_mad_and_list_incomplete_pairs(self):
        base = {"kind": "navigate", "model": "m", "effort": None,
                "requested_budget": None, "corpus_revision": "abc12345",
                "config_revision": "cfg", "schema_version": 2,
                "hologram_version": "1.0", "tool_revision": "tool",
                "experiment_id": "experiment-demo", "accepted": True,
                "answer_ok": True, "scope_ok": None, "duplicated": [],
                "reused": [], "runner_status": "ok",
                "acceptance_status": "ok", "files_read": 1,
                "searches": 1, "turns": 1, "tokens_in_fresh": 10,
                "tokens_in_cache_created": 20,
                "tokens_in_cache_read": 30, "tokens_in": 60,
                "tokens_out": 5}
        rows = []
        for rep, a_reads, b_reads in ((0, 1, 4), (1, 1, 5), (2, 100, 0)):
            pair = f"pair-{rep}"
            rows.extend([
                {**base, "task": "inspect", "rep": rep, "condition": "A",
                 "cell_id": f"a-{rep}", "pair_id": pair,
                 "reads": a_reads},
                {**base, "task": "inspect", "rep": rep, "condition": "B",
                 "cell_id": f"b-{rep}", "pair_id": pair,
                 "reads": b_reads},
            ])
        rows.append({**base, "task": "unpaired", "rep": 0,
                     "condition": "A", "cell_id": "unpaired-a",
                     "pair_id": "pair-unpaired", "reads": 2})

        md = bench.report(rows)

        self.assertIn("Matched treatment−B deltas", md)
        self.assertIn("| 3 |", md)
        self.assertIn("-3.0 ± 1.0", md)  # robust center, not the mean
        self.assertIn("Incomplete treatment/B pairs: 1", md)
        self.assertIn("unpaired [rep0] A−B: missing B", md)
        self.assertIn("fresh input", md)
        self.assertIn("cache-created", md)
        self.assertIn("cache-read", md)

    def test_matches_every_planned_treatment_against_control(self):
        base = {
            "task": "inspect", "kind": "navigate", "rep": 0,
            "model": "m", "schema_version": 3,
            "runner_mode": "unsafe-host", "runner_status": "ok",
            "acceptance_status": "ok", "acceptance_verdict": "pass",
            "accepted": True, "manual_only": False, "duplicated": [],
            "pair_id": "pair-demo", "wave_id": "wave-demo",
            "experiment_id": "experiment-demo",
            "experiment_conditions": ["A", "AC", "AR", "B"],
            "reads": 1,
        }
        rows = [
            {**base, "condition": condition, "cell_id": f"cell-{condition}",
             **({"review_findings": {"status": "ok"}}
                if condition == "AR" else {})}
            for condition in ("A", "AC", "AR", "B")
        ]

        md = bench.report(rows)

        self.assertIn("| A−B |", md)
        self.assertIn("| AC−B |", md)
        self.assertIn("| AR−B |", md)
        self.assertIn("Incomplete treatment/B pairs: 0", md)

    def test_planned_control_only_matrix_does_not_invent_treatment(self):
        row = {
            "task": "control", "kind": "navigate", "condition": "B",
            "rep": 0, "model": "m", "schema_version": 3,
            "runner_mode": "dry-run", "runner_status": "ok",
            "acceptance_status": "skipped_dry_run",
            "experiment_id": "experiment-control",
            "experiment_conditions": ["B"], "pair_id": "pair-control",
            "cell_id": "cell-control", "duplicated": [],
        }

        md = bench.report([row])

        self.assertIn("Incomplete treatment/B pairs: 0", md)
        self.assertNotIn("missing A", md)

    def test_report_makes_dry_and_paid_runner_modes_visible(self):
        base = {
            "task": "inspect", "kind": "navigate", "condition": "A",
            "rep": 0, "model": "m", "schema_version": 3,
            "runner_status": "ok", "acceptance_status": "skipped_dry_run",
            "acceptance_verdict": None, "accepted": False,
            "manual_only": False, "duplicated": [],
        }
        md = bench.report([
            {**base, "cell_id": "dry", "experiment_id": "experiment-dry",
             "runner_mode": "dry-run"},
            {**base, "cell_id": "paid", "experiment_id": "experiment-paid",
             "runner_mode": "unsafe-host", "acceptance_status": "ok",
             "acceptance_verdict": "pass", "accepted": True},
        ])

        self.assertIn("| runner |", md)
        self.assertIn("| dry-run |", md)
        self.assertIn("| unsafe-host |", md)

    def test_anonymous_report_redacts_task_identifiers_everywhere(self):
        private_id = "corpus-specific-private-task"
        row = {
            "task": private_id, "kind": "navigate", "condition": "A",
            "rep": 0, "model": "m", "schema_version": 3,
            "runner_mode": "unsafe-host", "runner_status": "timeout",
            "acceptance_status": "not_run", "accepted": False,
            "duplicated": [], "pair_id": "pair-private",
            "experiment_conditions": ["A", "B"],
        }

        md = bench.report([row], anon=True)

        self.assertNotIn(private_id, md)
        self.assertIn("task-001", md)


class CliTest(unittest.TestCase):
    def test_public_help_hides_internal_review_hook(self):
        import contextlib
        import io
        stdout = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(stdout):
            bench.main(["--help"])
        self.assertNotIn("_review-hook", stdout.getvalue())

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
                               "--conditions", "A", "--reps", "1",
                               "--dry-run"])
            self.assertEqual(code, 0)
            rows = [json.loads(l) for l in
                    (results / "runs.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["condition"], "A")
            self.assertEqual(rows[0]["acceptance_status"], "skipped_dry_run")
            self.assertIsNone(rows[0]["acceptance_verdict"])
            self.assertFalse(rows[0]["accepted"])

            code = bench.main(["report", "--results", str(results)])
            self.assertEqual(code, 0)
            self.assertTrue((results / "report.md").exists())

    def test_invalid_matrix_is_rejected_before_results_are_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "noop", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = Path(tmp) / "results"
            with self.assertRaisesRegex(SystemExit, "unknown condition"):
                bench.main(["run", str(taskfile), "--results", str(results),
                            "--conditions", "TYPO", "--dry-run"])
            self.assertFalse(results.exists())
            with self.assertRaisesRegex(SystemExit, "reps must be positive"):
                bench.main(["run", str(taskfile), "--results", str(results),
                            "--reps", "0", "--dry-run"])
            with self.assertRaisesRegex(SystemExit, "unknown --only"):
                bench.main(["run", str(taskfile), "--results", str(results),
                            "--only", "missing", "--dry-run"])

    def test_real_runner_requires_explicit_host_safety_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "noop", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = Path(tmp) / "results"
            with self.assertRaisesRegex(SystemExit, "not host-isolated"):
                bench.main(["run", str(taskfile), "--results", str(results)])
            self.assertFalse(results.exists())

            provenance = bench._runtime_provenance(
                "unsafe-host", runner_version="test-runner")
            with (mock.patch.object(bench, "claude_runner", bench._dry_runner),
                  mock.patch.object(bench, "_preflight_runner",
                                    return_value=provenance)):
                code = bench.main([
                    "run", str(taskfile), "--results", str(results),
                    "--conditions", "B", "--allow-unsafe-host"])
            row = bench._read_rows(results / "runs.jsonl")[0]
        self.assertEqual(code, 0)
        self.assertEqual(row["runner_mode"], "unsafe-host")
        self.assertTrue(row["host_execution_acknowledged"])

    def test_dry_run_never_executes_acceptance_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            marker = root / "host-side-effect"
            taskfile = root / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "dry", "kind": "navigate",
                           "prompt": "x", "accept_cmd": f"touch {marker}"}],
            }))
            results = root / "results"
            code = bench.main([
                "run", str(taskfile), "--results", str(results),
                "--conditions", "B", "--dry-run"])
            row = bench._read_rows(results / "runs.jsonl")[0]
            marker_exists = marker.exists()
        self.assertEqual(code, 0)
        self.assertFalse(marker_exists)
        self.assertEqual(row["acceptance_status"], "skipped_dry_run")

    def test_resume_skips_only_exact_compatible_terminal_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "noop", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = Path(tmp) / "results"
            common = ["run", str(taskfile), "--results", str(results),
                      "--conditions", "B", "--dry-run", "--seed", "7"]
            self.assertEqual(bench.main(common), 0)
            first = bench._read_rows(results / "runs.jsonl")
            self.assertEqual(bench.main([*common, "--resume"]), 0)
            skipped = bench._read_rows(results / "runs.jsonl")
            self.assertEqual(len(skipped), 1)

            changed_seed = [*common[:-1], "8", "--resume"]
            self.assertEqual(bench.main(changed_seed), 0)
            changed = bench._read_rows(results / "runs.jsonl")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(changed), 2)
        self.assertNotEqual(changed[0]["cell_id"], changed[1]["cell_id"])

    def test_resume_retries_and_preserves_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "retry", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = Path(tmp) / "results"
            args = ["run", str(taskfile), "--results", str(results),
                    "--conditions", "B", "--dry-run", "--seed", "3"]
            self.assertEqual(bench.main(args), 0)
            path = results / "runs.jsonl"
            failed = bench._read_rows(path)[0]
            failed["runner_status"] = "timeout"
            bench._atomic_replace(path, (json.dumps(failed) + "\n").encode())

            self.assertEqual(bench.main([*args, "--resume"]), 0)
            attempts = bench._read_rows(path)
            report = bench.report(attempts)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["runner_status"], "timeout")
        self.assertEqual(attempts[1]["runner_status"], "ok")
        self.assertIn("Infrastructure failure attempts (preserved)", report)

    def test_resume_uses_latest_attempt_not_any_older_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "latest", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = root / "results"
            args = ["run", str(taskfile), "--results", str(results),
                    "--conditions", "B", "--dry-run", "--seed", "11"]
            self.assertEqual(bench.main(args), 0)
            path = results / "runs.jsonl"
            success = bench._read_rows(path)[0]
            failed = {**success, "run_id": "newer-failure",
                      "runner_status": "timeout",
                      "acceptance_status": "not_run",
                      "acceptance_verdict": None}
            bench._append_jsonl_atomic(path, failed)
            self.assertEqual(bench.main([*args, "--resume"]), 0)
            attempts = bench._read_rows(path)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[-1]["runner_status"], "ok")

    def test_resume_retries_when_evidence_artifact_was_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "evidence", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = root / "results"
            args = ["run", str(taskfile), "--results", str(results),
                    "--conditions", "B", "--dry-run", "--seed", "12"]
            self.assertEqual(bench.main(args), 0)
            row = bench._read_rows(results / "runs.jsonl")[0]
            artifact = results / row["runner"]["stdout_artifact"]
            artifact.write_text("tampered")
            self.assertEqual(bench.main([*args, "--resume"]), 0)
            attempts = bench._read_rows(results / "runs.jsonl")
        self.assertEqual(len(attempts), 2)

    def test_resume_reruns_whole_condition_block_when_ar_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "block", "kind": "navigate",
                           "prompt": "x", "accept_cmd": "true"}],
            }))
            results = root / "results"
            args = ["run", str(taskfile), "--results", str(results),
                    "--conditions", "AR", "B", "--allow-unsafe-host",
                    "--seed", "13"]
            provenance = bench._runtime_provenance(
                "unsafe-host", runner_version="test-runner")

            def runner(*args, **kwargs):
                return TRANSCRIPT

            with (mock.patch.object(bench, "claude_runner", runner),
                  mock.patch.object(bench, "_preflight_runner",
                                    return_value=provenance)):
                self.assertEqual(bench.main(args), 0)
                path = results / "runs.jsonl"
                rows = bench._read_rows(path)
                ar = next(row for row in rows if row["condition"] == "AR")
                ar["review_findings"] = {
                    **ar["review_findings"], "status": "incomplete",
                }
                bench._atomic_replace(
                    path,
                    ("\n".join(json.dumps(row) for row in rows) + "\n").encode())
                self.assertEqual(bench.main([*args, "--resume"]), 0)
            attempts = bench._read_rows(results / "runs.jsonl")
        self.assertEqual(len(attempts), 4)
        self.assertEqual({row["condition"] for row in attempts[-2:]},
                         {"AR", "B"})
        self.assertEqual(len({row["wave_id"] for row in attempts[-2:]}), 1)

    def test_runner_circuit_breaker_stops_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [
                    {"id": f"task-{index}", "kind": "navigate",
                     "prompt": "x", "accept_cmd": "true"}
                    for index in range(3)
                ],
            }))
            results = root / "results"

            def unavailable(*args, **kwargs):
                return bench.RunnerOutcome(stderr="unavailable", returncode=9)

            provenance = bench._runtime_provenance(
                "unsafe-host", runner_version="test-runner")
            with (mock.patch.object(bench, "claude_runner", unavailable),
                  mock.patch.object(bench, "_preflight_runner",
                                    return_value=provenance)):
                code = bench.main([
                    "run", str(taskfile), "--results", str(results),
                    "--conditions", "B", "--allow-unsafe-host",
                    "--max-consecutive-runner-failures", "2"])
            rows = bench._read_rows(results / "runs.jsonl")
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["runner_status"] == "nonzero" for row in rows))

    def test_ar_measurement_failures_trip_breaker_across_control_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _mini_corpus(root)
            taskfile = root / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [
                    {"id": f"task-{index}", "kind": "navigate",
                     "prompt": "x", "accept_cmd": "true"}
                    for index in range(3)
                ],
            }))
            results = root / "results"
            runner = mock.Mock(return_value=TRANSCRIPT)
            provenance = bench._runtime_provenance(
                "unsafe-host", runner_version="test-runner")
            with (mock.patch.object(bench, "claude_runner", runner),
                  mock.patch.object(bench, "_preflight_runner",
                                    return_value=provenance),
                  mock.patch.object(
                      bench, "_review_final_state",
                      return_value=bench._empty_review_measurement("error"))):
                code = bench.main([
                    "run", str(taskfile), "--results", str(results),
                    "--conditions", "AR", "B", "--allow-unsafe-host",
                    "--max-consecutive-runner-failures", "2", "--seed", "13",
                ])
            rows = bench._read_rows(results / "runs.jsonl")
        self.assertEqual(code, 1)
        self.assertEqual(sum(row["condition"] == "AR" for row in rows), 2)
        self.assertLess(len(rows), 6)


if __name__ == "__main__":
    unittest.main()
