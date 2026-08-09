# Hologram v2 Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove Hologram v2's static accuracy and determinism against a frozen public corpus, then provide a trustworthy simple/complex B/C benchmark harness without running any paid benchmark sessions during implementation.

**Architecture:** Build two independent validation gates. The static gate loads source-grounded JSONL truth for a 748-file public census, a 103-file pinned gold sample, and 33 synthetic advertised-language fixtures; it exports canonical facts from the v2 project model, computes frozen metrics, and verifies three-run byte determinism. The benchmark gate strictly validates public or external-private manifests, runs balanced control (B) and embedded-map (C) pairs in isolated workspaces, accepts only terminally successful and verifier-passing runs, and partitions public reports while reducing private reports to numeric condition totals.

**Tech Stack:** Python 3.11+, `unittest`, frozen dataclasses, TOML via `tomllib`, JSON Lines, Git CLI, Hologram v2 canonical project/render IR, Claude Code CLI 2.1.224, exact model `claude-sonnet-5`.

---

## Prerequisites and non-negotiable boundaries

- Complete the foundation, extractor, analysis/render, and delivery plans first. This plan consumes foundation `ProjectConfig`, pipeline `BuildSnapshot`, extractor `ProjectIR`/`ResolutionResult`, and analysis/render `AnalyzedProject`/`RenderIR`; it does not add extractor heuristics to make scores pass.
- Keep public corpus source checkouts outside this repository. Track only corpus metadata, relative source paths, source-grounded facts, exclusions, synthetic fixtures, verifier assets, and public task manifests.
- Keep every new v2 private manifest, repository identity, revision, path,
  prompt, task ID, symbol, challenge, hidden test, transcript, diff, answer, and
  verifier log outside the Hologram worktree. The preexisting ignored
  `benchmark/archive/**` is legacy local state: never inspect, copy, summarize,
  rename, or delete it.
- Do not run a paid Claude session while implementing this plan. `--dry-run` and injected fake runners are the only implementation-time benchmark executions.
- A metric failure is evidence to fix an earlier v2 phase or correct demonstrably wrong gold data. Never lower a threshold in this plan's implementation.

## File map

### Static validation

- Create `validation/__init__.py` — validation package marker.
- Create `validation/schema.py` — strict corpus, census, gold-fact, and exclusion records.
- Create `validation/corpus.py` — corpus resolution, revision verification, census generation, and deterministic sample selection.
- Create `validation/observe.py` — convert canonical v2 project/render IR into comparable facts.
- Create `validation/metrics.py` — matching, metric calculation, thresholds, and failure formatting.
- Create `validation/run.py` — static validation and determinism CLI.
- Create `validation/corpora.toml` — five public corpus sources, full revisions, path environment variables, and sample quotas.
- Create `validation/gold/census.jsonl` — exactly 748 relative candidate-file records.
- Create `validation/gold/sample.jsonl` — exactly 103 selected-file records.
- Create `validation/gold/facts/*.jsonl` — source-grounded positive facts and explicit planted precision negatives.
- Create `validation/gold/exclusions/*.jsonl` — narrowly scoped unsupported or genuinely ambiguous facts.
- Create `validation/gold/README.md` — curation and review protocol.
- Create `validation/fixtures/advertised/**` — exactly 33 synthetic source files.
- Create `tests/test_validation_schema.py`, `tests/test_validation_corpus.py`, `tests/test_validation_metrics.py`, and `tests/test_validation_determinism.py`.

### Tiered benchmark

- Create `benchmark/schema.py` — strict benchmark manifest records and loader.
- Create `benchmark/corpus.py` — pinned public corpus preparation and workspace-asset linking.
- Create `benchmark/transcript.py` — terminal-result and usage parsing.
- Create `benchmark/reporting.py` — public partitioned and private redacted reports.
- Create `benchmark/verifiers/__init__.py` and `benchmark/verifiers/common.py` — verifier protocol and shared checks.
- Create `benchmark/verifiers/codecompanion.py` and `benchmark/verifiers/rubrics/codecompanion.json` — four public task verifiers.
- Create `benchmark/challenges/codecompanion-audit.patch` — frozen public audit challenge.
- Create `benchmark/tasks/codecompanion.json` — four public CodeCompanion tasks.
- Delete `benchmark/tasks/spring.json` — remove the obsolete active pre-tier manifest while retaining its qualified aggregate report.
- Modify `benchmark/bench.py` — CLI, scheduling, workspaces, runner, artifact writing, and compatibility re-exports.
- Modify `tests/test_bench.py` and create `tests/test_bench_schema.py`, `tests/test_bench_reporting.py`, `tests/test_bench_verifiers.py`, and `tests/test_bench_privacy.py`.
- Modify `benchmark/README.md`, `benchmark/results-spring-2026-08-08.md`, and `README.md`.

---

### Task 1: Freeze the static-validation record contracts

**Files:**
- Create: `validation/__init__.py`
- Create: `validation/schema.py`
- Test: `tests/test_validation_schema.py`

- [ ] **Step 1: Write the failing schema tests**

Define tests for the wished-for API:

````python
import tempfile
import unittest
from pathlib import Path

from validation.schema import (
    CensusRecord,
    CorpusRegistry,
    CorpusSpec,
    Exclusion,
    GoldFact,
    GoldSample,
    load_jsonl,
)


