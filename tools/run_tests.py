#!/usr/bin/env python3
"""Run the dependency-free or grammar-complete test contract."""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("core", "full"), required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.profile == "full":
        import hologram
        missing = sorted(lang for lang in hologram._GRAMMAR_MODULES
                         if not hologram.has_parser(lang))
        if missing:
            print("full test profile is missing parsers: " + ", ".join(missing),
                  file=sys.stderr)
            return 2

    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(
        verbosity=1 if args.quiet else 2).run(suite)
    if args.profile == "full" and result.skipped:
        print("full test profile had unexpected skips:", file=sys.stderr)
        for test, reason in result.skipped:
            print(f"- {test}: {reason}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
