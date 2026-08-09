import dataclasses
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
import hologram.config as config_module  # noqa: E402
from hologram.config import (  # noqa: E402
    ALLOWED_AGENTS,
    CONFIG_NAME,
    CONFIG_SCHEMA_VERSION,
    ConfigError,
    ProjectConfig,
    canonical_config_bytes,
    default_config,
    load_config,
    render_config,
)
from hologram.model import Language  # noqa: E402


VALID = """schema_version = 2
agents = ["claude", "codex", "gemini"]
languages = ["java", "python", "typescript"]
include = ["src/**", "tests/**"]
exclude = ["**/generated/**"]
hot_threshold = 10
output = "PROJECT_DIGEST.md"
"""


class ConfigTest(unittest.TestCase):
    def _write(self, root: Path, text: str, name: str = CONFIG_NAME) -> Path:
        path = root / name
        path.write_text(text, encoding="utf-8")
        return path

    def _assert_error(self, root: Path, text: str, field: str) -> ConfigError:
        path = self._write(root, text)
        with self.assertRaises(ConfigError) as caught:
            load_config(root)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn(field, str(caught.exception))
        return caught.exception

    def test_loads_complete_manifest_with_exact_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, VALID)

            config = load_config(root)

        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.agents, ("claude", "codex", "gemini"))
        self.assertEqual(
            config.languages,
            (Language.JAVA, Language.PYTHON, Language.TYPESCRIPT),
        )
        self.assertEqual(config.include, ("src/**", "tests/**"))
        self.assertEqual(config.exclude, ("**/generated/**",))
        self.assertEqual(config.hot_threshold, 10)
        self.assertEqual(config.output, "PROJECT_DIGEST.md")

    def test_missing_manifest_raises_with_selected_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(
                ConfigError,
                rf"missing .*{re.escape(CONFIG_NAME)}",
            ) as caught:
                load_config(root)
        self.assertIn(str(root / CONFIG_NAME), str(caught.exception))

    def test_minimal_present_manifest_uses_present_file_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, 'schema_version = 2\nagents = ["claude"]\n')

            config = load_config(root)

        self.assertEqual(config.agents, ("claude",))
        self.assertEqual(config.languages, ())
        self.assertEqual(config.include, ("**/*",))
        self.assertTrue(config.exclude)
        self.assertIsNone(config.output)

    def test_rejects_unknown_key_and_missing_schema_version(self):
        cases = [
            ('schema_version = 2\nextra = true\n', "extra"),
            ('agents = ["claude"]\n', "schema_version"),
        ]
        for text, field in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                self._assert_error(Path(tmp), text, field)

    def test_rejects_invalid_required_values_and_paths(self):
        cases = [
            ('schema_version = 1\n', "schema_version"),
            ('schema_version = 2\nhot_threshold = true\n', "hot_threshold"),
            ('schema_version = 2\nhot_threshold = 0\n', "hot_threshold"),
            ('schema_version = 2\nlanguages = ["brainfuck"]\n', "languages"),
            ('schema_version = 2\nagents = ["cursor"]\n', "agents"),
            ('schema_version = 2\noutput = "../escape.md"\n', "output"),
            ('schema_version = 2\noutput = "CLAUDE.md"\n', "output"),
            ('schema_version = 2\ninclude = ["/absolute/**"]\n', "include"),
        ]
        for text, field in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                self._assert_error(Path(tmp), text, field)

    def test_empty_agents_requires_standalone_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                'schema_version = 2\nagents = []\noutput = "digest.md"\n',
            )
            config = load_config(root)
            self.assertEqual(config.agents, ())
            self.assertEqual(config.output, "digest.md")

        with tempfile.TemporaryDirectory() as tmp:
            self._assert_error(
                Path(tmp),
                "schema_version = 2\nagents = []\n",
                "agents",
            )

    def test_default_render_round_trips_and_is_idempotent(self):
        original = default_config()
        rendered = render_config(original)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, rendered)
            loaded = load_config(root)

        self.assertEqual(loaded, original)
        self.assertEqual(render_config(loaded), rendered)
        self.assertTrue(rendered.endswith("\n"))
        self.assertFalse(rendered.endswith("\n\n"))

    def test_canonical_bytes_ignore_agent_and_language_order(self):
        first = """schema_version = 2
agents = ["gemini", "claude", "codex"]
languages = ["typescript", "java", "python"]
output = "digest.md"
"""
        second = """schema_version = 2
agents = ["codex", "gemini", "claude"]
languages = ["python", "typescript", "java"]
output = "digest.md"
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = self._write(root, first, "first.toml")
            second_path = self._write(root, second, "second.toml")
            one = load_config(root, first_path)
            two = load_config(root, second_path)

        self.assertEqual(canonical_config_bytes(one), canonical_config_bytes(two))
        self.assertEqual(canonical_config_bytes(one), render_config(one).encode("utf-8"))

    def test_rejects_duplicate_sequence_values(self):
        cases = [
            ('agents = ["claude", "claude"]', "agents"),
            ('languages = ["python", "python"]', "languages"),
            ('include = ["src/**", "src/**"]', "include"),
            ('exclude = ["dist/**", "dist/**"]', "exclude"),
        ]
        for assignment, field in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                self._assert_error(
                    Path(tmp),
                    f"schema_version = 2\n{assignment}\n",
                    field,
                )

    def test_rejects_malformed_toml_and_wrong_field_types(self):
        cases = [
            ("schema_version = [2\n", "TOML"),
            ('schema_version = "2"\n', "schema_version"),
            ('schema_version = 2\nagents = "claude"\n', "agents"),
            ('schema_version = 2\nagents = [1]\n', "agents"),
            ('schema_version = 2\nlanguages = "python"\n', "languages"),
            ('schema_version = 2\nlanguages = [1]\n', "languages"),
            ('schema_version = 2\ninclude = "src/**"\n', "include"),
            ('schema_version = 2\ninclude = [1]\n', "include"),
            ('schema_version = 2\nexclude = "dist/**"\n', "exclude"),
            ('schema_version = 2\nexclude = [1]\n', "exclude"),
            ('schema_version = 2\nhot_threshold = 1.5\n', "hot_threshold"),
            ('schema_version = 2\noutput = 3\n', "output"),
        ]
        for text, field in cases:
            with self.subTest(field=field, text=text), tempfile.TemporaryDirectory() as tmp:
                self._assert_error(Path(tmp), text, field)

    def test_wraps_toml_integer_conversion_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write(
                root,
                "schema_version = " + ("9" * 5001) + "\n",
            )

            with self.assertRaises(ConfigError) as caught:
                load_config(root)

        self.assertIn(str(path), str(caught.exception))
        self.assertIn("TOML", str(caught.exception))

    def test_rejects_empty_include_and_non_normalized_patterns(self):
        cases = [
            ('include = []', "include"),
            ('include = [""]', "include"),
            ('include = ["src\\\\**"]', "include"),
            ('include = ["src/../tests/**"]', "include"),
            ('include = ["src/./**"]', "include"),
            ('include = ["D:escape/**"]', "include"),
            ('exclude = [""]', "exclude"),
            ('exclude = ["build\\\\**"]', "exclude"),
            ('exclude = ["build/../dist/**"]', "exclude"),
            ('exclude = ["D:generated/**"]', "exclude"),
        ]
        for assignment, field in cases:
            with self.subTest(assignment=assignment), tempfile.TemporaryDirectory() as tmp:
                self._assert_error(
                    Path(tmp),
                    f"schema_version = 2\n{assignment}\n",
                    field,
                )

    def test_allows_explicit_empty_exclude(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "schema_version = 2\nexclude = []\n")
            self.assertEqual(load_config(root).exclude, ())

    def test_rejects_reserved_or_invalid_outputs(self):
        invalid = [
            ".hologram.toml",
            "CLAUDE.md",
            "AGENTS.md",
            "GEMINI.md",
            "digest.txt",
            "/digest.md",
            "nested/../digest.md",
            "nested\\digest.md",
            "nested/./digest.md",
            "D:escape.md",
            "claude.md",
            "Agents.MD",
            ".HOLOGRAM.TOML",
        ]
        for output in invalid:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                text = f"schema_version = 2\noutput = {json.dumps(output)}\n"
                self._assert_error(Path(tmp), text, "output")

    def test_preserves_case_for_nonreserved_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                'schema_version = 2\noutput = "Reports/ProjectDigest.md"\n',
            )
            self.assertEqual(
                load_config(root).output,
                "Reports/ProjectDigest.md",
            )

    def test_atomic_default_manifest_creation_is_exact_and_non_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_open = os.open
            with mock.patch.object(
                config_module.os,
                "open",
                wraps=real_open,
            ) as opened:
                self.assertTrue(config_module.create_default_manifest(root))

            manifest = root / CONFIG_NAME
            self.assertEqual(
                manifest.read_bytes(),
                canonical_config_bytes(default_config()),
            )
            path, flags, mode = opened.call_args.args
            self.assertEqual(Path(path), manifest)
            self.assertTrue(flags & os.O_WRONLY)
            self.assertTrue(flags & os.O_CREAT)
            self.assertTrue(flags & os.O_EXCL)
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertEqual(mode, 0o644)

            manifest.write_bytes(b"caller-owned")
            self.assertFalse(config_module.create_default_manifest(root))
            self.assertEqual(manifest.read_bytes(), b"caller-owned")

    def test_atomic_default_manifest_creation_loses_file_and_symlink_races(self):
        for raced_kind in ("file", "symlink"):
            with self.subTest(raced_kind=raced_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = root / CONFIG_NAME
                target = root / "target.toml"
                target.write_bytes(b"target-owned")
                real_open = os.open

                def racing_open(path, flags, mode):
                    self.assertEqual(Path(path), manifest)
                    self.assertTrue(flags & os.O_CREAT)
                    self.assertTrue(flags & os.O_EXCL)
                    self.assertEqual(mode, 0o644)
                    if raced_kind == "file":
                        manifest.write_bytes(b"raced-file")
                    else:
                        manifest.symlink_to(target.name)
                    return real_open(path, flags, mode)

                with mock.patch.object(
                    config_module.os,
                    "open",
                    side_effect=racing_open,
                ):
                    self.assertFalse(config_module.create_default_manifest(root))

                if raced_kind == "file":
                    self.assertEqual(manifest.read_bytes(), b"raced-file")
                else:
                    self.assertTrue(manifest.is_symlink())
                    self.assertEqual(manifest.readlink(), Path(target.name))
                self.assertEqual(target.read_bytes(), b"target-owned")

    def test_atomic_default_manifest_leaves_partial_file_and_closes_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / CONFIG_NAME
            real_write = os.write
            captured_fd: list[int] = []

            def failing_write(fd, data):
                captured_fd.append(fd)
                if len(captured_fd) == 1:
                    return real_write(fd, bytes(data[:5]))
                raise OSError("injected write failure")

            with mock.patch.object(
                config_module.os,
                "write",
                side_effect=failing_write,
            ):
                with self.assertRaisesRegex(OSError, "injected write failure"):
                    config_module.create_default_manifest(root)

            self.assertEqual(
                manifest.read_bytes(),
                canonical_config_bytes(default_config())[:5],
            )
            with self.assertRaises(OSError):
                os.fstat(captured_fd[0])

    def test_atomic_default_manifest_preserves_primary_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = OSError("write-primary")
            secondary = OSError("close-secondary")
            with (
                mock.patch.object(
                    config_module.os,
                    "write",
                    side_effect=primary,
                ),
                mock.patch.object(
                    config_module.os,
                    "close",
                    side_effect=secondary,
                ) as close,
            ):
                with self.assertRaises(OSError) as caught:
                    config_module.create_default_manifest(root)

            close.assert_called_once()
            self.assertIs(caught.exception, primary)
            self.assertEqual(str(caught.exception), "write-primary")
            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any("close-secondary" in note for note in notes)
                or caught.exception.__context__ is secondary
            )

    def test_atomic_default_manifest_never_unlinks_raced_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / CONFIG_NAME
            replacement = b"competitor-owned"
            real_lstat = type(manifest).lstat
            real_close = os.close
            replacement_installed = False

            def install_replacement() -> None:
                nonlocal replacement_installed
                manifest.unlink()
                manifest.write_bytes(replacement)
                replacement_installed = True

            def racing_lstat(path):
                owned_stat = real_lstat(path)
                install_replacement()
                return owned_stat

            def racing_close(fd):
                if not replacement_installed:
                    install_replacement()
                return real_close(fd)

            with (
                mock.patch.object(
                    config_module.os,
                    "write",
                    side_effect=OSError("injected write failure"),
                ),
                mock.patch.object(
                    type(manifest),
                    "lstat",
                    autospec=True,
                    side_effect=racing_lstat,
                ),
                mock.patch.object(
                    config_module.os,
                    "close",
                    side_effect=racing_close,
                ),
            ):
                with self.assertRaisesRegex(OSError, "injected write failure"):
                    config_module.create_default_manifest(root)

            self.assertEqual(manifest.read_bytes(), replacement)

    def test_project_config_owns_caller_lists(self):
        agents = ["claude"]
        languages = [Language.PYTHON]
        include = ["src/**"]
        exclude = ["dist/**"]
        config = ProjectConfig(
            schema_version=2,
            agents=agents,
            languages=languages,
            include=include,
            exclude=exclude,
            hot_threshold=10,
            output="digest.md",
        )

        agents.append("codex")
        languages.append(Language.JAVA)
        include.append("tests/**")
        exclude.clear()

        self.assertEqual(config.agents, ("claude",))
        self.assertEqual(config.languages, (Language.PYTHON,))
        self.assertEqual(config.include, ("src/**",))
        self.assertEqual(config.exclude, ("dist/**",))

    def test_project_config_rejects_ambiguous_sequence_containers(self):
        base = {
            "schema_version": 2,
            "agents": ("claude",),
            "languages": (Language.PYTHON,),
            "include": ("src/**",),
            "exclude": (),
            "hot_threshold": 10,
            "output": "digest.md",
        }
        factories = (
            lambda: "not-a-sequence-container",
            lambda: {"value"},
            lambda: frozenset({"value"}),
            lambda: {"key": "value"},
            lambda: iter(("value",)),
        )
        for field in ("agents", "languages", "include", "exclude"):
            for factory in factories:
                with self.subTest(field=field, container=type(factory()).__name__):
                    values = dict(base)
                    values[field] = factory()
                    with self.assertRaisesRegex(TypeError, field):
                        ProjectConfig(**values)

    def test_render_escapes_toml_strings_deterministically(self):
        config = ProjectConfig(
            schema_version=2,
            agents=("claude",),
            languages=(Language.PYTHON,),
            include=('src/"quoted"/\x7f/😀/**',),
            exclude=(),
            hot_threshold=3,
            output='reports/"digest"-\x7f-😀.md',
        )
        rendered = render_config(config)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, rendered)
            self.assertEqual(load_config(root), config)
        self.assertEqual(render_config(config), rendered)

    def test_default_constants_shape_and_package_exports(self):
        expected_exclude = (
            "**/.git/**",
            "**/.venv/**",
            "**/__pycache__/**",
            "**/bin/**",
            "**/build/**",
            "**/dist/**",
            "**/generated/**",
            "**/node_modules/**",
            "**/obj/**",
            "**/out/**",
            "**/target/**",
            "**/vendor/**",
        )
        expected = ProjectConfig(
            2,
            ("claude", "codex", "gemini"),
            (),
            ("**/*",),
            expected_exclude,
            10,
            "PROJECT_DIGEST.md",
        )
        self.assertEqual(CONFIG_NAME, ".hologram.toml")
        self.assertEqual(CONFIG_SCHEMA_VERSION, 2)
        self.assertEqual(ALLOWED_AGENTS, frozenset({"claude", "codex", "gemini"}))
        self.assertEqual(default_config(), expected)
        self.assertEqual(dataclasses.fields(ProjectConfig)[0].name, "schema_version")
        for name in (
            "CONFIG_NAME",
            "CONFIG_SCHEMA_VERSION",
            "ConfigError",
            "ProjectConfig",
            "load_config",
            "default_config",
            "render_config",
            "canonical_config_bytes",
        ):
            self.assertIs(getattr(hologram, name), globals()[name])


if __name__ == "__main__":
    unittest.main()
