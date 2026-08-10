# Hologram benchmark

> [!IMPORTANT]
> The current benchmark is the pinned, tiered B/C matrix described below. The
> Spring report is legacy, exploratory, pre-tier evidence and is not comparable
> with a current v2 report. No paid sessions are run by the implementation suite.

The current conditions B and C compare two fresh, provenance-matched workspaces
for every task:

- condition B is the control and contains no Hologram map or managed context;
- condition C has the complete map in a managed canonical v2 block in
  `CLAUDE.md` from turn zero.

Historical condition A used the retired on-disk legacy digest and is now rejected.
Every B/C pair uses the same challenged Git tree and copied workspace assets. Each
Claude invocation runs in an isolated configuration directory. A run is accepted
only when the Claude process terminates successfully and the task-specific answer
verifier passes. Navigation answers use strict JSON/evidence verifiers; acceptance
is not inferred from activity counts.

## Frozen public matrix

The active manifest is [`tasks/codecompanion.json`](tasks/codecompanion.json):

| setting | frozen value |
|---|---|
| corpus | `olimorris/codecompanion.nvim` |
| revision | `2b959b2bf5fdb13e3b333c078ba549996e477b7c` |
| model | `claude-sonnet-5` |
| Claude Code | `2.1.224` |
| turn limit | 40 |
| conditions | B/C |
| repetitions | 1 |
| seed | `20260809` |

The four tasks cover the simple/complex tiers and orientation, implementation,
planning, and audit capabilities. The manifest freezes each prompt, verifier
command, optional challenge hash, expected reuse targets, model, CLI version,
turn limit, B/C conditions, one repetition, and seed. The harness rejects a
different revision, origin, dirty corpus, missing dependency asset, Claude Code
version, repetition count, or incomplete B/C schedule.

## Prepare the public corpus

Choose an external destination. Preparation clones the declared URL, checks out
the exact revision, runs the declared `make deps` bootstrap, and then verifies the
origin, HEAD, cleanliness, and `deps` asset:

```bash
export HOLOGRAM_BENCH_CODECOMPANION=/absolute/external/codecompanion.nvim
.venv/bin/python benchmark/bench.py prepare \
  benchmark/tasks/codecompanion.json \
  --corpus "$HOLOGRAM_BENCH_CODECOMPANION"
```

Preparation performs network and dependency setup. `run` never fetches or repairs
the corpus; it fails closed if the prepared checkout no longer matches the frozen
manifest.

## Zero-cost public dry run

Use a new external results directory. This exercises manifest loading, exact
corpus verification, deterministic scheduling, provenance fields, row writing,
and reporting without invoking Claude:

```bash
public_results=$(mktemp -d /tmp/hologram-public-dry.XXXXXX)
.venv/bin/python benchmark/bench.py run \
  benchmark/tasks/codecompanion.json \
  --corpus "$HOLOGRAM_BENCH_CODECOMPANION" \
  --results "$public_results" \
  --dry-run
.venv/bin/python benchmark/bench.py report --results "$public_results"
```

The schedule must contain exactly eight unique rows: four tasks × B/C × one
repetition, forming four complete pairs. Dry-run rows are intentionally neither
completed nor accepted and contain no fabricated navigation or reuse result.

## Manual paid run

The following operation invokes Claude and costs money. It is a manual experiment,
outside automated tests and implementation verification. Do not run it in CI:

```bash
paid_results=$(mktemp -d /tmp/hologram-public-paid.XXXXXX)
.venv/bin/python benchmark/bench.py run \
  benchmark/tasks/codecompanion.json \
  --corpus "$HOLOGRAM_BENCH_CODECOMPANION" \
  --results "$paid_results"
.venv/bin/python benchmark/bench.py report --results "$paid_results"
```

Before a paid run, independently review the prompts, verifier commands, challenge,
prepared corpus, installed `claude` version, and results destination. The
implementation suite only uses `--dry-run`; no paid sessions are run by the
implementation suite.

## Private matrix