class ValidationSchemaTest(unittest.TestCase):
    def test_records_reject_absolute_paths_and_short_revisions(self):
        with self.assertRaises(ValueError):
            CensusRecord(
                corpus="sample",
                revision="abc123",
                path="/absolute/source.py",
                language="python",
            )

    def test_jsonl_loader_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.jsonl"
            path.write_text('{"id":"x","unknown":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown field"):
                load_jsonl(path, GoldFact)

    def test_gold_fact_has_stable_source_anchor(self):
        fact = GoldFact(
            id="sample:src/a.py:4:declaration:d7e23bcc6f819c56",
            corpus="sample",
            revision="a" * 40,
            path="src/a.py",
            line=4,
            language="python",
            category="declaration",
            subject='["python","src/a.py",[],"fn","f","()"]',
            value={"name": "f"},
            expected=True,
        )
        self.assertEqual(fact.path, "src/a.py")
````

Also test duplicate IDs, unsorted JSONL, malformed JSON, blank strings, line numbers below one, noncanonical language names, and an exclusion without a reason.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_schema -v
````

Expected: FAIL with `ModuleNotFoundError: No module named 'validation'`.

- [ ] **Step 3: Implement the immutable record API**

Implement these exact frozen dataclasses:

````python
@dataclass(frozen=True)
class CorpusSpec:
    name: str
    url: str
    revision: str
    path_env: str
    sample_files: int


@dataclass(frozen=True)
class CorpusRegistry:
    corpora: tuple[CorpusSpec, ...]
    expected_census_files: int
    expected_ordinary_yaml_exclusions: int
    outside_candidate_extensions: tuple[str, ...]


@dataclass(frozen=True)
class CensusRecord:
    corpus: str
    revision: str
    path: str
    language: str


@dataclass(frozen=True)
class GoldSample:
    corpus: str
    revision: str
    path: str
    language: str
    rank: str


@dataclass(frozen=True)
class GoldFact:
    id: str
    corpus: str
    revision: str
    path: str
    line: int
    language: str
    category: Literal[
        "declaration", "kind", "container", "visibility", "signature",
        "relation", "call", "call_order", "strong_x0",
        "zero_classification", "approximate",
    ]
    subject: str
    value: Mapping[str, object]
    expected: bool


@dataclass(frozen=True)
class Exclusion:
    id: str
    corpus: str
    revision: str
    path: str
    line: int | None
    language: str
    scope: str
    reason: str


T = TypeVar("T")


def load_jsonl(path: Path, record_type: type[T]) -> tuple[T, ...]: ...
def write_jsonl(path: Path, records: Iterable[object]) -> None: ...
````

Use strict allowed-field sets, POSIX relative paths, full 40-hex revisions, canonical UTF-8, and `json.dumps(..., sort_keys=True, separators=(",", ":"))`. Require unique sorted `id` values for `GoldFact`/`Exclusion`, and unique `(corpus, path)` rows sorted by that key for `CensusRecord`/`GoldSample`. Recursively freeze loaded `value` mappings/lists so callers cannot mutate a frozen record through nested containers. A record error must include `path:line`.

- [ ] **Step 4: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_schema -v
````

Expected: all schema tests PASS.

- [ ] **Step 5: Commit the record contracts**

````bash
git add validation/__init__.py validation/schema.py tests/test_validation_schema.py
git commit -m "feat(validation): define static gold schema"
````

---

### Task 2: Pin the five public corpora and freeze the 748/103 file sets

**Files:**
- Create: `validation/corpus.py`
- Create: `validation/corpora.toml`
- Create: `validation/gold/census.jsonl`
- Create: `validation/gold/sample.jsonl`
- Test: `tests/test_validation_corpus.py`

- [ ] **Step 1: Write failing corpus and count tests**

Tests must call:

````python
def load_registry(path: Path) -> CorpusRegistry: ...
def resolve_checkout(spec: CorpusSpec, environ: Mapping[str, str]) -> Path: ...
def verify_checkout(spec: CorpusSpec, checkout: Path) -> None: ...
def build_census(registry: CorpusRegistry, roots: Mapping[str, Path]) -> tuple[CensusRecord, ...]: ...
def select_gold_sample(
    census: Sequence[CensusRecord],
    registry: CorpusRegistry,
    *,
    seed: int = 20260809,
) -> tuple[GoldSample, ...]: ...
````

Assert:

````python
self.assertEqual(len(census), 748)
self.assertEqual(len(sample), 103)
self.assertEqual(
    Counter(row.corpus for row in sample),
    {
        "hologram": 9,
        "codecompanion": 24,
        "cypress": 38,
        "kafka-streams-examples": 26,
        "jdb": 6,
    },
)
self.assertEqual(registry.expected_ordinary_yaml_exclusions, 3)
self.assertEqual(registry.outside_candidate_extensions, (".scala", ".sh"))
````

Add focused temporary-repository tests proving normalized remote-URL equality, full-revision equality, dirty-checkout rejection, missing environment-variable errors, deterministic ordering, stable sample selection regardless of census input order, exactly three ordinary-YAML candidate exclusions, and Scala/Bash remaining outside the candidate census while still being declared in the registry.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_corpus -v
````

Expected: FAIL because `validation.corpus` and the frozen data files do not exist.

- [ ] **Step 3: Implement corpus resolution and deterministic sampling**

Use `git rev-parse HEAD` and `git status --porcelain` for checkout verification. Build the census from v2 scanner candidates, recording only corpus key, full revision, relative POSIX path, and language. Rank sample candidates by:

````python
rank = hashlib.sha256(
    f"{seed}\0{record.corpus}\0{record.path}".encode("utf-8")
).hexdigest()
````

Within each corpus, sort by `(rank, path)` and take the configured quota. Never use file size, prompt length, or current extractor success when selecting the sample.

- [ ] **Step 4: Freeze full public revisions and file records**

In `validation/corpora.toml`, add exactly five records named `hologram`, `codecompanion`, `cypress`, `kafka-streams-examples`, and `jdb`. Each record must contain the checkout's normalized HTTPS remote URL, one `HOLOGRAM_VALIDATION_*` path environment variable, and the quota above. Freeze these reviewed public revisions:

````toml
[census]
expected_files = 748
expected_ordinary_yaml_exclusions = 3
outside_candidate_extensions = [".scala", ".sh"]

[[corpora]]
name = "hologram"
url = "https://github.com/lazymaniac/hologram.git"
revision = "6604cfac743466f56bf4b7b4ea68ce6dae3c4d18"
path_env = "HOLOGRAM_VALIDATION_HOLOGRAM"
sample_files = 9

[[corpora]]
name = "codecompanion"
url = "https://github.com/olimorris/codecompanion.nvim.git"
revision = "2b959b2bf5fdb13e3b333c078ba549996e477b7c"
path_env = "HOLOGRAM_VALIDATION_CODECOMPANION"
sample_files = 24

[[corpora]]
name = "cypress"
url = "https://github.com/cypress-io/cypress-realworld-app.git"
revision = "c2d37e6ff38232a386525265e8ef6e3c6a4d62a9"
path_env = "HOLOGRAM_VALIDATION_CYPRESS"
sample_files = 38

[[corpora]]
name = "kafka-streams-examples"
url = "https://github.com/confluentinc/kafka-streams-examples.git"
revision = "9df6d342cc754926673d2ed6c41952616f3ad879"
path_env = "HOLOGRAM_VALIDATION_KAFKA_STREAMS_EXAMPLES"
sample_files = 26

[[corpora]]
name = "jdb"
url = "https://github.com/brunoborges/jdb-agentic-debugger.git"
revision = "213939fcb92ccb910ff1d93a4a1a07631b34b779"
path_env = "HOLOGRAM_VALIDATION_JDB"
sample_files = 6
````

The Hologram record is deliberately frozen at the pre-v2 base revision `6604cfac743466f56bf4b7b4ea68ce6dae3c4d18`. Do not replace it with the planning HEAD or the eventual implementation commit: either would change the 748-file census and invalidate the reviewed 103-file sample.

For every external checkout, normalize `git config --get remote.origin.url` to HTTPS and verify the revision with `git -C "$CORPUS_PATH" rev-parse HEAD`. Do not infer repository URLs from corpus display names.

Generate and review the data:

````bash
.venv/bin/python -m validation.corpus freeze \
  --registry validation/corpora.toml \
  --census validation/gold/census.jsonl \
  --sample validation/gold/sample.jsonl \
  --seed 20260809
````

Expected: the command prints `census=748 sample=103 ordinary_yaml_exclusions=3 outside_candidates=.scala,.sh` and the five exact sample subtotals. The three ordinary YAML files remain candidate census records and receive explicit `ordinary_yaml_not_helm` exclusions. Scala and Bash are recorded in registry policy but never enter the 748 candidate rows. If the census is not 748, stop and reconcile scanner policy or corpus revisions; do not add or drop files to force the count.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_corpus -v
````

Expected: all corpus tests PASS and both JSONL files round-trip byte-for-byte.

- [ ] **Step 6: Commit the frozen corpus inventory**

````bash
git add validation/corpus.py validation/corpora.toml validation/gold/census.jsonl validation/gold/sample.jsonl tests/test_validation_corpus.py
git commit -m "test(validation): freeze public corpus census"
````

---

### Task 3: Curate source-grounded facts and exclusions for all 103 gold files

**Files:**
- Create: `validation/gold/README.md`
- Create: `validation/gold/facts/hologram.jsonl`
- Create: `validation/gold/facts/codecompanion.jsonl`
- Create: `validation/gold/facts/cypress.jsonl`
- Create: `validation/gold/facts/kafka-streams-examples.jsonl`
- Create: `validation/gold/facts/jdb.jsonl`
- Create: `validation/gold/exclusions/hologram.jsonl`
- Create: `validation/gold/exclusions/codecompanion.jsonl`
- Create: `validation/gold/exclusions/cypress.jsonl`
- Create: `validation/gold/exclusions/kafka-streams-examples.jsonl`
- Create: `validation/gold/exclusions/jdb.jsonl`
- Modify: `tests/test_validation_corpus.py`

- [ ] **Step 1: Write the failing gold-coverage test**

For every sample row, require at least one positive declaration fact or one file-level exclusion. Require every fact to match the sample's corpus, revision, path, and language. Require source lines to exist and the anchored identifier to occur on the declared line. Require every supported declaration to have explicit positive kind, container, visibility, and signature facts; every omitted candidate needs an exclusion. Every declaration eligible for a strong-zero decision must also have `strong_x0` with `expected=true` or `expected=false`, or a narrowly justified dynamic/reachability exclusion after corpus-wide source review. Outside the synthetic closed-world fixture, forbid negative facts for any other category. Across the census/gold exclusions, require exactly three ordinary YAML records with scope `file` and reason `ordinary_yaml_not_helm`. Reject any Scala or Bash path in `census.jsonl` or `sample.jsonl`.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_corpus.ValidationGoldCoverageTest -v
````

Expected: FAIL listing all 103 sample files as uncovered.

- [ ] **Step 3: Document and apply the curation protocol**

`validation/gold/README.md` must require:

1. A curator reads the pinned source, not Hologram output.
2. Facts describe direct syntax and source-grounded relations only.
3. Every sampled callable records its complete direct-call list, including an empty list; calls preserve lexical order and duplicates.
4. Dynamic or ambiguous calls are exclusions, never guessed targets.
5. Every sampled declaration records the complete applicable non-call relation set, including the absence of relations through the declaration's closed reviewed scope.
6. Generated/vendor files are recorded as file-level exclusions if present in the frozen census policy.
7. A second reviewer checks source anchors, closed scopes, and exclusions.
8. Corrections change gold only when the source proves the old record wrong; score pressure is not evidence.

- [ ] **Step 4: Add all positive and excluded records**

Use stable IDs `corpus:path:line:category:<fact-hash>`, where `fact-hash` is the first 16 lowercase hex characters of SHA-256 over canonical compact JSON containing `subject`, `value`, and `expected`. This keeps repeated calls on one line distinct without making line a symbol identity. Represent signatures structurally:

````json
{"text":"handle(Request): Result","params":["Request"],"returns":"Result","raises":[]}
````

Represent ordered calls as:

````json
{"targets":[["java","src/Type.java",[],"method","first","()"],["java","src/Type.java",[],"method","second","()"],["java","src/Type.java",[],"method","first","()"]]}
````

Encode every symbol subject as compact canonical JSON for the frozen `SymbolId` array `[language,file,container_path,kind,name,signature_key]`; never put line numbers in identity. Freeze these value shapes:

| Category | `value` |
|---|---|
| `declaration` | `{"name": <string>}` |
| `kind` | `{"kind": <SymbolKind value>}` |
| `container` | `{"container": [<string>, ...]}` |
| `visibility` | `{"visibility": <Visibility value>}` |
| `signature` | `{"text": <canonical signature>, "params": [...], "returns": <string-or-null>, "raises": [...]}` |
| `relation` | `{"kind": <relation kind>, "target": {"symbol": <SymbolId array>}}` or `{"kind": "dependency", "target": {"external": <module string>}}` |
| `call` | `{"target": <SymbolId array>, "ordinal": <zero-based integer>}` |
| `call_order` | `{"targets": [<SymbolId array>, ...]}` |
| `strong_x0` | `{"classification": "strong"}` |
| `approximate` | `{"peer": <SymbolId array>}` with the lower SymbolId as subject |

Use the canonical non-call relation kinds `super`, `permit`, `component`, `reexport`, and `dependency`; normalize language-specific extends/implements syntax to `super`. Treat construction as an ordered call, not a non-call edge. Exclude ambiguous/unresolved calls from positive call gold, and keep uncertain runtime/configuration reachability in narrowly scoped exclusions.

For each reviewed callable, emit one `call_order` fact even when `targets` is empty, plus one `call` fact per target occurrence with its lexical ordinal. Call precision/recall compares the occurrence multiset without ordinals; call-order accuracy compares the complete ordered target array.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_corpus.ValidationGoldCoverageTest -v
````

Expected: PASS with `files=103 uncovered=0 invalid_anchors=0`.

- [ ] **Step 6: Commit the reviewed gold sample**

````bash
git add validation/gold tests/test_validation_corpus.py
git commit -m "test(validation): add source-grounded public gold"
````

---

### Task 4: Add 33 planted advertised-language fixtures

**Files:**
- Create: `validation/fixtures/advertised/**`
- Create: `validation/gold/facts/synthetic.jsonl`
- Create: `validation/gold/exclusions/synthetic.jsonl`
- Modify: `tests/test_validation_corpus.py`

- [ ] **Step 1: Write the failing synthetic matrix test**

Assert exactly these 33 files:

````text
java/Types.java
java/Calls.java
csharp/Types.cs
csharp/Calls.cs
typescript/types.ts
typescript/calls.ts
javascript/types.js
javascript/calls.js
tsx/Component.tsx
tsx/Calls.tsx
jsx/Component.jsx
jsx/Calls.jsx
python/types.py
python/calls.py
kotlin/Types.kt
kotlin/Calls.kt
go/types.go
go/calls.go
rust/types.rs
rust/calls.rs
c/types.c
c/types.h
cpp/types.cpp
cpp/types.hpp
lua/types.lua
lua/calls.lua
vue/Component.vue
vue/Calls.vue
svelte/Component.svelte
svelte/Calls.svelte
html/page.html
helm/values.yaml
helm/templates/_helpers.tpl
````

Require at least one declaration/signature fact for all 16 frozen `Language`
values, a distinct JSX syntax case classified as `Language.TSX`, planted inheritance
or type relation wherever the language supports it, ordered direct calls for
Java/Python/TypeScript/TSX/Lua, one true strong-`×0` declaration, one public
zero-use `×0?` surface, one dynamic-reachability `×0?` decoy, one
same-file-used private `NONE` case, one arbitrary-string/comment-only private
`STRONG` case, one active exact helper clone, and one
similar-but-not-duplicate negative.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_corpus.SyntheticFixtureMatrixTest -v
````

Expected: FAIL with `expected 33 advertised fixtures, found 0`.

- [ ] **Step 3: Create the planted sources**

Use two-file call pairs so cross-file resolution is measurable. Keep each file below 40 lines. Plant exact names prefixed `Gold`, such as `GoldUnusedStrong`, `goldExactClone`, `goldDynamicCallback`, and `goldOrderedCaller`, so the test can prove the intended cases exist without relying on Hologram output. HTML supplies ID/custom-element facts; Helm supplies values and template-definition facts.

- [ ] **Step 4: Add independent synthetic facts and exclusions**

Record every planted case in `synthetic.jsonl` with `corpus="synthetic"` and a
fixed all-zero 40-character revision. Give every synthetic production
declaration an explicit `zero_classification` value of `none`, `strong`, or
`uncertain`, making that category closed-world. Record the dynamic callback as
`strong_x0` with `expected=false` and the similar-but-not-duplicate pair as
`approximate` with `expected=false`; do not hide either planted precision decoy
in exclusions. The exact clone and strong unused declaration use
`expected=true`. Reserve `synthetic` exclusions for genuinely ambiguous or
unsupported syntax.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_corpus.SyntheticFixtureMatrixTest -v
````

Expected: PASS with `fixtures=33 languages=16 syntax_modes=17 planted_cases=all`.

- [ ] **Step 6: Commit the synthetic truth set**

````bash
git add validation/fixtures validation/gold/facts/synthetic.jsonl validation/gold/exclusions/synthetic.jsonl tests/test_validation_corpus.py
git commit -m "test(validation): plant advertised-language truth"
````

---

### Task 5: Export comparable facts from the canonical v2 model

**Files:**
- Create: `validation/observe.py`
- Test: `tests/test_validation_metrics.py`

- [ ] **Step 1: Write failing observed-fact tests**

Freeze this API:

````python
@dataclass(frozen=True)
class ObservedFact:
    category: str
    subject: str
    value: Mapping[str, object]
    corpus: str
    path: str
    line: int
    language: str


def observe_project(
    *,
    corpus: str,
    root: Path,
    config: ProjectConfig,
) -> tuple[ObservedFact, ...]: ...


def observe_rendered_map(
    *,
    corpus: str,
    rendered: str,
) -> tuple[ObservedFact, ...]: ...
````

Test that ordered calls remain ordered and duplicated, ambiguity remains unresolved, declaration provenance is exact, exclusions can remove only their declared scope, and facts decoded from the rendered map match facts from the canonical render IR. Stub `pipeline.build_project()` with `complete=False` and require `pipeline.IncompleteBuildError` before any fact is emitted.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_metrics.ObservedFactTest -v
````

Expected: FAIL because `validation.observe` does not exist.

- [ ] **Step 3: Implement a read-only adapter over frozen v2 APIs**

Use the phase APIs without conflating their model types:

````python
snapshot = pipeline.build_project(root, config)
snapshot.require_complete()
analyzed = analysis.analyze_project(
    snapshot.project,
    snapshot.resolution,
    hot_threshold=config.hot_threshold,
)
render_ir = render.project_render_ir(
    analyzed,
    state=snapshot.state.value,
    hot_threshold=config.hot_threshold,
)
text = render.render_project(render_ir)
decoded = render.decode_render(text)
````

`pipeline.build_project(root: Path, config: ProjectConfig) -> BuildSnapshot` must be called exactly once per validation build. Call the frozen `BuildSnapshot.require_complete()` before analysis; it raises `pipeline.IncompleteBuildError` with extraction diagnostics when `complete` is false. Map that exception to CLI exit 3 and never score a partial project. Emit declarations and their kind/container/visibility/signature facts, non-call relations, direct ordered calls, every zero classification, strong `×0` advisories, and approximate groups from `analyzed`. Obtain rendered-map facts from `decoded`; do not parse Markdown with validation-specific regular expressions. Encode `text` as UTF-8 only at byte-comparison or file-write boundaries.

Sort observed facts only at the final serialization boundary by `(corpus, path, line, category, subject, canonical_json(value))`. Preserve call-list order inside `value`.

- [ ] **Step 4: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_metrics.ObservedFactTest -v
````

Expected: all observed-fact and model/render equivalence tests PASS.

- [ ] **Step 5: Commit the observation adapter**

````bash
git add validation/observe.py tests/test_validation_metrics.py
git commit -m "feat(validation): export canonical observed facts"
````

---

### Task 6: Implement the frozen accuracy metrics and thresholds

**Files:**
- Create: `validation/metrics.py`
- Modify: `tests/test_validation_metrics.py`

- [ ] **Step 1: Write failing metric-formula tests**

Freeze:

````python
@dataclass(frozen=True)
class Metric:
    name: str
    numerator: int
    denominator: int
    value: float
    minimum: float
    passed: bool


@dataclass(frozen=True)
class StaticReport:
    metrics: tuple[Metric, ...]
    failures: tuple[str, ...]


def evaluate_static(
    gold: Sequence[GoldFact],
    exclusions: Sequence[Exclusion],
    observed: Sequence[ObservedFact],
) -> StaticReport: ...


def require_thresholds(report: StaticReport) -> None: ...
````

Use hand-constructed true-positive, false-positive, and false-negative sets to
prove micro precision/recall formulas, per-language grouping, exclusion scoping,
explicit `expected=false` precision decoys, exact structured signature matching,
ordered-call comparison, non-vacuous advisory precision, planted-unused recall,
closed-world zero classification, and planted-duplicate recall. Reject gold in
which an exclusion overlaps an explicit positive or negative fact; exclusions
must never suppress a planted precision decoy.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_metrics.StaticMetricTest -v
````

Expected: FAIL because `evaluate_static` is missing.

- [ ] **Step 3: Implement exact matching and frozen gates**

Use these immutable gates:

| Metric | Gate |
|---|---:|
| Declaration micro precision | ≥99% |
| Declaration micro recall | ≥97% |
| Declaration precision, each of Java/Python/TypeScript/TSX | ≥97% |
| Declaration recall, each of Java/Python/TypeScript/TSX | ≥95% |
| Kind accuracy on matched declarations | ≥99% |
| Container accuracy on matched declarations | ≥99% |
| Visibility accuracy on matched declarations | ≥99% |
| Signature exact accuracy overall | ≥95% |
| Signature exact accuracy per language with gold signatures | ≥90% |
| Non-call relation exact accuracy | ≥97% |
| Call precision, Java/Python/TypeScript/TSX | ≥95% |
| Call recall, Java/Python/TypeScript/TSX | ≥85% |
| Call precision, Lua | ≥90% |
| Call recall, Lua | ≥70% |
| Exact lexical call-order accuracy | ≥85% |
| Strong `×0` precision | 100% |
| Strong `×0` recall over planted unused declarations | 100% |
| Zero-marker classification accuracy on the closed synthetic set | 100% |
| Approximate-group precision | 100% |
| Approximate-group recall over planted duplicates | ≥80% |

Primary-language call metrics are per language, not pooled. `kind`, `container`, `visibility`, and `signature` use exact-match accuracy over their corresponding gold facts. Call-order accuracy compares exact arrays only for reviewed callers with at least two target occurrences, so empty/singleton lists cannot inflate the gate. Non-call relation exact set accuracy is `TP / (TP + FP + FN)`, so both invented and missed edges count against the 97% gate. Strong `×0` precision fails when its observed denominator is zero; planted recall requires every `expected=true` strong-unused fact. Zero classification compares the exact `none`/`strong`/`uncertain` value for every synthetic production declaration, so silence, overmarking, and dynamic false positives all fail. Evaluate both map-decoded approximate precision and recall only against the synthetic corpus's closed-world planted positive and negative pairs; public natural similarities are not exhaustively labeled and never enter that denominator. The planted fixtures guarantee a nonzero approximate denominator and recall uses only planted positive duplicates.

- [ ] **Step 4: Make failure output actionable**

For each failed metric, print numerator, denominator, actual percentage, threshold, and up to 20 stable fact IDs split into false positives and false negatives. Never print absolute corpus paths.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_metrics -v
````

Expected: all formula, grouping, exclusion, and threshold tests PASS.

- [ ] **Step 6: Commit the accuracy gate**

````bash
git add validation/metrics.py tests/test_validation_metrics.py
git commit -m "feat(validation): enforce static accuracy gates"
````

---

### Task 7: Add the public static-validation CLI and three-run byte determinism

**Files:**
- Create: `validation/run.py`
- Create: `tests/test_validation_determinism.py`

- [ ] **Step 1: Write failing determinism and CLI tests**

Freeze:

````python
def validate_corpora(
    registry: Path,
    *,
    environ: Mapping[str, str],
    runs: int = 3,
) -> StaticReport: ...


def assert_byte_determinism(
    root: Path,
    config: ProjectConfig,
    *,
    runs: int = 3,
) -> None: ...


def main(argv: Sequence[str] | None = None) -> int: ...
````

The test injects reversed scanner enumeration on one run and proves canonical output remains identical. A deliberately nondeterministic renderer must fail with the first differing byte offset and SHA-256 values.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_validation_determinism -v
````

Expected: FAIL because `validation.run` does not exist.

- [ ] **Step 3: Implement the three-run gate**

For every public corpus and the synthetic root:

1. Verify full revision and cleanliness.
2. Construct exactly one explicit foundation config with `dataclasses.replace(hologram.config.default_config(), agents=(), output="PROJECT_DIGEST.md")`; empty languages retain foundation auto-detection. Do not call `load_config()` for corpora that intentionally have no `.hologram.toml`, and do not create validation-only parser defaults.
3. Build the canonical project and rendered bytes three times in fresh temporary directories.
4. Set `LC_ALL=C`, `TZ=UTC`, and a fixed `SOURCE_DATE_EPOCH` for each run.
5. Compare project-fact JSONL bytes and rendered map bytes across all three runs.
6. Evaluate gold facts only after byte equality succeeds.

Write optional machine output only to the caller's `--output` path. Default to stdout; never write generated reports beneath `validation/`.

- [ ] **Step 4: Run the synthetic GREEN gate**

Run:

````bash
.venv/bin/python -m validation.run \
  --synthetic validation/fixtures/advertised \
  --runs 3 \
  --output /tmp/hologram-v2-synthetic-validation.json
````

Expected: exit 0, `byte_equal=true`, `runs=3`, and every applicable synthetic threshold passes.

- [ ] **Step 5: Run the full public GREEN gate**

Run with all five `HOLOGRAM_VALIDATION_*` variables set:

````bash
.venv/bin/python -m validation.run \
  --registry validation/corpora.toml \
  --census validation/gold/census.jsonl \
  --sample validation/gold/sample.jsonl \
  --facts validation/gold/facts \
  --exclusions validation/gold/exclusions \
  --runs 3 \
  --output /tmp/hologram-v2-static-validation.json
````

Expected: exit 0; `census=748`, `sample=103`, `synthetic_files=33`; every frozen threshold passes; all map and fact outputs are byte-identical across three runs.

- [ ] **Step 6: Commit the static validation entrypoint**

````bash
git add validation/run.py tests/test_validation_determinism.py
git commit -m "feat(validation): add deterministic static gate"
````

---

### Task 8: Introduce the strict tiered benchmark manifest

**Files:**
- Create: `benchmark/__init__.py`
- Create: `benchmark/schema.py`
- Create: `tests/test_bench_schema.py`
- Delete: `benchmark/tasks/spring.json`
- Modify: `benchmark/bench.py`
- Modify: `tests/test_bench.py`

- [ ] **Step 1: Write failing manifest tests**

Freeze these records:

````python
@dataclass(frozen=True)
class BenchmarkCorpus:
    name: str
    visibility: Literal["public", "private"]
    url: str | None
    revision: str
    path_env: str
    bootstrap_cmd: str | None = None
    workspace_assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Challenge:
    patch: Path
    sha256: str


@dataclass(frozen=True)
class Task:
    id: str
    tier: Literal["simple", "complex"]
    capability: Literal["orientation", "planning", "implementation", "audit"]
    kind: Literal["navigate", "reuse"]
    visibility: Literal["public", "private"]
    prompt: str
    accept_cmd: str
    expect_reuse: tuple[str, ...] = ()
    challenge: Challenge | None = None


@dataclass(frozen=True)
class Config:
    schema_version: int
    corpus: BenchmarkCorpus
    tasks: tuple[Task, ...]
    model: str
    claude_code_version: str
    max_turns: int
    conditions: tuple[Literal["B", "C"], ...]
    reps: int
    seed: int


def load_tasks(
    path: Path,
    *,
    corpus_override: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config: ...
````

Tests must reject unknown fields, duplicate/unsafe IDs, missing tiers, invalid capability/kind pairs, mixed visibility, blank prompts, no-op verifiers, navigation commands missing `{answer}` or `{ws}`, reuse commands missing `{ws}` or expected reuse, asymmetric conditions, reps other than one, mutable short aliases, and invalid turn limits. Assert the active `benchmark/tasks/` directory contains no pre-tier manifest after migration.

Require the exact reproducibility string `claude-sonnet-5`. Do not impose a date-suffix regex: this full non-alias name is the frozen model for the suite. Reject every other value, including known aliases such as `sonnet`, `opus`, `haiku`, `default`, `latest`, and names ending `-latest`.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_schema -v
````

Expected: FAIL because `benchmark.schema` does not exist and the current loader accepts `sonnet` and `true`.

- [ ] **Step 3: Implement strict loading and compatibility re-exports**

Require:

````json
{
  "schema_version": 2,
  "model": "claude-sonnet-5",
  "claude_code_version": "2.1.224",
  "max_turns": 40,
  "conditions": ["B", "C"],
  "reps": 1
}
````

`implementation` maps only to `reuse`; `orientation`, `planning`, and `audit` map only to `navigate`. Resolve public corpus paths from `--corpus` or `path_env`. Resolve challenge paths relative to the manifest. Make `benchmark` an explicit package. Re-export `Task`, `Config`, and `load_tasks` from `bench.py` so existing `import bench` callers have one migration point; select relative imports when `__package__` is set and local sibling imports when `benchmark/bench.py` runs directly, without catching unrelated import failures.

Treat `max_turns=40` only as a safety and reproducibility turn limit. Token limits, cost limits, and budgeting are out of scope.

Delete `benchmark/tasks/spring.json` instead of translating it: it is a pre-tier manifest with a mutable alias and no-op navigation verifier. Preserve its numeric public history only in the explicitly qualified aggregate report updated in Task 15.

- [ ] **Step 4: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_schema tests.test_bench.TaskLoaderTest -v
````

Expected: all strict and migrated loader tests PASS.

- [ ] **Step 5: Commit the tiered schema**

````bash
git add benchmark/__init__.py benchmark/schema.py benchmark/bench.py tests/test_bench.py tests/test_bench_schema.py
git add -u benchmark/tasks/spring.json
git commit -m "feat(bench): validate tiered manifests"
````

---

### Task 9: Make terminal completion and verifier success jointly mandatory

**Files:**
- Create: `benchmark/transcript.py`
- Modify: `benchmark/bench.py`
- Modify: `tests/test_bench.py`

- [ ] **Step 1: Write failing transcript and max-turn regressions**

Freeze:

````python
@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


@dataclass(frozen=True)
class TranscriptSummary:
    terminal_status: str
    terminal_count: int
    is_error: bool
    stop_reason: str | None
    final_answer: str
    reported_model: str | None
    reads: int
    searches: int
    edits: int
    map_hits: int
    turns: int
    tokens_in: int
    tokens_out: int


def parse_transcript(text: str, *, requested_model: str) -> TranscriptSummary: ...
def terminal_succeeded(process: ProcessResult, summary: TranscriptSummary) -> bool: ...
````

Add a fake `error_max_turns` transcript whose agent made a verifier-passing partial edit. Assert `verifier_passed is True` and `accepted is False`. Add missing/multiple result, permission error, timeout, empty answer, non-`end_turn` stop, nonzero process exit, context overflow, and transcript-model mismatch cases.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench.TranscriptMetricsTest tests.test_bench.RunOneTest -v
````

Expected: FAIL because current acceptance ignores terminal subtype and a passing command accepts the max-turn partial patch.

- [ ] **Step 3: Implement fail-closed terminal semantics**

Completion requires all of:

- process exit code zero;
- no process timeout;
- exactly one terminal `result` event;
- subtype `success`;
- `is_error == false`;
- final assistant stop reason `end_turn`;
- nonblank final answer;
- transcript-reported model exactly `claude-sonnet-5`.

Any other state receives a stable terminal status and remains rejected. Run the verifier after terminal failure for diagnostics, but derive:

````python
completed = terminal_succeeded(process, summary)
accepted = completed and verifier.passed
````

- [ ] **Step 4: Save and expose the final answer**

Write `TASK-CONDITION-REP.answer.txt` beside the transcript. Expand `{answer}`
and `{ws}` with shell-quoted absolute paths. Persist `terminal_status`,
`completed`, `verifier_passed`, `accepted`, model/version/turn limit, corpus
revision, seed, pair index, challenged-tree SHA-256, workspace-asset SHA-256,
tier/capability/kind/visibility, score, and metrics in each row.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench.TranscriptMetricsTest tests.test_bench.RunOneTest -v
````

Expected: all terminal-state tests PASS; a verifier-passing `error_max_turns` row remains rejected.

- [ ] **Step 6: Commit fail-closed grading**

````bash
git add benchmark/transcript.py benchmark/bench.py tests/test_bench.py
git commit -m "fix(bench): reject incomplete agent runs"
````

---

### Task 10: Pin Claude Code and isolate every B/C run

**Files:**
- Modify: `benchmark/bench.py`
- Modify: `tests/test_bench.py`

- [ ] **Step 1: Write failing runner-isolation tests**

Freeze:

````python
def claude_version(run=subprocess.run) -> str: ...

def claude_runner(
    prompt: str,
    workspace: Path,
    model: str,
    max_turns: int,
    *,
    config_dir: Path,
) -> ProcessResult: ...
````

Assert exact version equality with `2.1.224`, exact `--model
claude-sonnet-5` and `--max-turns 40` arguments, distinct configuration
directories for every pair member, inherited credentials through the normal
platform mechanism rather than copied tracked files, captured timeout output,
and rejection of a results directory containing any prior artifact or row.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench.RunnerIsolationTest -v
````

Expected: FAIL because the current runner returns only stdout, never checks the CLI version, and shares default configuration.

- [ ] **Step 3: Implement exact version and isolated configuration checks**

Before a non-dry matrix, run `claude --version` and require the normalized exact version `2.1.224`. For each session, create a fresh configuration directory beneath that run's results directory and set `CLAUDE_CONFIG_DIR` only for the child process. Preserve the parent environment for platform/keychain authentication. Set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and never reuse session state between B and C.

Before either a dry or paid matrix, resolve the results directory, create it if
absent, and require it to be empty. Refuse append/resume semantics so repeated
commands cannot silently mix schedules. After scheduling, assert internally
that there are exactly `len(tasks) * reps * 2` unique run rows and exactly
`len(tasks) * reps` complete B/C pair keys before writing any artifact.

Catch `subprocess.TimeoutExpired` and return a `ProcessResult` with preserved partial stdout/stderr and `timed_out=True`.

- [ ] **Step 4: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench.RunnerIsolationTest -v
````

Expected: all argument, version, environment, timeout, and per-session isolation tests PASS.

- [ ] **Step 5: Commit runner reproducibility**

````bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): isolate pinned Claude runs"
````

---

### Task 11: Prepare pinned public workspaces and balanced B/C pairs

**Files:**
- Create: `benchmark/corpus.py`
- Modify: `benchmark/bench.py`
- Modify: `tests/test_bench.py`

- [ ] **Step 1: Write failing public-corpus, challenge, and schedule tests**

Freeze:

````python
@dataclass(frozen=True)
class RunSpec:
    task: Task
    condition: Literal["B", "C"]
    rep: int
    pair_index: int


def prepare_public_corpus(
    spec: BenchmarkCorpus,
    destination: Path,
    *,
    run=subprocess.run,
) -> Path: ...


def schedule_runs(
    tasks: Sequence[Task],
    *,
    conditions: Sequence[str],
    reps: int,
    seed: int,
) -> tuple[RunSpec, ...]: ...


def make_workspace(
    corpus: Path,
    workspace: Path,
    condition: str,
    *,
    challenge: Challenge | None = None,
    workspace_assets: Sequence[str] = (),
) -> Path: ...
````

Prove exact revision checking, dirty-source rejection, challenge SHA-256
verification, challenge-before-map ordering, identical B/C challenged source-tree
hashes, per-session independent dependency-asset copies, matching pre-run asset
tree hashes, post-run asset immutability checks, deterministic task shuffle, and
alternating B/C pair order.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench.WorkspaceTest tests.test_bench.ScheduleTest -v
````

Expected: FAIL because challenge-aware workspace setup and balanced scheduling do not exist.

- [ ] **Step 3: Implement public corpus preparation**

Add:

````text
bench.py prepare TASKFILE --corpus PATH
````

`prepare` may clone the public URL, detach at the exact full revision, run `bootstrap_cmd`, and verify declared workspace assets. `run` never fetches or mutates the source checkout; it resolves `--corpus` or `path_env` and verifies clean exact `HEAD`.

- [ ] **Step 4: Implement B/C setup order**

For every pair:

1. Create a fresh independent local clone of the prepared corpus (never a Git
   worktree), then detach it at the pinned revision.
2. Copy each prepared ignored asset into the session independently; never use a
   symlink, hard link, shared writable mount, or Git alternates for workspace
   files. Reject an asset symlink that escapes its source asset root; otherwise
   materialize regular bytes into the session copy.
3. Verify and apply the challenge, if present.
4. Record the challenged source tree hash.
5. For B, add only neutral benchmark instructions.
6. For C, start with `base = hologram.config.default_config()` and build the complete challenged-source map with `dataclasses.replace(base, agents=("claude",), output=None, exclude=(*base.exclude, "**/deps/**"))`. This excludes the copied ignored dependency asset and changes treatment only through the managed block in `CLAUDE.md`; do not create a standalone map or touch `AGENTS.md`/`GEMINI.md`.
7. Compute and persist one canonical SHA-256 over each copied asset tree and
   require B/C initial hashes to match. Commit setup so agent changes start from
   a clean baseline. Recompute after the session and reject the run if an ignored
   asset changed; include the initial asset hash in matched-pair identity.

The condition treatment is the only B/C difference.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench.WorkspaceTest tests.test_bench.ScheduleTest -v
````

Expected: all corpus, challenge, equality, and scheduling tests PASS.

- [ ] **Step 6: Commit paired workspace support**

````bash
git add benchmark/corpus.py benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): balance pinned B C workspaces"
````

