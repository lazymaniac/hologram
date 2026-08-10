# Benchmark run: spring-framework, 2026-08-08

> [!CAUTION]
> This report is legacy, exploratory, pre-tier historical evidence. It predates
> the hardened B/C protocol, used a mutable model alias, and cannot validate or be
> compared numerically with a current v2 benchmark report. Navigation acceptance
> was not automated. No paid sessions are run by the implementation suite today;
> the paid sessions summarized here were a separate historical experiment.

**Setup.** Corpus: spring-projects/spring-framework @ da4b31c (1.54M LOC, 9,240 Java
files; digest = 563k tokens, 28s build). Model: sonnet, `--max-turns 40`, headless
`claude -p`. 7 tasks (4 reuse-bait, 3 navigation) × conditions A (digest +
instructions) / B (control) × 1 rep = 14 sessions, 12.6M tokens total. Detector
verdicts were hand-reviewed under the retired protocol.

## Historical observation: no benefit on this corpus — a real cost instead

| condition | reuse achieved | duplication | nav turns (mean) | total tokens |
|---|---|---|---|---|
| A (digest) | 3–4 of 4 | 0 | 5.7 | 8.1M |
| B (control) | 3–4 of 4 | 0 | 5.0 | 4.5M |

- **Outcomes were identical.** Both conditions found and reused the canonical Spring
  APIs (`ClassUtils.forName`, `ResolvableType.forClass`, `AnnotatedElementUtils.
  findAnnotation` — the last missed by the automated judge, confirmed by transcript
  review, symmetrically in both conditions). Zero duplicated helpers anywhere.
- **The digest was consulted** (digest_hits 1–5 in six of seven A-sessions), and
  still didn't change outcomes. In the worst case (`trim-to-null`) it tripled the
  work: 39 turns / 1.9M tokens vs 15 / 0.6M, same one-method result.
- **Condition A cost +80% tokens overall.**
- The starkest control datapoint: `find-converter-choice` B answered correctly in
  **one turn with zero file reads** — pure training-data memory of Spring.

## Why: the corpus-familiarity confound dominates

Sonnet has spring-framework substantially memorized. A control agent doesn't need a
map of a city it grew up in — it walks straight to `StringUtils` and reuses the right
API from memory. The digest can only add overhead in that regime, and at 1.5M LOC the
digest itself (563k tokens) is far past linear readability, so condition A pays
search-inside-the-map costs on top.

This run therefore does **not** test the scenario hologram was built for — private
codebases the model has never seen, where the control condition has no memory to fall
back on. It cleanly measures the opposite corner, and the answer there is: skip the
digest.

## What this run historically observed

1. The retired harness emitted complete rows and a `digest_hits` counter. That
   counter and these rows do not establish that the hardened harness is correct.
2. In this one historical matrix, the legacy digest added cost and no observed
   benefit on a large, famous, well-structured OSS corpus.
3. The duplication failure mode did not reproduce on Spring at all (n=4 tasks) —
   against a memorized, conventions-rich codebase, sonnet reuses existing APIs
   unprompted.
4. The decisive experiment is the same matrix on a private codebase where
   training-data memory can't answer for the control. That remains unrun.

## Legacy protocol caveats

- There was one repetition per cell and one model, identified by `sonnet`, a
  mutable model alias rather than a pinned model version.
- The experiment predates simple/complex tiers and capability partitions; it
  pooled task metrics.
- Reuse acceptance commands often verified only that a change occurred. Outcome
  quality depended on transcript review, and the change-only judge could miss a
  valid API outside its expected-name list.
- Navigation acceptance was not automated: those commands returned `true`, so
  correctness depended on manual transcript review.
- The 40-turn ceiling did not gate `error_max_turns`; a timed-out run could still
  be mixed into the observations.

The obsolete active task manifest was removed because it predates the hardened
terminal-success and answer-verifier contract. Any ignored local legacy archive is
outside Git and must not be inspected, summarized, copied, or cited in tracked
documentation. The numerical observations above remain only as historical context.
