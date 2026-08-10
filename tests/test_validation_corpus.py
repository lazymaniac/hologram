from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from validation import corpus as corpus_module
from validation.corpus import (
    build_census,
    load_registry,
    resolve_checkout,
    select_gold_sample,
    verify_checkout,
)
from validation.schema import (
    CensusRecord,
    CorpusRegistry,
    CorpusSpec,
    GoldSample,
    load_jsonl,
    write_jsonl,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "validation" / "corpora.toml"
CENSUS_PATH = PROJECT_ROOT / "validation" / "gold" / "census.jsonl"
SAMPLE_PATH = PROJECT_ROOT / "validation" / "gold" / "sample.jsonl"
REVISION = "a" * 40


def spec(
    *,
    name: str = "sample",
    url: str = "https://github.com/Example/Corpus.git",
    revision: str = REVISION,
    path_env: str = "HOLOGRAM_VALIDATION_SAMPLE",
    sample_files: int = 2,
) -> CorpusSpec:
    return CorpusSpec(name, url, revision, path_env, sample_files)


def registry(*corpora: CorpusSpec) -> CorpusRegistry:
    return CorpusRegistry(
        corpora=corpora or (spec(),),
        expected_census_files=0,
        expected_ordinary_yaml_exclusions=0,
        outside_candidate_extensions=(".scala", ".sh"),
    )


def census(
    path: str,
    *,
    corpus: str = "sample",
    revision: str = REVISION,
    language: str = "python",
) -> CensusRecord:
    return CensusRecord(corpus, revision, path, language)


class GitRepository:
    def __init__(self, root: Path, remote: str) -> None:
        self.root = root
        self.run("init", "-q")
        self.run("config", "user.email", "validation@example.test")
        self.run("config", "user.name", "Validation Test")
        self.run("remote", "add", "origin", remote)
        (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.run("add", "tracked.py")
        self.run("commit", "-qm", "fixture")

    def run(self, *arguments: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(self.root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    @property
    def revision(self) -> str:
        return self.run("rev-parse", "HEAD")


class RegistryTest(unittest.TestCase):
    def test_frozen_registry_has_exact_reviewed_public_pins_and_policy(self) -> None:
        loaded = load_registry(REGISTRY_PATH)

        self.assertEqual(loaded.expected_census_files, 748)
        self.assertEqual(loaded.expected_ordinary_yaml_exclusions, 3)
        self.assertEqual(loaded.outside_candidate_extensions, (".scala", ".sh"))
        self.assertEqual(
            tuple(
                (
                    row.name,
                    row.url,
                    row.revision,
                    row.path_env,
                    row.sample_files,
                )
                for row in loaded.corpora
            ),
            (
                (
                    "hologram",
                    "https://github.com/lazymaniac/hologram.git",
                    "6604cfac743466f56bf4b7b4ea68ce6dae3c4d18",
                    "HOLOGRAM_VALIDATION_HOLOGRAM",
                    9,
                ),
                (
                    "codecompanion",
                    "https://github.com/olimorris/codecompanion.nvim.git",
                    "2b959b2bf5fdb13e3b333c078ba549996e477b7c",
                    "HOLOGRAM_VALIDATION_CODECOMPANION",
                    24,
                ),
                (
                    "cypress",
                    "https://github.com/cypress-io/cypress-realworld-app.git",
                    "c2d37e6ff38232a386525265e8ef6e3c6a4d62a9",
                    "HOLOGRAM_VALIDATION_CYPRESS",
                    38,
                ),
                (
                    "kafka-streams-examples",
                    "https://github.com/confluentinc/kafka-streams-examples.git",
                    "9df6d342cc754926673d2ed6c41952616f3ad879",
                    "HOLOGRAM_VALIDATION_KAFKA_STREAMS_EXAMPLES",
                    26,
                ),
                (
                    "jdb",
                    "https://github.com/brunoborges/jdb-agentic-debugger.git",
                    "213939fcb92ccb910ff1d93a4a1a07631b34b779",
                    "HOLOGRAM_VALIDATION_JDB",
                    6,
                ),
            ),
        )

    def test_registry_loader_is_strict_and_reports_the_manifest(self) -> None:
        documents = {
            "top level: unknown field": """
corpora = []
surprise = true

[census]
expected_files = 0
expected_ordinary_yaml_exclusions = 0
outside_candidate_extensions = []
""",
            "census: unknown field": """
corpora = []

[census]
expected_files = 0
expected_ordinary_yaml_exclusions = 0
outside_candidate_extensions = []
surprise = true
""",
            "corpora[1]: unknown field": """
[census]
expected_files = 0
expected_ordinary_yaml_exclusions = 0
outside_candidate_extensions = []

[[corpora]]
name = "sample"
url = "https://github.com/example/sample.git"
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
path_env = "HOLOGRAM_VALIDATION_SAMPLE"
sample_files = 1
surprise = true
""",
            "missing field": """
[census]
expected_files = 0
expected_ordinary_yaml_exclusions = 0
outside_candidate_extensions = []

[[corpora]]
name = "sample"
url = "https://github.com/example/sample.git"
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
path_env = "HOLOGRAM_VALIDATION_SAMPLE"
""",
            "normalized HTTPS": """
[census]
expected_files = 0
expected_ordinary_yaml_exclusions = 0
outside_candidate_extensions = []

[[corpora]]
name = "sample"
url = "git@github.com:example/sample.git"
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
path_env = "HOLOGRAM_VALIDATION_SAMPLE"
sample_files = 1
""",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (message, document) in enumerate(documents.items()):
                path = root / f"bad-{index}.toml"
                path.write_text(document, encoding="utf-8")
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(
                        ValueError,
                        f"{re.escape(str(path))}.*{re.escape(message)}",
                    ),
                ):
                    load_registry(path)

    def test_registry_loader_rejects_malformed_toml_and_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / "malformed.toml"
            malformed.write_text("[census\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, f"{malformed}.*TOML"):
                load_registry(malformed)

            bom = root / "bom.toml"
            bom.write_bytes(b"\xef\xbb\xbf[census]\n")
            with self.assertRaisesRegex(ValueError, f"{bom}.*BOM"):
                load_registry(bom)

    def test_registry_requires_canonical_validation_path_environment_names(
        self,
    ) -> None:
        invalid_names = (
            "ROOT",
            "HOLOGRAM_VALIDATION_",
            "HOLOGRAM_VALIDATION_sample",
            "HOLOGRAM-VALIDATION-SAMPLE",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, path_env in enumerate(invalid_names):
                path = root / f"bad-env-{index}.toml"
                path.write_text(
                    "\n".join(
                        (
                            "[census]",
                            "expected_files = 1",
                            "expected_ordinary_yaml_exclusions = 0",
                            'outside_candidate_extensions = [".sh"]',
                            "",
                            "[[corpora]]",
                            'name = "sample"',
                            'url = "https://github.com/example/sample.git"',
                            f'revision = "{"a" * 40}"',
                            f'path_env = "{path_env}"',
                            "sample_files = 1",
                            "",
                        )
                    ),
                    encoding="utf-8",
                )
                with (
                    self.subTest(path_env=path_env),
                    self.assertRaisesRegex(
                        ValueError,
                        "path_env.*HOLOGRAM_VALIDATION_",
                    ),
                ):
                    load_registry(path)


class CheckoutTest(unittest.TestCase):
    def test_resolve_checkout_requires_a_nonblank_existing_directory(self) -> None:
        selected = spec(path_env="CORPUS_ROOT")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                resolve_checkout(selected, {"CORPUS_ROOT": str(root)}),
                root.resolve(),
            )
            for environ in ({}, {"CORPUS_ROOT": ""}, {"CORPUS_ROOT": "missing"}):
                with (
                    self.subTest(environ=environ),
                    self.assertRaisesRegex(ValueError, "CORPUS_ROOT"),
                ):
                    resolve_checkout(selected, environ)

            with self.assertRaisesRegex(ValueError, "CORPUS_ROOT.*absolute"):
                resolve_checkout(selected, {"CORPUS_ROOT": "validation"})
            with self.assertRaisesRegex(ValueError, "CORPUS_ROOT.*absolute"):
                resolve_checkout(selected, {"CORPUS_ROOT": "~"})

            for internal in (PROJECT_ROOT, PROJECT_ROOT / "validation"):
                with self.assertRaisesRegex(ValueError, "external checkout"):
                    resolve_checkout(selected, {"CORPUS_ROOT": str(internal)})

            for ancestor in (PROJECT_ROOT.parent, Path("/")):
                with self.assertRaisesRegex(ValueError, "external checkout"):
                    resolve_checkout(selected, {"CORPUS_ROOT": str(ancestor)})

            regular = root / "regular"
            regular.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CORPUS_ROOT.*directory"):
                resolve_checkout(selected, {"CORPUS_ROOT": str(regular)})

    def test_verify_checkout_normalizes_ssh_remote_and_requires_full_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(
                root,
                "git@github.com:Example/Corpus.git",
            )
            selected = spec(revision=repository.revision)
            verify_checkout(selected, root)

            wrong_revision = dataclasses.replace(selected, revision="f" * 40)
            with self.assertRaisesRegex(ValueError, "revision.*expected"):
                verify_checkout(wrong_revision, root)

    def test_verify_checkout_rejects_wrong_remote_and_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "ssh://git@github.com/other/repo.git")
            selected = spec(revision=repository.revision)
            with self.assertRaisesRegex(ValueError, "remote.*expected"):
                verify_checkout(selected, root)

        mutations = {
            "unstaged": lambda repository: (repository.root / "tracked.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            ),
            "staged": lambda repository: (
                (repository.root / "tracked.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                ),
                repository.run("add", "tracked.py"),
            ),
            "untracked": lambda repository: (
                repository.root / "untracked.txt"
            ).write_text("dirty\n", encoding="utf-8"),
        }
        for state, mutate in mutations.items():
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repository = GitRepository(root, "https://github.com/Example/Corpus")
                selected = spec(revision=repository.revision)
                mutate(repository)
                with self.assertRaisesRegex(ValueError, "dirty"):
                    verify_checkout(selected, root)

    def test_verify_checkout_rejects_remote_credentials_ports_and_suffix_data(
        self,
    ) -> None:
        remotes = (
            "https://user@github.com/Example/Corpus.git",
            "https://user:password@github.com/Example/Corpus.git",
            "https://github.com:443/Example/Corpus.git",
            "https://github.com/Example/Corpus.git?ref=main",
            "https://github.com/Example/Corpus.git#main",
        )
        for remote in remotes:
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repository = GitRepository(root, remote)
                selected = spec(revision=repository.revision)
                with self.assertRaisesRegex(ValueError, "invalid origin remote"):
                    verify_checkout(selected, root)

    def test_verify_checkout_requires_the_exact_external_git_toplevel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            nested = root / "nested"
            nested.mkdir()
            selected = spec(revision=repository.revision)

            with self.assertRaisesRegex(ValueError, "Git worktree root"):
                verify_checkout(selected, nested)

            relative = Path(os.path.relpath(root, PROJECT_ROOT))
            with self.assertRaisesRegex(ValueError, "absolute"):
                verify_checkout(selected, relative)


class CensusTest(unittest.TestCase):
    def test_census_uses_foundation_scanner_policy_and_is_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            (root / "tracked.py").unlink()
            files = {
                "z.py": "Z = 1\n",
                "a.ts": "export const a = 1\n",
                "codecov.yml": "coverage: 80\n",
                ".hidden.py": "HIDDEN = True\n",
                ".cache/nested.py": "HIDDEN = True\n",
                "fixtures/example.py": "FIXTURE = True\n",
                "node_modules/dependency.ts": "export const dep = 1\n",
                "Model.scala": "object Model {}\n",
                "script.sh": "#!/bin/sh\n",
                "notes.txt": "unsupported\n",
            }
            for relative, content in files.items():
                path = root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            repository.run("add", "-A")
            repository.run("commit", "-qm", "census fixture")

            selected = spec(revision=repository.revision, sample_files=1)
            configured = CorpusRegistry(
                corpora=(selected,),
                expected_census_files=3,
                expected_ordinary_yaml_exclusions=1,
                outside_candidate_extensions=(".scala", ".sh"),
            )
            result = build_census(configured, {"sample": root})

        self.assertEqual(
            tuple((row.path, row.language) for row in result),
            (("a.ts", "typescript"), ("codecov.yml", "helm"), ("z.py", "python")),
        )
        self.assertEqual({row.revision for row in result}, {selected.revision})

    def test_ordinary_yaml_count_excludes_helm_chart_layout_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            (root / "tracked.py").unlink()
            files = {
                "codecov.yml": "coverage: 80\n",
                "chart/Chart.yaml": "apiVersion: v2\nname: sample\n",
                "chart/values.yaml": "replicas: 1\n",
                "chart/templates/service.yaml": "kind: Service\n",
            }
            for relative, content in files.items():
                path = root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            repository.run("add", "-A")
            repository.run("commit", "-qm", "YAML fixture")
            selected = spec(revision=repository.revision, sample_files=1)
            configured = CorpusRegistry(
                corpora=(selected,),
                expected_census_files=4,
                expected_ordinary_yaml_exclusions=1,
                outside_candidate_extensions=(".scala", ".sh"),
            )

            result = build_census(configured, {"sample": root})

        self.assertEqual(len(result), 4)

    def test_census_verifies_clean_checkout_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            selected = spec(revision=repository.revision, sample_files=1)
            (root / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
            configured = CorpusRegistry(
                corpora=(selected,),
                expected_census_files=1,
                expected_ordinary_yaml_exclusions=0,
                outside_candidate_extensions=(".scala", ".sh"),
            )
            with self.assertRaisesRegex(ValueError, "dirty"):
                build_census(configured, {"sample": root})

    def test_census_rejects_relative_and_internal_direct_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            selected = spec(revision=repository.revision, sample_files=1)
            configured = CorpusRegistry(
                corpora=(selected,),
                expected_census_files=1,
                expected_ordinary_yaml_exclusions=0,
                outside_candidate_extensions=(".scala", ".sh"),
            )
            relative = Path(os.path.relpath(root, PROJECT_ROOT))
            with self.assertRaisesRegex(ValueError, "absolute"):
                build_census(configured, {"sample": relative})

        internal = CorpusRegistry(
            corpora=(spec(sample_files=1),),
            expected_census_files=1,
            expected_ordinary_yaml_exclusions=0,
            outside_candidate_extensions=(".scala", ".sh"),
        )
        for path in (PROJECT_ROOT, PROJECT_ROOT.parent):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "external checkout"),
            ):
                build_census(internal, {"sample": path})

    def test_census_rejects_missing_roots_and_count_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing corpus root.*sample"):
            build_census(registry(), {})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            with self.assertRaisesRegex(ValueError, "census.*expected 2.*got 1"):
                build_census(
                    CorpusRegistry(
                        corpora=(spec(revision=repository.revision),),
                        expected_census_files=2,
                        expected_ordinary_yaml_exclusions=0,
                        outside_candidate_extensions=(".scala", ".sh"),
                    ),
                    {"sample": root},
                )

    def test_census_rejects_scanner_failures_instead_of_silently_dropping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = GitRepository(root, "https://github.com/Example/Corpus.git")
            (root / "tracked.py").unlink()
            (root / "invalid.py").write_bytes(b"\xff")
            repository.run("add", "-A")
            repository.run("commit", "-qm", "invalid UTF-8 fixture")
            with self.assertRaisesRegex(ValueError, "scan.*incomplete.*invalid.py"):
                build_census(
                    CorpusRegistry(
                        corpora=(spec(revision=repository.revision),),
                        expected_census_files=1,
                        expected_ordinary_yaml_exclusions=0,
                        outside_candidate_extensions=(".scala", ".sh"),
                    ),
                    {"sample": root},
                )


class SampleSelectionTest(unittest.TestCase):
    def test_selection_uses_only_seed_corpus_path_and_is_input_order_independent(
        self,
    ) -> None:
        records = tuple(census(f"src/{name}.py") for name in "abcdef")
        configured = dataclasses.replace(registry(), expected_census_files=6)

        forward = select_gold_sample(records, configured, seed=17)
        reverse = select_gold_sample(tuple(reversed(records)), configured, seed=17)

        expected = sorted(
            records,
            key=lambda row: (
                hashlib.sha256(f"17\0{row.corpus}\0{row.path}".encode()).hexdigest(),
                row.path,
            ),
        )[:2]
        self.assertEqual(forward, reverse)
        self.assertEqual(
            [(row.corpus, row.path) for row in forward],
            sorted((row.corpus, row.path) for row in expected),
        )
        for row in forward:
            self.assertEqual(
                row.rank,
                hashlib.sha256(f"17\0{row.corpus}\0{row.path}".encode()).hexdigest(),
            )

    def test_selection_rejects_unknown_corpus_revision_drift_and_short_quota(
        self,
    ) -> None:
        cases = (
            ((census("a.py", corpus="unknown"),), "unknown corpus"),
            ((census("a.py", revision="b" * 40),), "revision"),
            ((census("a.py"),), "quota.*2.*only 1"),
        )
        for records, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                select_gold_sample(records, registry())


class FrozenInventoryTest(unittest.TestCase):
    def test_frozen_inventory_bytes_and_language_totals_are_reviewed(self) -> None:
        self.assertEqual(
            hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest(),
            "48c483d60bec1f4bd79918cd1db5dcac19f89a741a1a3207c963d6b905d2e525",
        )
        self.assertEqual(
            hashlib.sha256(CENSUS_PATH.read_bytes()).hexdigest(),
            "c5c52d359348d94eebfaca5b3cca04a4a578ce89599ee5ef7c1119e666bf1bee",
        )
        self.assertEqual(
            hashlib.sha256(SAMPLE_PATH.read_bytes()).hexdigest(),
            "78a51272a602dda71a0b34d1774532b61e65356a496be047d85adb102bd1dab3",
        )
        frozen_census = load_jsonl(CENSUS_PATH, CensusRecord)
        frozen_sample = load_jsonl(SAMPLE_PATH, GoldSample)
        self.assertEqual(
            Counter(row.language for row in frozen_census),
            Counter(
                {
                    "go": 1,
                    "helm": 3,
                    "html": 2,
                    "java": 122,
                    "javascript": 4,
                    "lua": 442,
                    "python": 12,
                    "tsx": 69,
                    "typescript": 93,
                }
            ),
        )
        self.assertEqual(
            Counter(row.language for row in frozen_sample),
            Counter(
                {
                    "java": 32,
                    "lua": 24,
                    "python": 9,
                    "tsx": 10,
                    "typescript": 28,
                }
            ),
        )

    def test_frozen_census_and_sample_have_reviewed_counts_and_membership(self) -> None:
        configured = load_registry(REGISTRY_PATH)
        frozen_census = load_jsonl(CENSUS_PATH, CensusRecord)
        frozen_sample = load_jsonl(SAMPLE_PATH, GoldSample)

        self.assertEqual(len(frozen_census), 748)
        self.assertEqual(len(frozen_sample), 103)
        self.assertEqual(
            Counter(row.corpus for row in frozen_census),
            Counter(
                {
                    "hologram": 9,
                    "codecompanion": 444,
                    "cypress": 169,
                    "kafka-streams-examples": 120,
                    "jdb": 6,
                }
            ),
        )
        self.assertEqual(
            Counter(row.corpus for row in frozen_sample),
            Counter(
                {
                    "hologram": 9,
                    "codecompanion": 24,
                    "cypress": 38,
                    "kafka-streams-examples": 26,
                    "jdb": 6,
                }
            ),
        )
        self.assertEqual(
            {
                (row.corpus, row.path)
                for row in frozen_census
                if Path(row.path).suffix in {".yaml", ".yml"}
            },
            {
                ("cypress", "codecov.yml"),
                ("kafka-streams-examples", "docker-compose.yml"),
                ("kafka-streams-examples", "service.yml"),
            },
        )
        self.assertFalse(
            any(
                Path(row.path).suffix in configured.outside_candidate_extensions
                for row in frozen_census
            )
        )
        census_keys = {(row.corpus, row.path) for row in frozen_census}
        self.assertTrue(
            all((row.corpus, row.path) in census_keys for row in frozen_sample)
        )

    def test_frozen_jsonl_is_canonical_and_round_trips_byte_for_byte(self) -> None:
        for source, record_type in (
            (CENSUS_PATH, CensusRecord),
            (SAMPLE_PATH, GoldSample),
        ):
            records = load_jsonl(source, record_type)
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / source.name
                write_jsonl(target, records)
                self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_frozen_sample_is_the_recomputed_seeded_selection(self) -> None:
        configured = load_registry(REGISTRY_PATH)
        frozen_census = load_jsonl(CENSUS_PATH, CensusRecord)
        frozen_sample = load_jsonl(SAMPLE_PATH, GoldSample)
        self.assertEqual(
            frozen_sample,
            select_gold_sample(frozen_census, configured, seed=20260809),
        )


class FreezeWriteSafetyTest(unittest.TestCase):
    def test_freeze_cli_preflights_outputs_before_resolving_corpora(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "inventory.jsonl"
            with self.assertRaisesRegex(ValueError, "distinct.*non-aliasing"):
                corpus_module.main(
                    (
                        "freeze",
                        "--registry",
                        str(REGISTRY_PATH),
                        "--census",
                        str(target),
                        "--sample",
                        str(target),
                    )
                )

    def test_freeze_preflight_rejects_same_and_aliasing_output_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "inventory.jsonl"
            first.write_text("original\n", encoding="utf-8")
            aliases = (first, root / "." / "inventory.jsonl")
            for alias in aliases:
                with (
                    self.subTest(alias=alias),
                    self.assertRaisesRegex(ValueError, "distinct.*non-aliasing"),
                ):
                    corpus_module._preflight_inventory_targets(first, alias)

            hardlink = root / "hardlink.jsonl"
            os.link(first, hardlink)
            with self.assertRaisesRegex(ValueError, "distinct.*non-aliasing"):
                corpus_module._preflight_inventory_targets(first, hardlink)

    def test_freeze_stages_both_serializations_before_replacing_outputs(self) -> None:
        records = (census("src/a.py"),)
        samples = (GoldSample("sample", REVISION, "src/a.py", "python", "b" * 64),)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            census_path = root / "census.jsonl"
            sample_path = root / "sample.jsonl"
            census_path.write_bytes(b"old census\n")
            sample_path.write_bytes(b"old sample\n")
            real_write = write_jsonl
            writes = 0

            def fail_second_write(path: Path, rows: object) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("second staged write failed")
                real_write(path, rows)  # type: ignore[arg-type]

            with (
                mock.patch.object(
                    corpus_module,
                    "write_jsonl",
                    side_effect=fail_second_write,
                ),
                self.assertRaisesRegex(OSError, "second staged write failed"),
            ):
                corpus_module._write_inventory_pair(
                    census_path,
                    records,
                    sample_path,
                    samples,
                )

            self.assertEqual(census_path.read_bytes(), b"old census\n")
            self.assertEqual(sample_path.read_bytes(), b"old sample\n")
            self.assertEqual(
                {path.name for path in root.iterdir()},
                {"census.jsonl", "sample.jsonl"},
            )

    def test_freeze_rolls_back_first_replacement_if_second_replacement_fails(
        self,
    ) -> None:
        records = (census("src/a.py"),)
        samples = (GoldSample("sample", REVISION, "src/a.py", "python", "b" * 64),)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            census_path = root / "census.jsonl"
            sample_path = root / "sample.jsonl"
            census_path.write_bytes(b"old census\n")
            sample_path.write_bytes(b"old sample\n")
            real_replace = os.replace
            sample_failure = False

            def fail_sample_once(source: Path, target: Path) -> None:
                nonlocal sample_failure
                if Path(target) == sample_path and not sample_failure:
                    sample_failure = True
                    raise OSError("sample replacement failed")
                real_replace(source, target)

            with (
                mock.patch.object(
                    corpus_module.os,
                    "replace",
                    side_effect=fail_sample_once,
                ),
                self.assertRaisesRegex(OSError, "sample replacement failed"),
            ):
                corpus_module._write_inventory_pair(
                    census_path,
                    records,
                    sample_path,
                    samples,
                )

            self.assertEqual(census_path.read_bytes(), b"old census\n")
            self.assertEqual(sample_path.read_bytes(), b"old sample\n")
            self.assertEqual(
                {path.name for path in root.iterdir()},
                {"census.jsonl", "sample.jsonl"},
            )


if __name__ == "__main__":
    unittest.main()
