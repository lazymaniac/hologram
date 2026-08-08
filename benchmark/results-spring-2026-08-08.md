# Benchmark run: spring-framework, 2026-08-08

**Setup.** Corpus: spring-projects/spring-framework @ da4b31c (1.54M LOC, 9,240 Java
files; digest = 563k tokens, 28s build). Model: sonnet, `--max-turns 40`, headless
`claude -p`. 7 tasks (4 reuse-bait, 3 navigation) × conditions A (digest +
instructions) / B (control) × 1 rep = 14 sessions, 12.6M tokens total. Detector
verdicts hand-reviewed per the runbook.

## Result: no benefit on this corpus — a real cost instead

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

## What this run establishes

1. The harness works end to end and the instrumentation is trustworthy
   (digest_hits separates "had the digest" from "used the digest").
2. **Honest negative:** on large, famous, well-structured OSS corpora, the digest
   adds cost and no measurable benefit. The README should and does say this.
3. The duplication failure mode did not reproduce on Spring at all (n=4 tasks) —
   against a memorized, conventions-rich codebase, sonnet reuses existing APIs
   unprompted.
4. The decisive experiment is the same matrix on a private corpus (e.g. private-corpus) where
   training-data memory can't answer for the control. That remains unrun.

## Caveats

n=1 per cell; one model; acceptance commands verified only "made a change" for reuse
tasks (outcome quality came from transcript review); the automated reuse judge
undercounts when the agent picks a valid API not on the expect list — review its
verdicts, as the runbook says.
