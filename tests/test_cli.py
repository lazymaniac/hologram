import shlex
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


def _hologram_hook_lines(content: str) -> list[str]:
    return [
        line for line in content.splitlines()
        if "--root" in line
        and ("-m hologram build" in line or "hologram.py" in line)
    ]


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

    def test_digest_regen_command_uses_module_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "hologram.md"
            run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                     "--quiet"])

            header = out.read_text().splitlines()[0]
            regen = header.split(" · regen: ", 1)[1]
            self.assertNotIn("legacy.py", regen)
            self.assertEqual(
                shlex.split(regen),
                [sys.executable, "-m", "hologram", "build"],
            )


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
            self.assertEqual(content.count("-m hologram"), 1)
            self.assertNotIn("hologram.py", content)
            gitignore = (repo / ".gitignore").read_text()
            self.assertEqual(gitignore.count("PROJECT_DIGEST.md"), 1)

    def test_init_replaces_generated_command_when_options_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            run_cli(["init", "--root", str(repo), "--lang", "java", "--quiet"])
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_text(hook.read_text() + "echo keep-existing\n")

            run_cli(["init", "--root", str(repo), "--embed", "--quiet"])

            content = hook.read_text()
            commands = _hologram_hook_lines(content)
            self.assertEqual(len(commands), 1)
            self.assertIn("--embed", commands[0])
            self.assertNotIn("--lang java", commands[0])
            self.assertIn("echo keep-existing", content)

    def test_init_upgrades_legacy_generated_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_text(
                "#!/bin/sh\n"
                "echo before\n"
                f'{sys.executable} "/opt/hologram/hologram.py" build '
                f'--root "{repo.resolve()}" --lang java --quiet || true\n'
                "echo after\n"
            )

            run_cli(["init", "--root", str(repo), "--quiet"])

            content = hook.read_text()
            self.assertEqual(len(_hologram_hook_lines(content)), 1)
            self.assertNotIn("hologram.py", content)
            self.assertIn("echo before", content)
            self.assertIn("echo after", content)

    def test_generated_hook_quotes_interpreter_and_root_path(self):
        with tempfile.TemporaryDirectory(prefix="hook $HOME path; ") as tmp:
            tmp_path = Path(tmp)
            repo = _make_repo(tmp_path)
            actual_python = sys.executable
            wrapper = tmp_path / "python launcher"
            wrapper.write_text(
                f"#!/bin/sh\nexec {shlex.quote(actual_python)} \"$@\"\n"
            )
            wrapper.chmod(0o755)
            sys.executable = str(wrapper)
            try:
                run_cli(["init", "--root", str(repo), "--quiet"])
            finally:
                sys.executable = actual_python

            hook = repo / ".git" / "hooks" / "post-commit"
            command = _hologram_hook_lines(hook.read_text())[0]
            self.assertTrue(command.endswith(" || true"))
            self.assertEqual(
                shlex.split(command.removesuffix(" || true")),
                [str(wrapper), "-m", "hologram", "build", "--root",
                 str(repo.resolve()), "--quiet"],
            )

            digest = repo / "PROJECT_DIGEST.md"
            digest.unlink()
            result = subprocess.run(
                [str(hook)], cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(digest.exists(), result.stderr)

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

    def test_cli_fails_fast_with_parser_extra_guidance_without_writes(self):
        saved = hologram._PARSERS["java"]
        hologram._PARSERS["java"] = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "d.md"
                with self.assertRaises(SystemExit) as ctx:
                    run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                             "--quiet"])
                self.assertFalse(out.exists())
            self.assertIn(
                f"{sys.executable} -m pip install 'hologram-code-map[parsers]'",
                str(ctx.exception),
            )
            self.assertIn("tree-sitter-java", str(ctx.exception))
        finally:
            hologram._PARSERS["java"] = saved


class HookPythonSelectionTest(unittest.TestCase):
    def test_hook_uses_current_python(self):
        from hologram import _hook_python

        self.assertEqual(_hook_python(), sys.executable)


if __name__ == "__main__":
    unittest.main()
