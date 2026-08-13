# hologram benchmark

Measures agents with the map in context (condition A) against agents without
(condition B) on the same tasks in the same corpus. Every run is a headless
`claude -p` session in a throwaway git worktree; metrics come from the transcript,
an acceptance command, and a map-diff duplication check.

## Smoke run (do this first — ~4 sessions)

    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json \
        --only trim-to-null find-getbean-flow --reps 1
    .venv/bin/python benchmark/bench.py report

Read the two transcripts in `benchmark/results/` end to end once. Check that the
condition-A workspace really carries the embedded block in its `CLAUDE.md` and that
the acceptance commands measured what you meant.

## Full matrix (~60 sessions — this costs real money)

    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json --reps 3
    .venv/bin/python benchmark/bench.py report

## Reading the report

- **duplication** is the headline: % of reuse-task runs where the agent wrote a
  name-similar function instead of calling the existing one. Directional claim:
  A < B.
- **reads / searches / turns / tokens** are the navigation story: A should read
  and search less on navigate tasks.
- With reps=3 the numbers are directional, not significant. Don't publish a
  percentage without saying n. The per-task list at the bottom of the report is
  for eyeballing which tasks discriminate — drop tasks that saturate (everyone
  succeeds or everyone fails) and replace them.

## Honest limitations

- The duplication detector is a heuristic (call-chain + name similarity). Review
  its verdicts manually before quoting them; the per-task list makes that fast.
- One corpus, one model, ten tasks. This answers "does the map help *here*",
  not "does it help everywhere".
- Corpora the model has memorized (large, famous OSS) measure the wrong thing: the
  control answers from training memory, so the map can only add cost. Run this
  against code the model has never seen.
- Navigation tasks are judged by acceptance `true` — their signal is in
  reads/searches/tokens, not correctness. A human should spot-check the answers.

## Measured results — 2026-08 round (anonymized)

Corpus: a private, unpublished production repo (Java + Python, several hundred
source files; condition-A map: ~14k tokens, language-filtered). Model aliases
are the claude CLI defaults at run time; no thinking/effort flags were set, so
each tier ran its stock configuration. All identifiers below are neutral run
ids; no corpus symbol names appear in this report by policy.

### Navigation and lookup tasks (sonnet, 6 tasks × A/B, 1 rep)

| condition | answer ok | turns | reads | searches | tokens in | tokens out |
|---|---|---|---|---|---|---|
| A (map) | 100% | 1.2 | 0.0 | 0.2 | 81,610 | 143 |
| B (control) | 100% | 3.5 | 0.5 | 2.0 | 146,695 | 354 |

Constants with values, interface→implementor lists, and route→handler pairs
were read directly off the map: ~1 turn, zero file reads, −45% input tokens,
−60% output tokens. Both conditions answered everything correctly — the map's
value here is effort, not accuracy, and it is unambiguous.

### Long-session generative task, model sweep (1 task × A/B × 3 reps × 3 models)

One 60-turn-budget task: write a test suite exercising an exception-mapping
path end to end (the HTTP mapping layer *and* the raising implementation),
tests only, following existing conventions. Acceptance = the expected exception
type exercised from new test code. **18/18 runs passed acceptance** — every
quality difference below is invisible to a grep-based gate.

Effort (means over 3 reps):

| model | cond | turns | reads | edits | tokens out |
|---|---|---|---|---|---|
| haiku | A | 33.7 | 17.3 | 10.3 | 19,271 |
| haiku | B | 48.0 | 19.3 | 7.7 | 19,278 |
| sonnet | A | 28.3 | 15.0 | 5.0 | 29,577 |
| sonnet | B | 37.0 | 18.3 | 7.3 | 32,070 |
| opus | A | 26.7 | 12.3 | 3.0 | 21,229 |
| opus | B | 34.0 | 14.3 | 2.0 | 22,889 |

- **The map's turn saving replicates at every tier**: haiku −30%, sonnet −24%,
  opus −21%. The weaker the model, the more the map helps.
- **The map stabilizes runs.** Every A cell is tight (sonnet 28/29/28; opus
  23/28/29); every B cell is wide (sonnet 28–42; opus 20–41; haiku 43–54).
  Variance reduction matters operationally: predictable sessions are
  schedulable sessions.
- **A cheaper model with the map matches a stronger model without it on
  effort**: haiku-A (33.7 turns) ≈ opus-B (34.0); sonnet-A (28.3) beats
  opus-B. Whether that trade holds for *quality* is the next table.

Output quality, reconstructed from every run's Write/Edit payloads:

| model | cond | tested the real implementation | avg tests | asserts/test | comment noise | mocks | near-dup tests |
|---|---|---|---|---|---|---|---|
| haiku | A | 0/3 | 22 | 1.7 | high (4–24 lines) | 0 | 0 |
| haiku | B | 0/3 | 26 | 1.6 | high | 0 | 0 |
| sonnet | A | **3/3** | 17 | 1.3 | none | 0 | 0 |
| sonnet | B | 1/3 | 13 | 1.9 | low | 0 | 0 |
| opus | A | 3/3 | 19 | 2.0 | very low | 0 | 0 |
| opus | B | 3/3 | 16 | 2.1 | very low | 0 | 0 |

- **Classic AI-slop markers were absent everywhere**: zero mocks, zero
  near-duplicate test bodies, house-style structure in all 18 runs. The
  corpus's strict agent instructions (present in both conditions) set that
  floor. Slop, where it existed, was subtler: comment chatter (haiku),
  stub-instead-of-real-implementation scope narrowing, and re-covering ground
  existing tests already held.
- **The task's hardest requirement — test the real raising implementation,
  not a stub — is where the tiers separate.** Opus did it in 6/6 runs, map or
  not. Haiku never did (0/6); the map does not rescue a capability that isn't
  there. Sonnet is the interesting tier: **1/3 without the map, 3/3 with it** —
  the map's implementor lists and call chains appear to steer the mid-tier
  model to the real collaborator it should instantiate.
- Only opus ever discovered the corpus's existing payload-driver helper
  (2/6 runs); 17/18 runs rolled their own request builder. The map's test
  index names test files and classes but not their helpers — a possible
  future fact.
- Haiku writes the most tests (up to 37) with the most comment noise; opus
  writes fewer, denser, cleaner tests. Test count anticorrelates with quality
  here.

### Takeaways for this corpus

1. The map is decisively cheaper on navigation: answers come off the map in
   one turn.
2. On generative work, quality is set by model tier, effort by the map — with
   one exception that matters: at the mid tier the map changed *what* got
   tested, turning a scope-narrowed suite into a task-complete one in every
   rep.
3. Grep acceptance saturates (18/18); future rounds need scope-aware judges
   (e.g. expected collaborators must appear in new test imports).
4. Reps matter: single-rep effort numbers from earlier in the round were
   outliers in both directions; n=3 means were stable.