---

### Task 12: Add deterministic verifier protocol and four CodeCompanion tasks

**Files:**
- Create: `benchmark/verifiers/__init__.py`
- Create: `benchmark/verifiers/common.py`
- Create: `benchmark/verifiers/codecompanion.py`
- Create: `benchmark/verifiers/rubrics/codecompanion.json`
- Create: `benchmark/challenges/codecompanion-audit.patch`
- Create: `benchmark/tasks/codecompanion.json`
- Create: `tests/test_bench_verifiers.py`

- [ ] **Step 1: Write failing verifier tests**

Freeze:

````python
@dataclass(frozen=True)
class Verification:
    passed: bool
    score: float
    diagnostics: tuple[str, ...]


def clean_worktree(workspace: Path) -> bool: ...
def changed_paths(workspace: Path) -> frozenset[str]: ...
def verify_file_edited_lifecycle(workspace: Path, answer: Path) -> Verification: ...
def verify_read_file_integer_ranges(workspace: Path, answer: Path) -> Verification: ...
def verify_move_file_plan(workspace: Path, answer: Path) -> Verification: ...
def verify_duplicate_unused_audit(workspace: Path, answer: Path) -> Verification: ...
````

Use synthetic git workspaces and adversarial answers. Read-only verifiers must reject a modified worktree, missing/incorrect answers, keyword dumps that lack required relationships, and accepted decoys. The implementation verifier must reject unrelated paths, fractional-range acceptance, missing canonical reuse, a second range parser, failing focused/full tests, or formatting errors.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_verifiers -v
````

