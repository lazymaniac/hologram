from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from hologram.model import Language, SymbolKind, Visibility
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
    Exclusion,
    GoldFact,
    GoldSample,
    load_jsonl,
    write_jsonl,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "validation" / "corpora.toml"
CENSUS_PATH = PROJECT_ROOT / "validation" / "gold" / "census.jsonl"
SAMPLE_PATH = PROJECT_ROOT / "validation" / "gold" / "sample.jsonl"
FACTS_PATH = PROJECT_ROOT / "validation" / "gold" / "facts"
EXCLUSIONS_PATH = PROJECT_ROOT / "validation" / "gold" / "exclusions"
GOLD_README_PATH = PROJECT_ROOT / "validation" / "gold" / "README.md"
ADVERTISED_ROOT = PROJECT_ROOT / "validation" / "fixtures" / "advertised"
SYNTHETIC_REVISION = "0" * 40
SYNTHETIC_FACT_SHA256 = (
    "d0869f47a383456f3a3553552b86274890cfd5da08962c3c99209b21d2f62e7b"
)
SYNTHETIC_EXCLUSION_SHA256 = (
    "33d0ee2790a2a99537893a9fcd21e73f031c5aa673d3484966f34af617d4bc3c"
)
REVISION = "a" * 40

ADVERTISED_FIXTURES = (
    "c/types.c",
    "c/types.h",
    "cpp/types.cpp",
    "cpp/types.hpp",
    "csharp/Calls.cs",
    "csharp/Types.cs",
    "go/calls.go",
    "go/types.go",
    "helm/templates/_helpers.tpl",
    "helm/values.yaml",
    "html/page.html",
    "java/Calls.java",
    "java/Types.java",
    "javascript/calls.js",
    "javascript/types.js",
    "jsx/Calls.jsx",
    "jsx/Component.jsx",
    "kotlin/Calls.kt",
    "kotlin/Types.kt",
    "lua/calls.lua",
    "lua/types.lua",
    "python/calls.py",
    "python/types.py",
    "rust/calls.rs",
    "rust/types.rs",
    "svelte/Calls.svelte",
    "svelte/Component.svelte",
    "tsx/Calls.tsx",
    "tsx/Component.tsx",
    "typescript/calls.ts",
    "typescript/types.ts",
    "vue/Calls.vue",
    "vue/Component.vue",
)

PUBLIC_CORPORA = (
    "codecompanion",
    "cypress",
    "hologram",
    "jdb",
    "kafka-streams-examples",
)
CORE_FACT_CATEGORIES = frozenset(
    {"declaration", "kind", "container", "visibility", "signature"}
)
GOLD_CATEGORIES = frozenset(
    {
        "declaration",
        "kind",
        "container",
        "visibility",
        "signature",
        "relation",
        "call",
        "call_order",
        "strong_x0",
        "zero_classification",
        "approximate",
    }
)
CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION.value, SymbolKind.METHOD.value, SymbolKind.CONSTRUCTOR.value}
)
EXCLUSION_REASONS = frozenset(
    {
        "ambiguous_call_target",
        "ambiguous_declaration_identity",
        "ambiguous_relation_target",
        "discarded_callable_owner",
        "external_call_target",
        "external_relation_target",
        "ordinary_yaml_not_helm",
        "reexport_only_no_supported_declaration",
        "shadowed_callable_declaration",
        "unresolved_dynamic_target",
    }
)


def thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def parse_symbol_id(value: object) -> list[Any]:
    decoded = json.loads(value) if isinstance(value, str) else thaw_json(value)
    if not isinstance(decoded, list) or len(decoded) != 6:
        raise ValueError("SymbolId must be a six-item JSON array")
    language, file, container, kind, name, signature_key = decoded
    if language not in {item.value for item in Language}:
        raise ValueError("SymbolId language is not canonical")
    if not isinstance(file, str) or not file:
        raise ValueError("SymbolId file must be nonblank")
    if not isinstance(container, list) or any(
        not isinstance(item, str) or not item for item in container
    ):
        raise ValueError("SymbolId container must contain nonblank strings")
    if kind not in {item.value for item in SymbolKind}:
        raise ValueError("SymbolId kind is not canonical")
    if not isinstance(name, str) or not name:
        raise ValueError("SymbolId name must be nonblank")
    if not isinstance(signature_key, str):
        raise TypeError("SymbolId signature key must be a string")
    return decoded


