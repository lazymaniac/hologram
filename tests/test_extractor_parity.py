from __future__ import annotations

import json
import unittest
from pathlib import Path

from hologram import legacy
from hologram.config import default_config
from hologram.pipeline import build_project
from tests.parser_assertions import assert_body_fact_events

TESTS = Path(__file__).resolve().parent
FIXTURES = tuple(
    TESTS / "fixtures" / name for name in ("javamini", "pymini", "tsmini", "polyglot")
)
ORACLE = TESTS / "fixtures" / "expected" / "extractors-v1.json"


def normalize(symbol: legacy.Symbol) -> dict[str, object]:
    return {
        "file": symbol.file,
        "line": symbol.line,
        "name": symbol.name,
        "kind": symbol.kind,
        "signature": symbol.signature,
        "params": list(symbol.params),
        "returns": symbol.returns,
        "visibility": symbol.visibility,
        "container": symbol.container,
        "lang": symbol.lang,
        "calls": list(symbol.calls),
        "supers": list(symbol.supers),
        "permits": list(symbol.permits),
        "raises": list(symbol.raises),
        "bindings": [
            [name, type_name] for name, type_name in sorted(symbol.bindings.items())
        ],
        "body_lines": symbol.size,
    }


def legacy_key(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record["file"],
        record["line"],
        record["kind"],
        record["container"] or "",
        record["name"],
        record["signature"],
    )


class ExtractorParityTest(unittest.TestCase):
    def test_reviewed_v1_oracle_is_consumed_read_only_from_one_snapshot(self) -> None:
        self.assertTrue(ORACLE.is_file(), f"missing reviewed oracle: {ORACLE}")
        before = ORACLE.read_bytes()
        expected = json.loads(before)
        self.assertEqual(set(expected), {fixture.name for fixture in FIXTURES})

        actual: dict[str, list[dict[str, object]]] = {}
        for fixture in FIXTURES:
            snapshot = build_project(fixture, default_config()).require_complete()
            records: list[dict[str, object]] = []
            for file_ir in snapshot.project.files:
                assert_body_fact_events(self, file_ir)
                records.extend(
                    normalize(symbol) for symbol in legacy._canonical_to_legacy(file_ir)
                )
            records.sort(key=legacy_key)
            expected_records = expected[fixture.name]
            expected_keys = [legacy_key(record) for record in expected_records]
            self.assertEqual(len(expected_keys), len(set(expected_keys)))
            self.assertEqual(records, expected_records, fixture.name)
            actual[fixture.name] = records

        self.assertEqual(actual, expected)
        self.assertEqual(ORACLE.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
