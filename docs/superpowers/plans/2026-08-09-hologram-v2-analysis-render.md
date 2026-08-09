# Hologram v2 Analysis and Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn resolved extractor facts into conservative reference and duplicate advisories, then encode every renderable fact in a deterministic compact map whose canonical `RenderIR` decodes without loss.

**Architecture:** Add two focused modules. `analysis.py` consumes the frozen `ProjectIR` and `ResolutionResult`, indexes facts by the frozen line-independent `SymbolId`, and produces an immutable `AnalyzedProject`. `render.py` projects that model into an explicit-file-leaf `RenderIR`, renders one canonical text grammar, and decodes the text back to the identical IR; extractor-only body evidence is intentionally not part of the round-trip projection.

**Tech Stack:** Python 3.11+, immutable dataclasses, stdlib `re`/`hashlib`/`json`, Tree-sitter facts supplied by the extractor phase, `unittest`, Ruff, mypy.

---

## Frozen inputs and file structure

Do not alter these foundation contracts:

```python
SymbolId(language, file, container_path, kind, name, signature_key)
SourceSpan(file, start_line, start_column, end_line, end_column)
Symbol.id
Symbol.span
FileIR
ProjectIR
ResolutionResult
ProjectConfig.hot_threshold
```

`SymbolId` never contains a line number. Provenance always comes from `SourceSpan`.
`FileIR.source` is the immutable
`SourceFile(path, file, language, role, raw, sha256)` captured by the scanner's
single read, and `FileIR.bodies` contains
`BodyIR(owner, span, events)` records. Body analysis consumes the frozen events
only; it never reparses or reopens source. Classification always uses
`SourceFile.role`, never path-name heuristics.

Create:

- `src/hologram/analysis.py` — reference facts, canonical body profiles, duplicate scoring, immutable analyzed model.
- `src/hologram/render.py` — canonical render projection, explicit-file grammar, encoder, decoder.
- `tests/test_analysis.py` — reference and duplicate unit tests using small immutable IR fixtures.
- `tests/test_render.py` — canonical grammar, ownership, ordering, and exact round-trip tests.

Modify:

- `src/hologram/__init__.py` — add the phase-level analysis/render exports while preserving the foundation model, config, scan, extraction, and resolution exports.
- `tests/test_simple_mode.py` — replace assertions that require lossy cross-file shape grouping.
- `tests/test_freshness_and_markers.py` — move marker expectations to the approved semantics.
- `README.md` — document the v2 file-leaf grammar and advisory meanings after behavior is green.

The analysis records are exact:

```python
# src/hologram/analysis.py
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from .model import (
    BodyEventKind,
    BodyIR,
    FileIR,
    ProjectIR,
    SourceSpan,
    Symbol,
    SymbolId,
)
from .resolve import ResolutionResult


class ZeroReference(StrEnum):
    NONE = "none"
    STRONG = "strong"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ReferenceFacts:
    production_files: tuple[PurePosixPath, ...]
    possible_files: tuple[PurePosixPath, ...]
    test_files: tuple[PurePosixPath, ...]
    generated_files: tuple[PurePosixPath, ...]
    zero: ZeroReference


@dataclass(frozen=True, slots=True)
class BodyProfile:
    semantic_tokens: tuple[str, ...]
    ast_shingles: frozenset[tuple[str, str, str, str, str]]
    control_flow: tuple[str, ...]
    resolved_calls: frozenset[SymbolId]
    name_tokens: frozenset[str]
    arity: int
    return_key: str
    semantic_size: int
    excluded_reason: str | None


@dataclass(frozen=True, slots=True)
class DuplicateScore:
    ast: float
    control_flow: float
    calls: float
    names: float
    total: float
    exact: bool


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    left: SymbolId
    right: SymbolId
    left_span: SourceSpan
    right_span: SourceSpan
    score: DuplicateScore


@dataclass(frozen=True, slots=True)
class AnalyzedSymbol:
    symbol: Symbol
    references: ReferenceFacts
    body: BodyProfile | None
    duplicate_peers: tuple[SymbolId, ...]


@dataclass(frozen=True, slots=True)
class AnalyzedProject:
    project: ProjectIR
    resolution: ResolutionResult
    symbols: tuple[AnalyzedSymbol, ...]
    map_duplicates: tuple[DuplicateMatch, ...]


MAP_AST_MIN = 0.88
MAP_TOTAL_MIN = 0.90
DIFF_AST_MIN = 0.72
DIFF_TOTAL_MIN = 0.78
```

The exact public callables are
`analyze_project(project: ProjectIR, resolution: ResolutionResult, *, hot_threshold: int) -> AnalyzedProject`
and
`find_diff_duplicates(project: AnalyzedProject) -> tuple[DuplicateMatch, ...]`.

## Approved analysis rules

