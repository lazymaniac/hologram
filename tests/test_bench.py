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


if __name__ == "__main__":
    unittest.main()
