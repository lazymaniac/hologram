from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hologram.config import canonical_config_bytes, default_config
from hologram.model import Language

ROOT = Path(__file__).resolve().parents[1]
REMOVED_FLAGS = (
    "--embed",
    "--embed-max-tokens",
    "--out",
    "--lang",
    "--private",
    "--behaviors",
    "--if-stale",
)


def _invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "hologram", *args),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class ModuleCliSmokeTest(unittest.TestCase):
    def test_top_level_help_lists_exact_v2_commands(self) -> None:
        result = _invoke("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: hologram", result.stdout)
        self.assertIn("{init,build,check,diff}", result.stdout)
        for flag in REMOVED_FLAGS:
            self.assertNotIn(flag, result.stdout)
        self.assertEqual(result.stderr, "")

    def test_each_command_help_exposes_only_its_owned_options(self) -> None:
        expected = {
            "init": ("--root", "--config", "--quiet", "--agent", "--no-hook"),
            "build": ("--root", "--config", "--quiet"),
            "check": ("--root", "--config", "--quiet"),
            "diff": ("--root", "--config", "--quiet", "REV"),
        }
        for command, options in expected.items():
            with self.subTest(command=command):
                result = _invoke(command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"usage: hologram {command}", result.stdout)
                for option in options:
                    self.assertIn(option, result.stdout)
                for flag in REMOVED_FLAGS:
                    self.assertNotIn(flag, result.stdout)
                self.assertEqual(result.stderr, "")

    def test_no_command_and_every_removed_flag_exit_two_without_traceback(self) -> None:
        cases = (
            (),
            ("build", "--embed"),
            ("build", "--embed-max-tokens", "1"),
            ("build", "--out", "map.md"),
            ("build", "--lang", "python"),
            ("build", "--private"),
            ("build", "--behaviors"),
            ("build", "--if-stale"),
        )
        for args in cases:
            with self.subTest(args=args):
                result = _invoke(*args)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.startswith("hologram: "))
                self.assertNotIn("usage:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_real_schema_two_build_check_and_incomplete_exit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            source = root / "app.py"
            source.write_text("def answer() -> int:\n    return 42\n", encoding="utf-8")
            config = dataclasses.replace(
                default_config(),
                agents=(),
                languages=(Language.PYTHON,),
                include=("**/*.py",),
                exclude=(),
                output="map.md",
            )
            (root / ".hologram.toml").write_bytes(canonical_config_bytes(config))

            built = _invoke("build", "--root", str(root), "--quiet")
            self.assertEqual(built.returncode, 0, built.stderr)
            output = root / "map.md"
            complete = output.read_bytes()
            checked = _invoke("check", "--root", str(root), "--quiet")
            self.assertEqual(checked.returncode, 0, checked.stderr)

            source.write_text("def broken(:\n", encoding="utf-8")
            incomplete = _invoke("build", "--root", str(root), "--quiet")
            self.assertEqual(incomplete.returncode, 3, incomplete.stderr)
            self.assertTrue(incomplete.stderr.startswith("hologram: "))
            self.assertNotIn("Traceback", incomplete.stderr)
            self.assertEqual(output.read_bytes(), complete)


if __name__ == "__main__":
    unittest.main()
