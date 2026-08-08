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
            "corpus": "~/workspace/private-corpus",
            "model": "sonnet",
            "max_turns": 40,
            "tasks": [
                {"id": "weighted-avg", "kind": "reuse",
                 "prompt": "Add a weighted average.",
                 "accept_cmd": "grep -rq weightedAverage {ws}/src",
                 "expect_reuse": ["normalize", "add"]},
                {"id": "find-lifecycle", "kind": "navigate",
                 "prompt": "Where is claim lifecycle handled?",
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
        {"type": "tool_use", "name": "Edit", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {}}]}}),
    json.dumps({"type": "result", "num_turns": 7,
                "usage": {"input_tokens": 91000, "output_tokens": 4200}}),
    "not-json-noise",
])


class TranscriptMetricsTest(unittest.TestCase):
    def test_counts_and_usage(self):
        m = bench.parse_transcript(TRANSCRIPT)
        self.assertEqual(m["reads"], 1)
        self.assertEqual(m["searches"], 1)
        self.assertEqual(m["edits"], 2)          # Edit + Write
        self.assertEqual(m["turns"], 7)
        self.assertEqual(m["tokens_in"], 91000)
        self.assertEqual(m["tokens_out"], 4200)

    def test_empty_transcript_gives_zeroes(self):
        m = bench.parse_transcript("")
        self.assertEqual(m, {"reads": 0, "searches": 0, "edits": 0,
                             "turns": 0, "tokens_in": 0, "tokens_out": 0})


BEFORE_DIGEST = """# corpus @x 2026-08-08 · 100 LOC · state aaa · regen: x
· legend: …
src
 math
  MathOps(C)
   add(Rational,Rational):Rational
   normalize(List<Rational>):List<Rational> > add
"""

AFTER_REUSED = BEFORE_DIGEST + """\
  Averages(C)
   weightedAverage(List<Rational>):Rational > MathOps.add,normalize
"""

AFTER_DUPLICATED = BEFORE_DIGEST + """\
  Averages(C)
   normalizeWeights(List<Rational>):List<Rational>
   weightedAverage(List<Rational>):Rational > normalizeWeights
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
    def test_condition_a_has_digest_and_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                self.assertTrue((ws / "PROJECT_DIGEST.md").exists())
                self.assertIn("PROJECT_DIGEST.md", (ws / "CLAUDE.md").read_text())
                self.assertTrue((ws / "svc.py").exists())
            finally:
                bench.drop_workspace(repo, ws)

    def test_condition_b_is_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsB", "B")
            try:
                self.assertFalse((ws / "PROJECT_DIGEST.md").exists())
                self.assertNotIn("PROJECT_DIGEST", (ws / "CLAUDE.md").read_text())
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

    def test_corpus_claude_md_preserved_and_setup_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                text = (ws / "CLAUDE.md").read_text()
                self.assertIn("Corpus conventions", text)      # corpus part kept
                self.assertIn("PROJECT_DIGEST.md", text)       # snippet appended
                diff = subprocess.run(["git", "-C", str(ws), "diff", "--stat"],
                                      capture_output=True, text=True).stdout
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

            row = bench.run_one(repo, task, "A", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40,
                                runner=fake_runner)
        self.assertEqual(row["task"], "avg")
        self.assertEqual(row["condition"], "A")
        self.assertTrue(row["accepted"])
        self.assertEqual(row["reused"], ["normalize"])
        self.assertEqual(row["duplicated"], [])
        self.assertEqual(row["reads"], 1)
        self.assertEqual(row["tokens_in"], 91000)

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

    def test_empty_rows(self):
        self.assertIn("no runs", bench.report([]))


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