- `×N` counts distinct production referring files and is rendered when `N >= hot_threshold`.
- Zero classification follows this exact precedence table:
  1. A test/generated declaration → `NONE`.
  2. Any definite production reference, including one from the defining file →
     `NONE`.
  3. A production public/protected/re-exported surface with zero definite
     references → `UNCERTAIN` (`×0?`).
  4. A private/internal declaration with no definite, test,
     possible/ambiguous-candidate, or intrinsic evidence → `STRONG` (`×0`).
  5. Test, generated, possible/ambiguous-candidate, or intrinsic evidence with
     zero definite references → `UNCERTAIN` (`×0?`).
- `✓` is independent of production fan-in and appears whenever at least one test file refers to the symbol.
- References are keyed by `SymbolId`; a same-named declaration never inherits another declaration's definite references.
- Possible references prevent a strong claim but never inflate `×N`.
- An annotation/decorator, override or implementation modifier, recognized
  framework registration, or language entrypoint is intrinsic reachability. It
  prevents `×0` even when no referring file exists; uncertain cases render
  `×0?` and never inflate `×N`.
- Test-defined and generated declarations are rendered for completeness but
  always use `ZeroReference.NONE`; only production declarations receive `×0`
  or `×0?`. Test-origin outgoing references contribute `✓`; generated-origin
  references are tracked separately and suppress a strong zero claim, but never
  inflate `×N` or `✓`.
- Comments never count. Repeated uses in one file count once.
- Only extractor-emitted recognized possible references count. Arbitrary string
  literals and comments are not evidence. Same-file production references do
  not inflate `×N`, but they do prevent any zero marker.
- `≈N` is the number of qualifying peer declarations. It is not a rank or confidence score.
- Map duplicate comparison excludes tests, generated code, constructors, accessors, trivial delegates, and bodies with fewer than 12 semantic tokens.
- Canonical bodies ignore comments/formatting, alpha-rename parameters and locals, and preserve control-flow nodes, operators, member names, literal categories, and resolved project references.
- Exact canonical clones qualify automatically after exclusions.
- Near-map candidates require same language and kind, equal arity, compatible normalized return, semantic-size ratio in `[2/3, 3/2]`, AST five-shingle Jaccard at least `.88`, weighted total at least `.90`, and nonzero call or name corroboration.
- The weighted total is exactly `.55 * ast + .20 * control_flow + .15 * calls + .10 * names`; an empty comparison set scores `0.0` except two identical nonempty canonical bodies, which use exact equality.
- Diff uses the same compatibility filters and weights but thresholds `.72` AST and `.78` total. It emits every qualifying candidate in stable provenance order.

### Task 1: Build a SymbolId-keyed conservative reference index

**Files:**

- Create: `src/hologram/analysis.py`
- Create: `tests/test_analysis.py`

- [ ] **Step 1: Write the failing reference tests**

Create test helpers that construct foundation `Symbol`, `FileIR`, `ProjectIR`, and `ResolutionResult` values. Add these tests:

```python
class ReferenceAnalysisTest(unittest.TestCase):
    def test_distinct_files_and_same_named_symbols_do_not_cross_contaminate(self):
        project, resolution, ids = reference_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=2)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(tuple(map(str, by_id[ids["used"]].production_files)),
                         ("app/a.py", "app/b.py"))
        self.assertEqual(by_id[ids["shadow"]].production_files, ())
        self.assertEqual(by_id[ids["shadow"]].zero, ZeroReference.STRONG)

    def test_public_zero_and_dynamic_private_are_uncertain(self):
        project, resolution, ids = dynamic_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[ids["public"]].zero, ZeroReference.UNCERTAIN)
        self.assertEqual(by_id[ids["callback"]].zero, ZeroReference.UNCERTAIN)
        self.assertEqual(tuple(map(str, by_id[ids["callback"]].possible_files)),
                         ("config/routes.yaml",))

    def test_test_reference_is_independent_from_production_fan_in(self):
        project, resolution, symbol_id = test_reference_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        facts = next(item.references for item in analyzed.symbols
                     if item.symbol.id == symbol_id)
        self.assertEqual(facts.production_files, ())
        self.assertEqual(tuple(map(str, facts.test_files)), ("tests/test_api.py",))
        self.assertEqual(facts.zero, ZeroReference.UNCERTAIN)

    def test_test_and_generated_declarations_never_get_dead_markers(self):
        project, resolution, ids = nonproduction_declaration_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[ids["test_helper"]].zero, ZeroReference.NONE)
        self.assertEqual(by_id[ids["generated_helper"]].zero, ZeroReference.NONE)

    def test_generated_caller_suppresses_zero_without_test_or_fan_in_marker(self):
        project, resolution, target = generated_caller_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=1)
        facts = next(item.references for item in analyzed.symbols
                     if item.symbol.id == target)
        self.assertEqual(facts.production_files, ())
        self.assertEqual(facts.test_files, ())
        self.assertEqual(tuple(map(str, facts.generated_files)),
                         ("generated/client.py",))
        self.assertEqual(facts.zero, ZeroReference.UNCERTAIN)

    def test_intrinsic_framework_override_and_entrypoint_reachability_is_uncertain(self):
        project, resolution, ids = intrinsic_reachability_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        for name in ("bean", "override", "main"):
            self.assertEqual(by_id[ids[name]].production_files, ())
            self.assertEqual(by_id[ids[name]].zero, ZeroReference.UNCERTAIN)

    def test_zero_decision_table_covers_same_file_protected_and_ambiguity(self):
        project, resolution, ids = zero_decision_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[ids["same_file_used"]].zero, ZeroReference.NONE)
        self.assertEqual(by_id[ids["protected_surface"]].zero,
                         ZeroReference.UNCERTAIN)
        self.assertEqual(by_id[ids["ambiguous_candidate"]].zero,
                         ZeroReference.UNCERTAIN)
        self.assertEqual(by_id[ids["arbitrary_string_decoy"]].zero,
                         ZeroReference.STRONG)
```

