import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import build_digest, run_cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


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


class CliBuildTest(unittest.TestCase):
    def test_build_writes_digest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "digest.md"
            code = run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                            "--budget", "4000", "--quiet"])
            self.assertEqual(code, 0)
            content = out.read_text()
            self.assertIn("PricingEngine", content)
            self.assertIn("> ", content)


class InitHooksTest(unittest.TestCase):
    def test_init_installs_hooks_and_gitignore_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            hook = repo / ".git" / "hooks" / "post-commit"
            self.assertTrue(hook.exists())
            content = hook.read_text()
            self.assertEqual(content.count("digest.py"), 1)
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
            self.assertIn("mdl-digest", content)


class CacheTest(unittest.TestCase):
    def test_cache_created_and_output_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            first = build_digest(repo, budget=4000, cache_dir=repo / ".git" / "mdl-digest")
            cache_file = repo / ".git" / "mdl-digest" / "cache.json"
            self.assertTrue(cache_file.exists())
            second = build_digest(repo, budget=4000, cache_dir=repo / ".git" / "mdl-digest")
            self.assertEqual(first, second)

    def test_cache_invalidated_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            cache_dir = repo / ".git" / "mdl-digest"
            build_digest(repo, budget=4000, cache_dir=cache_dir)
            target = repo / "src" / "engine" / "PricingEngine.java"
            target.write_text(target.read_text().replace("evaluate", "quoteFor"))
            out = build_digest(repo, budget=4000, cache_dir=cache_dir)
            self.assertIn("quoteFor", out)
            cache = json.loads((cache_dir / "cache.json").read_text())
            entry = cache["files"]["src/engine/PricingEngine.java"]
            self.assertTrue(any(s["name"] == "quoteFor" for s in entry["symbols"]))


if __name__ == "__main__":
    unittest.main()


class InitLangTest(unittest.TestCase):
    def test_lang_flag_baked_into_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            run_cli(["init", "--root", str(repo), "--lang", "java", "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
            self.assertIn("--lang java", hook)


class HookPythonSelectionTest(unittest.TestCase):
    def test_hook_uses_tool_venv_python_when_present(self):
        from digest import _hook_python
        tool_dir = Path(__file__).resolve().parents[1]
        venv_py = tool_dir / ".venv" / "bin" / "python"
        expected = str(venv_py) if venv_py.exists() else "python3"
        self.assertEqual(_hook_python(), expected)
