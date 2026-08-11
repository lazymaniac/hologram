import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import hologram
from hologram import CONFIG_NAME, default_config, render_config
from hologram.cli import command_build, command_check


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


class CanonicalDeliveryFreshnessTest(unittest.TestCase):
    def test_managed_context_is_fresh_then_stale_without_touching_authored_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "app.py"
            source.write_text("def answer() -> int:\n    return 42\n", encoding="utf-8")
            authored = b"# Project rules\r\nPreserve this exactly.\r\n"
            context_path = root / "CLAUDE.md"
            context_path.write_bytes(authored)
            config = replace(
                default_config(),
                agents=("claude",),
                languages=(hologram.Language.PYTHON,),
                include=("**/*.py",),
                exclude=(),
                output=None,
            )
            config_path = root / CONFIG_NAME
            config_path.write_text(render_config(config), encoding="utf-8")

            self.assertEqual(command_build(root, config_path, quiet=True), 0)
            delivered = context_path.read_bytes()
            self.assertTrue(delivered.startswith(authored))
            self.assertIn(b"hologram:start", delivered)
            self.assertIn(b"# hologram state=", delivered)
            self.assertFalse((root / "PROJECT_DIGEST.md").exists())
            self.assertEqual(command_check(root, config_path, quiet=True), 0)

            source.write_text("def answer() -> int:\n    return 43\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(command_check(root, config_path, quiet=True), 1)
            self.assertEqual(context_path.read_bytes(), delivered)


class PhasePublicApiTest(unittest.TestCase):
    def test_cli_internals_are_not_package_root_exports(self):
        for name in (
            "main",
            "EXIT_OK",
            "EXIT_STALE",
            "EXIT_USAGE",
            "EXIT_INCOMPLETE",
        ):
            self.assertNotIn(name, hologram.__all__)
            with self.assertRaises(AttributeError):
                getattr(hologram, name)

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

from hologram import AnalyzedProject, analyze_project

analysis = sys.modules["hologram.analysis"]
assert AnalyzedProject is analysis.AnalyzedProject
assert analyze_project is analysis.analyze_project
assert "hologram.render" not in sys.modules

from hologram import RenderIR, decode_render, project_render_ir, render_project

render = sys.modules["hologram.render"]
assert RenderIR is render.RenderIR
assert decode_render is render.decode_render
assert project_render_ir is render.project_render_ir
assert render_project is render.render_project
phase_public = set(analysis.__all__) | set(render.__all__)
assert set(hologram.__all__) & phase_public == expected
assert "hologram.analysis" in sys.modules
"""
        render_first = """
import sys
import hologram

assert "hologram.analysis" not in sys.modules
assert "hologram.render" not in sys.modules

from hologram import RenderIR, decode_render, project_render_ir, render_project

render = sys.modules["hologram.render"]
assert RenderIR is render.RenderIR
assert decode_render is render.decode_render
assert project_render_ir is render.project_render_ir
assert render_project is render.render_project
assert "hologram.analysis" in sys.modules
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


if __name__ == "__main__":
    unittest.main()