The fixture must also contain a comment-only spelling and two uses from one production file; assertions prove comments add no possible reference and the repeated uses add one file.

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_analysis.ReferenceAnalysisTest -v
```

Expected: import failure for `hologram.analysis` or missing `analyze_project`; no test may fail because of malformed fixture construction.

- [ ] **Step 3: Implement minimal reference analysis**

Implement these private functions in `analysis.py`:

```python
def _symbol_order(symbol: Symbol) -> tuple[str, str, tuple[str, ...], str, str, str]:
    sid = symbol.id
    return (sid.language.value, sid.file, sid.container_path, sid.kind.value,
            sid.name, sid.signature_key)


def _sorted_paths(paths: set[PurePosixPath]) -> tuple[PurePosixPath, ...]:
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def _reference_index(
    project: ProjectIR,
    resolution: ResolutionResult,
) -> dict[SymbolId, ReferenceFacts]:
    """Fold resolved definite, possible/dynamic, and test edges by target ID.

    Exclude a target's defining file only from displayed production/possible
    fan-in; retain its definite edge for zero classification. Treat test-origin
    edges only as test evidence and generated-origin edges only as generated
    reachability. Apply the approved precedence table exactly, including
    protected/re-exported visibility, ambiguous candidates, and intrinsic
    annotation/decorator, override/implementation, framework, and entrypoint
    evidence. Only a private/internal declaration with none of that evidence is
    strong zero.
    """
```

Use the extractor phase's resolved-edge collections; do not rescan source text and do not resolve names in this module. Deduplicate source paths with sets, then sort them. `analyze_project()` initially returns `AnalyzedSymbol` values with `body=None`, empty peers, and symbols sorted by `_symbol_order()`.

- [ ] **Step 4: Run reference tests to verify GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_analysis.ReferenceAnalysisTest -v
```

Expected: all reference tests pass; output contains no warnings.

- [ ] **Step 5: Commit the reference slice**

```bash
git add src/hologram/analysis.py tests/test_analysis.py
git commit -m "feat: analyze conservative symbol references"
```

### Task 2: Canonicalize substantive bodies without losing semantic differences

**Files:**

- Modify: `src/hologram/analysis.py`
- Modify: `tests/test_analysis.py`

- [ ] **Step 1: Write failing body-profile tests**

Add `BodyProfileTest` with ordered body-event fixtures supplied by the extractor IR:

```python
class BodyProfileTest(unittest.TestCase):
    def test_comments_formatting_and_local_names_share_exact_profile(self):
        left, left_file, right, right_file = equivalent_body_symbols()
        self.assertEqual(canonical_body(left, left_file,
                                        resolved_body_targets(left)),
                         canonical_body(right, right_file,
                                        resolved_body_targets(right)))

    def test_operator_member_literal_category_control_and_call_are_preserved(self):
        base_symbol_value, base_file = base_symbol()
        base = canonical_body(base_symbol_value, base_file,
                              resolved_body_targets(base_symbol_value))
        for changed, changed_file in semantically_changed_symbols():
            with self.subTest(changed=changed.id.name):
                self.assertNotEqual(base.semantic_tokens,
                                    canonical_body(
                                        changed, changed_file,
                                        resolved_body_targets(changed),
                                    ).semantic_tokens)

    def test_ineligible_bodies_record_exact_exclusion_reason(self):
        expected = {
            "test helper": "test",
            "generated helper": "generated",
            "constructor": "constructor",
            "getter": "accessor",
            "delegate": "trivial-delegate",
            "tiny": "fewer-than-12-semantic-tokens",
        }
        for symbol, file_ir in excluded_symbols():
            with self.subTest(symbol=symbol.id.name):
                self.assertEqual(canonical_body(symbol, file_ir,
                                                {}).excluded_reason,
                                 expected[symbol.id.name])
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_analysis.BodyProfileTest -v
```

Expected: `ImportError` for `canonical_body`.

- [ ] **Step 3: Implement the canonical profile**

Expose this internal-but-tested function:

```python
def canonical_body(
    symbol: Symbol,
    file_ir: FileIR,
    resolved_targets: Mapping[tuple[BodyEventKind, SourceSpan], SymbolId],
) -> BodyProfile:
    """Convert extractor body events into the approved semantic profile."""


def _resolved_body_targets(
    resolution: ResolutionResult,
) -> dict[tuple[BodyEventKind, SourceSpan], SymbolId]:
    """Index only uniquely resolved call/reference targets by extractor event."""
```

