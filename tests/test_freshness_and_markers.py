import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import Symbol, build_digest, render_simple, run_cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _proj(tmp: Path) -> Path:
    root = tmp / "proj"
    root.mkdir()
    (root / "svc.py").write_text(
        "class Svc:\n"
        "    def run(self) -> int:\n        return self._step()\n"
        "    def _step(self) -> int:\n        return 1\n"
    )
    (root / "test_svc.py").write_text(
        "from svc import Svc\n\n"
        "def test_run_returns_one():\n    assert Svc().run() == 1\n"
    )
    return root


class StateAndCheckTest(unittest.TestCase):
    def test_state_stamp_matches_state_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = Path(tmp) / "d.md"
            run_cli(["build", "--root", str(root), "--out", str(out), "--quiet"])
            self.assertEqual(hologram._digest_state(out),
                             hologram._state_hash(root))

    def test_check_fresh_then_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = Path(tmp) / "d.md"
            run_cli(["build", "--root", str(root), "--out", str(out), "--quiet"])
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--out", str(out), "--quiet"]), 0)
            (root / "svc.py").write_text("def added() -> int:\n    return 2\n")
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--out", str(out), "--quiet"]), 1)

    def test_build_if_stale_skips_when_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = Path(tmp) / "d.md"
            run_cli(["build", "--root", str(root), "--out", str(out), "--quiet"])
            mtime = out.stat().st_mtime_ns
            run_cli(["build", "--root", str(root), "--out", str(out),
                     "--if-stale", "--quiet"])
            self.assertEqual(out.stat().st_mtime_ns, mtime)   # untouched
            (root / "svc.py").write_text("def other() -> int:\n    return 3\n")
            run_cli(["build", "--root", str(root), "--out", str(out),
                     "--if-stale", "--quiet"])
            self.assertNotEqual(out.stat().st_mtime_ns, mtime)


class TestedMarkerTest(unittest.TestCase):
    def test_symbol_named_in_tests_gets_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = build_digest(root)
        run_line = next(ln for ln in out.splitlines() if "run()" in ln)
        self.assertIn("✓", run_line)                     # named in test file
        self.assertIn("✓=referenced from tests", out)    # legend

    def test_untested_symbol_unmarked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "a.py").write_text("def lonely() -> int:\n    return 1\n")
            out = build_digest(root)
        lonely = next(ln for ln in out.splitlines() if "lonely()" in ln)
        self.assertNotIn("✓", lonely)


class SizeMarkerTest(unittest.TestCase):
    def test_large_body_marked_small_not(self):
        big = "def big() -> int:\n" + "".join(
            f"    x{i} = {i}\n" for i in range(60)) + "    return 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "a.py").write_text(big + "\ndef small() -> int:\n    return 1\n")
            out = build_digest(root)
        big_line = next(ln for ln in out.splitlines() if "big()" in ln)
        self.assertIn("⋮", big_line)
        small_line = next(ln for ln in out.splitlines() if "small()" in ln)
        self.assertNotIn("⋮", small_line)


class BehaviorsTest(unittest.TestCase):
    def test_opt_in_behavior_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            plain = build_digest(root)
            with_b = build_digest(root, behaviors=True)
        self.assertNotIn("? ", plain.split("legend")[1])
        self.assertIn("? test_svc: test_run_returns_one", with_b)


class DepsMapTest(unittest.TestCase):
    def test_cross_module_type_reference_produces_edge(self):
        syms = [
            Symbol(name="Core", kind="class", file="core/c.py", line=1,
                   visibility="pub"),
            Symbol(name="use_core", kind="fn", file="app/a.py", line=1,
                   signature="use_core()", visibility="pub"),
        ]
        tokens = {"core/c.py": {"Core"},
                  "app/a.py": {"use_core", "Core"}}
        lines = hologram._dep_lines(syms, tokens, min_refs=1)
        self.assertTrue(any("app→core" in ln for ln in lines))


class DiffCommandTest(unittest.TestCase):
    def test_diff_shows_added_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qm", "one"], cwd=root, check=True)
            (root / "svc.py").write_text(
                (root / "svc.py").read_text()
                + "\ndef fresh_fn() -> int:\n    return 9\n")
            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = run_cli(["diff", "HEAD", "--root", str(root), "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("+fresh_fn():int", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
