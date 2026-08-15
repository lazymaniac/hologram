# hologram benchmark

Measures agents under controlled Hologram conditions on the same tasks and
corpus. The default A/B comparison isolates the map itself; it is not the full
shipped `init` behavior. Paid runs use a headless `claude -p` session in a
throwaway local clone; metrics come from the transcript, an acceptance command,
and a map-diff duplication check. Dry runs exercise setup and evidence
persistence without invoking the provider or the acceptance shell.

## Privacy boundary

Private-corpus task files, prompts, paths, identifiers, transcripts, results,
and derived aggregates must stay local. Put private task configurations under
`benchmark/tasks/local-*.json`; put run evidence under `benchmark/results/` or
`benchmark/archive/`. Those paths are gitignored and excluded from release
artifacts. Anonymization is not sufficient permission to publish private
corpus data.

Only publish benchmark material from a corpus whose redistribution and result
publication are explicitly authorized. Review source archives, package
metadata, release notes, and generated artifacts before publishing.

## Conditions

| condition | setup | purpose |
|---|---|---|
| `A` | map only; the shipped coaching sentence is removed | isolates map content from coaching |
| `AC` | map plus the shipped coaching sentence | measures the static context produced by `build` |
| `AR` | map, coaching, and the shipped post-commit hooks | measures `init`, including live review and structured final-state counts |
| `B` | no map, coaching, or Hologram hooks | control |

The CLI defaults to `--conditions A B`. Include `AR` explicitly when evaluating
the shipped hook behavior, for example `--conditions AR B`; use
`--conditions A AC AR B` only when the experiment is designed to compare all
four interventions.

## Smoke run

Run a small, authorized task selection before a paid matrix. Real provider
runs are refused unless you explicitly acknowledge that the runner is not
host-isolated; use this flag only in a disposable, appropriately scoped host:

    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json \
        --only trim-to-null find-getbean-flow --reps 1 --dry-run \
        --results benchmark/results/smoke-dry
    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json \
        --only trim-to-null find-getbean-flow --reps 1 \
        --conditions AR B \
        --allow-unsafe-host
    .venv/bin/python benchmark/bench.py report

Read the transcripts and captured runner/acceptance outputs in
`benchmark/results/` end to end. Check that each map-bearing treatment's size is
nonzero and that every acceptance command measures the intended outcome. Each
run gets immutable experiment, pair, cell, and attempt IDs. Rerunning a cell
does not rewrite earlier evidence. `runs.jsonl` updates use a locked atomic
replacement, so a killed writer leaves either the old complete log or the new
complete log. Full output artifacts are fsynced and recorded with byte counts
and SHA-256 digests.

## Full matrix

For the checked-in seven-task A/B configuration:

    .venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json \
        --reps 3 --resume --allow-unsafe-host
    .venv/bin/python benchmark/bench.py report

`--resume` considers the newest attempt for each exact cell, but skips only a
complete task/repetition block. Every planned condition in that block must have
a terminal result from the same execution wave and resolved model; otherwise the
whole block is rerun so its treatment/control comparison remains usable. A real
cell requires a valid terminal agent result and a declared acceptance pass or
fail; an AR cell also requires a complete structured-review measurement. A
dry-run block is separately identified and resumable after its non-provider,
non-acceptance harness paths complete. In every mode, referenced evidence
artifacts must still match their recorded size and digest. Timeouts, invalid
transcripts, setup errors, unknown acceptance exit codes, missing/tampered
artifacts, incomplete AR review capture, and other infrastructure failures
remain in the append-only evidence and cause their entire block to be retried.
By default exit 0
passes and exit 1 is an observed task failure; tasks can declare other disjoint
`accept_pass_codes` and `accept_fail_codes`. Every undeclared exit is judge
infrastructure failure, not a negative task verdict.

Changes to the corpus, task or judge configuration, requested model, effort,
turn limit, map budget, tool/schema version, selected tasks/conditions/
repetitions, runner mode/version, Python/platform runtime, scheduling policy,
or order seed produce a different identity and cannot be silently reused. A
dry-run cell can therefore never satisfy a paid resume. Before a paid matrix,
the harness preflights the runner. It stops after two consecutive infrastructure
failures globally or for the same condition by default, so a persistent AR
measurement failure cannot be hidden by successful control cells. Change that threshold with
`--max-consecutive-runner-failures` and resume after fixing the cause.

Condition order starts from a seeded permutation and rotates across task/rep
blocks to counterbalance order effects. The deterministic default seed and the
planned per-pair condition order are recorded in every row. Attempts also carry
an execution-wave ID, UTC start time, and actual wave index. Treatment and B
attempts selected from different waves are listed as incomplete instead of
being silently paired. Pass `--seed N` to choose a preregistered order explicitly;
reuse the same seed when resuming.

## Reading the report

