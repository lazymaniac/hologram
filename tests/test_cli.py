import contextlib
import io
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hologram  # noqa: E402
import hologram.parsers.api as parser_api
from hologram import (  # noqa: E402
    CONFIG_NAME,
    ConfigError,
    ProjectConfig,
    default_config,
    render_config,
    run_cli,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"

needs_java = unittest.skipUnless(hologram.has_parser("java"),
                                 "tree-sitter-java not installed")


def write_manifest(root: Path) -> ProjectConfig:
    config = default_config()
    (root / CONFIG_NAME).write_text(render_config(config), encoding="utf-8")
    return config


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
            repo = _make_repo(Path(tmp))
            write_manifest(repo)
            out = Path(tmp) / "hologram.md"
            code = run_cli(["build", "--root", str(repo), "--out", str(out),
                            "--quiet"])
            self.assertEqual(code, 0)
            content = out.read_text()
            self.assertIn("PricingEngine", content)
            self.assertIn("> ", content)

    def test_digest_regen_command_uses_module_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            write_manifest(repo)
            out = Path(tmp) / "hologram.md"
            run_cli(["build", "--root", str(repo), "--out", str(out),
                     "--quiet"])

            header = out.read_text().splitlines()[0]
            regen = header.split(" · regen: ", 1)[1]
            self.assertNotIn("legacy.py", regen)
            self.assertEqual(
                shlex.split(regen),
                [sys.executable, "-m", "hologram", "build",
                 "--root", str(repo.resolve()),
                 "--out", str(out.resolve())],
            )

    def test_digest_regen_command_reproduces_non_default_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _make_repo(tmp_path)
            write_manifest(repo)
            (tmp_path / "artifacts").mkdir()
            relative_out = Path("artifacts") / "custom digest.md"

            with contextlib.chdir(tmp_path):
                run_cli([
                    "build",
                    "--root", str(repo),
                    "--out", str(relative_out),
                    "--lang", "python,java",
                    "--private",
                    "--behaviors",
                    "--embed",
                    "--embed-max-tokens", "1234",
                    "--if-stale",
                    "--quiet",
                ])

            out = tmp_path / relative_out
            header = out.read_text().splitlines()[0]
            regen = header.split(" · regen: ", 1)[1]
            self.assertNotIn("legacy.py", regen)
            self.assertEqual(
                shlex.split(regen),
                [sys.executable, "-m", "hologram", "build",
                 "--root", str(repo.resolve()),
                 "--out", str(out.resolve()),
                 "--lang", "java",
                 "--lang", "python",
                 "--private",
                 "--behaviors",
                 "--embed",
                 "--embed-max-tokens", "1234"],
            )


class ConfigBoundaryTest(unittest.TestCase):
    def test_build_and_check_require_manifest_before_scanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "app.py").write_text("def app() -> int:\n    return 1\n")
            for command in ("build", "check"):
                with self.subTest(command=command):
                    with mock.patch.object(
                        hologram.legacy,
                        "scan_files",
                        side_effect=AssertionError("scan must not run"),
                    ):
                        with self.assertRaises(ConfigError) as caught:
                            run_cli([command, "--root", str(root), "--quiet"])
                    self.assertIn(str(root / CONFIG_NAME), str(caught.exception))

    def test_init_does_not_write_through_dangling_manifest_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            target = root / "missing-manifest.toml"
            manifest = root / CONFIG_NAME
            manifest.symlink_to(target.name)

            with self.assertRaises(ConfigError):
                run_cli(["init", "--root", str(root), "--quiet"])

            self.assertTrue(manifest.is_symlink())
            self.assertEqual(manifest.readlink(), Path(target.name))
            self.assertFalse(target.exists())


