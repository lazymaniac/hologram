from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmark.schema import (
    BenchmarkCorpus,
    Challenge,
    Config,
    Task,
    load_tasks,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TieredManifestTest(unittest.TestCase):
    def _data(self, root: Path) -> dict[str, object]:
        corpus = root / "corpus"
        corpus.mkdir(exist_ok=True)
        patch = root / "challenge.patch"
        patch.write_bytes(b"diff --git a/a b/a\n")
        return {
            "corpus": {
                "name": "example",
                "visibility": "public",
                "url": "https://example.com/owner/example.git",
                "revision": "a" * 40,
                "path_env": "HOLOGRAM_BENCH_EXAMPLE",
                "bootstrap_cmd": "make deps",
                "workspace_assets": ["deps"],
            },
            "tasks": [
                {
                    "id": "simple-implementation",
                    "tier": "simple",
                    "capability": "implementation",
                    "kind": "reuse",
                    "visibility": "public",
                    "prompt": "Implement the bounded change.",
                    "accept_cmd": "verify-reuse {ws}",
                    "expect_reuse": ["extract_range"],
                },
                {
                    "id": "complex-audit",
                    "tier": "complex",
                    "capability": "audit",
                    "kind": "navigate",
                    "visibility": "public",
                    "prompt": "Audit the challenged implementation.",
                    "accept_cmd": "verify-audit {ws} {answer}",
                    "challenge": {
                        "patch": "challenge.patch",
                        "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                    },
                },
            ],
            "model": "claude-sonnet-5",
            "claude_code_version": "2.1.224",
            "max_turns": 40,
            "conditions": ["B", "C"],
            "reps": 1,
            "seed": 20260809,
        }

    def _write(self, root: Path, data: dict[str, object]) -> Path:
        path = root / "tasks.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _load(self, root: Path, data: dict[str, object] | None = None) -> Config:
        selected = self._data(root) if data is None else data
        return load_tasks(
            self._write(root, selected),
            environ={"HOLOGRAM_BENCH_EXAMPLE": str(root / "corpus")},
        )

    def test_records_are_frozen_with_exact_field_order(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(BenchmarkCorpus)),
            (
                "name",
                "visibility",
                "url",
                "revision",
                "path_env",
                "bootstrap_cmd",
                "workspace_assets",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(Challenge)),
            ("patch", "sha256"),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(Task)),
            (
                "id",
                "tier",
                "capability",
                "kind",
                "visibility",
                "prompt",
                "accept_cmd",
                "expect_reuse",
                "challenge",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(Config)),
            (
                "corpus",
                "tasks",
                "model",
                "claude_code_version",
                "max_turns",
                "conditions",
                "reps",
                "seed",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.model = "other"  # type: ignore[misc]

    def test_loads_strict_manifest_and_resolves_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._load(root)
            self.assertEqual(config.model, "claude-sonnet-5")
            self.assertEqual(config.claude_code_version, "2.1.224")
            self.assertEqual(config.conditions, ("B", "C"))
            self.assertEqual(config.reps, 1)
            self.assertEqual(config.seed, 20260809)
            self.assertIsInstance(config.tasks, tuple)
            self.assertEqual(config.tasks[0].expect_reuse, ("extract_range",))
            challenge = config.tasks[1].challenge
            self.assertIsNotNone(challenge)
            assert challenge is not None
            self.assertEqual(challenge.patch, (root / "challenge.patch").resolve())

    def test_unknown_fields_are_rejected_at_every_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mutations = (
                lambda data: data.__setitem__("unknown", True),
                lambda data: data["corpus"].__setitem__("unknown", True),  # type: ignore[union-attr]
                lambda data: data["tasks"][0].__setitem__("unknown", True),  # type: ignore[index,union-attr]
                lambda data: data["tasks"][1]["challenge"].__setitem__(  # type: ignore[index,union-attr]
                    "unknown", True
                ),
            )
            for mutate in mutations:
                with self.subTest(mutate=mutate):
                    data = self._data(root)
                    mutate(data)
                    with self.assertRaisesRegex(ValueError, "unknown"):
                        self._load(root, data)

    def test_ids_tiers_visibility_and_capability_kind_pairs_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases: tuple[tuple[str, object], ...] = (
                ("unsafe id", "../escape"),
                ("duplicate id", "simple-implementation"),
                ("missing tier", "simple"),
                ("mixed visibility", "private"),
                ("implementation navigate", "navigate"),
                ("audit reuse", "reuse"),
            )
            for label, value in cases:
                with self.subTest(label=label):
                    data = self._data(root)
                    tasks = data["tasks"]  # type: ignore[assignment]
                    if label == "unsafe id":
                        tasks[0]["id"] = value  # type: ignore[index]
                    elif label == "duplicate id":
                        tasks[1]["id"] = value  # type: ignore[index]
                    elif label == "missing tier":
                        tasks[1]["tier"] = value  # type: ignore[index]
                    elif label == "mixed visibility":
                        tasks[1]["visibility"] = value  # type: ignore[index]
                    elif label == "implementation navigate":
                        tasks[0]["kind"] = value  # type: ignore[index]
                    else:
                        tasks[1]["kind"] = value  # type: ignore[index]
                    with self.assertRaises(ValueError):
                        self._load(root, data)

    def test_prompts_verifiers_placeholders_and_reuse_are_nonvacuous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases: tuple[tuple[int, str, object], ...] = (
                (0, "prompt", "   "),
                (0, "accept_cmd", "true"),
                (0, "accept_cmd", "verify-reuse"),
                (0, "expect_reuse", []),
                (1, "accept_cmd", "verify-audit {ws}"),
                (1, "accept_cmd", "verify-audit {answer}"),
            )
            for index, field, value in cases:
                with self.subTest(index=index, field=field, value=value):
                    data = self._data(root)
                    data["tasks"][index][field] = value  # type: ignore[index]
                    with self.assertRaises(ValueError):
                        self._load(root, data)

    def test_reproducibility_fields_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {
                "model": (
                    "sonnet",
                    "opus",
                    "haiku",
                    "default",
                    "latest",
                    "claude-sonnet-5-latest",
                ),
                "claude_code_version": ("2.1.223", "latest"),
                "max_turns": (0, 39, 41, True),
                "conditions": (["B"], ["C", "B"], ["B", "C", "B"]),
                "reps": (0, 2, True),
                "seed": (-1, True, "20260809"),
            }
            for field, values in cases.items():
                for value in values:
                    with self.subTest(field=field, value=value):
                        data = self._data(root)
                        data[field] = value
                        with self.assertRaises(ValueError):
                            self._load(root, data)

    def test_corpus_path_comes_only_from_override_or_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write(root, self._data(root))
            with self.assertRaisesRegex(ValueError, "HOLOGRAM_BENCH_EXAMPLE"):
                load_tasks(path, environ={})

            override = root / "override"
            override.mkdir()
            config = load_tasks(path, corpus_override=override, environ={})
            self.assertEqual(config.corpus.name, "example")

            with self.assertRaises(ValueError):
                load_tasks(path, corpus_override=root / "missing", environ={})

    def test_active_task_manifests_use_the_pinned_model(self) -> None:
        active = PROJECT_ROOT / "benchmark" / "tasks"
        manifests = sorted(active.glob("*.json"))
        for manifest in manifests:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data.get("model"), "claude-sonnet-5", manifest)


if __name__ == "__main__":
    unittest.main()