Expected: FAIL because no verifier package exists.

- [ ] **Step 3: Implement the JSON verifier protocol**

Every verifier prints one final JSON object:

````json
{"passed":true,"score":1.0,"diagnostics":[]}
````

Its process exit code is zero only when `passed` is true. `bench.py` captures stdout/stderr into an external verifier log and parses only the final JSON object. Malformed output is a verifier failure.

Every navigation prompt requires the final answer itself to be one strict JSON object with exactly `schema_version`, `task`, `claims`, and `evidence`. Task-specific schemas in `benchmark/verifiers/rubrics/codecompanion.json` define the closed set of claim keys and cardinalities. Each claim contains a nonblank explanation plus one or more evidence IDs; each evidence record contains a repository-relative POSIX path, one-based line, and exact source anchor. Reject unknown/missing fields, duplicate evidence IDs, absolute or escaping paths, nonexistent lines, anchors absent from the pinned source line, claims without evidence, and prose/keyword dumps outside the JSON object. This is deterministic validation; do not call a model from a verifier.

Freeze these required claim keys:

- `file-edited-lifecycle`: `event_helper`, `producers`, `consumers`, `installation`, `payloads`, `deletion_consequence`.
- `move-file-plan`: `touchpoints`, `rename_reuse`, `containment_reuse`, `approval_reuse`, `registration_reuse`, `tracking_reuse`, `operation_order`, `boundary_cases`, `tests`, `commands`.
- `duplicate-unused-audit`: `active_clone`, `canonical_replacement`, `strong_zero`, `uncertain_surface`, `reachable_decoy`.

