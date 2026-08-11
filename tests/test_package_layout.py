import subprocess
import sys
import tomllib
import unittest
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTest(unittest.TestCase):
    def test_root_monolith_is_removed(self):
        self.assertFalse((ROOT / "hologram.py").exists())

    def test_legacy_module_is_removed(self):
        self.assertFalse((ROOT / "src" / "hologram" / "legacy.py").exists())
        self.assertIsNone(find_spec("hologram.legacy"))

    def test_import_resolves_to_src_package(self):
        import hologram

        self.assertEqual(
            Path(hologram.__file__).resolve(),
            ROOT / "src" / "hologram" / "__init__.py",
        )

    def test_module_execution_exposes_cli(self):
        result = subprocess.run(
            [sys.executable, "-m", "hologram", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: hologram", result.stdout)

    def test_console_script_source_targets_v2_cli(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]

        self.assertEqual(project["scripts"], {"hologram": "hologram.cli:main"})

    def test_editable_install_metadata_is_ignored(self):
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertNotIn(".egg-info/", result.stdout)


if __name__ == "__main__":
    unittest.main()
