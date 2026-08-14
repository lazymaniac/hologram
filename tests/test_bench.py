import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))

import bench  # noqa: E402


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
                 "expect_reuse": [],
                 "expect_answer": ["LifecycleManager", r"records\.py"],
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
        self.assertEqual(cfg.tasks[0].id, "weighted-avg")
        self.assertEqual(cfg.tasks[0].expect_reuse, ["normalize", "add"])
        self.assertEqual(cfg.tasks[0].expect_answer, [])
        self.assertIsNone(cfg.tasks[0].max_turns)
        self.assertEqual(cfg.tasks[1].expect_answer,
                         ["LifecycleManager", r"records\.py"])
        self.assertEqual(cfg.tasks[1].max_turns, 8)  # session-length dial
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
        self.assertEqual(m["tokens_in"], 91000 + 30000 + 500000)
        self.assertEqual(m["tokens_out"], 4200)

    def test_empty_transcript_gives_zeroes(self):
        m = bench.parse_transcript("")
        self.assertEqual(m, {"reads": 0, "searches": 0, "edits": 0,
                             "turns": 0, "tokens_in": 0, "tokens_out": 0,
                             "files_read": 0, "result_text": "",
                             "review_seen": False,
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
 math
  MathOps(C)
   add(left,right):Fraction
   normalize(values):List<Fraction> > add
"""

AFTER_REUSED = BEFORE_DIGEST + """\
  Averages(C)
   weightedAverage(values):Fraction > MathOps.add,normalize
"""

AFTER_DUPLICATED = BEFORE_DIGEST + """\
  Averages(C)
   normalizeWeights(values):List<Fraction>
   weightedAverage(values):Fraction > normalizeWeights
"""


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
        self.assertTrue(any("weightedAverage" in ln for ln in v["new_lines"]))

    def test_no_change_is_clean(self):
        v = bench.judge_reuse(BEFORE_DIGEST, BEFORE_DIGEST, ["normalize"])
        self.assertEqual(v, {"new_lines": [], "reused": [], "duplicated": []})


import subprocess  # noqa: E402


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
                self.assertIn("normalize", (repo / "svc.py").read_text())
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
            finally:
                bench.drop_workspace(repo, ws)


class ActedOnFindingsTest(unittest.TestCase):
    def _t(self, *events):
        lines = []
        for kind, payload in events:
            if kind == "review":
                lines.append(json.dumps({
                    "type": "user", "message": {"content": [
                        {"type": "tool_result",
                         "content": "hologram review vs HEAD: 1 finding(s)\n"
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


class ReviewConditionTest(unittest.TestCase):
    def test_ar_workspace_has_review_hook_corpus_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _mini_corpus(Path(tmp))
            ws = Path(tmp) / "wsAR"
            bench.make_workspace(corpus, ws, "AR")
            hook = ws / ".git" / "hooks" / "pre-commit"
            self.assertTrue(hook.exists())
            self.assertIn("review HEAD ", hook.read_text())
            post = ws / ".git" / "hooks" / "post-commit"
            self.assertNotIn("review", post.read_text())  # build-only
            self.assertIn("hologram:start", (ws / "CLAUDE.md").read_text())
            self.assertFalse(
                (corpus / ".git" / "hooks" / "pre-commit").exists())
            self.assertFalse(
                (corpus / ".git" / "hooks" / "post-commit").exists())
            bench.drop_workspace(corpus, ws)

    def test_review_seen_metric(self):
        t = "\n".join([
            '{"type": "assistant", "message": {"content": [{"type": "text",'
            ' "text": "tool said: hologram review vs HEAD~1: 2 finding(s)"}]}}'])
        self.assertTrue(bench.parse_transcript(t)["review_seen"])
        self.assertFalse(bench.parse_transcript("nothing")["review_seen"])


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
                id="avg", kind="reuse",
                prompt="Add average() that reuses normalize.",
                accept_cmd="grep -q average {ws}/svc.py",
                expect_reuse=["normalize"])

            def fake_runner(prompt: str, ws: Path, model: str, max_turns: int,
                            effort: str | None = None) -> str:
                # the "agent" appends a function that calls normalize
                (ws / "svc.py").write_text(
                    (ws / "svc.py").read_text()
                    + "\ndef average(xs: list) -> float:\n"
                      "    return sum(normalize(xs)) / len(xs)\n")
                return TRANSCRIPT

            row = bench.run_one(repo, task, "A", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40,
                                runner=fake_runner)
        self.assertEqual(row["task"], "avg")
        self.assertEqual(row["condition"], "A")
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

            def fake_runner(prompt, ws, model, max_turns, effort=None):
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
        self.assertIn("t2", md)
        self.assertIn("reused", md)


class CliTest(unittest.TestCase):
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

            code = bench.main(["report", "--results", str(results)])
            self.assertEqual(code, 0)
            self.assertTrue((results / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