The rubric JSON assigns rational weights summing to 1.0 and exact source anchors/allowed symbol sets for every key. Orientation and audit pass only when every key is satisfied. Planning passes at `score >= 0.90` only when every reuse/order claim is present; any parallel replacement abstraction, missing mandatory reuse, dirty tree, invalid evidence, or accepted decoy is fatal and caps score below 0.90. Reuse verification is binary (`1.0` only when every code, reuse, formatting, focused-test, and full-suite check passes; otherwise `0.0`).

- [ ] **Step 4: Implement the four public verifiers**

- **Simple orientation / navigate:** require a clean tree and a source-grounded explanation of the bounded `CodeCompanionFileEdited` graph: shared event helper, every producer, both consumers, installation point, emitted payload distinctions, and the consequence of deletion not participating.
- **Simple implementation / reuse:** permit changes only to the built-in read-file tool and its focused test; require integer JSON schema bounds, runtime fractional rejection with existing whole-number/sentinel/clamping/error semantics preserved, and continued use of the canonical range parser. Run the focused Mini.Test file, `stylua --check .`, `git diff --check`, and the full suite.
- **Complex planning / navigate:** require a clean tree and a decision-complete move-file plan that reuses existing rename, cwd-containment, approval, tool/group registration, and edited-file tracking mechanisms; covers ordering, boundary cases, exact touchpoints, focused Mini.Test cases, and native commands. Parallel replacement abstractions cap the score below passing.
- **Complex audit / navigate:** require a clean tree and the exact active clone/canonical replacement, true unused private finding, uncertain exported zero-internal-reference surface, and reachable configuration/string decoy. Reject edits and false-positive decoy findings.