Implement it as follows:

1. Locate the one `BodyIR` whose owner is `symbol.id` and consume its immutable
   `events` tuple. Never slice, tokenize, or parse `file_ir.source.raw`, and never
   open `root / symbol.span.file`; extraction is the sole parser boundary.
2. Return an exclusion reason before scoring tests/generated/constructors/accessors/trivial delegates.
3. Walk the snapshot's ordered body events once. Derive `resolved_targets` from
   `ResolutionResult`: map each uniquely resolved call/reference fact to the
   matching `(BodyEventKind, SourceSpan)`; ambiguous, external, and unresolved
   facts deliberately have no target token. Conflicting targets for the same
   event key are an invariant `ValueError` for manually malformed IR;
   `BuildSnapshot.require_complete()` guarantees pipeline IR cannot contain the
   conflict.
4. Map each parameter/local declaration to `$0`, `$1`, and so on on first declaration and substitute later uses.
5. Preserve keywords/control nodes, operators, member names, and literal category tokens (`STR`, `INT`, `FLOAT`, `BOOL`, `NULL`, `REGEX`); literal values do not survive.
6. Represent a resolved project reference as
   `REF:<stable SymbolId serialization>` at that exact event position and
   preserve unresolved member/name text with its event-kind prefix. Populate
   `BodyProfile.resolved_calls` from resolved `CALL`/`CONSTRUCT` event targets,
   not from an unordered project-wide set.
7. Construct consecutive five-token shingles with `zip(tokens, tokens[1:], tokens[2:], tokens[3:], tokens[4:])`.
8. Split symbol names on snake case, punctuation, lower-to-upper, and acronym-to-word boundaries; lowercase and remove empty tokens.
9. On `CONTROL_ENTER`, assign the next sibling ordinal under the current stack,
   append a path token such as `loop:0/if:1`, and push it; on `CONTROL_EXIT`,
   require the matching kind and pop. Reject underflow, mismatches, or a nonempty
   final stack with invariant `ValueError` for manually malformed IR. Preserve
   the resulting ordered path tuple and use its nonempty set for similarity.
10. Normalize return types with the resolver's canonical type key. Unknown is compatible only with unknown.
11. Count semantic tokens after normalization. Fewer than 12 is excluded.

Build `_resolved_body_targets()` once per project, reject two different targets
for the same key with invariant `ValueError`, and pass the immutable mapping to
each `canonical_body()` call. Add the produced profile to each `AnalyzedSymbol`
in `analyze_project()`.

Add a snapshot-coherence regression: extract a `ProjectIR`, mutate the on-disk
source, then analyze the already captured IR. The profile must remain identical
because it is derived only from the captured `BodyIR.events`; accepting newly
parsed bytes under the old state is forbidden.

- [ ] **Step 4: Run body-profile tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_analysis.BodyProfileTest -v
```

Expected: all body-profile tests pass.

- [ ] **Step 5: Commit canonicalization**

```bash
git add src/hologram/analysis.py tests/test_analysis.py
git commit -m "feat: canonicalize substantive symbol bodies"
```

### Task 3: Score exact and conservative near duplicates

**Files:**

- Modify: `src/hologram/analysis.py`
- Modify: `tests/test_analysis.py`

- [ ] **Step 1: Write failing similarity and peer-count tests**

Add tests for the fixed arithmetic and gates:

```python
class DuplicateAnalysisTest(unittest.TestCase):
    def test_weighted_score_uses_frozen_coefficients(self):
        score = combine_similarity(ast=0.92, control_flow=1.0,
                                   calls=0.5, names=0.25, exact=False)
        self.assertAlmostEqual(score.total,
                               .55 * .92 + .20 * 1.0 + .15 * .5 + .10 * .25)

    def test_exact_three_member_group_marks_two_peers_each(self):
        project, resolution, clone_ids = three_clone_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        peers = {item.symbol.id: item.duplicate_peers for item in analyzed.symbols}
        for symbol_id in clone_ids:
            self.assertEqual(set(peers[symbol_id]), set(clone_ids) - {symbol_id})

    def test_map_rejects_boundary_and_diff_accepts_broader_candidate(self):
        project, resolution, pair = broad_candidate_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        self.assertFalse(any({match.left, match.right} == set(pair)
                             for match in analyzed.map_duplicates))
        self.assertTrue(any({match.left, match.right} == set(pair)
                            for match in find_diff_duplicates(analyzed)))

    def test_call_or_name_corroboration_is_required_for_near_match(self):
        left, right = high_ast_unrelated_pair()
        self.assertIsNone(score_duplicate(left, right, policy="map"))
```

Also assert comparison rejection for language, kind, arity, return, size ratio, and every exclusion reason.

- [ ] **Step 2: Run duplicate tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_analysis.DuplicateAnalysisTest -v
```

Expected: missing `combine_similarity`, `score_duplicate`, or `find_diff_duplicates`.

- [ ] **Step 3: Implement exact scoring and stable matching**

Implement:

