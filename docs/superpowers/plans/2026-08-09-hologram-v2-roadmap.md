# Hologram v2 Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a deterministic, complete, multi-agent project map that exposes precise source provenance, conservative unused and duplicate advisories, and trustworthy benchmark evidence.

**Architecture:** Replace the 2,639-line script with a `src/hologram` package built around immutable extraction and render IRs. Implement the work as five green, independently reviewable phases; each phase depends only on the public contracts frozen by the preceding phase.

**Tech Stack:** Python 3.11+, `unittest`, `tomllib`, Tree-sitter 0.26 with pinned language grammars, Git CLI, Markdown managed blocks, Claude Code benchmark runner.

---

## Baseline and plan order

The starting branch is `codex/benchmark-task-tiers`. Baseline verification on
2026-08-09 is 124 passing tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Assessment driving the roadmap

The core product idea is sound: one deterministic whole-project map gives an
LLM a better orientation and reuse surface than ad hoc source search. The current
implementation already proves the useful shape—declarations, signatures,
relations, calls, tests, fan-in, module coupling, and multi-language extraction—
but its reliability boundary is not yet strong enough for deletion or
duplication decisions.

| Current condition | Risk to the product goal | Planned response |
|---|---|---|
| One 2,639-line script with runtime parser installation | Hard to change precisely; setup mutates its own environment | Installable `src/hologram` package, pinned optional parsers, no runtime installation |
| Scan/hash paths can omit or reread source state | A fresh-looking map can be incomplete or combine snapshots | Complete Git candidate ledger, one-read bytes, fail-closed extraction, versioned full SHA-256 |
| Line-sensitive or presentation-shaped identity/grouping | Edits churn identities and same-shaped declarations lose ownership | Line-independent `SymbolId`, separate spans, explicit file leaves, exact codec round-trip |
| Name/token-based popularity evidence | Same names, comments, tests, and dynamic use can misclassify symbols | Symbol-resolved references, distinct production-file fan-in, `×0`/`×0?`/`✓` decision table |
| No body-grounded duplicate inventory | Agents can miss an existing implementation and create a clone | Frozen body events, exact/near duplicate scoring, `≈N`, and new-code diff advisories |
| Optional/lossy delivery and write-capable freshness hooks | The map can be absent, stale, truncated, or mutate during checks | Complete managed blocks, atomic preflight, read-only check/pre-commit, no budget or truncation |
| Historical benchmark acceptance did not make terminal success part of outcome | Partial/max-turn work can look successful | Pinned isolated runner, terminal-plus-verifier acceptance, answer validation, matched B/C reporting |
| Static evidence can never prove runtime deadness | A literal promise of zero unused code would be unsafe | Strong findings only for closed private cases; uncertain public/dynamic cases stay `×0?`; 100% planted precision/recall gates and human review |

The practical target is therefore zero *unnoticed* newly introduced unused or
duplicate code in the battle-tested cases, not an unsound claim that static
analysis can prove semantic deadness in every framework or reflective runtime.

Implement these plans in order:

1. [Package and foundation](2026-08-09-hologram-v2-foundation.md)
2. [Language extractors and resolution](2026-08-09-hologram-v2-extractors.md)
3. [Reference, duplicate, and lossless rendering analysis](2026-08-09-hologram-v2-analysis-render.md)
4. [Configuration, managed contexts, CLI, diff, and hook delivery](2026-08-09-hologram-v2-delivery.md)
5. [Static accuracy corpus and tiered agent benchmarks](2026-08-09-hologram-v2-validation.md)

Do not combine phase commits. At the end of each phase, run the complete test
suite and review the generated self-map before starting the next plan.

## Frozen cross-phase contracts

- Python 3.11+; no runtime virtualenv creation or package installation.
- No database, daemon, query service, embeddings, or model-assisted indexing.
- Git scans include tracked and untracked nonignored files.
- Every supported candidate is indexed, explicitly excluded, or failed; any
  failed candidate makes extraction incomplete.