A private manifest must be outside the Hologram worktree, declare
`visibility: "private"` and `url: null`, and point only to external corpus,
challenge, verifier, hidden-test, and workspace-asset paths. Private raw results
must also be an explicit external path; there is no in-repository default and no
tracked private fixture.

```bash
private_results=$(mktemp -d /tmp/hologram-private-dry.XXXXXX)
.venv/bin/python benchmark/bench.py run \
  /absolute/private/tasks.json \
  --corpus /absolute/private/corpus \
  --results "$private_results" \
  --dry-run
.venv/bin/python benchmark/bench.py report --results "$private_results"
```

External private results retain the raw rows for their owner. The rendered private
report exposes condition totals and numeric aggregates only; it does not echo task,
corpus, model, prompt, answer, path, or provenance text.

## Reading reports

Public reports match each B row with its C row by the complete frozen experimental
identity and partition results by model/version, tier, and capability. Acceptance,
turns, reads, and searches are computed only from matched rows. Navigation means
use accepted, completed pairs. Implementation reuse and duplication likewise use
only accepted, completed pairs and preserve empty cells as dashes rather than
silently pooling them. Legacy or incomplete historical rows appear only under
`legacy / unclassified`.

Private reports deliberately omit those partitions and show B/C totals only. Never
combine public and private rows in one report.

## Static validation gate

The benchmark measures downstream agent behavior; the independent static gate
measures whether the map itself is accurate and deterministic. Provide clean,
external checkouts at the revisions in `validation/corpora.toml`, then run:

```bash
export HOLOGRAM_VALIDATION_HOLOGRAM=/absolute/external/hologram
export HOLOGRAM_VALIDATION_CODECOMPANION=/absolute/external/codecompanion.nvim
export HOLOGRAM_VALIDATION_CYPRESS=/absolute/external/cypress-realworld-app
export HOLOGRAM_VALIDATION_KAFKA_STREAMS_EXAMPLES=/absolute/external/kafka-streams-examples
export HOLOGRAM_VALIDATION_JDB=/absolute/external/jdb-agentic-debugger

.venv/bin/python -m validation.run \
  --registry validation/corpora.toml \
  --census validation/gold/census.jsonl \
  --sample validation/gold/sample.jsonl \
  --facts validation/gold/facts \
  --exclusions validation/gold/exclusions \
  --runs 3 \
  --output /tmp/hologram-v2-static-validation.json
```

The frozen inventory is 748 census files, a 103-file reviewed sample split
9/24/38/26/6 across Hologram, CodeCompanion, Cypress, Kafka, and JDB, and 33
synthetic files. The three-run gate requires byte-identical facts and maps.

Frozen minimums are:

| metric | minimum |
|---|---:|
| declaration micro precision / recall | 99% / 97% |
| declaration precision / recall for Java, Python, TypeScript, and TSX | 97% / 95% each |
| kind, container, and visibility accuracy | 99% each |
| signature accuracy | 95% overall; 90% per represented language |
| non-call relation exact accuracy | 97% |
| call precision / recall for Java, Python, TypeScript, and TSX | 95% / 85% each |
| Lua call precision / recall | 90% / 70% |
| lexical call-order accuracy | 85% |
| strong `×0` precision / recall | 100% / 100% |
| synthetic zero-classification accuracy | 100% |
| approximate precision / recall | 100% / 80% |

Gold and thresholds are reviewed inputs, not knobs to change when a run fails.

## Legacy evidence

[`results-spring-2026-08-08.md`](results-spring-2026-08-08.md) preserves numerical
observations from a legacy, exploratory, and pre-tier experiment only, with n=1 per cell.
In that protocol, `sonnet` is a mutable model alias.
Its reuse acceptance commands often verify only that a change occurred.
Its navigation correctness is not automated (`true`).
Its 40-turn ceiling is not outcome-gated. In other words, navigation acceptance was
not automated. It cannot validate the current hardened harness or be compared
numerically with a future v2 report.

The obsolete active Spring task manifest was removed because it predates the
terminal-success and answer-verifier contract. Ignored local legacy archives stay
outside Git and must not be inspected, summarized, copied, or cited in tracked
documentation.
