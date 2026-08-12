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
