import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import run_cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"
PYMINI_FILE = FIXTURES / "pymini" / "app.py"

needs_java = unittest.skipUnless(hologram.has_parser("java"),
                                 "tree-sitter-java not installed")


def _make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    shutil.copytree(JAVAMINI, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


@needs_java
class CliBuildTest(unittest.TestCase):
    def test_build_writes_digest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "hologram.md"
            code = run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                            "--quiet"])
            self.assertEqual(code, 0)
            content = out.read_text()
            self.assertIn("PricingEngine", content)
            self.assertIn("> ", content)


@needs_java
class InitHooksTest(unittest.TestCase):
    def test_init_installs_hooks_and_gitignore_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            hook = repo / ".git" / "hooks" / "post-commit"
            self.assertTrue(hook.exists())
            content = hook.read_text()
            self.assertEqual(content.count("hologram.py"), 1)
            gitignore = (repo / ".gitignore").read_text()
            self.assertEqual(gitignore.count("PROJECT_DIGEST.md"), 1)

    def test_init_chains_existing_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_text("#!/bin/sh\necho existing\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertIn("echo existing", content)
            self.assertIn("hologram", content)


@needs_java
class InitLangTest(unittest.TestCase):
    def test_lang_flag_baked_into_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            run_cli(["init", "--root", str(repo), "--lang", "java", "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
            self.assertIn("--lang java", hook)


class BootstrapTest(unittest.TestCase):
    def test_missing_parser_langs_detects_gap(self):
        files = [JAVAMINI / "src/App.java", PYMINI_FILE]
        saved = hologram._PARSERS["java"]
        hologram._PARSERS["java"] = None
        try:
            self.assertEqual(hologram._missing_parser_langs(files), {"java"})
        finally:
            hologram._PARSERS["java"] = saved
        # python never needs a parser
        self.assertEqual(hologram._missing_parser_langs([PYMINI_FILE]), set())

    def test_cli_exits_with_instructions_when_bootstrap_exhausted(self):
        saved = hologram._PARSERS["java"]
        hologram._PARSERS["java"] = None
        os.environ["HOLOGRAM_BOOTSTRAPPED"] = "1"  # pretend re-exec already happened
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "d.md"
                with self.assertRaises(SystemExit) as ctx:
                    run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                             "--quiet"])
            self.assertIn("pip install", str(ctx.exception))
            self.assertIn("tree-sitter-java", str(ctx.exception))
        finally:
            hologram._PARSERS["java"] = saved
            del os.environ["HOLOGRAM_BOOTSTRAPPED"]


class HookPythonSelectionTest(unittest.TestCase):
    def test_hook_uses_tool_venv_python_when_present(self):
        from hologram import _hook_python
        tool_dir = Path(__file__).resolve().parents[1]
        venv_py = tool_dir / ".venv" / "bin" / "python"
        expected = str(venv_py) if venv_py.exists() else "python3"
        self.assertEqual(_hook_python(), expected)


if __name__ == "__main__":
    unittest.main()
