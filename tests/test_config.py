import dataclasses
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
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

    def test_rejects_empty_include_and_non_normalized_patterns(self):
        cases = [
            ('include = []', "include"),
            ('include = [""]', "include"),
            ('include = ["src\\\\**"]', "include"),
            ('include = ["src/../tests/**"]', "include"),
            ('include = ["src/./**"]', "include"),
            ('exclude = [""]', "exclude"),
            ('exclude = ["build\\\\**"]', "exclude"),
            ('exclude = ["build/../dist/**"]', "exclude"),
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
        ]
        for output in invalid:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                text = f"schema_version = 2\noutput = {json.dumps(output)}\n"
                self._assert_error(Path(tmp), text, "output")

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