- Raw calls and relations remain ordered, direct, uncapped facts. Ambiguity is
  preserved rather than guessed away.
- Every declaration, including test declarations, has an exact file and line.
- `×N` counts distinct production referring files; `×0`, `×0?`, `✓`, and `≈N`
  follow the approved conservative contracts.
- `×0` and `×0?` are advisories, never automatic deletion authority. `diff`
  calls out newly introduced advisories so reviews can prevent unintentional
  unused code without pretending static analysis proves semantic deadness.
- Rendering has no token budget, ranked omission, degradation, or truncation.
- The decoder round-trips the canonical render IR exactly.
- Hologram updates only managed blocks in root `CLAUDE.md`, `AGENTS.md`, and
  `GEMINI.md`; authored purpose remains untouched.
- The pre-commit hook runs read-only `hologram check`; it never builds or stages.
- Exit codes are 0 success/advisory, 1 stale context, 2 usage/configuration, and
  3 incomplete extraction or revision analysis.
- Public benchmarks may be tracked. Private manifests and raw artifacts remain
  outside the repository; only condition totals may be reported.
- No paid benchmark session is part of implementation verification.

## Phase completion checklist

- [ ] **Foundation complete:** editable package imports from `src/hologram`,
      root script is gone, strict config/scanner/state APIs exist, all tests pass.
- [ ] **Extractors complete:** all advertised languages emit immutable raw facts,
      import/alias resolution is conservative, all fixture/golden tests pass.
- [ ] **Analysis/render complete:** marker and duplicate fixtures pass, renderer
      and decoder round-trip, self-map has no unexplained strong findings.
- [ ] **Delivery complete:** three agent files update atomically, `check` is
      read-only, `diff` is model-based, pre-commit behavior is proven by tests.
- [ ] **Validation complete:** public static thresholds pass, benchmark dry-run
      covers eight public rows (four matched B/C pairs), private redaction tests
      prove non-leakage.

## Final verification

Run after all five plans:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache src tests benchmark validation
.venv/bin/mypy --cache-dir=/tmp/hologram-v2-mypy-cache src/hologram
.venv/bin/python -m validation.run \
  --registry validation/corpora.toml \
  --census validation/gold/census.jsonl \
  --sample validation/gold/sample.jsonl \
  --facts validation/gold/facts \
  --exclusions validation/gold/exclusions \
  --runs 3 \
  --output /tmp/hologram-v2-static-validation.json
.venv/bin/python -m hologram build --root . --quiet
.venv/bin/python -m hologram check --root . --quiet
test -n "$HOLOGRAM_BENCH_CODECOMPANION"
hologram_public_results=$(mktemp -d /tmp/hologram-public-bench-dry.XXXXXX)
.venv/bin/python benchmark/bench.py run benchmark/tasks/codecompanion.json \
  --dry-run \
  --corpus "$HOLOGRAM_BENCH_CODECOMPANION" \
  --results "$hologram_public_results"
.venv/bin/python -m unittest tests.test_bench_privacy.PrivateDryRunMatrixTest -v
git diff --check
git diff --cached --check
git check-ignore -q benchmark/archive
test -z "$(git status --porcelain=v1)"
if git ls-files | rg -q '(^|/)(private-manifest|private-results)(/|$)|(^|/)(runs\.jsonl|.*\.answer\.txt|.*\.transcript\.jsonl|.*\.verifier\.log)$'; then
  echo "forbidden tracked benchmark artifact"
  exit 1
fi
if git ls-files benchmark/archive | rg -q .; then
  echo "legacy benchmark archive still tracked"
  exit 1
fi
```

Expected: all tests pass; Ruff and mypy report no errors; the 748-file census,
103-file gold sample, 33 synthetic fixtures, and three-run determinism gate pass;
build and check exit 0; dry runs internally validate eight public and eight
external-private rows without invoking Claude; privacy guards are silent; the
standalone map is fresh and ignored; and tracked/untracked nonignored status is
empty.