def expected_fact_id(fact: GoldFact) -> str:
    digest = hashlib.sha256(
        canonical_json(
            {
                "expected": fact.expected,
                "subject": fact.subject,
                "value": fact.value,
            }
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{fact.corpus}:{fact.path}:{fact.line}:{fact.category}:{digest}"


def expected_exclusion_id(exclusion: Exclusion) -> str:
    digest = hashlib.sha256(
        canonical_json({"reason": exclusion.reason, "scope": exclusion.scope}).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    line = exclusion.line if exclusion.line is not None else 0
    return f"{exclusion.corpus}:{exclusion.path}:{line}:exclusion:{digest}"


def source_role(path: str) -> str:
    pure = Path(path)
    directories = tuple(part.casefold() for part in pure.parts[:-1])
    if any(part in {"test", "tests", "spec", "specs"} for part in directories):
        return "test"
    stem = pure.stem.casefold()
    if (
        stem.startswith("test_")
        or stem.endswith(("_test", ".test", ".spec"))
        or pure.stem.endswith(("Test", "Tests"))
    ):
        return "test"
    if "generated" in directories:
        return "generated"
    return "production"


def parse_exclusion_scope(scope: str) -> Any:
    if scope == "file":
        return scope
    try:
        decoded = json.loads(scope)
    except json.JSONDecodeError as error:
        raise ValueError("exclusion scope must be file or canonical JSON") from error
    if canonical_json(decoded) != scope:
        raise ValueError("structured exclusion scope must be canonical JSON")
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("structured exclusion scope must be a nonempty array")
    tag = decoded[0]
    if tag == "fact" and len(decoded) == 4:
        if decoded[1] not in GOLD_CATEGORIES:
            raise ValueError("fact exclusion category is invalid")
        parse_symbol_id(decoded[2])
        if not isinstance(decoded[3], dict):
            raise ValueError("fact exclusion value must be an object")
        return decoded
    if tag == "category" and len(decoded) == 3:
        if decoded[1] not in GOLD_CATEGORIES:
            raise ValueError("category exclusion category is invalid")
        parse_symbol_id(decoded[2])
        return decoded
    if tag == "source_call" and len(decoded) == 4:
        parse_symbol_id(decoded[1])
        if (
            isinstance(decoded[2], bool)
            or not isinstance(decoded[2], int)
            or decoded[2] < 0
            or not isinstance(decoded[3], str)
            or not decoded[3]
        ):
            raise ValueError("source_call exclusion payload is invalid")
        return decoded
    if tag == "candidate" and len(decoded) == 3:
        if any(not isinstance(item, str) or not item for item in decoded[1:]):
            raise ValueError("candidate exclusion payload is invalid")
        return decoded
    raise ValueError("unknown structured exclusion scope")


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


class ValidationGoldCoverageTest(unittest.TestCase):
    registry: ClassVar[CorpusRegistry]
    census: ClassVar[tuple[CensusRecord, ...]]
    sample: ClassVar[tuple[GoldSample, ...]]
    census_by_key: ClassVar[dict[tuple[str, str], CensusRecord]]
    sample_by_key: ClassVar[dict[tuple[str, str], GoldSample]]
    facts_by_corpus: ClassVar[dict[str, tuple[GoldFact, ...]]]
    exclusions_by_corpus: ClassVar[dict[str, tuple[Exclusion, ...]]]
    facts: ClassVar[tuple[GoldFact, ...]]
    exclusions: ClassVar[tuple[Exclusion, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry(REGISTRY_PATH)
        cls.census = load_jsonl(CENSUS_PATH, CensusRecord)
        cls.sample = load_jsonl(SAMPLE_PATH, GoldSample)
        cls.census_by_key = {(row.corpus, row.path): row for row in cls.census}
        cls.sample_by_key = {(row.corpus, row.path): row for row in cls.sample}
        cls.facts_by_corpus = {
            corpus: load_jsonl(FACTS_PATH / f"{corpus}.jsonl", GoldFact)
            for corpus in PUBLIC_CORPORA
        }
        cls.exclusions_by_corpus = {
            corpus: load_jsonl(
                EXCLUSIONS_PATH / f"{corpus}.jsonl",
                Exclusion,
            )
            for corpus in PUBLIC_CORPORA
        }
        cls.facts = tuple(
            fact for corpus in PUBLIC_CORPORA for fact in cls.facts_by_corpus[corpus]
        )
        cls.exclusions = tuple(
            exclusion
            for corpus in PUBLIC_CORPORA
            for exclusion in cls.exclusions_by_corpus[corpus]
        )

    def test_public_gold_files_and_readme_are_present(self) -> None:
        self.assertEqual(
            {path.stem for path in FACTS_PATH.glob("*.jsonl")} & set(PUBLIC_CORPORA),
            set(PUBLIC_CORPORA),
        )
        self.assertEqual(
            {path.stem for path in EXCLUSIONS_PATH.glob("*.jsonl")}
            & set(PUBLIC_CORPORA),
            set(PUBLIC_CORPORA),
        )
        text = GOLD_README_PATH.read_text(encoding="utf-8")
        normalized_text = " ".join(text.split())
        for required in (
            "pinned source",
            "direct syntax",
            "lexical order",
            "Dynamic or ambiguous calls",
            "complete applicable non-call relation set",
            "Generated/vendor",
            "second reviewer",
            "score pressure is not evidence",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized_text)

    def test_reviewed_public_gold_counts_hashes_and_bytes_are_frozen(self) -> None:
        expected = {
            ("facts", "codecompanion"): (
                1771,
                "5b01babaf788702ea42fa9f069e71bade456df7976477a1d9d3911322f23f4a6",
            ),
            ("facts", "cypress"): (
                1219,
                "9b202c6b7e857bf3013a3328837877d64eabc342eacb592a8cdf70798dfe5132",
            ),
            ("facts", "hologram"): (
                3380,
                "34c65c804474ca0ed800ea099b830092c9424c8e2ec13bb8f6f59e0023b4fbb3",
            ),
            ("facts", "jdb"): (
                204,
                "50684c242fe3449de135dc85d9439f0cf74537646de38f41f09ebb5d5ea8f71e",
            ),
            ("facts", "kafka-streams-examples"): (
                1804,
                "8d0d5a176e7adecbded88d9d68874fcbe349103b778d4225a3d409de4e3eb6b7",
            ),
            ("exclusions", "codecompanion"): (
                917,
                "4dc3c86cffc9097e201e4fce4c858a9ce79c27917db2fdc5e1f3d4da0149d0c1",
            ),
            ("exclusions", "cypress"): (
                246,
                "566484c626eda4bad930c8977868cbb8445e6b73ddb7ebf899cc86067d50d953",
            ),
            ("exclusions", "hologram"): (
                1569,
                "e39bae0a13dbc28ebd2b2f594ad0a553b04e77ce5ccee7d769ff6a2f0d81aab3",
            ),
            ("exclusions", "jdb"): (
                65,
                "efa207f8634fad189c06f38b2d27dd7f148d0b3efbd5eaa1b9e6e23e55d6d587",
            ),
            ("exclusions", "kafka-streams-examples"): (
                1272,
                "f3e580be9e42a4454c2b4c917ddfd0383b2933b551ea86b7a07fe991253b9890",
            ),
        }
        roots = {"facts": FACTS_PATH, "exclusions": EXCLUSIONS_PATH}
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            for key, (expected_count, expected_hash) in expected.items():
                kind, corpus = key
                source = roots[kind] / f"{corpus}.jsonl"
                raw = source.read_bytes()
                records: tuple[object, ...]
                if kind == "facts":
                    records = load_jsonl(source, GoldFact)
                else:
                    records = load_jsonl(source, Exclusion)
                with self.subTest(kind=kind, corpus=corpus):
                    self.assertEqual(len(records), expected_count)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_hash)
                    target = temporary / f"{kind}-{corpus}.jsonl"
                    write_jsonl(target, records)
                    self.assertEqual(target.read_bytes(), raw)

    def test_fact_metadata_ids_subjects_and_value_shapes_are_canonical(self) -> None:
        census_paths = {
            corpus: {row.path: row for row in self.census if row.corpus == corpus}
            for corpus in PUBLIC_CORPORA
        }
        for fact in self.facts:
            with self.subTest(fact=fact.id):
                self.assertIn(fact.corpus, PUBLIC_CORPORA)
                self.assertIn((fact.corpus, fact.path), self.sample_by_key)
                sample = self.sample_by_key[(fact.corpus, fact.path)]
                self.assertEqual(fact.revision, sample.revision)
                self.assertEqual(fact.language, sample.language)
                self.assertEqual(fact.id, expected_fact_id(fact))

                subject = parse_symbol_id(fact.subject)
                self.assertEqual(
                    fact.subject,
                    json.dumps(subject, ensure_ascii=False, separators=(",", ":")),
                )
                self.assertEqual(subject[0], fact.language)
                self.assertEqual(subject[1], fact.path)
                value = thaw_json(fact.value)

                if fact.category == "declaration":
                    self.assertEqual(value, {"name": subject[4]})
                elif fact.category == "kind":
                    self.assertEqual(value, {"kind": subject[3]})
                elif fact.category == "container":
                    self.assertEqual(value, {"container": subject[2]})
                elif fact.category == "visibility":
                    self.assertEqual(set(value), {"visibility"})
                    self.assertIn(
                        value["visibility"],
                        {item.value for item in Visibility},
                    )
                elif fact.category == "signature":
                    self.assertEqual(
                        set(value),
                        {"text", "params", "returns", "raises"},
                    )
                    self.assertIsInstance(value["text"], str)
                    self.assertIsInstance(value["params"], list)
                    self.assertTrue(
                        all(isinstance(item, str) for item in value["params"])
                    )
                    self.assertTrue(
                        value["returns"] is None or isinstance(value["returns"], str)
                    )
                    self.assertIsInstance(value["raises"], list)
                    self.assertTrue(
                        all(isinstance(item, str) for item in value["raises"])
                    )
                    expected_key = (
                        f"({','.join(value['params'])})"
                        if subject[3] in CALLABLE_KINDS
                        else ""
                    )
                    self.assertEqual(subject[5], expected_key)
                elif fact.category == "relation":
                    self.assertEqual(set(value), {"kind", "target"})
                    self.assertIn(
                        value["kind"],
                        {"super", "permit", "component", "reexport", "dependency"},
                    )
                    target = value["target"]
                    self.assertIsInstance(target, dict)
                    if value["kind"] == "dependency":
                        self.assertEqual(set(target), {"external"})
                        self.assertIsInstance(target["external"], str)
                        self.assertTrue(target["external"])
                    else:
                        self.assertEqual(set(target), {"symbol"})
                        target_id = parse_symbol_id(target["symbol"])
                        target_row = census_paths[fact.corpus].get(target_id[1])
                        self.assertIsNotNone(target_row)
                        assert target_row is not None
                        self.assertEqual(target_id[0], target_row.language)
                elif fact.category == "call":
                    self.assertEqual(set(value), {"target", "ordinal"})
                    target_id = parse_symbol_id(value["target"])
                    target_row = census_paths[fact.corpus].get(target_id[1])
                    self.assertIsNotNone(target_row)
                    assert target_row is not None
                    self.assertEqual(target_id[0], target_row.language)
                    self.assertIs(type(value["ordinal"]), int)
                    self.assertGreaterEqual(value["ordinal"], 0)
                elif fact.category == "call_order":
                    self.assertEqual(set(value), {"targets"})
                    self.assertIsInstance(value["targets"], list)
                    for target in value["targets"]:
                        target_id = parse_symbol_id(target)
                        target_row = census_paths[fact.corpus].get(target_id[1])
                        self.assertIsNotNone(target_row)
                        assert target_row is not None
                        self.assertEqual(target_id[0], target_row.language)
                elif fact.category == "strong_x0":
                    self.assertEqual(value, {"classification": "strong"})
                else:
                    self.fail(
                        f"public Task 3 fact uses synthetic-only category "
                        f"{fact.category!r}"
                    )

                if fact.category != "strong_x0":
                    self.assertTrue(fact.expected)

    def test_exclusion_ids_metadata_and_scopes_are_canonical(self) -> None:
        seen_scopes: set[tuple[str, str, int | None, str]] = set()
        for exclusion in self.exclusions:
            with self.subTest(exclusion=exclusion.id):
                self.assertIn(exclusion.corpus, PUBLIC_CORPORA)
                census = self.census_by_key.get((exclusion.corpus, exclusion.path))
                self.assertIsNotNone(census)
                assert census is not None
                self.assertEqual(exclusion.revision, census.revision)
                self.assertEqual(exclusion.language, census.language)
                self.assertEqual(exclusion.id, expected_exclusion_id(exclusion))
                scope = parse_exclusion_scope(exclusion.scope)
                self.assertTrue(
                    exclusion.reason in EXCLUSION_REASONS
                    or exclusion.reason.startswith("runtime_reachability_ambiguous:")
                )
                if scope == "file":
                    self.assertIsNone(exclusion.line)
                else:
                    self.assertIsNotNone(exclusion.line)
                    assert isinstance(scope, list)
                    subject = (
                        scope[2]
                        if scope[0] in {"fact", "category"}
                        else (scope[1] if scope[0] == "source_call" else None)
                    )
                    if subject is not None:
                        owner = parse_symbol_id(subject)
                        self.assertEqual(owner[0], exclusion.language)
                        self.assertEqual(owner[1], exclusion.path)
                        if scope[0] == "source_call":
                            self.assertIn(owner[3], CALLABLE_KINDS)
                scope_key = (
                    exclusion.corpus,
                    exclusion.path,
                    exclusion.line,
                    exclusion.scope,
                )
                self.assertNotIn(scope_key, seen_scopes)
                seen_scopes.add(scope_key)

    def test_every_sample_file_is_covered_and_core_bundles_are_complete(self) -> None:
        declaration_facts = tuple(
            fact
            for fact in self.facts
            if fact.category == "declaration" and fact.expected
        )
        declarations = {(fact.corpus, fact.subject): fact for fact in declaration_facts}
        self.assertEqual(len(declarations), len(declaration_facts))

        file_exclusions = {
            (item.corpus, item.path) for item in self.exclusions if item.scope == "file"
        }
        covered = {
            (item.corpus, item.path) for item in declaration_facts
        } | file_exclusions
        self.assertEqual(set(self.sample_by_key), covered & set(self.sample_by_key))

        facts_by_subject: dict[tuple[str, str], list[GoldFact]] = {}
        for fact in self.facts:
            facts_by_subject.setdefault((fact.corpus, fact.subject), []).append(fact)
        for key, declaration in declarations.items():
            with self.subTest(corpus=key[0], subject=key[1]):
                grouped = facts_by_subject[key]
                core = [
                    fact for fact in grouped if fact.category in CORE_FACT_CATEGORIES
                ]
                self.assertEqual(
                    Counter(fact.category for fact in core),
                    Counter({category: 1 for category in CORE_FACT_CATEGORIES}),
                )
                self.assertTrue(all(fact.expected for fact in core))
                self.assertEqual({fact.line for fact in core}, {declaration.line})

        for fact in self.facts:
            with self.subTest(subject=fact.subject, category=fact.category):
                self.assertIn((fact.corpus, fact.subject), declarations)
                self.assertEqual(
                    fact.line,
                    declarations[(fact.corpus, fact.subject)].line,
                )

    def test_every_callable_has_one_complete_lexical_call_list(self) -> None:
        facts_by_subject: dict[tuple[str, str], list[GoldFact]] = {}
        for fact in self.facts:
            facts_by_subject.setdefault((fact.corpus, fact.subject), []).append(fact)

        callable_subjects = {
            (fact.corpus, fact.subject)
            for fact in self.facts
            if fact.category == "kind"
            and thaw_json(fact.value)["kind"] in CALLABLE_KINDS
        }
        call_subjects = {
            (fact.corpus, fact.subject)
            for fact in self.facts
            if fact.category in {"call", "call_order"}
        }
        self.assertEqual(call_subjects, callable_subjects)
        for key in callable_subjects:
            with self.subTest(corpus=key[0], subject=key[1]):
                owned = facts_by_subject[key]
                order = [fact for fact in owned if fact.category == "call_order"]
                self.assertEqual(len(order), 1)
                calls = sorted(
                    (fact for fact in owned if fact.category == "call"),
                    key=lambda fact: thaw_json(fact.value)["ordinal"],
                )
                self.assertEqual(
                    [thaw_json(fact.value)["ordinal"] for fact in calls],
                    list(range(len(calls))),
                )
                self.assertEqual(
                    thaw_json(order[0].value)["targets"],
                    [thaw_json(fact.value)["target"] for fact in calls],
                )
                declaration_line = next(
                    fact.line for fact in owned if fact.category == "declaration"
                )
                self.assertEqual(order[0].line, declaration_line)
                self.assertTrue(all(fact.line == declaration_line for fact in calls))

    def test_every_policy_strong_candidate_has_a_decision_or_exact_exclusion(
        self,
    ) -> None:
        category_exclusions = {
            (exclusion.corpus, canonical_json(scope[2]))
            for exclusion in self.exclusions
            if isinstance((scope := parse_exclusion_scope(exclusion.scope)), list)
            and scope[0] == "category"
            and scope[1] == "strong_x0"
        }
        by_subject: dict[tuple[str, str], dict[str, GoldFact]] = {}
        strong_counts: Counter[tuple[str, str]] = Counter()
        for fact in self.facts:
            by_subject.setdefault((fact.corpus, fact.subject), {})[fact.category] = fact
            if fact.category == "strong_x0":
                strong_counts[(fact.corpus, fact.subject)] += 1

        self.assertTrue(all(count == 1 for count in strong_counts.values()))

        for key, categories in by_subject.items():
            kind = thaw_json(categories["kind"].value)["kind"]
            visibility = thaw_json(categories["visibility"].value)["visibility"]
            declaration = categories["declaration"]
            eligible = (
                source_role(declaration.path) == "production"
                and visibility
                not in {Visibility.PUBLIC.value, Visibility.PROTECTED.value}
                and kind != SymbolKind.REEXPORT.value
            )
            if eligible:
                with self.subTest(corpus=key[0], subject=key[1]):
                    self.assertTrue(
                        "strong_x0" in categories
                        or (key[0], canonical_json(parse_symbol_id(key[1])))
                        in category_exclusions
                    )
            else:
                self.assertNotIn("strong_x0", categories)

    def test_scoring_exclusions_do_not_overlap_explicit_facts(self) -> None:
        fact_keys = {
            (
                fact.corpus,
                fact.path,
                fact.category,
                fact.subject,
                canonical_json(fact.value),
            )
            for fact in self.facts
        }
        category_keys = {
            (fact.corpus, fact.path, fact.category, fact.subject) for fact in self.facts
        }
        for exclusion in self.exclusions:
            scope = parse_exclusion_scope(exclusion.scope)
            if not isinstance(scope, list):
                continue
            if scope[0] == "fact":
                fact_key = (
                    exclusion.corpus,
                    exclusion.path,
                    scope[1],
                    json.dumps(scope[2], ensure_ascii=False, separators=(",", ":")),
                    canonical_json(scope[3]),
                )
                self.assertNotIn(fact_key, fact_keys)
            elif scope[0] == "category":
                category_key = (
                    exclusion.corpus,
                    exclusion.path,
                    scope[1],
                    json.dumps(scope[2], ensure_ascii=False, separators=(",", ":")),
                )
                self.assertNotIn(category_key, category_keys)

    def test_exact_ordinary_yaml_exclusions_and_outside_extensions(self) -> None:
        ordinary = {
            (item.corpus, item.path, item.line, item.scope, item.reason)
            for item in self.exclusions
            if item.reason == "ordinary_yaml_not_helm"
        }
        self.assertEqual(
            ordinary,
            {
                ("cypress", "codecov.yml", None, "file", "ordinary_yaml_not_helm"),
                (
                    "kafka-streams-examples",
                    "docker-compose.yml",
                    None,
                    "file",
                    "ordinary_yaml_not_helm",
                ),
                (
                    "kafka-streams-examples",
                    "service.yml",
                    None,
                    "file",
                    "ordinary_yaml_not_helm",
                ),
            },
        )
        outside = set(self.registry.outside_candidate_extensions)
        self.assertFalse(
            any(Path(row.path).suffix in outside for row in self.census)
            or any(Path(row.path).suffix in outside for row in self.sample)
        )

    def test_pinned_source_anchors_when_checkouts_are_available(self) -> None:
        configured = {spec.name: spec for spec in self.registry.corpora}
        present = {
            spec.name: os.environ.get(spec.path_env)
            for spec in self.registry.corpora
            if os.environ.get(spec.path_env)
        }
        if not present:
            self.skipTest(
                "set all HOLOGRAM_VALIDATION_* paths to verify source anchors"
            )
        self.assertEqual(set(present), set(configured))
        roots = {
            name: resolve_checkout(configured[name], os.environ) for name in configured
        }
        for name, root in roots.items():
            verify_checkout(configured[name], root)

        lines_by_file: dict[tuple[str, str], list[str]] = {}
        for fact in self.facts:
            key = (fact.corpus, fact.path)
            lines = lines_by_file.setdefault(
                key,
                (roots[fact.corpus] / fact.path)
                .read_text(encoding="utf-8")
                .splitlines(),
            )
            self.assertLessEqual(fact.line, len(lines))
            subject = parse_symbol_id(fact.subject)
            path_module = Path(fact.path).with_suffix("").as_posix()
            python_module = path_module.replace("/", ".").removesuffix(".__init__")
            implicit_module = subject[3] == SymbolKind.MODULE.value and (
                (fact.language == Language.PYTHON.value and subject[4] == python_module)
                or (
                    fact.language
                    in {
                        Language.TYPESCRIPT.value,
                        Language.JAVASCRIPT.value,
                        Language.TSX.value,
                    }
                    and subject[4] == path_module
                )
            )
            if not implicit_module:
                self.assertIn(subject[4], lines[fact.line - 1])

        for exclusion in self.exclusions:
            if exclusion.line is None:
                continue
            key = (exclusion.corpus, exclusion.path)
            lines = lines_by_file.setdefault(
                key,
                (roots[exclusion.corpus] / exclusion.path)
                .read_text(encoding="utf-8")
                .splitlines(),
            )
            self.assertLessEqual(exclusion.line, len(lines))


class SyntheticFixtureMatrixTest(unittest.TestCase):
    facts: ClassVar[tuple[GoldFact, ...]]
    exclusions: ClassVar[tuple[Exclusion, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.facts = load_jsonl(FACTS_PATH / "synthetic.jsonl", GoldFact)
        cls.exclusions = load_jsonl(
            EXCLUSIONS_PATH / "synthetic.jsonl",
            Exclusion,
        )

    def test_exact_advertised_fixture_matrix(self) -> None:
        actual = tuple(
            sorted(
                path.relative_to(ADVERTISED_ROOT).as_posix()
                for path in ADVERTISED_ROOT.rglob("*")
                if path.is_file()
            )
        )
        self.assertEqual(actual, ADVERTISED_FIXTURES)
        self.assertTrue(
            all(
                len(
                    (ADVERTISED_ROOT / relative)
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                < 40
                for relative in actual
            )
        )

    def test_synthetic_truth_bytes_are_frozen(self) -> None:
        facts = (FACTS_PATH / "synthetic.jsonl").read_bytes()
        exclusions = (EXCLUSIONS_PATH / "synthetic.jsonl").read_bytes()
        self.assertEqual(len(self.facts), 942)
        self.assertEqual(len(self.exclusions), 4)
        self.assertEqual(hashlib.sha256(facts).hexdigest(), SYNTHETIC_FACT_SHA256)
        self.assertEqual(
            hashlib.sha256(exclusions).hexdigest(),
            SYNTHETIC_EXCLUSION_SHA256,
        )

    def test_synthetic_metadata_ids_and_source_anchors_are_exact(self) -> None:
        for fact in self.facts:
            with self.subTest(fact=fact.id):
                self.assertEqual(fact.corpus, "synthetic")
                self.assertEqual(fact.revision, SYNTHETIC_REVISION)
                self.assertIn(fact.path, ADVERTISED_FIXTURES)
                self.assertEqual(fact.id, expected_fact_id(fact))
                subject = parse_symbol_id(fact.subject)
                self.assertEqual(subject[0], fact.language)
                self.assertEqual(subject[1], fact.path)
                lines = (
                    (ADVERTISED_ROOT / fact.path)
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                self.assertLessEqual(fact.line, len(lines))
                implicit = (
                    subject[3] == SymbolKind.MODULE.value
                    or fact.language in {Language.HTML.value, Language.HELM.value}
                    or (
                        fact.language in {Language.VUE.value, Language.SVELTE.value}
                        and subject[3] == SymbolKind.CLASS.value
                        and subject[4] == Path(fact.path).stem
                    )
                )
                if not implicit:
                    self.assertIn(subject[4], lines[fact.line - 1])
        for exclusion in self.exclusions:
            with self.subTest(exclusion=exclusion.id):
                self.assertEqual(exclusion.corpus, "synthetic")
                self.assertEqual(exclusion.revision, SYNTHETIC_REVISION)
                self.assertIn(exclusion.path, ADVERTISED_FIXTURES)
                self.assertEqual(exclusion.id, expected_exclusion_id(exclusion))
                parse_exclusion_scope(exclusion.scope)

    def test_all_languages_syntax_modes_and_relations_are_planted(self) -> None:
        declarations = [fact for fact in self.facts if fact.category == "declaration"]
        self.assertEqual(
            {fact.language for fact in declarations}, {item.value for item in Language}
        )
        self.assertTrue(
            any(
                fact.language == Language.TSX.value and fact.path.endswith(".jsx")
                for fact in declarations
            )
        )
        self.assertEqual(
            {path.parts[0] for path in (Path(fact.path) for fact in declarations)},
            {Path(path).parts[0] for path in ADVERTISED_FIXTURES},
        )
        relation_languages = {
            fact.language for fact in self.facts if fact.category == "relation"
        }
        self.assertTrue(
            {
                "java",
                "python",
                "typescript",
                "javascript",
                "tsx",
                "kotlin",
                "go",
                "rust",
                "csharp",
                "cpp",
                "vue",
                "svelte",
            }.issubset(relation_languages)
        )

    def test_ordered_calls_are_repeated_and_lexical(self) -> None:
        orders = {
            (fact.language, fact.subject): thaw_json(fact.value)["targets"]
            for fact in self.facts
            if fact.category == "call_order"
        }
        for language in ("java", "python", "typescript", "tsx", "lua"):
            matching = [
                targets
                for (candidate, _subject), targets in orders.items()
                if candidate == language
                and len(targets) >= 3
                and targets[0] == targets[2]
                and targets[0] != targets[1]
            ]
            with self.subTest(language=language):
                self.assertTrue(matching)

    def test_planted_advisory_and_duplicate_cases_are_exact(self) -> None:
        by_name: dict[str, list[GoldFact]] = {}
        for fact in self.facts:
            name = str(parse_symbol_id(fact.subject)[4])
            by_name.setdefault(name, []).append(fact)

        zero = {
            name: thaw_json(
                next(
                    fact for fact in facts if fact.category == "zero_classification"
                ).value
            )["classification"]
            for name, facts in by_name.items()
            if any(fact.category == "zero_classification" for fact in facts)
        }
        self.assertEqual(zero["GoldUnusedStrong"], "strong")
        self.assertEqual(zero["GoldPublicSurface"], "uncertain")
        self.assertEqual(zero["GoldDynamicCallback"], "uncertain")
        self.assertEqual(zero["GoldSameFileUsed"], "none")
        self.assertEqual(zero["GoldStringOnlyStrong"], "strong")

        strong = {
            name: fact.expected
            for name, facts in by_name.items()
            for fact in facts
            if fact.category == "strong_x0"
        }
        self.assertTrue(strong["GoldUnusedStrong"])
        self.assertFalse(strong["GoldDynamicCallback"])
        self.assertTrue(strong["GoldStringOnlyStrong"])

        approximate = [fact for fact in self.facts if fact.category == "approximate"]
        self.assertTrue(any(fact.expected for fact in approximate))
        self.assertTrue(any(not fact.expected for fact in approximate))
        positive_names = {
            str(parse_symbol_id(fact.subject)[4])
            for fact in approximate
            if fact.expected
        }
        negative_names = {
            str(parse_symbol_id(fact.subject)[4])
            for fact in approximate
            if not fact.expected
        }
        self.assertIn("goldExactCloneA", positive_names)
        self.assertIn("goldSimilarNegativeA", negative_names)

    def test_zero_classification_is_closed_for_every_declaration(self) -> None:
        declarations = {
            fact.subject for fact in self.facts if fact.category == "declaration"
        }
        zeros = [fact for fact in self.facts if fact.category == "zero_classification"]
        self.assertEqual({fact.subject for fact in zeros}, declarations)
        self.assertEqual(len(zeros), len(declarations))
        self.assertTrue(
            all(
                thaw_json(fact.value)["classification"]
                in {"none", "strong", "uncertain"}
                for fact in zeros
            )
        )

    def test_synthetic_bundles_calls_relations_and_advisories_are_closed(
        self,
    ) -> None:
        by_subject: dict[str, list[GoldFact]] = {}
        for fact in self.facts:
            by_subject.setdefault(fact.subject, []).append(fact)
        declarations = {
            fact.subject: fact for fact in self.facts if fact.category == "declaration"
        }
        self.assertEqual(len(declarations), 132)

        callable_subjects: set[str] = set()
        strong_candidates: set[str] = set()
        for subject, declaration in declarations.items():
            with self.subTest(subject=subject):
                grouped = by_subject[subject]
                core = [
                    fact for fact in grouped if fact.category in CORE_FACT_CATEGORIES
                ]
                self.assertEqual(
                    Counter(fact.category for fact in core),
                    Counter({category: 1 for category in CORE_FACT_CATEGORIES}),
                )
                self.assertTrue(all(fact.expected for fact in core))
                self.assertTrue(all(fact.line == declaration.line for fact in grouped))
                kind = thaw_json(
                    next(fact for fact in grouped if fact.category == "kind").value
                )["kind"]
                visibility = thaw_json(
                    next(
                        fact for fact in grouped if fact.category == "visibility"
                    ).value
                )["visibility"]
                if kind in CALLABLE_KINDS:
                    callable_subjects.add(subject)
                if (
                    visibility
                    in {
                        Visibility.INTERNAL.value,
                        Visibility.PRIVATE.value,
                    }
                    and kind != SymbolKind.REEXPORT.value
                ):
                    strong_candidates.add(subject)

        call_subjects = {
            fact.subject
            for fact in self.facts
            if fact.category in {"call", "call_order"}
        }
        self.assertEqual(call_subjects, callable_subjects)
        for subject in callable_subjects:
            grouped = by_subject[subject]
            order = [fact for fact in grouped if fact.category == "call_order"]
            self.assertEqual(len(order), 1)
            calls = sorted(
                (fact for fact in grouped if fact.category == "call"),
                key=lambda fact: thaw_json(fact.value)["ordinal"],
            )
            self.assertEqual(
                [thaw_json(fact.value)["ordinal"] for fact in calls],
                list(range(len(calls))),
            )
            targets = thaw_json(order[0].value)["targets"]
            self.assertEqual(
                targets,
                [thaw_json(fact.value)["target"] for fact in calls],
            )
            self.assertTrue(
                all(canonical_json(target) in declarations for target in targets)
            )

        for relation in (fact for fact in self.facts if fact.category == "relation"):
            value = thaw_json(relation.value)
            self.assertIn(value["kind"], {"component", "super"})
            self.assertIn(canonical_json(value["target"]["symbol"]), declarations)
            self.assertTrue(relation.expected)

        strong = [fact for fact in self.facts if fact.category == "strong_x0"]
        self.assertEqual({fact.subject for fact in strong}, strong_candidates)
        for fact in strong:
            zero = thaw_json(
                next(
                    candidate
                    for candidate in by_subject[fact.subject]
                    if candidate.category == "zero_classification"
                ).value
            )["classification"]
            self.assertEqual(fact.expected, zero == "strong")
            self.assertEqual(thaw_json(fact.value), {"classification": "strong"})

        approximate = [fact for fact in self.facts if fact.category == "approximate"]
        self.assertEqual(len(approximate), 2)
        for fact in approximate:
            peer = canonical_json(thaw_json(fact.value)["peer"])
            self.assertIn(peer, declarations)
            self.assertLess(parse_symbol_id(fact.subject)[4], parse_symbol_id(peer)[4])

        for exclusion in self.exclusions:
            self.assertIn(exclusion.reason, EXCLUSION_REASONS)
            self.assertIsNotNone(exclusion.line)
            scope = parse_exclusion_scope(exclusion.scope)
            self.assertIn(scope[0], {"candidate", "source_call"})
            if scope[0] == "source_call":
                self.assertIn(canonical_json(scope[1]), declarations)


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
