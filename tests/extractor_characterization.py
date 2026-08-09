from __future__ import annotations

import argparse
import json
from pathlib import Path

from hologram import legacy

TESTS = Path(__file__).resolve().parent
FIXTURES = tuple(
    TESTS / "fixtures" / name for name in ("javamini", "pymini", "tsmini", "polyglot")
)
OUTPUT = TESTS / "fixtures" / "expected" / "extractors-v1.json"
EXPECTED_LANGUAGES = {
    "java",
    "python",
    "typescript",
    "tsx",
    "vue",
    "csharp",
    "kotlin",
    "c",
    "cpp",
    "go",
    "lua",
    "rust",
    "html",
    "helm",
}


def normalize(symbol) -> dict[str, object]:
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
        "bindings": sorted(symbol.bindings.items()),
        "body_lines": symbol.size,
    }


def render() -> str:
    results: dict[Path, list[dict[str, object]]] = {}
    represented: set[str] = set()
    for fixture in FIXTURES:
        records: list[dict[str, object]] = []
        for path in legacy.scan_files(fixture):
            symbols = legacy.extract_file(path, fixture)
            if symbols:
                language = legacy.detect_language(path)
                if language is not None:
                    represented.add(language)
            records.extend(normalize(symbol) for symbol in symbols)
        records.sort(
            key=lambda item: (
                item["file"],
                item["line"],
                item["kind"],
                item["container"] or "",
                item["name"],
                item["signature"],
            )
        )
        results[fixture] = records

    if represented != EXPECTED_LANGUAGES:
        missing = sorted(EXPECTED_LANGUAGES - represented)
        unexpected = sorted(represented - EXPECTED_LANGUAGES)
        raise AssertionError(
            f"legacy oracle language mismatch: missing={missing}, "
            f"unexpected={unexpected}"
        )

    payload = {fixture.name: records for fixture, records in sorted(results.items())}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that the reviewed oracle is byte-stable without rewriting it",
    )
    args = parser.parse_args()
    rendered = render()
    if args.check:
        if OUTPUT.read_bytes() != rendered.encode("utf-8"):
            raise SystemExit(f"stale characterization oracle: {OUTPUT}")
        return
    OUTPUT.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
