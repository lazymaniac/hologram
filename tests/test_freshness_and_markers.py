import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import (  # noqa: E402
    CONFIG_NAME,
    ProjectConfig,
    Symbol,
    build_digest,
    default_config,
    render_config,
    render_simple,
    run_cli,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def write_manifest(root: Path) -> ProjectConfig:
    config = default_config()
    (root / CONFIG_NAME).write_text(render_config(config), encoding="utf-8")
    return config


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
            write_manifest(root)
            out = Path(tmp) / "d.md"
            run_cli(["build", "--root", str(root), "--out", str(out), "--quiet"])
            self.assertEqual(hologram._digest_state(out),
                             hologram._state_hash(root, default_config()))
            self.assertRegex(
                out.read_text(encoding="utf-8").splitlines()[0],
                r"(?:^|[ ·])state=[0-9a-f]{64}(?=$|[ ·])",
            )

    def test_check_fresh_then_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            write_manifest(root)
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
            write_manifest(root)
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

    def test_build_scans_once_and_never_rereads_snapshot_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            source_paths = {
                (root / "svc.py").resolve(),
                (root / "test_svc.py").resolve(),
            }
            real_scan = hologram.legacy.scan.scan_project
            real_read_bytes = Path.read_bytes
            real_read_text = Path.read_text
            scan_calls = 0

            def counted_scan(*args, **kwargs):
                nonlocal scan_calls
                scan_calls += 1
                return real_scan(*args, **kwargs)

            def guarded_read_bytes(path, *args, **kwargs):
                if path.resolve() in source_paths:
                    raise AssertionError(f"source path reread: {path}")
                return real_read_bytes(path, *args, **kwargs)

            def guarded_read_text(path, *args, **kwargs):
                if path.resolve() in source_paths:
                    raise AssertionError(f"source path reread: {path}")
                return real_read_text(path, *args, **kwargs)

            with (
                mock.patch.object(
                    hologram.legacy.scan,
                    "scan_project",
                    side_effect=counted_scan,
                ),
                mock.patch.object(Path, "read_bytes", new=guarded_read_bytes),
                mock.patch.object(Path, "read_text", new=guarded_read_text),
            ):
                digest = build_digest(root, config=default_config())

        self.assertEqual(scan_calls, 1)
        self.assertIn("Svc", digest)

    def test_incomplete_scan_never_compares_as_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            config = write_manifest(root)
            diagnostics = (
                hologram.Diagnostic(
                    "scan-root-open-failed",
                    hologram.DiagnosticSeverity.ERROR,
                    "first failure",
                ),
                hologram.Diagnostic(
                    "scan-walk-error",
                    hologram.DiagnosticSeverity.ERROR,
                    "second failure",
                ),
            )
            scan_result = hologram.ScanResult(
                (
                    hologram.ScanEntry(
                        root / "<filesystem>",
                        "<filesystem>",
                        None,
                        hologram.ScanStatus.FAILED,
                        "root-open-failed",
                        None,
                    ),
                    hologram.ScanEntry(
                        root / "blocked/private",
                        "blocked/private",
                        None,
                        hologram.ScanStatus.FAILED,
                        "walk-error",
                        None,
                    ),
                ),
                diagnostics,
                False,
            )
            state = hologram.compute_state(
                root,
                config,
                scan_result,
                extractor_versions=hologram.legacy.LEGACY_EXTRACTOR_VERSIONS,
                parser_versions=hologram.legacy.LEGACY_PARSER_VERSIONS,
            )
            out = Path(tmp) / "digest.md"
            out.write_text(
                f"# proj · state={state.value} · regen: hologram build\n",
                encoding="utf-8",
            )

            actions = (
                lambda: hologram._state_hash(root, config),
                lambda: run_cli(
                    [
                        "check",
                        "--root",
                        str(root),
                        "--out",
                        str(out),
                        "--quiet",
                    ]
                ),
            )
            for action in actions:
                with self.subTest(action=action):
                    with mock.patch.object(
                        hologram.legacy.scan,
                        "scan_project",
                        return_value=scan_result,
                    ):
                        with self.assertRaises(SystemExit) as caught:
                            action()
                    self.assertEqual(
                        str(caught.exception),
                        "first failure; second failure",
                    )


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


class EmbedTest(unittest.TestCase):
    DIGEST = ("# proj @x 2026-08-08 · 10 LOC · state=" + "a" * 64
              + " · regen: x\n"
              "· legend: …\n"
              "src\n"
              " Svc(C)\n"
              "  run():int ✓ > _step\n"
              "  - _step\n")

    def test_embed_creates_block_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm = Path(tmp) / "CLAUDE.md"
            cm.write_text("# My rules\nUse tabs.\n")
            tier = hologram.embed_digest(cm, self.DIGEST)
            text = cm.read_text()
        self.assertEqual(tier, "full")
        self.assertIn("My rules", text)                     # user content kept
        self.assertIn("hologram:start", text)
        self.assertIn("run():int ✓ > _step", text)
        self.assertIn("whole codebase at a glance", text)

    def test_embed_is_idempotent_and_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm = Path(tmp) / "CLAUDE.md"
            cm.write_text("before\n")
            hologram.embed_digest(cm, self.DIGEST)
            hologram.embed_digest(cm, self.DIGEST.replace("run()", "go()"))
            text = cm.read_text()
        self.assertEqual(text.count("hologram:start"), 1)   # one block, replaced
        self.assertIn("go():int", text)
        self.assertNotIn("run():int", text)
        self.assertTrue(text.startswith("before"))

    def test_degradation_tiers(self):
        big = self.DIGEST + "\n".join(
            f"  method{i}(int):int > callee{i},other{i}" for i in range(400))
        body, tier = hologram._reduce_for_embed(big, max_tokens=2000)
        self.assertEqual(tier, "types-only")
        self.assertNotIn("> callee1,", body)                # chains gone
        self.assertNotIn("method1(int)", body)              # methods gone
        self.assertIn("Svc(C)", body)                       # shape kept

    def test_cli_build_embed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            write_manifest(root)
            out = Path(tmp) / "d.md"
            run_cli(["build", "--root", str(root), "--out", str(out),
                     "--embed", "--quiet"])
            text = (root / "CLAUDE.md").read_text()
        self.assertIn("hologram:start", text)
        self.assertIn("Svc(C)", text)


class DiffCommandTest(unittest.TestCase):
    def test_diff_shows_added_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            write_manifest(root)
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