```python
def _jaccard(left: frozenset[object], right: frozenset[object]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def combine_similarity(
    *, ast: float, control_flow: float, calls: float, names: float, exact: bool
) -> DuplicateScore:
    return DuplicateScore(ast, control_flow, calls, names,
                          .55 * ast + .20 * control_flow + .15 * calls + .10 * names,
                          exact)


```

The exact scoring entry point is
`score_duplicate(left: AnalyzedSymbol, right: AnalyzedSymbol, *, policy: str) -> DuplicateScore | None`.

For control flow, compare the nonempty set of indexed path tokens such as `if:0`, `loop:0/if:0`, and `try:0/catch:0`; use Jaccard so nesting remains significant. Exact means equality of semantic tokens, control-flow tuple, and resolved-call set after compatibility filters. For near matches apply map or diff constants and require `calls > 0.0 or names > 0.0`. Validate `policy` against `{"map", "diff"}` and raise `ValueError` otherwise.

Compare each eligible pair once after sorting by `_symbol_order()`. Store symmetric peer lists sorted by the same key. `find_diff_duplicates()` recomputes only with the broad thresholds; it does not mutate map peers.

- [ ] **Step 4: Run duplicate tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_analysis.DuplicateAnalysisTest -v
```

Expected: all duplicate tests pass, including exact `.88/.90` and `.72/.78` boundaries.

- [ ] **Step 5: Commit duplicate analysis**

```bash
git add src/hologram/analysis.py tests/test_analysis.py
git commit -m "feat: detect deterministic duplicate candidates"
```

### Task 4: Define the exact canonical RenderIR projection

**Files:**

- Create: `src/hologram/render.py`
- Create: `tests/test_render.py`

- [ ] **Step 1: Write failing projection tests**

```python
class RenderProjectionTest(unittest.TestCase):
    def test_projection_has_explicit_files_and_separate_source_lines(self):
        analyzed = analyzed_projection_fixture()
        ir = project_render_ir(analyzed, state="a" * 64, hot_threshold=2)
        self.assertEqual(tuple(file.path for file in ir.files),
                         ("src/a.py", "src/b.py"))
        self.assertEqual(ir.files[0].symbols[0].source_line, 7)
        self.assertNotIn("7", ir.files[0].symbols[0].symbol_id.signature_key)

    def test_markers_follow_analysis_without_reclassification(self):
        ir = project_render_ir(analyzed_marker_fixture(), state="a" * 64,
                               hot_threshold=2)
        by_name = {symbol.symbol_id.name: symbol.markers
                   for file in ir.files for symbol in file.symbols}
        self.assertEqual(by_name["hot"], ("×2",))
        self.assertEqual(by_name["dead"], ("×0",))
        self.assertEqual(by_name["surface"], ("×0?", "✓"))
        self.assertEqual(by_name["clone"], ("≈1",))

    def test_render_ir_has_no_root_name_head_or_generation_date(self):
        left, right = equivalent_projects_in_roots("/tmp/alpha", "/tmp/renamed-clone")
        left_ir = project_render_ir(left, state="a" * 64, hot_threshold=2)
        right_ir = project_render_ir(right, state="a" * 64, hot_threshold=2)
        self.assertEqual(left_ir, right_ir)

    def test_relation_and_call_names_are_shortest_unambiguous_descriptions(self):
        ir = project_render_ir(name_collision_fixture(), state="a" * 64,
                               hot_threshold=10)
        calls = calls_by_symbol_name(ir)
        self.assertEqual(calls["unique_caller"], ("unique_target",))
        self.assertEqual(calls["container_caller"], ("Left.run",))
        self.assertEqual(calls["file_caller"], ("pkg/left.py:Left.run",))

    def test_framework_and_module_topology_facts_are_explicit(self):
        ir = project_render_ir(framework_topology_fixture(), state="a" * 64,
                               hot_threshold=10)
        app = next(file for file in ir.files if file.path == "src/app/Main.java")
        self.assertEqual((app.role, app.module), ("production", "app"))
        bean = next(symbol for symbol in app.symbols
                    if symbol.symbol_id.name == "client")
        self.assertEqual(bean.annotations, ("Bean",))
        self.assertIn("public", bean.modifiers)
        self.assertEqual(ir.dependencies, ("app→core",))
        self.assertEqual(bean.behaviors, ("ClientTest.createsClient",))

    def test_symbol_less_wildcard_reexport_stays_owned_by_its_file(self):
        ir = project_render_ir(symbol_less_reexport_fixture(), state="a" * 64,
                               hot_threshold=10)
        index = next(file for file in ir.files if file.path == "src/index.ts")
        self.assertEqual(index.reexports,
                         (RenderReexport("./api", None, None, True),))
        self.assertEqual(index.symbols, ())
```

- [ ] **Step 2: Run projection tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_render.RenderProjectionTest -v
```

Expected: import failure for `hologram.render`.

- [ ] **Step 3: Implement immutable RenderIR records and projection**

Use these exact render records:

```python
@dataclass(frozen=True, slots=True)
class RenderSymbol:
    symbol_id: SymbolId
    source_line: int
    source_column: int
    visibility: str
    signature: str
    parameters: tuple[str, ...]
    returns: str | None
    annotations: tuple[str, ...]
    modifiers: tuple[str, ...]
    components: tuple[str, ...]
    supers: tuple[str, ...]
    permits: tuple[str, ...]
    ordered_calls: tuple[str, ...]
    throws: tuple[str, ...]
    behaviors: tuple[str, ...]
    body_lines: int
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderIntern:
    alias: str
    value: str


@dataclass(frozen=True, slots=True)
class RenderReexport:
    module: str
    name: str | None
    alias: str | None
    wildcard: bool


@dataclass(frozen=True, slots=True)
class RenderFile:
    path: str
    language: str
    role: str
    module: str | None
    reexports: tuple[RenderReexport, ...]
    symbols: tuple[RenderSymbol, ...]


@dataclass(frozen=True, slots=True)
class RenderIR:
    schema_version: int
    state: str
    interns: tuple[RenderIntern, ...]
    dependencies: tuple[str, ...]
    files: tuple[RenderFile, ...]


```

The exact projection entry point is
`project_render_ir(analyzed: AnalyzedProject, *, state: str, hot_threshold: int) -> RenderIR`.

`schema_version` is `2`; `state` is the full 64-character lowercase SHA-256 from the frozen `StateResult`. `RenderIR` deliberately contains no root basename, absolute path, Git HEAD, generation date, or clock value. Group symbols only beneath their exact file. Never group same-shaped declarations across files. Sort files by POSIX path and symbols by `SymbolId` plus `SourceSpan.start_line/start_column` only as a deterministic tie-breaker; line remains provenance, never identity.

Project one `RenderFile` for every indexed `FileIR`, including files that emit
zero symbols. This makes supported empty/configuration files visible and keeps
file ownership complete without inventing declarations.

Map approved facts directly: file role and declared module;
`Symbol.signature`/`params`/`returns`/`annotations`/`modifiers`/`components`/
`supers`/`permits`/`raises` and `body_lines`; ordered direct calls from
`ResolutionResult`; test behavior facts; and analysis markers
each receive their own field. Do not hide any of them inside a presentation-only
signature or combined relations string.

Project every `ImportRef(reexport=True)` onto its owning `RenderFile.reexports`
as the exact `(module, name, alias, wildcard)` tuple in source order, with exact
duplicate tuples removed stably. This preserves wildcard and symbol-less
reexports without inventing a declaration or assigning a file fact to an
unrelated symbol. A legacy extractor-emitted `SymbolKind.REEXPORT` declaration
still renders as the declaration it is, independently of this raw file fact.

`dependencies` is the complete sorted unique set of production module-coupling
edges, encoded as `source→target`. A file's module key is its nonblank
`FileIR.module`, otherwise its relative parent directory, otherwise `.`. Build
edges only from resolved imports, calls, and references whose source and target
files are both production and whose module keys differ. Ambiguous, external,
test, and generated facts do not invent an edge; do not use token occurrence or
a minimum-count threshold. `behaviors` on a production symbol is the stable set
of shortest unambiguous names of test callables whose definite resolved call or
reference targets that exact `SymbolId`. This is the same independent test
evidence behind `✓`, made descriptive rather than guessed from matching names.
Generated callers suppress zero through `ReferenceFacts.generated_files` but do
not appear in `behaviors` or create `✓`.

For relations and calls, choose the shortest unambiguous descriptive name against
the complete project symbol index: bare name when unique; otherwise
`Container.name`; otherwise the shortest unique suffix of
`file:Container.name`. Never guess through ambiguity and never emit a numeric or
opaque target ID. Intern repeated string values only when they occur at least three
times and exact UTF-8 encoded-byte accounting, including the intern declaration and
alias uses, is strictly net-positive. Aliases are descriptive, stable, and never
numeric. Task 5 freezes the alias grammar and decoder expansion.

Marker order is exactly `×N` or `×0`/`×0?`, then `✓`, then `≈N`. Omit positive fan-in below `hot_threshold`; never suppress zero/test/duplicate markers.