- [ ] **Step 5: Add the frozen public manifest and challenge**

`benchmark/tasks/codecompanion.json` must declare `https://github.com/olimorris/codecompanion.nvim.git` at `2b959b2bf5fdb13e3b333c078ba549996e477b7c`, `HOLOGRAM_BENCH_CODECOMPANION`, `make deps`, the ignored `deps` workspace asset, model `claude-sonnet-5`, Claude Code `2.1.224`, max turns 40, conditions B/C, one rep, seed 20260809, and four tasks covering both tiers and all capabilities.

Freeze this exact public task matrix; verifier commands run with the Hologram worktree as their current directory:

| Task ID | Tier | Capability | Kind | `accept_cmd` | `expect_reuse` | Challenge |
|---|---|---|---|---|---|---|
| `file-edited-lifecycle` | simple | orientation | navigate | `.venv/bin/python -m benchmark.verifiers.codecompanion file-edited-lifecycle {ws} {answer}` | empty | none |
| `read-file-integer-ranges` | simple | implementation | reuse | `.venv/bin/python -m benchmark.verifiers.codecompanion read-file-integer-ranges {ws}` | `extract_range` | none |
| `move-file-plan` | complex | planning | navigate | `.venv/bin/python -m benchmark.verifiers.codecompanion move-file-plan {ws} {answer}` | empty | none |
| `duplicate-unused-audit` | complex | audit | navigate | `.venv/bin/python -m benchmark.verifiers.codecompanion duplicate-unused-audit {ws} {answer}` | empty | manifest-relative `../challenges/codecompanion-audit.patch` plus its exact SHA-256 |

