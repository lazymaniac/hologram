#!/usr/bin/env python3
"""Dev-only token measurement: build each corpus digest, count real o200k tokens.

Run from a scratch venv that has tiktoken and the tree-sitter grammars
installed — tiktoken is never a runtime or test dependency; `estimate_tokens`
in hologram.render stays the dependency-free heuristic. This script exists to
verify representation decisions against the tokenizer they were measured with.

Usage:
    measure_tokens.py [--repo <hologram-checkout>] [corpus-root ...]

`--repo` selects which hologram checkout is imported and measured as "self"
(defaults to the checkout containing this script), so a baseline can be taken
from a worktree of an older revision. Corpus roots default to the fixture
corpora plus the repo itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo",
                    default=str(Path(__file__).resolve().parent.parent),
                    help="hologram checkout to import and measure as 'self'")
    ap.add_argument("roots", nargs="*",
                    help="corpus roots (default: fixture corpora + repo)")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))
    import hologram

    try:
        import tiktoken
    except ImportError:
        sys.exit("tiktoken not importable — run from the scratch measurement venv")
    enc = tiktoken.get_encoding("o200k_base")

    fixtures = repo / "tests" / "fixtures"
    roots = [Path(r).resolve() for r in args.roots] or [
        fixtures / "javamini", fixtures / "pymini", fixtures / "tsmini",
        fixtures / "polyglot", repo]
    out = {}
    for root in roots:
        digest = hologram.build_digest(root)
        key = "self" if root == repo else root.name
        out[key] = {"tokens": len(enc.encode(digest)), "chars": len(digest),
                    "lines": digest.count("\n") + 1}
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
