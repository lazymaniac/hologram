import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram
from hologram import (
    CONFIG_NAME,
    ProjectConfig,
    build_digest,
    default_config,
    render_config,
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


def rendered_state(digest: str) -> str:
    match = hologram.state.STATE_HEADER_RE.search(digest.split("\n", 1)[0])
    if match is None:
        raise AssertionError("digest is missing a canonical state header")
    return match.group(1)


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

    def test_render_modes_have_distinct_library_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            config = default_config()
            modes = (
                (False, False),
                (True, False),
                (False, True),
                (True, True),
            )
            states = []
            for private_sigs, behaviors in modes:
                digest = build_digest(
                    root,
                    private_sigs=private_sigs,
                    behaviors=behaviors,
                    config=config,
                )
                state = rendered_state(digest)
                states.append(state)
                self.assertEqual(
                    state,
                    hologram._state_hash(
                        root,
                        config,
                        private_sigs=private_sigs,
                        behaviors=behaviors,
                    ),
                )
        self.assertEqual(len(set(states)), len(modes))

    def test_cli_freshness_rejects_different_render_modes(self):
        for option in ("--private", "--behaviors"):
            with self.subTest(option=option):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    root = _proj(Path(tmp))
                    write_manifest(root)
                    out = Path(tmp) / "digest.md"
                    flagged = [
                        "build",
                        "--root",
                        str(root),
                        "--out",
                        str(out),
                        option,
                        "--quiet",
                    ]
                    run_cli(flagged)
                    flagged_state = rendered_state(out.read_text(encoding="utf-8"))
                    self.assertEqual(
                        run_cli(
                            [
                                "check",
                                "--root",
                                str(root),
                                "--out",
                                str(out),
                                option,
                                "--quiet",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        run_cli(
                            [
                                "check",
                                "--root",
                                str(root),
                                "--out",
                                str(out),
                                "--quiet",
                            ]
                        ),
                        1,
                    )
                    run_cli(
                        [
                            "build",
                            "--root",
                            str(root),
                            "--out",
                            str(out),
                            "--if-stale",
                            "--quiet",
                        ]
                    )
                    public_state = rendered_state(out.read_text(encoding="utf-8"))
                    self.assertNotEqual(flagged_state, public_state)
                    self.assertEqual(
                        run_cli(
                            [
                                "check",
                                "--root",
                                str(root),
                                "--out",
                                str(out),
                                option,
                                "--quiet",
                            ]
                        ),
                        1,
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

    def test_stale_if_stale_reuses_one_snapshot_for_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            write_manifest(root)
            out = Path(tmp) / "digest.md"
            out.write_text(
                f"# proj · state={'0' * 64} · regen: old\n",
                encoding="utf-8",
            )
            real_build = hologram.legacy.build_project
            snapshots = []

            def build_then_mutate(*args, **kwargs):
                snapshot = real_build(*args, **kwargs)
                snapshots.append(snapshot)
                (root / "svc.py").write_text(
                    "def disk_only() -> int:\n    return 2\n",
                    encoding="utf-8",
                )
                return snapshot

            with mock.patch.object(
                hologram.legacy,
                "build_project",
                side_effect=build_then_mutate,
            ):
                run_cli(
                    [
                        "build",
                        "--root",
                        str(root),
                        "--out",
                        str(out),
                        "--if-stale",
                        "--quiet",
                    ]
                )

            digest = out.read_text(encoding="utf-8")
        self.assertEqual(len(snapshots), 1)
        self.assertIn("Svc", digest)
        self.assertNotIn("disk_only", digest)

    def test_build_scans_once_and_never_rereads_snapshot_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            source_paths = {
                (root / "svc.py").resolve(),
                (root / "test_svc.py").resolve(),
            }
            from hologram import pipeline

            real_scan = pipeline.scan_project
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
                    pipeline,
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
                extractor_versions={},
                parser_versions={},
            )
            out = Path(tmp) / "digest.md"
            out.write_text(
                f"# proj · state={state.value} · regen: hologram build\n",
                encoding="utf-8",
            )

            with mock.patch(
                "hologram.pipeline.scan_project",
                return_value=scan_result,
            ):
                with self.assertRaises(
                    hologram.IncompleteBuildError
                ) as caught:
                    hologram._state_hash(root, config)
                self.assertEqual(caught.exception.diagnostics, diagnostics)

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = run_cli(
                        [
                            "check",
                            "--root",
                            str(root),
                            "--out",
                            str(out),
                            "--quiet",
                        ]
                    )
                self.assertEqual(code, 3)
                self.assertIn("scan-root-open-failed", stderr.getvalue())
                self.assertIn("scan-walk-error", stderr.getvalue())


class MarkerEndToEndTest(unittest.TestCase):
    def test_rendered_map_has_only_approved_advisories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "core.py").write_text(
                "def hot(value: int) -> int:\n"
                "    adjusted = value + 1\n"
                "    if adjusted > 5:\n"
                "        return adjusted * 2\n"
                "    return adjusted - 1\n\n"
                "def _unused_private(value: int) -> int:\n"
                "    return value + 1\n\n"
                "def public_surface(value: int) -> int:\n"
                "    return value + 2\n\n"
                "def tested_api(value: int) -> int:\n"
                "    return value + 3\n\n"
                "def _dynamic_callback(value: int) -> int:\n"
                "    return value + 4\n\n"
                "def _comment_decoy(value: int) -> int:\n"
                "    return value + 5\n\n"
                "def clone_a(value: int) -> int:\n"
                "    total = value + 1\n"
                "    if total > 3:\n"
                "        total = total * 2\n"
                "    else:\n"
                "        total = total - 2\n"
                "    for offset in range(2):\n"
                "        total = total + offset\n"
                "    return total\n\n"
                "def clone_b(value: int) -> int:\n"
                "    total = value + 1\n"
                "    if total > 3:\n"
                "        total = total * 2\n"
                "    else:\n"
                "        total = total - 2\n"
                "    for offset in range(2):\n"
                "        total = total + offset\n"
                "    return total\n\n"
                "def keep_clones_live(value: int) -> int:\n"
                "    return clone_a(value) + clone_b(value)\n",
                encoding="utf-8",
            )
            (root / "left.py").write_text(
                "from core import hot\n\n"
                "def left(value: int) -> int:\n"
                "    return hot(value)\n",
                encoding="utf-8",
            )
            (root / "right.py").write_text(
                "from core import hot\n\n"
                "def right(value: int) -> int:\n"
                "    return hot(value)\n",
                encoding="utf-8",
            )
            (root / "config.py").write_text(
                'set_callback(callback="_dynamic_callback")\n',
                encoding="utf-8",
            )
            (root / "test_core.py").write_text(
                "from core import tested_api\n\n"
                "def test_api() -> None:\n"
                "    assert tested_api(1) == 4\n",
                encoding="utf-8",
            )
            (root / "notes.py").write_text(
                "# _comment_decoy() is documentation, not a reference.\n",
                encoding="utf-8",
            )

            config = replace(default_config(), hot_threshold=2)
            snapshot = hologram.build_project(root, config).require_complete()
            analyzed = hologram.analyze_project(
                snapshot.project,
                snapshot.resolution,
                hot_threshold=config.hot_threshold,
            )
            projected = hologram.project_render_ir(
                analyzed,
                state=snapshot.state.value,
                hot_threshold=config.hot_threshold,
            )
            text = hologram.render_project(projected)
            decoded = hologram.decode_render(text)

        self.assertEqual(decoded, projected)
        self.assertEqual(hologram.render_project(decoded), text)
        by_name = {
            symbol.symbol_id.name: symbol.markers
            for file_ir in decoded.files
            for symbol in file_ir.symbols
        }
        self.assertEqual(by_name["hot"], ("×2",))
        self.assertEqual(by_name["_unused_private"], ("×0",))
        self.assertEqual(by_name["public_surface"], ("×0?",))
        self.assertEqual(by_name["tested_api"], ("×0?", "✓"))
        self.assertEqual(by_name["clone_a"], ("≈1",))
        self.assertEqual(by_name["clone_b"], ("≈1",))
        self.assertEqual(by_name["_dynamic_callback"], ("×0?",))
        self.assertEqual(by_name["_comment_decoy"], ("×0",))


class PhasePublicApiTest(unittest.TestCase):
    def test_exact_phase_exports_are_lazy_and_identity_preserving(self):
        expected = {
            "AnalyzedProject",
            "RenderIR",
            "analyze_project",
            "decode_render",
            "project_render_ir",
            "render_project",
        }
        self.assertTrue(expected <= set(hologram.__all__))
        self.assertNotIn("RenderDecodeError", hologram.__all__)

        script = """
import sys
import hologram

expected = {
    "AnalyzedProject",
    "RenderIR",
    "analyze_project",
    "decode_render",
    "project_render_ir",
    "render_project",
}
assert expected <= set(hologram.__all__)
assert "RenderDecodeError" not in hologram.__all__
assert "hologram.analysis" not in sys.modules
assert "hologram.render" not in sys.modules
assert "hologram.legacy" not in sys.modules

from hologram import AnalyzedProject, analyze_project

analysis = sys.modules["hologram.analysis"]
assert AnalyzedProject is analysis.AnalyzedProject
assert analyze_project is analysis.analyze_project
assert "hologram.render" not in sys.modules
assert "hologram.legacy" not in sys.modules

from hologram import RenderIR, decode_render, project_render_ir, render_project

render = sys.modules["hologram.render"]
assert RenderIR is render.RenderIR
assert decode_render is render.decode_render
assert project_render_ir is render.project_render_ir
assert render_project is render.render_project
phase_public = set(analysis.__all__) | set(render.__all__)
assert set(hologram.__all__) & phase_public == expected
assert "hologram.analysis" in sys.modules
assert "hologram.legacy" not in sys.modules
"""
        render_first = """
import sys
import hologram

assert "hologram.analysis" not in sys.modules
assert "hologram.render" not in sys.modules
assert "hologram.legacy" not in sys.modules

from hologram import RenderIR, decode_render, project_render_ir, render_project

render = sys.modules["hologram.render"]
assert RenderIR is render.RenderIR
assert decode_render is render.decode_render
assert project_render_ir is render.project_render_ir
assert render_project is render.render_project
assert "hologram.analysis" in sys.modules
assert "hologram.legacy" not in sys.modules
"""
        for name, child_script in (
            ("analysis-first", script),
            ("render-first", render_first),
        ):
            child = subprocess.run(
                [sys.executable, "-c", child_script],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(child.returncode, 0, f"{name}: {child.stderr}")


class TestedMarkerTest(unittest.TestCase):
    def test_symbol_named_in_tests_gets_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = build_digest(root, config=default_config())
        run_line = next(ln for ln in out.splitlines() if "run()" in ln)
        self.assertIn("✓", run_line)                     # named in test file
        self.assertIn("✓=referenced from tests", out)    # legend

    def test_untested_symbol_unmarked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "a.py").write_text("def lonely() -> int:\n    return 1\n")
            out = build_digest(root, config=default_config())
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
            out = build_digest(root, config=default_config())
        big_line = next(ln for ln in out.splitlines() if "big()" in ln)
        self.assertIn("⋮", big_line)
        small_line = next(ln for ln in out.splitlines() if "small()" in ln)
        self.assertNotIn("⋮", small_line)


class BehaviorsTest(unittest.TestCase):
    def test_opt_in_behavior_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            config = default_config()
            plain = build_digest(root, config=config)
            with_b = build_digest(root, behaviors=True, config=config)
        self.assertNotIn("? ", plain.split("legend")[1])
        self.assertIn("? test_svc: test_run_returns_one", with_b)


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
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = run_cli(["diff", "HEAD", "--root", str(root), "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("+fresh_fn():int", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