The audit patch SHA-256 is stored in the task. Apply the identical patch before map generation for both conditions.

- [ ] **Step 6: Run GREEN without paid sessions**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_verifiers tests.test_bench_schema -v
test -n "$HOLOGRAM_BENCH_CODECOMPANION"
hologram_public_results=$(mktemp -d /tmp/hologram-public-bench-dry.XXXXXX)
.venv/bin/python benchmark/bench.py run benchmark/tasks/codecompanion.json \
  --dry-run --results "$hologram_public_results"
````

Expected: tests PASS; the internally validated dry-run writes exactly eight
unique planned rows and four complete B/C pairs, with both tiers and all four
capabilities; it never invokes Claude or task verifiers.

- [ ] **Step 7: Commit the public matrix**

````bash
git add benchmark/verifiers benchmark/challenges/codecompanion-audit.patch benchmark/tasks/codecompanion.json tests/test_bench_verifiers.py
git commit -m "feat(bench): add public CodeCompanion matrix"
````

---

### Task 13: Support external private manifests and prove non-leakage

**Files:**
- Create: `tests/test_bench_privacy.py`
- Modify: `benchmark/schema.py`
- Modify: `benchmark/bench.py`
- Create: `benchmark/reporting.py`

- [ ] **Step 1: Write failing path-boundary and redaction tests**

Create private manifests only under `TemporaryDirectory`. Seed every string field with unique sentinels representing repository identity, revision, paths, prompt, task IDs, symbols, patch hash, answers, transcript, diff, and verifier logs. Assert private execution rejects:

- a manifest inside the Hologram worktree;
- results inside the Hologram worktree;
- a resolved private corpus checkout inside the Hologram worktree;
- challenge, hidden-test, or verifier assets inside the Hologram worktree;
- a private report whose raw results path is inside the Hologram worktree;
- a symlink outside the worktree that resolves to any in-worktree path;
- the default public results directory;
- mixed public/private rows.

Assert the generated private report contains none of the sentinels.

Assert `git ls-files benchmark/archive` is empty while leaving ignored local archive bytes untouched. Report only the count on failure, never basenames.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_privacy -v
````

Expected: FAIL because current code permits private paths/results inside the repository and the report emits task rows.

- [ ] **Step 3: Implement the private path guard**

Freeze:

````python
def require_outside_worktree(path: Path, *, worktree: Path, label: str) -> Path: ...
def private_report(rows: Sequence[Mapping[str, object]]) -> str: ...
````

Resolve symlinks before containment checks. Before reading or writing any private
input, apply `require_outside_worktree()` to the manifest, resolved corpus
checkout, run-results directory, report raw-results path, challenge, hidden-test,
verifier, and every declared workspace asset. Private `run` requires explicit
external `--results`; private reporting requires an explicit external raw-results
path. Do not create a tracked private manifest example, placeholder, task ID,
rubric, or report.

- [ ] **Step 4: Enforce numeric condition-total reporting**

The only private input fields allowed into aggregation are the validated condition key plus this numeric/boolean allowlist:

````python
PRIVATE_GROUP_FIELDS = frozenset({"condition"})  # value must be exactly "B" or "C"

PRIVATE_NUMERIC_FIELDS = frozenset({
    "completed",
    "accepted",
    "rubric_score",
    "reads",
    "searches",
    "turns",
})
````

Output exactly two condition rows, B and C, containing only integer/decimal totals for runs, completed runs, accepted runs, rubric-score sum, exploration-call sum (`reads + searches`), and turn sum. Do not output means, model, tier, capability, task count by class, task IDs, or arbitrary error text.

- [ ] **Step 5: Exercise the external four-task shape without identifiers**

The temporary test manifest must contain four generic tasks—simple orientation, simple implementation, complex planning, and complex audit—at one rep under B/C. Its dry run must schedule eight external rows. Combined with the public dry run, assert exactly 16 planned sessions.

- [ ] **Step 6: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_privacy -v
````

Expected: all path-boundary, 16-session coverage, and sentinel non-leakage tests PASS.

- [ ] **Step 7: Commit private isolation**

````bash
git add benchmark/schema.py benchmark/bench.py benchmark/reporting.py tests/test_bench_privacy.py
git commit -m "feat(bench): isolate and redact private runs"
````

---

