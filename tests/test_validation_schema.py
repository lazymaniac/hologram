from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from validation import schema
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

REVISION = "a" * 40


def census(
    corpus: str = "sample",
    path: str = "src/a.py",
    language: str = "python",
) -> CensusRecord:
    return CensusRecord(
        corpus=corpus,
        revision=REVISION,
        path=path,
        language=language,
    )


def sample(
    corpus: str = "sample",
    path: str = "src/a.py",
    language: str = "python",
) -> GoldSample:
    return GoldSample(
        corpus=corpus,
        revision=REVISION,
        path=path,
        language=language,
        rank="b" * 64,
    )


def fact(
    fact_id: str = "sample:src/a.py:4:declaration:d7e23bcc6f819c56",
    *,
    path: str = "src/a.py",
    line: int = 4,
    value: object | None = None,
) -> GoldFact:
    return GoldFact(
        id=fact_id,
        corpus="sample",
        revision=REVISION,
        path=path,
        line=line,
        language="python",
        category="declaration",
        subject='["python","src/a.py",[],"fn","f","()"]',
        value={"name": "f"} if value is None else value,  # type: ignore[arg-type]
        expected=True,
    )


def exclusion(
    exclusion_id: str = "sample:src/a.py:ordinary-yaml",
    *,
    line: int | None = None,
    reason: str = "This is application configuration, not a Helm template.",
) -> Exclusion:
    return Exclusion(
        id=exclusion_id,
        corpus="sample",
        revision=REVISION,
        path="src/a.py",
        line=line,
        language="python",
        scope="ordinary_yaml_not_helm",
        reason=reason,
    )


