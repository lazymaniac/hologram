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

### Validation round — 0.6.0 map features (sonnet + haiku, effort=low, 3 reps)

Four real-world task shapes on the same private corpus, run at pinned low
reasoning effort against the 0.6.0 map (coverage edges + test helpers):

| model | cond | kind | accepted | answer ok | scope ok | duplication | reads | searches | turns |
|---|---|---|---|---|---|---|---|---|---|
| sonnet | A | navigate | 100% | 83% | — | 0% | 0.5 | 1.0 | 2.5 |
| sonnet | B | navigate | 100% | 67% | — | 0% | 2.3 | 3.0 | 1.8 |
| haiku | A | reuse | 100% | — | — | **0%** | 7.0 | 2.3 | 13.3 |
| haiku | B | reuse | 100% | — | — | **33%** | 19.0 | 11.7 | 15.7 |
| haiku | A | navigate | 100% | 50% | — | 0% | 0.8 | 0.7 | 2.7 |
| haiku | B | navigate | 100% | 50% | — | 0% | 7.3 | 4.8 | 4.8 |

- **The only duplication event of the entire measurement program landed in
  haiku-control**: one run re-invented an existing value helper the map
  names; all map-condition runs reused it. At the weakest tier and lowest
  effort — where context must do the work reasoning can't — the map is the
  difference between reuse and re-invention.
- **Coverage-awareness converged**: a well-posed "ensure coverage exists"
  task was answered correctly by both conditions (existing test cited, no
  duplicate written). An earlier, imperative phrasing of the same task made
  even map-equipped runs write duplicate tests — task phrasing dominates,
  and coverage edges name symbols, not behaviors; a behavior-level question
  still needs a grep.
- Map condition again halves exploration for haiku (reads 7 vs 19 on reuse,
  0.8 vs 7.3 on navigate).
- Harness lesson that cost two restarts, now fixed in the tool: corpora
  whose conventions make agents commit their own work blank a
  working-tree diff — all judges now diff against the recorded setup
  commit.

### Review-loop round — 0.7.0 (A vs AC vs AR, sonnet + haiku, effort=low, 3 reps)

Three map-bearing conditions on two write-task shapes: **A** = embedded map,
**AC** = map + the coaching sentence in the embed note, **AR** = map +
coaching + the post-commit `hologram review` hook live in the workspace.
Task shapes: a *duplication bait* (add a small utility whose value logic
already exists in the corpus) and a *coverage-placement task* with a
verified premise (write a test for an endpoint behavior that production
declares but no test file touches — confirmed by map and grep before the
round). All 39 runs passed acceptance.

| model | task | cond | reuse | parallel test file | review seen | turns |
|---|---|---|---|---|---|---|
| sonnet | dup bait | A | 3/3 | — | — | 7.7 |
| sonnet | dup bait | AC | 3/3 | — | — | 8.7 |
| sonnet | dup bait | AR | 3/3 | — | 3/3 | 7.7 |
| sonnet | coverage | A/AC/AR | — | 0/9 | 0/3 (clean) | 18–25 |
| haiku | dup bait | A | 2/3 | — | — | 26.0 |
| haiku | dup bait | AC | 3/3 | — | — | 18.7 |
| haiku | dup bait | AR | 3/3 | — | 3/3 | 13.0 |
| haiku | coverage | A | — | 1/3 | — | 23.0 |
| haiku | coverage | AC | — | 1/3 | — | 30.0 |
| haiku | coverage | AR | — | 2/3 | 3/3 | 32.3 |

- **Zero duplication events in any condition** — every map-bearing run on
  the bait either called the existing helper directly or delegated to it.
  The 0.6.0 round's haiku-control duplication did not recur because every
  0.7.0 condition carries the map; the map remains the first line of
  defense.
- **Placement splits by tier, not condition**: sonnet extended the existing
  endpoint test class in 9/9 coverage runs; haiku invented a parallel test
  file in 4/9, roughly evenly across conditions. The map alone saturates
  placement at mid-tier; at the weakest tier placement decisions happen
  *before* any feedback can fire.
- **The review loop fired exactly when it should**: every AR commit that
  drifted got findings in-session — *recover* findings naming the classes
  that already cover the paths a parallel test file re-covered, *dead*
  findings for the bait utility (task-induced: the task plants an uncalled
  helper) and for an unrequested production exception handler one haiku run
  added. Clean commits printed nothing (`--quiet-if-clean`), so sonnet's
  coverage runs saw no review output at all — silence is the designed
  behavior for clean work.
- **Seeing is not yet acting at low effort**: haiku agents read the
  findings, re-ran tests, and inspected the named originals — but none
  restructured already-committed work. The loop reliably *surfaces* drift
  at the moment it happens; acting on it still depends on model capability
  (and on prompts that leave room for a follow-up commit).
- The coaching sentence (AC) neither helped nor hurt measurably on these
  saturated tasks (haiku bait reuse 3/3 vs A's 2/3 is inside noise at n=3);
  it stays because its cost is ~30 tokens.
- The round itself caught two harness/tool bugs now fixed: the post-commit
  review died silently inside git hooks (`GIT_DIR` environment poisoning —
  the AR bait cell for sonnet was rerun after the fix), and a corpus
  context file instructing agents to work in its home checkout by absolute
  path let one early agent commit into the real corpus — bench workspaces
  are now path-confined clones with no origin remote.

Cross-round comparisons to the 0.5.0 tables are directional only: these
runs pinned `--effort low`; the earlier rounds ran at CLI defaults.