### Task 14: Partition public reports and exclude invalid efficiency data

**Files:**
- Modify: `benchmark/reporting.py`
- Create: `tests/test_bench_reporting.py`
- Modify: `benchmark/bench.py`

- [ ] **Step 1: Write failing grouping and denominator tests**

Freeze:

````python
def matched_pairs(rows: Sequence[Mapping[str, object]]) -> tuple[tuple[dict, dict], ...]: ...
def public_report(rows: Sequence[Mapping[str, object]]) -> str: ...
def report(rows: Sequence[Mapping[str, object]]) -> str: ...
````

Use deliberately different simple/complex and capability metrics so any pooled mean is obvious. Add one failed B row, one failed C row, one max-turn row, one unmatched row, two models, and two CLI versions. Assert:

- partitions are model/version → tier → capability → condition;
- simple and complex means never mix;
- efficiency uses accepted matched pairs only;
- failed rows never enter efficiency or duplication denominators;
- unique task and run counts are both shown;
- empty denominators render `—`, not zero;
- old rows without schema fields appear as `legacy / unclassified`.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_reporting -v
````

Expected: FAIL because the current report pools task kinds and tiers by condition.

- [ ] **Step 3: Implement public report partitions**

Within each model/version section, render capability-specific tables. Include completion, acceptance, max-turn failures, rubric score, and eligible denominator. Show duplication/reuse only for implementation tasks. Show reads/searches/map hits/turns/tokens only for accepted matched navigation pairs.

`matched_pairs()` groups on `(visibility, corpus_revision, task_id, rep,
pair_index, model, claude_code_version, max_turns, seed,
challenged_tree_sha256, workspace_asset_sha256, tier, capability, kind)` and
requires exactly one B row and one C row with identical grouped metadata. A pair
is efficiency-eligible only when both rows are accepted. Duplicate conditions,
mismatched source/asset hashes, or missing members remain visible in
completion/acceptance counts but never enter efficiency, reuse, or duplication
means.

Dispatch `report()` by homogeneous visibility: `public_report` for public rows and the numeric-only `private_report` for private rows.

- [ ] **Step 4: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_reporting tests.test_bench_privacy -v
````

Expected: all grouping, matched-pair, denominator, legacy, and redaction tests PASS.

- [ ] **Step 5: Commit trustworthy reporting**

````bash
git add benchmark/reporting.py benchmark/bench.py tests/test_bench_reporting.py
git commit -m "feat(bench): report matched tiered evidence"
````

---

### Task 15: Publish the runbook and label historical evidence as legacy

**Files:**
- Modify: `benchmark/README.md`
- Modify: `benchmark/results-spring-2026-08-08.md`
- Modify: `README.md`

- [ ] **Step 1: Write a failing documentation assertion**

Add a test in `tests/test_bench_reporting.py` that reads the three documents and requires the phrases `legacy`, `pre-tier`, `navigation acceptance was not automated`, `mutable model alias`, and `no paid sessions are run by the implementation suite`. Require current commands to name `codecompanion.json`, B/C, `claude-sonnet-5`, `2.1.224`, and external private results.

- [ ] **Step 2: Run RED and verify the failure**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_reporting.BenchmarkDocumentationTest -v
````

Expected: FAIL because current docs still present the Spring harness as trustworthy and describe navigation acceptance as `true` plus manual spot checks.

- [ ] **Step 3: Rewrite the benchmark runbook**

Document:

- public corpus preparation and exact revision verification;
- public dry-run commands and the eight-row expected shape;
- paid-run command as an explicitly manual, out-of-scope operation;
- exact model, CLI version, turn limit, B/C conditions, one rep, seed, answer/verifier protocol, and isolated configuration;
- external-only private manifest/results rules;
- public partitioned versus private condition-total reports;
- static validation commands and all frozen thresholds.

- [ ] **Step 4: Qualify historical claims**

Add a prominent legacy banner to the Spring report. State that it predates tiers, used a mutable model alias, used change-only reuse checks, did not automatically validate navigation answers, did not gate `error_max_turns`, pooled task metrics, and had one repetition. Preserve its numeric observations as historical context, but remove any claim that it validates the hardened harness.

State that the obsolete active task manifest was removed because it predates the hardened terminal/verifier contract. Ignored local legacy archives remain outside Git and must not be inspected, summarized, copied, or cited in tracked documentation.

Update the root README's benchmark section so historical pull/embed figures are explicitly exploratory pre-tier evidence and cannot be compared with future v2 reports.

- [ ] **Step 5: Run GREEN**

Run:

````bash
.venv/bin/python -m unittest tests.test_bench_reporting.BenchmarkDocumentationTest -v
````

Expected: PASS with all legacy and current-run qualifications present.

- [ ] **Step 6: Commit the runbook and qualification**

````bash
git add README.md benchmark/README.md benchmark/results-spring-2026-08-08.md tests/test_bench_reporting.py
git commit -m "docs(validation): qualify legacy benchmark evidence"
````

---

## Final verification: Run the complete no-paid-session validation gate

**Files:**
- Modify only if a preceding test exposes a defect; do not change thresholds or gold to make this task pass.

- [ ] **Step 1: Run all automated tests**

````bash
.venv/bin/python -m unittest discover -s tests -v
````

Expected: all tests PASS with no warnings or leaked temporary paths.

- [ ] **Step 2: Run static analysis**

````bash
.venv/bin/ruff check --no-cache src tests benchmark validation
.venv/bin/mypy --cache-dir=/tmp/hologram-v2-mypy-cache src/hologram
````

Expected: both commands exit 0 with no findings.

- [ ] **Step 3: Run the complete public static gate**

````bash
.venv/bin/python -m validation.run \
  --registry validation/corpora.toml \
  --census validation/gold/census.jsonl \
  --sample validation/gold/sample.jsonl \
  --facts validation/gold/facts \
  --exclusions validation/gold/exclusions \
  --runs 3 \
  --output /tmp/hologram-v2-static-validation.json
````

Expected: 748 census files, 103 gold files with 9/24/38/26/6 corpus split, 33 synthetic files, every threshold passing, and three-run byte equality.

- [ ] **Step 4: Run public and private dry-run coverage**

Run the public manifest:

````bash
test -n "$HOLOGRAM_BENCH_CODECOMPANION"
hologram_public_results=$(mktemp -d /tmp/hologram-public-bench-dry.XXXXXX)
.venv/bin/python benchmark/bench.py run benchmark/tasks/codecompanion.json \
  --dry-run --results "$hologram_public_results"
````

Run the test-created external private-manifest dry-run through:

````bash
.venv/bin/python -m unittest tests.test_bench_privacy.PrivateDryRunMatrixTest -v
````

Expected: the internally validated schedules contain exactly eight public plus
eight external-private unique planned rows = 16 B/C sessions and eight complete
pairs; one repetition; both tiers; all four capabilities; zero Claude
invocations.

- [ ] **Step 5: Review the generated standalone map**

````bash
.venv/bin/python -m hologram build --root . --quiet
.venv/bin/python -m hologram check --root . --quiet
````

Expected: both commands exit 0; the ignored standalone map contains the new
validation and benchmark APIs; any strong `×0` or approximate finding is
manually explained before completion.

- [ ] **Step 6: Verify final repository hygiene and privacy**

````bash
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
````

Expected: diff checks pass; tracked and untracked nonignored status is empty;
both guards exit silently because no legacy archive or private
manifest/raw artifact/transcript/answer/verifier log/run JSONL is tracked.
Ignored local archive bytes are outside every read and remain untouched.

- [ ] **Step 7: Re-run the complete gate after any defect fix**

If a verified defect required changes, return to the owning task's RED/GREEN cycle, commit it at that task's boundary, then repeat Steps 1–6. Do not create an empty verification commit and do not weaken thresholds or alter correct gold data.

Expected: the repeated complete gate passes, the ignored standalone map is
fresh, and the tracked working tree is clean.