@needs_java
class InitHooksTest(unittest.TestCase):
    def test_init_creates_exact_default_manifest_then_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))

            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)

            self.assertEqual(
                (repo / CONFIG_NAME).read_text(),
                render_config(default_config()),
            )
            self.assertTrue((repo / "PROJECT_DIGEST.md").exists())

    def test_init_loads_existing_manifest_without_overwriting_it(self):
        existing = """schema_version = 2
agents = ["claude"]
languages = ["java"]
include = ["src/**"]
exclude = []
hot_threshold = 7
output = "CUSTOM_DIGEST.md"
"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            manifest = repo / CONFIG_NAME
            manifest.write_text(existing)

            with mock.patch.object(
                hologram.legacy,
                "load_config",
                wraps=hologram.load_config,
            ) as load:
                self.assertEqual(
                    run_cli(["init", "--root", str(repo), "--quiet"]),
                    0,
                )

            self.assertEqual(manifest.read_text(), existing)
            load.assert_called_once_with(repo.resolve())
            self.assertTrue((repo / "PROJECT_DIGEST.md").exists())

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
            legacy_script = ROOT / "hologram.py"
            hook.write_text(
                "#!/bin/sh\n"
                "echo before\n"
                + shlex.join([
                    sys.executable, str(legacy_script), "build",
                    "--root", str(repo.resolve()),
                    "--lang", "java", "--quiet",
                ])
                + " || true\n"
                + "echo after\n"
            )

            run_cli(["init", "--root", str(repo), "--quiet"])

            content = hook.read_text()
            self.assertEqual(len(_hologram_hook_lines(content)), 1)
            self.assertNotIn("hologram.py", content)
            self.assertIn("echo before", content)
            self.assertIn("echo after", content)

    def test_init_preserves_non_owned_hologram_commands_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _make_repo(tmp_path)
            other_root = tmp_path / "different repo"
            other_root.mkdir()
            unrelated_script = tmp_path / "unrelated" / "hologram.py"
            other_repo_line = (
                shlex.join([
                    sys.executable, "-m", "hologram", "build",
                    "--root", str(other_root.resolve()), "--quiet",
                ])
                + " || true\n"
            )
            unrelated_script_line = (
                shlex.join([
                    sys.executable, str(unrelated_script), "build",
                    "--root", str(repo.resolve()), "--quiet",
                ])
                + " || true\n"
            )
            unknown_option_line = (
                shlex.join([
                    sys.executable, "-m", "hologram", "build",
                    "--root", str(repo.resolve()), "--private", "--quiet",
                ])
                + " || true\n"
            )
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_bytes(
                ("#!/bin/sh\n"
                 + other_repo_line
                 + unrelated_script_line
                 + unknown_option_line
                 + "echo untouched\n").encode()
            )

            run_cli(["init", "--root", str(repo), "--quiet"])

            updated = hook.read_bytes()
            self.assertIn(other_repo_line.encode(), updated)
            self.assertIn(unrelated_script_line.encode(), updated)
            self.assertIn(unknown_option_line.encode(), updated)
            self.assertIn(b"echo untouched\n", updated)

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
    def test_cli_returns_three_for_missing_parser_without_writes(self):
        class MissingJavaRegistry:
            def has_parser(self, language):
                return language is not hologram.Language.JAVA

            def parser_for(self, language):
                del language

            def versions(self):
                return {language.value: "test" for language in hologram.Language}

        registry = MissingJavaRegistry()

        def extract_without_java(root, sources):
            return parser_api.extract_project(root, sources, registry=registry)

        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            write_manifest(repo)
            out = Path(tmp) / "d.md"
            stderr = io.StringIO()
            with mock.patch(
                "hologram.pipeline.extract_project",
                side_effect=extract_without_java,
            ), contextlib.redirect_stderr(stderr):
                code = run_cli(
                    [
                        "build",
                        "--root",
                        str(repo),
                        "--out",
                        str(out),
                        "--quiet",
                    ]
                )
            self.assertFalse(out.exists())
        self.assertEqual(code, 3)
        self.assertIn("missing-parser", stderr.getvalue())

    def test_module_entrypoint_returns_three_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            write_manifest(repo)
            (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            out = Path(tmp) / "d.md"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "hologram",
                    "build",
                    "--root",
                    str(repo),
                    "--out",
                    str(out),
                    "--quiet",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse(out.exists())
            self.assertIn("python-syntax-error", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


class HookPythonSelectionTest(unittest.TestCase):
    def test_hook_uses_current_python(self):
        from hologram import _hook_python

        self.assertEqual(_hook_python(), sys.executable)


if __name__ == "__main__":
    unittest.main()
