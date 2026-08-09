# Benchmark Task Tiers Design

## Objective

Add explicit simple and complex benchmark tiers without equating difficulty with
prompt length or file count. The suite must test the four product claims that a
Hologram map should improve: orientation, planning, precise implementation with
reuse, and duplicate/unused-code review.

The first matrix contains eight tasks: four against the pinned public
CodeCompanion corpus and four against an authorized private Java holdout. Each
task runs once under the control condition and once with the embedded map, for
16 sessions. Private task content and raw results never enter tracked files.

## Difficulty Model

Difficulty is behavioral:

- **Simple** tasks have one bounded decision, a narrow source surface, and a
  deterministic verifier. They may be read-only or make a focused edit.
- **Complex** tasks require cross-layer reasoning, multiple interacting
  invariants, or distinguishing true findings from plausible reachability
  decoys.

Difficulty is orthogonal to capability and execution kind. Every task declares:

```json
{
  "id": "read-file-integer-ranges",
  "tier": "simple",
  "capability": "implementation",
  "kind": "reuse",
  "visibility": "public",
  "prompt": "Make read_file reject fractional range bounds while preserving whole-number behavior.",
  "accept_cmd": "python benchmark/verifiers/codecompanion.py read-file-integer-ranges {ws}",
  "expect_reuse": ["extract_range"]
}
```

Allowed values:

- `tier`: `simple`, `complex`
- `capability`: `orientation`, `planning`, `implementation`, `audit`
- `kind`: `navigate`, `reuse`
- `visibility`: `public`, `private`

`orientation`, `planning`, and `audit` are read-only `navigate` tasks.
`implementation` is a workspace-changing `reuse` task. A navigate verifier must
consume `{answer}` and require a clean worktree; a reuse verifier must consume
`{ws}` and run task-specific behavioral checks.

Top-level configuration records the exact corpus commit, full model name,
Claude Code version, and positive `max_turns`. Public corpus paths come from an
environment variable or CLI override rather than a checked-in absolute path.
Private configuration is supplied from an untracked external manifest.

The loader rejects unknown fields, duplicate or unsafe IDs, invalid enum
combinations, empty prompts, no-op verifiers, mutable model aliases, invalid
turn limits, reuse tasks without expected reuse, and asymmetric condition task
sets.

## Public Task Matrix

The public corpus is CodeCompanion at
`2b959b2bf5fdb13e3b333c078ba549996e477b7c`.

### Simple orientation: FileEdited lifecycle

Read-only trace of the bounded `CodeCompanionFileEdited` event graph. The answer
must identify the shared event helper, every production producer, both
consumers, their installation point, emitted payloads, and the consequence of
`delete_file` not participating. A source-grounded answer verifier checks the
required symbols and anchors; the worktree must remain clean.

This is simple because one exact event name bounds the graph and no design
decision or implementation is required.

### Simple implementation: integer read ranges

Make `read_file` accept only integer start/end bounds in both its JSON schema
and runtime validation. Fractional values must return normal tool errors while
preserving zero-based inclusive ranges, the `-1` end sentinel, clamping,
negative/out-of-range checks, reversed-range handling, and whole-number numeric
strings.

Only the tool implementation and focused test file may change. Acceptance runs
the focused Mini.Test file, formatting, the full suite, and static checks. The
implementation must extend the existing range parser rather than add a second
one.

### Complex planning: built-in move-file tool

Produce a read-only, decision-complete plan for a built-in `move_file` tool.
The plan must discover and reuse the existing rename, cwd-containment,
approval, configuration/group registration, and edited-file tracking
mechanisms. It must order validation, approval, mutation, event emission, and
failure behavior; cover source/destination boundary cases; name exact
touchpoints; and provide a focused Mini.Test matrix and native commands.

The verifier uses a frozen source-grounded rubric. Proposing parallel rename,
containment, approval, or tracking abstractions caps the score below passing.

### Complex audit: duplicate and unused challenge

Apply an identical frozen challenge patch before both conditions and build the
map from the challenged tree. The patch adds an active local clone of the
canonical cwd-containment helper and an unused private context helper.

The audit must report:

- the active clone and canonical replacement (`≈1`),
- the private zero-reference helper as a strong `×0` candidate,
- an existing exported zero-internal-reference utility as uncertain
  (`×0?`), and
- a config/string-resolved callback as reachable rather than dead.

It must make no edits. The exact challenge diff hash, clean-worktree check,
finding set, canonical counterpart, and rejected-decoy set are deterministic.

## Private Task Matrix

The private holdout uses the authorized pinned base through an external local
manifest. Tracked documentation and reports use only these descriptions:

- **Simple orientation:** trace one request through its port, orchestration,
  durable payload publication, inbox recording, and outcome mapping.
- **Simple implementation:** repair a value object's public construction paths
  so they enforce the same canonical representation, with focused hidden tests.
- **Complex planning:** plan one additional fail-closed provenance mode while
  retaining all existing validation, governance, generation, and negative-test
  controls.
- **Complex audit:** review a coherent three-file framework-managed diagnostics
  feature containing one active exact helper clone (`≈1`), one truly unused
  private symbol (`×0`), one public zero-internal-use surface (`×0?`), and
  one annotation-reachable zero-static-caller decoy.

Exact prompts, repository identity, revision, paths, symbols, source text,
challenge patch, hidden tests, gold answers, transcripts, diffs, and verifier
logs stay outside the tracked repository. The public reporter constructs the
private summary from an allowlist of numeric fields and emits condition totals
only.

## Runner and Grading Semantics

A verifier is diagnostic unless the agent completed successfully. A run is
accepted only when both are true:

1. Claude exits successfully with exactly one terminal result whose subtype is
   `success`, `is_error` is false, stop reason is `end_turn`, and final answer is
   nonempty.
2. The task-specific verifier exits zero.

`error_max_turns`, timeout after the first assistant event, missing/multiple
terminal results, permission errors, context overflow, model fallback, or a
partial patch always fail. The verifier still runs against partial work for
diagnostics but cannot override terminal failure.

Navigation final text is saved separately and exposed to its verifier as
`{answer}`. Each result row records tier, capability, visibility, pinned model,
turn limit, terminal status, verifier outcome, derived acceptance, reuse and
duplication evidence, reads, searches, edits, turns, and map hits.

Reports partition by model/version, then by tier, capability, and condition.
Efficiency means use accepted matched pairs only. Failed runs never enter
efficiency or duplication denominators. Reports show both unique task and run
counts and never mix simple and complex means. Legacy rows without the new
schema are explicitly unclassified rather than silently treated as simple.

## Privacy and Reproducibility

- Public task prompts, rubrics, verifier assets, corpus URL/commit, and complete
  per-task results may be tracked.
- Private manifests and raw artifacts must be outside the worktree. The harness
  refuses to write private artifacts into a tracked directory.
- Private reporting emits only control/map totals for completion, acceptance,
  rubric score, exploration calls, and turns. It never emits task rows or raw
  strings.
- Both conditions receive byte-identical corpus/challenge state. The treatment
  differs only by the managed map block.
- Run pairs use fresh workspaces and isolated Claude configuration. Pair order
  is balanced and task order is deterministically shuffled from a recorded
  seed.

## Acceptance Criteria

- All eight tasks load with valid tier/capability/kind combinations.
- Dry-run coverage exercises every tier, capability, visibility, and condition.
- A fake `error_max_turns` run with a verifier-passing partial patch remains
  rejected.
- Read-only verifiers reject modified worktrees and incorrect/missing answers.
- Implementation verifiers reject unrelated paths, missing canonical reuse,
  duplicate helpers, and unintended unused production symbols.
- Reports keep simple/complex and capability metrics separate and omit failed
  rows from efficiency means.
- Private-report tests prove that prompts, paths, task IDs, symbols, hashes, and
  transcripts cannot reach generated summaries.
- The existing test suite remains green.

## Out of Scope

- Running paid benchmark sessions in this change.
- Adding more than two difficulty tiers or a numeric complexity score.
- Token/cost budgets or statistical-significance claims.
- Publishing private task-level results.
- Implementing Hologram v2 extraction or marker behavior itself; this work only
  supplies the tiered task suite and trustworthy harness semantics needed to
  evaluate it.
