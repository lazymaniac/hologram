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


if __name__ == "__main__":
    unittest.main()