- [ ] **Step 4: Run projection tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_render.RenderProjectionTest -v
```

Expected: projection tests pass, including exact role/module, annotation,
modifier, behavior, and production module-coupling facts.

- [ ] **Step 5: Commit RenderIR**

```bash
git add src/hologram/render.py tests/test_render.py
git commit -m "feat: define canonical render projection"
```

### Task 5: Render and decode a lossless compact grammar

**Files:**

- Modify: `src/hologram/render.py`
- Modify: `tests/test_render.py`
- Modify: `tests/test_simple_mode.py`

- [ ] **Step 1: Write failing canonical grammar and round-trip tests**

```python
class RenderRoundTripTest(unittest.TestCase):
    def test_file_leaves_make_same_shape_ownership_explicit(self):
        text = render_project(project_render_ir(two_file_same_shape_fixture(),
                                                state="a" * 64,
                                                hot_threshold=10))
        self.assertIn('@ "src/ids/ItemId.java"', text)
        self.assertIn('@ "src/ids/OrderId.java"', text)
        self.assertNotIn("ItemId,OrderId", text)

    def test_decoder_round_trips_canonical_ir_exactly(self):
        ir = all_render_fields_fixture()
        text = render_project(ir)
        self.assertEqual(decode_render(text), ir)
        self.assertEqual(render_project(decode_render(text)), text)

    def test_input_order_cannot_change_bytes(self):
        left, right = permuted_equivalent_analyzed_projects()
        self.assertEqual(render_project(project_render_ir(left, state="a" * 64,
                                                          hot_threshold=10)),
                         render_project(project_render_ir(right, state="a" * 64,
                                                          hot_threshold=10)))

    def test_differently_named_clones_render_identically(self):
        left_snapshot, right_snapshot = build_equivalent_snapshots_in_named_roots(
            self.tmp / "project-one", self.tmp / "other-name")
        self.assertEqual(left_snapshot.state.value, right_snapshot.state.value)
        left = analyze_project(left_snapshot.project, left_snapshot.resolution,
                               hot_threshold=10)
        right = analyze_project(right_snapshot.project, right_snapshot.resolution,
                                hot_threshold=10)
        self.assertEqual(
            render_project(project_render_ir(left, state=left_snapshot.state.value,
                                             hot_threshold=10)),
            render_project(project_render_ir(right, state=right_snapshot.state.value,
                                             hot_threshold=10)),
        )

    def test_interning_requires_three_uses_and_positive_exact_savings(self):
        profitable = profitable_intern_fixture(occurrences=3)
        unprofitable = short_value_fixture(occurrences=4)
        only_two = profitable_intern_fixture(occurrences=2)
        self.assertEqual(len(profitable.interns), 1)
        self.assertEqual(unprofitable.interns, ())
        self.assertEqual(only_two.interns, ())

    def test_intern_aliases_are_descriptive_collision_safe_and_reversible(self):
        ir = adversarial_intern_fixture()
        self.assertTrue(all(alias.alias.startswith("&")
                            and not alias.alias[1:].isdigit()
                            for alias in ir.interns))
        self.assertEqual(len({alias.alias for alias in ir.interns}), len(ir.interns))
        self.assertEqual(decode_render(render_project(ir)), ir)
```

The all-fields fixture includes spaces and Unicode in a path, every file role, a
declared and absent module, a symbol-less wildcard reexport, overloads, a nested
container, generics with commas, annotations, modifiers, every relation, ordered direct calls, throws, test
behaviors, dependencies, body size, and every legal marker combination.

- [ ] **Step 2: Run render tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_render.RenderRoundTripTest -v
```

Expected: missing `render_project` or `decode_render`.

- [ ] **Step 3: Implement the canonical grammar**

Use UTF-8 and `\n` line endings. The grammar is:

```text
# hologram:2 state=<64 lowercase hex> · regen: hologram build
· intern <JSON alias string> <JSON expanded value string>
· deps <JSON array of strings>
@ <JSON path string> <JSON language string> <JSON role string> <JSON module string or null>
  reexport <JSON array of [module,name-or-null,alias-or-null,wildcard] arrays>
  :<line>:<column> <JSON local SymbolId array> <JSON visibility>
    signature <JSON string or alias reference>
    param <JSON array>
    return <JSON string or null>
    annotation <JSON array>
    modifier <JSON array>
    component <JSON array>
    super <JSON array>
    permit <JSON array>
    call <JSON ordered array>
    throw <JSON array>
    behavior <JSON array>
    body <nonnegative integer>
    mark <JSON array>
```

The local SymbolId JSON array is exactly:

```text
[container_path_array,kind,name,signature_key]
```

The enclosing `@` leaf supplies `language` and `file`; the decoder reconstructs
the complete frozen `SymbolId(language, file, container_path, kind, name,
signature_key)`. Do not repeat file or language on symbol lines.

Canonical aliases match `&[A-Za-z_][A-Za-z0-9_.:-]*`. Derive the shortest unique
alias from the value's identifier segments, starting with `&` plus its last
descriptive segment and extending leftward on collision. If no descriptive unique
alias exists without a numeric suffix, do not intern the value. Alias references
are JSON strings containing the alias. A literal value beginning with `&` is escaped
as `&&...`; decoder expansion first resolves an exact declared alias and otherwise
unescapes one leading ampersand. Intern declarations are nonrecursive and sorted by
alias.

Eligible values are strings in file modules, signature, parameters, returns,
annotations, modifiers, components, supers, permits, ordered calls, throws,
reexports, behaviors, and dependencies. A value is eligible only at three or
more occurrences. Compute savings from the exact canonical UTF-8 bytes: sum the
encoded original occurrences, subtract encoded alias-reference occurrences and
the complete `· intern <alias> <value>\n` declaration. Add the intern only when
the result is greater than zero. Recompute canonical text once after selecting
all independently profitable, collision-free aliases; do not use estimated token
counts.