- **accept cmd** reports automatic command outcomes only; dry-run and
  `manual_only` rows are excluded. **semantic** reports only tasks whose author
  explicitly set `semantic_judge: true`; `manual pending` is separate. Every
  percentage has an eligible-observation denominator, and an empty or
  inapplicable metric is `—`, never 0%.
- **duplication** is the percentage of applicable reuse-task runs where the agent wrote a
  name-similar function instead of calling the existing one. Directional goal:
  treatment < B.
- **reads / searches / turns / tokens** describe navigation effort. Numeric
  summaries are median ± median absolute deviation (MAD), not means. Fresh
  input, cache-created input, cache-read input, legacy total input, and output
  tokens remain separate. Directional goal: a treatment should explore less
  than B without degrading the task-specific evidence.
- **matched treatment−B deltas** compare each selected treatment (`A`, `AC`, or
  `AR`) with control `B`, only for the same immutable task and repetition from
  the same execution wave and resolved provider model (when reported); positive
  numbers mean the treatment used or achieved more than B. Each delta includes
  its own `n`. Missing, infrastructure-invalid, or cross-wave partners are
  excluded and listed under incomplete pairs instead of becoming unmatched
  averages. Runner mode is shown explicitly, so dry and paid rows cannot look
  like the same observation.
- Rows are separated by model, effort, requested budget, corpus revision,
  immutable task-file revision, Hologram/harness revision, and result schema.
  Infrastructure failures appear in `infra` and are excluded from quality and
  cost summaries. Repeated compatible cells use the newest attempt in
  aggregates, while every failed attempt remains listed and retained in
  `runs.jsonl`.
- `report --anon` replaces task IDs with report-local pseudonyms and suppresses
  per-task symbol names; it is for safer local inspection, not publication
  permission. In anonymous rows, `rv` means a real review tool result was observed and
  `rv+action` means a later relevant edit and commit followed. It is an action
  proxy, not proof that the finding was resolved.
- **structured review final-state counts** apply only to paid AR rows whose
  capture/final scan completed with `status=ok` and whose runner/acceptance
  evidence is infrastructure-valid. The report shows total, eligible, and
  excluded cells plus each non-success status, so missing measurements cannot
  look like zero findings. The
  benchmark captures stable finding IDs at each post-commit review, then
  compares their deduplicated union with one cumulative review of the final
  working tree against the setup commit. `resolved` means the same ID is absent
  at the end; `persisting` means it remains; `new final` was not emitted by an
  earlier hook. The report exposes counts only. Raw result IDs are
  corpus-derived evidence and remain subject to the privacy boundary above.
- Small repetition counts are directional, not statistically conclusive.
  Inspect per-task results and replace tasks that saturate.

## Honest limitations

- Agent sessions run with `--dangerously-skip-permissions`. The throwaway clone
  and removed origin do **not** isolate the host filesystem, credentials,
  processes, or network. Real runs therefore require the explicit
  `--allow-unsafe-host` acknowledgement. The acknowledgement is recorded for
  provenance; it adds no protection. Use a disposable container or VM with
  scoped credentials for untrusted prompts or models.
- Dry runs do not invoke the provider or acceptance shell, but they still clone
  and inspect the configured corpus. They are a harness check, not a sandbox;
  use only trusted task files and corpora.
- The duplication detector is heuristic. Review its verdicts manually before
  relying on them.
- Structured review resolution is identity-based, not proof that the underlying
  issue was fixed correctly. Renames and moves may replace one ID with another,
  so `new final` must be considered alongside `resolved`. The harness makes no
  per-finding attempt claim; `rv+action` remains a separate global proxy.
- The private AR capture ledger is experiment evidence, not a tamper-proof
  attestation. Its path is necessarily present in the throwaway clone's hook,
  and an unrestricted agent could forge or remove records. Run adversarial
  experiments inside an independently isolated host and treat the ledger as a
  measurement aid rather than a security boundary.
- `accept_cmd` is structural acceptance evidence unless the task author opts in
  with `semantic_judge: true`. The checked configuration intentionally makes no
  automatic semantic claim: its change tasks check for a diff and its
  navigation tasks are `manual_only`. Inspect those outputs. Optional `judge`
  metadata and the complete judge configuration are hashed into provenance.
- A mutable requested model alias can change behind the same name. The harness
  records the CLI version and the resolved model when the transcript exposes
  it, and refuses to pair different resolved models, but it cannot resolve a
  future alias before deciding whether to resume. Pin immutable model versions
  for longitudinal experiments.
- Corpora the model has memorized measure a different effect because the
  control may navigate from training memory.
- The harness rejects unknown configuration keys and validates task IDs,
  conditions, regexes, command templates, exit-code protocols, budgets, turn
  limits, selections, and repetitions before the first provider call. It
  cannot prove that an author-declared semantic judge is meaningful.
- The benchmark does not grant publication rights. Corpus owners must approve
  both the experiment and any result disclosure.
