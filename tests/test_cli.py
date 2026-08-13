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
    def test_build_embeds_map_in_claude_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            code = run_cli(["build", "--root", str(proj), "--quiet"])
            self.assertEqual(code, 0)
            content = hologram.embedded_digest(proj / "CLAUDE.md")
            self.assertIn("PricingEngine", content)
            self.assertIn("> ", content)


@needs_java
class InitHooksTest(unittest.TestCase):
    def test_init_installs_hooks_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            hook = repo / ".git" / "hooks" / "post-commit"
            self.assertTrue(hook.exists())
            content = hook.read_text()
            self.assertEqual(content.count("hologram.py"), 1)
            self.assertNotIn("--embed", content)     # embedding is the only mode
            self.assertIn("hologram:start", (repo / "CLAUDE.md").read_text())

    def test_init_replaces_hook_line_from_older_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            script = Path(hologram.__file__).resolve().parent.parent / "hologram.py"
            hook = repo / ".git" / "hooks" / "post-commit"
            old = (f'python3 "{script}" build --root "{repo.resolve()}" '
                   f'--no-embed --quiet || true')
            hook.write_text("#!/bin/sh\n" + old + "\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertNotIn("--no-embed", content)
            self.assertEqual(content.count("hologram.py"), 1)

    def test_init_preserves_custom_wrapped_hologram_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            script = Path(hologram.__file__).resolve().parent.parent / "hologram.py"
            custom = (f'[ -f /tmp/run-hologram ] && python3 "{script}" build '
                      f'--root "{repo.resolve()}" --quiet || true')
            hook.write_text("#!/bin/sh\n" + custom + "\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertIn(custom, content)
            self.assertEqual(content.count("hologram.py"), 2)

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


@needs_java
class HookQuotingTest(unittest.TestCase):
    def test_dollar_in_repo_path_is_escaped_in_hook_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "x$(touch pwned)"
            outer.mkdir()
            repo = _make_repo(outer)
            run_cli(["init", "--root", str(repo), "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
        self.assertIn("x\\$(touch pwned)", hook)
        self.assertNotIn('"' + str(repo) + '"', hook)  # raw form absent

    def test_reinit_replaces_escaped_line_not_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "y$z"
            outer.mkdir()
            repo = _make_repo(outer)
            run_cli(["init", "--root", str(repo), "--quiet"])
            run_cli(["init", "--root", str(repo), "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
        self.assertEqual(hook.count("build --root"), 1)


class LangFilterPersistenceTest(unittest.TestCase):
    """--lang is stamped into the map header and recalled by later commands."""

    def _proj(self, tmp: Path) -> Path:
        root = tmp / "p"
        root.mkdir()
        (root / "app.py").write_text("def visible():\n    pass\n")
        (root / "tool.sh").write_text("hidden() {\n  true\n}\n")
        return root

    def test_filter_stamped_recalled_and_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--lang", "python", "--quiet"])
            text = (root / "CLAUDE.md").read_text()
            self.assertIn("· langs python", text)
            self.assertNotIn("hidden", text)
            # check without --lang recalls the filter
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 0)
            # out-of-filter edits don't stale the map; in-filter edits do
            (root / "tool.sh").write_text("changed() {\n  true\n}\n")
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 0)
            (root / "app.py").write_text("def visible2():\n    pass\n")
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 1)
            # rebuild without --lang keeps the stored filter
            run_cli(["build", "--root", str(root), "--quiet"])
            text = (root / "CLAUDE.md").read_text()
            self.assertIn("· langs python", text)
            self.assertIn("visible2", text)
            self.assertNotIn("changed", text)

    @unittest.skipUnless(hologram.has_parser("bash"),
                         "tree-sitter-bash not installed")
    def test_lang_all_clears_stored_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--lang", "python", "--quiet"])
            run_cli(["build", "--root", str(root), "--lang", "all", "--quiet"])
            text = (root / "CLAUDE.md").read_text()
            self.assertNotIn("· langs", text)
            self.assertIn("hidden", text)


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
                proj = Path(tmp) / "proj"
                shutil.copytree(JAVAMINI, proj)
                with self.assertRaises(SystemExit) as ctx:
                    run_cli(["build", "--root", str(proj), "--quiet"])
            self.assertIn("pip install", str(ctx.exception))
            self.assertIn("tree-sitter-java", str(ctx.exception))
        finally:
            hologram._PARSERS["java"] = saved
            del os.environ["HOLOGRAM_BOOTSTRAPPED"]


@needs_java
class PrintCommandTest(unittest.TestCase):
    def test_print_writes_digest_to_stdout_and_touches_nothing(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            before = sorted(p.relative_to(proj) for p in proj.rglob("*"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_cli(["print", "--root", str(proj), "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("PricingEngine", out.getvalue())
            self.assertIn("# hologram ·", out.getvalue())
            after = sorted(p.relative_to(proj) for p in proj.rglob("*"))
            self.assertEqual(before, after)  # no CLAUDE.md created, nothing embedded


@needs_java
class UninstallTest(unittest.TestCase):
    def test_uninstall_removes_hooks_and_blocks_preserving_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            claude = repo / "CLAUDE.md"
            claude.write_text("# My notes\n\nHand-written guidance.\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            self.assertIn("hologram:start", claude.read_text())
            self.assertTrue((repo / ".git" / "hooks" / "post-commit").exists())
            run_cli(["uninstall", "--root", str(repo), "--quiet"])
            content = claude.read_text()
            self.assertNotIn("hologram:start", content)
            self.assertIn("Hand-written guidance.", content)
            self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())

    def test_uninstall_keeps_foreign_hook_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_text("#!/bin/sh\necho existing\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            run_cli(["uninstall", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertIn("echo existing", content)
            self.assertNotIn("hologram:managed", content)

    def test_uninstall_deletes_managed_rule_dir_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            (repo / ".cursor" / "rules").mkdir(parents=True)
            run_cli(["build", "--root", str(repo), "--quiet"])
            managed = repo / ".cursor" / "rules" / "hologram.mdc"
            self.assertTrue(managed.exists())
            run_cli(["uninstall", "--root", str(repo), "--quiet"])
            self.assertFalse(managed.exists())

    def test_keep_blocks_limits_to_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            run_cli(["init", "--root", str(repo), "--quiet"])
            run_cli(["uninstall", "--root", str(repo), "--keep-blocks", "--quiet"])
            self.assertIn("hologram:start", (repo / "CLAUDE.md").read_text())
            self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())


@needs_java
class SizeWarningTest(unittest.TestCase):
    def test_warns_over_threshold_but_embeds_exactly(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                run_cli(["build", "--root", str(proj), "--warn-tokens", "10",
                         "--quiet"])
            self.assertIn("warning", err.getvalue())
            self.assertIn("--lang", err.getvalue())
            embedded = hologram.embedded_digest(proj / "CLAUDE.md")
            self.assertIn("PricingEngine", embedded)  # embedded exactly, not cut

    def test_zero_disables_warning(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                run_cli(["build", "--root", str(proj), "--warn-tokens", "0",
                         "--quiet"])
            self.assertEqual(err.getvalue(), "")


class HookPythonSelectionTest(unittest.TestCase):
    def test_hook_uses_tool_venv_python_when_present(self):
        from hologram import _hook_python
        tool_dir = Path(__file__).resolve().parents[1]
        venv_py = tool_dir / ".venv" / "bin" / "python"
        expected = str(venv_py) if venv_py.exists() else "python3"
        self.assertEqual(_hook_python(), expected)


if __name__ == "__main__":
    unittest.main()
