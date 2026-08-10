# hologram benchmark

> [!IMPORTANT]
> Historical condition A measured the retired on-disk legacy digest and appears
> only in archived evidence. The harness now rejects A. The current conditions B and C
> compare no Hologram context with a managed canonical v2 block in
> `CLAUDE.md`.

Every run is a headless `claude -p` session in a throwaway git worktree. Condition
B is the control; condition C receives the complete managed map from turn zero.
Metrics come from the transcript, an acceptance command, and canonical maps decoded
before and after the run.

## Smoke run (do this first — ~4 sessions)

    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json \
        --only trim-to-null find-getbean-flow --reps 1
    .venv/bin/python benchmark/bench.py report

Read the two transcripts in `benchmark/results/` end to end once. Confirm that the
condition-C context contains the managed map, condition B contains no Hologram
context, and the acceptance commands measured what you meant.

## Full matrix (~60 sessions — this costs real money)

    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json --reps 3
    .venv/bin/python benchmark/bench.py report

## Reading the report

- **duplication** is the headline: % of reuse-task runs where the agent wrote a
  name-similar function instead of calling the existing one. Directional claim:
  C < B.
- **reads / searches / turns / tokens** are the navigation story: C should read
  and search less on navigate tasks.
- With reps=3 the numbers are directional, not significant. Don't publish a
  percentage without saying n. The per-task list at the bottom of the report is
  for eyeballing which tasks discriminate — drop tasks that saturate (everyone
  succeeds or everyone fails) and replace them.

## Honest limitations

- Archived evidence is legacy, exploratory, and pre-tier; its runs used n=1 per cell.
- `sonnet` is a mutable model alias, not a pinned model version.
- The reuse acceptance commands often verify only that a change occurred, not that
  its full behavior is correct.
- In this harness, navigation correctness is not automated (`true`); transcript
  review is required.
- The 40-turn ceiling is not outcome-gated, so an accepted and a timed-out run can
  consume the same configured ceiling.
- The duplication detector uses decoded canonical call data plus name similarity.
  The name comparison remains heuristic, so review verdicts before quoting them.
- One corpus, one model, ten tasks. This answers "does the digest help *here*",
  not "does it help everywhere".
- Navigation tasks are judged by acceptance `true` — their signal is in
  reads/searches/tokens, not correctness. A human should spot-check the answers.