Omit the file-level `reexport` line when its array is empty. Omit symbol-level
`param`, `annotation`, `modifier`, `component`, `super`, `permit`, `call`,
`throw`, `behavior`, `body`, and `mark` child lines only when their canonical
value is empty or zero; always emit `signature` and `return` so
absent/empty values remain distinct. The decoder restores only the specified
empty defaults, expands aliases into every eligible field, and retains the
canonical `RenderIntern` table so decoded IR equals projected IR. Emit one `@`
leaf per file, including zero-symbol indexed files, so file ownership and module
topology remain complete. Use
`json.dumps(value, ensure_ascii=False, separators=(",", ":"))`. Reject unknown
lines, duplicate child keys, duplicate aliases/values, undeclared alias
references, nonprofitable or under-three-use intern declarations, duplicate file
leaves, duplicate SymbolIds, noncanonical JSON spacing, unsorted files/symbols,
invalid roles or marker order, a non-64-character state, a nonconstant header,
and trailing whitespace with `RenderDecodeError`.

Implement:

```python
class RenderDecodeError(ValueError):
    pass
```

The exact codec entry points are `render_project(ir: RenderIR) -> str` and
`decode_render(text: str) -> RenderIR`.

`decode_render()` must finish by checking `render_project(decoded) == text`; otherwise raise `RenderDecodeError("noncanonical hologram text")`.

Update old renderer tests to assert explicit leaves and retained per-file methods instead of cross-file grouping or hole notation. Preserve tests for direct ordered calls, relations, exceptions, private visibility, and body sizes.

- [ ] **Step 4: Run renderer tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_render tests.test_simple_mode -v
```

Expected: all tests pass; canonical output ends in exactly one newline.

- [ ] **Step 5: Commit the lossless grammar**

```bash
git add src/hologram/render.py tests/test_render.py tests/test_simple_mode.py
git commit -m "feat: round-trip canonical hologram maps"
```

### Task 6: Integrate marker behavior, public API, documentation, and phase verification

**Files:**

- Modify: `src/hologram/__init__.py`
- Modify: `tests/test_freshness_and_markers.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing end-to-end marker test**

Add a temporary Python project containing a hot helper, truly unused private helper, public zero-reference API, test-referenced API, dynamic callback, exact clone pair, and comment-only decoy. Extract and resolve it through the preceding phase APIs, then analyze and render:

```python
class MarkerEndToEndTest(unittest.TestCase):
    def test_rendered_map_has_only_approved_advisories(self):
        projected = build_analyzed_fixture_ir(hot_threshold=2)
        decoded = decode_render(render_project(projected))
        by_name = {symbol.symbol_id.name: symbol.markers
                   for file in decoded.files for symbol in file.symbols}
        self.assertEqual(by_name["hot"], ("×2",))
        self.assertEqual(by_name["unused_private"], ("×0",))
        self.assertEqual(by_name["public_surface"], ("×0?",))
        self.assertEqual(by_name["tested_api"], ("×0?", "✓"))
        self.assertEqual(by_name["clone_a"], ("≈1",))
        self.assertEqual(by_name["dynamic_callback"], ("×0?",))
        self.assertEqual(by_name["comment_decoy"], ("×0",))
```

- [ ] **Step 2: Run end-to-end test to verify RED**

```bash
.venv/bin/python -m unittest tests.test_freshness_and_markers.MarkerEndToEndTest -v
```

Expected: failure because phase-level exports or the integrated analysis/render helper do not exist.

- [ ] **Step 3: Export the phase API and update documentation**

Export `AnalyzedProject`, `RenderIR`, `analyze_project`, `project_render_ir`, `render_project`, and `decode_render` from `src/hologram/__init__.py`.

Update README output and legend. State explicitly:

- `×0` is a strong static candidate, not proof of semantic deadness.
- `×0?` is uncertain or externally reachable and must not be proposed for deletion from the map alone.
- `✓` is test-reference evidence, not correctness proof.
- `≈N` counts conservative peers; inspect bodies before consolidating.
- File leaves and source positions provide exact provenance.
- Maps are complete and never budget-truncated or ranked.

- [ ] **Step 4: Run phase verification to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_analysis tests.test_render tests.test_freshness_and_markers -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests
.venv/bin/mypy src/hologram
git diff --check
```

Expected: all tests pass; Ruff and mypy report no errors; diff check is silent.

- [ ] **Step 5: Commit the integrated phase**

```bash
git add src/hologram/__init__.py tests/test_freshness_and_markers.py README.md
git commit -m "docs: explain hologram analysis advisories"
```

## Phase risks and handoff

- Static resolution can be incomplete around reflection and framework registration. Possible edges must always suppress `×0`; never convert uncertainty into a definite count.
- Five-shingle scores can be unstable if extractor body events change. Golden threshold tests make such changes explicit and require a reviewed format/schema decision.
- `SymbolId` must stay line-independent. Symbol identity, aggregation, and
  advisory maps may never key by span or line. The one permitted span-keyed map
  is the frozen `(BodyEventKind, SourceSpan)` join between extractor events and
  their resolution records; it is local to body profiling and never defines
  symbol identity.
- Exact file leaves cost some tokens but are required for losslessness. Do not recover compactness with cross-file grouping, ranking, or truncation.
- The delivery phase must consume `AnalyzedProject` and `RenderIR`; it must not parse rendered text to rediscover semantic facts.