class ValidationSchemaTest(unittest.TestCase):
    def test_public_api_and_exact_frozen_record_fields(self):
        expected = {
            "CensusRecord",
            "CorpusRegistry",
            "CorpusSpec",
            "Exclusion",
            "GoldFact",
            "GoldSample",
            "load_jsonl",
            "write_jsonl",
        }
        self.assertEqual(set(schema.__all__), expected)
        expected_fields = {
            CorpusSpec: ("name", "url", "revision", "path_env", "sample_files"),
            CorpusRegistry: (
                "corpora",
                "expected_census_files",
                "expected_ordinary_yaml_exclusions",
                "outside_candidate_extensions",
            ),
            CensusRecord: ("corpus", "revision", "path", "language"),
            GoldSample: ("corpus", "revision", "path", "language", "rank"),
            GoldFact: (
                "id",
                "corpus",
                "revision",
                "path",
                "line",
                "language",
                "category",
                "subject",
                "value",
                "expected",
            ),
            Exclusion: (
                "id",
                "corpus",
                "revision",
                "path",
                "line",
                "language",
                "scope",
                "reason",
            ),
        }
        for record_type, fields in expected_fields.items():
            with self.subTest(record_type=record_type.__name__):
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(record_type)),
                    fields,
                )
                self.assertTrue(record_type.__dataclass_params__.frozen)
                self.assertFalse(hasattr(record_type, "__slots__"))

        row = census()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.path = "src/changed.py"  # type: ignore[misc]

    def test_records_reject_absolute_non_normalized_and_blank_paths(self):
        for invalid in (
            "/absolute/source.py",
            "../source.py",
            "src/../source.py",
            "src\\source.py",
            "src//source.py",
            "./src/source.py",
            "src/\x00source.py",
            "",
            ".",
        ):
            with (
                self.subTest(path=invalid),
                self.assertRaisesRegex(ValueError, "normalized relative POSIX path"),
            ):
                census(path=invalid)

    def test_records_reject_short_uppercase_and_non_string_revisions(self):
        invalid = ("abc123", "A" * 40, "g" * 40, 1, None)
        for revision in invalid:
            with (
                self.subTest(revision=revision),
                self.assertRaisesRegex((TypeError, ValueError), "revision"),
            ):
                CensusRecord(
                    corpus="sample",
                    revision=revision,  # type: ignore[arg-type]
                    path="src/a.py",
                    language="python",
                )

    def test_records_accept_only_canonical_language_names(self):
        canonical = (
            "java",
            "python",
            "typescript",
            "javascript",
            "tsx",
            "vue",
            "svelte",
            "kotlin",
            "go",
            "rust",
            "csharp",
            "c",
            "cpp",
            "lua",
            "html",
            "helm",
        )
        self.assertEqual(
            tuple(census(language=name).language for name in canonical), canonical
        )
        for invalid in ("Python", "py", "js", "c#", "", 7):
            with (
                self.subTest(language=invalid),
                self.assertRaisesRegex((TypeError, ValueError), "language"),
            ):
                census(language=invalid)  # type: ignore[arg-type]

    def test_blank_strings_and_invalid_scalar_types_are_rejected(self):
        constructors = (
            lambda: CorpusSpec(" ", "https://example.test/a.git", REVISION, "ROOT", 1),
            lambda: CorpusSpec("sample", "", REVISION, "ROOT", 1),
            lambda: CorpusSpec("sample", "https://example.test/a.git", REVISION, "", 1),
            lambda: CorpusSpec(
                "sample", "https://example.test/a.git", REVISION, "ROOT", True
            ),
            lambda: CorpusSpec(
                "sample", "https://example.test/a.git", REVISION, "ROOT", 0
            ),
            lambda: CensusRecord("", REVISION, "src/a.py", "python"),
            lambda: GoldSample("sample", REVISION, "src/a.py", "python", ""),
            lambda: GoldFact(
                "",
                "sample",
                REVISION,
                "src/a.py",
                1,
                "python",
                "declaration",
                "subject",
                {},
                True,
            ),
            lambda: GoldFact(
                "id",
                "sample",
                REVISION,
                "src/a.py",
                1,
                "python",
                "unknown",  # type: ignore[arg-type]
                "subject",
                {},
                True,
            ),
            lambda: GoldFact(
                "id",
                "sample",
                REVISION,
                "src/a.py",
                1,
                "python",
                "declaration",
                "",
                {},
                True,
            ),
            lambda: GoldFact(
                "id",
                "sample",
                REVISION,
                "src/a.py",
                1,
                "python",
                "declaration",
                "subject",
                {},
                1,  # type: ignore[arg-type]
            ),
            lambda: Exclusion(
                "id", "sample", REVISION, "src/a.py", None, "python", "", "reason"
            ),
        )
        for constructor in constructors:
            with (
                self.subTest(constructor=constructor),
                self.assertRaises((TypeError, ValueError)),
            ):
                constructor()

    def test_gold_sample_rank_is_a_full_lowercase_sha256_hex(self):
        self.assertEqual(sample().rank, "b" * 64)
        for rank in ("", "b" * 63, "B" * 64, "g" * 64, 4):
            with (
                self.subTest(rank=rank),
                self.assertRaisesRegex((TypeError, ValueError), "rank"),
            ):
                GoldSample(
                    corpus="sample",
                    revision=REVISION,
                    path="src/a.py",
                    language="python",
                    rank=rank,  # type: ignore[arg-type]
                )

    def test_registry_requires_unique_path_envs_and_sorted_extension_tokens(self):
        first = CorpusSpec(
            "a", "https://example.test/a.git", REVISION, "VALIDATION_A", 1
        )
        second = CorpusSpec(
            "b", "https://example.test/b.git", REVISION, "VALIDATION_B", 2
        )
        registry = CorpusRegistry((first, second), 3, 0, (".scala", ".sh"))
        self.assertEqual(registry.corpora, (first, second))

        duplicate_env = dataclasses.replace(second, path_env="VALIDATION_A")
        cases = (
            ((first, duplicate_env), (".scala", ".sh"), "path_env"),
            ((first, second), (".sh", ".scala"), "sorted"),
            ((first, second), (".sh", ".sh"), "unique"),
            ((first, second), ("scala",), "extension tokens"),
            ((first, second), (".Scala",), "extension tokens"),
        )
        for corpora, extensions, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex((TypeError, ValueError), message),
            ):
                CorpusRegistry(corpora, 3, 0, extensions)

    def test_gold_fact_has_stable_source_anchor_and_positive_line(self):
        anchored = fact()
        self.assertEqual(anchored.path, "src/a.py")
        for invalid in (0, -1, True, 1.5, "1"):
            with (
                self.subTest(line=invalid),
                self.assertRaisesRegex((TypeError, ValueError), "line"),
            ):
                fact(line=invalid)  # type: ignore[arg-type]

    def test_exclusion_requires_reason_and_valid_optional_line(self):
        self.assertIsNone(exclusion().line)
        self.assertEqual(exclusion(line=3).line, 3)
        for reason in ("", " \t"):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, "reason"),
            ):
                exclusion(reason=reason)
        for line in (0, -1, True, "2"):
            with (
                self.subTest(line=line),
                self.assertRaisesRegex((TypeError, ValueError), "line"),
            ):
                exclusion(line=line)  # type: ignore[arg-type]

    def test_gold_fact_value_is_recursively_immutable(self):
        mutable = {
            "details": {"names": ["f", {"arity": 0}]},
            "flags": [True, None],
        }
        frozen = fact(value=mutable).value
        mutable["details"]["names"].append("later")  # type: ignore[index,union-attr]

        self.assertIsInstance(frozen, MappingProxyType)
        self.assertIsInstance(frozen["details"], MappingProxyType)
        self.assertEqual(
            frozen["details"]["names"], ("f", MappingProxyType({"arity": 0}))
        )  # type: ignore[index]
        with self.assertRaises(TypeError):
            frozen["new"] = "value"  # type: ignore[index]
        with self.assertRaises(TypeError):
            frozen["details"]["name"] = "changed"  # type: ignore[index]

    def test_gold_fact_value_rejects_non_json_values_and_non_string_keys(self):
        for value in (
            [],
            {1: "value"},
            {"bad": {1, 2}},
            {"bad": b"bytes"},
            {"bad": float("nan")},
            {"bad": float("inf")},
        ):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                fact(value=value)

    def test_jsonl_loader_rejects_unknown_and_missing_fields_with_location(self):
        cases = (
            ('{"id":"x","unknown":true}\n', "unknown field"),
            ('{"id":"x"}\n', "missing field"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.jsonl"
            for contents, message in cases:
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"{path}:1:.*{message}",
                    ):
                        load_jsonl(path, GoldFact)

    def test_jsonl_loader_rejects_malformed_non_object_blank_and_invalid_utf8(self):
        cases = (
            (b'{"id":\n', "malformed JSON"),
            (b"[]\n", "JSON object"),
            (b"\n", "blank line"),
            (b"\xef\xbb\xbf{}\n", "UTF-8 BOM"),
            (b"{\xff}\n", "UTF-8"),
            (b'{"corpus":"a","corpus":"b"}\n', "duplicate JSON object key"),
            (b'{"value":NaN}\n', "non-finite JSON constant"),
            (b'{"value":Infinity}\n', "non-finite JSON constant"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            for contents, message in cases:
                with self.subTest(contents=contents):
                    path.write_bytes(contents)
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"{path}:1:.*{message}",
                    ):
                        load_jsonl(path, CensusRecord)

    def test_jsonl_loader_wraps_record_type_errors_with_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.jsonl"
            for source_path in ("/absolute/a.py", "src/\x00a.py"):
                with self.subTest(source_path=source_path):
                    payload = {
                        "corpus": "sample",
                        "language": "python",
                        "path": source_path,
                        "revision": REVISION,
                    }
                    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"{path}:1:.*normalized relative POSIX path",
                    ):
                        load_jsonl(path, CensusRecord)

    def test_jsonl_rejects_duplicate_and_unsorted_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.jsonl"
            for rows, message, line in (
                ((fact("a"), fact("a")), "duplicate id", 2),
                ((fact("b"), fact("a")), "sorted by id", 2),
            ):
                with self.subTest(message=message):
                    write_jsonl(path, (rows[0],))
                    with path.open("a", encoding="utf-8", newline="") as handle:
                        payload = {
                            field.name: getattr(rows[1], field.name)
                            for field in dataclasses.fields(rows[1])
                        }
                        payload["value"] = {"name": "f"}
                        handle.write(
                            json.dumps(payload, sort_keys=True, separators=(",", ":"))
                            + "\n"
                        )
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"{path}:{line}:.*{message}",
                    ):
                        load_jsonl(path, GoldFact)

    def test_jsonl_rejects_duplicate_and_unsorted_corpus_path_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.jsonl"
            cases = (
                ((census("a", "x.py"), census("a", "x.py")), "duplicate"),
                ((census("b", "x.py"), census("a", "z.py")), "sorted"),
            )
            for rows, message in cases:
                with self.subTest(message=message):
                    payloads = [dataclasses.asdict(row) for row in rows]
                    path.write_text(
                        "".join(
                            json.dumps(row, sort_keys=True, separators=(",", ":"))
                            + "\n"
                            for row in payloads
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"{path}:2:.*{message}.*corpus.*path",
                    ):
                        load_jsonl(path, CensusRecord)

    def test_writer_rejects_mixed_unsorted_and_duplicate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            cases = (
                ((fact("b"), fact("a")), "sorted by id"),
                ((fact("a"), fact("a")), "duplicate id"),
                ((census(), sample()), "same record type"),
            )
            for records, message in cases:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex((TypeError, ValueError), message),
                ):
                    write_jsonl(path, records)

    def test_canonical_writer_and_loader_are_byte_stable(self):
        records = (
            fact("a", value={"z": [1, {"b": False}], "a": "text"}),
            fact("b", value={"nested": {"null": None}}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.jsonl"
            second = Path(tmp) / "second.jsonl"
            write_jsonl(first, records)
            loaded = load_jsonl(first, GoldFact)
            write_jsonl(second, loaded)

            expected_lines = [
                json.dumps(
                    {
                        field.name: (
                            {"z": [1, {"b": False}], "a": "text"}
                            if field.name == "value"
                            else getattr(records[0], field.name)
                        )
                        for field in dataclasses.fields(records[0])
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        field.name: (
                            {"nested": {"null": None}}
                            if field.name == "value"
                            else getattr(records[1], field.name)
                        )
                        for field in dataclasses.fields(records[1])
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
            expected = ("\n".join(expected_lines) + "\n").encode("utf-8")
            self.assertEqual(first.read_bytes(), expected)
            self.assertEqual(second.read_bytes(), expected)
            self.assertEqual(loaded, records)
            self.assertIsInstance(loaded[0].value["z"][1], MappingProxyType)  # type: ignore[index]

    def test_empty_jsonl_round_trips_to_empty_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            write_jsonl(path, ())
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(load_jsonl(path, Exclusion), ())


if __name__ == "__main__":
    unittest.main()
