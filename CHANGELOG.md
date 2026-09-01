# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--features params`.** Parameter names were 14–24% of a real map and were
  the one large fact class with no name, because they sat in the always-on
  "identity" remainder alongside the trie and the type headers. They are a fact
  about the code, not the map's structure, so they are selectable now.
  Deselecting keeps arity and drops only the names — `place(order,items)`
  becomes `place(_,_)`, reusing the placeholder the renderer already emits for
  an argument no extractor could name — and takes about 7% off a map that keeps
  everything else.

### Changed

- **Grouped landmarks take the declared extension too.** `{ItemId,OrderId}.java`
  now renders `{ItemId,OrderId}` under a `# hologram ·.java` header, like every
  other leaf. They had been exempt so that payload naming files could tell a
  reader the bare node above it was a directory — which cost the hoist most of
  its value exactly where it pays best, on the one-public-class-per-file
  languages whose every landmark is grouped. Worth 3.9% of the `javamini`
  fixture, 0.7% of a Spring corpus and 0.5% (306 tokens) of a 16,000-file Java
  codebase. The reader's rule is now simply that a node with nodes below it is a
  directory; a directory whose files are all grouped landmarks reads as a file,
  measured at about one node in fifty, and never silently — the reconstructed
  path does not exist while the directory does.
- **Call targets are named relative to the caller.** The description ladder
  picked the shortest name that is unambiguous across the whole project, which
  is the right question to ask once and the wrong one to answer on every line:
  a chain inside `a/b/Order.java` that read `> a/b/Order.java:Order.total`
  spent 24 characters restating the node the reader was standing in. A target
  now carries only what the caller's own path, file and owner do not already
  say. Worth 7.9% of a monorepo of near-identical services, 2.4% of a Java
  example corpus, and 1.2% — 699 tokens — of a 16,000-file Java codebase. Two
  targets that would shorten to the same text both keep their project-wide
  names, so one name never stands for two.

### Fixed

- **Framework-wired code is no longer reported as dead.** `×0` means no static
  project reference, and a container's wiring is invisible to static fan-in, so
  Spring stereotypes and `@Bean` factories, JSR-330 `@Named`/`@Inject`,
  `@PostConstruct`/`@PreDestroy` lifecycle callbacks, JPA `@Entity`, and Spring
  AI/MCP `@Tool` surfaces now join routes, listeners and schedulers as
  entrypoints. On a Spring AI corpus this removed 63 of 113 `×0` markers — a
  `@Configuration @EnableWebSecurity` class and every `@Bean` factory had been
  reading as deletable — and made the map 38 tokens smaller in the process.

## [0.15.0] - 2026-08-31

Theme: you choose the facts, and the map stops repeating itself.

### Added

- **`--features` selects which fact classes the map carries.** A budget decides
  how much of the map fits; it could not say what was eligible in the first
  place.
  Twelve named classes (`calls`, `relations`, `fields`, `constants`,
  `decorators`, `raises`, `tested`, `usage`, `size`, `private`, `tests`,
  `support`) can now be dropped whole, with `types` making thirteen. Gating
  happens at each render site before the budget catalog, so a deselected class
  never enters the adaptive search and cannot be restored by it. The package
  trie, type headers, and public signatures are the map's identity and stay
  unselectable; `--features none` leaves exactly those. The selection is stamped
  in the footer and recalled like `--lang` and `--budget`, and `--interactive`
  on `build`/`init` prices a selection against the full map before writing it.
- **`--features types`.** Declared return and parameter types are now a
  selectable fact class, closing a gap between the feature catalog and the
  structure floor: L8 already stripped them, but nothing named them. They cost
  1.0% of this repository's map, 2.8% of a TypeScript corpus and 5.1% of a Java
  one — more than seven of the twelve classes that were already selectable.
- **L8, a structure-only floor below the semantic floor.** When even L7 cannot fit a
  `--budget`, Hologram now renders the same facts in project vocabulary alone: return
  and parameter types, decorators and route paths, `!throws`, `~N`/`✓`/`×0` markers,
  `{field}` lists, and `: super` / `←impls` / `sealed:` relations all go, leaving the
  source tree, type names, and function names with their parameter names. On this
  repository the floor drops from 714 to 460 tokens. Ranked whole facts still compete
  for slack above it, and the previous "no complete map fits" fallback now runs only
  when L8 overflows too. `BudgetStats.skeleton_tokens` keeps meaning the L7 semantic
  floor; `effective_detail` reports `L8` or `L8-adaptive:n/m`.

### Changed

- **The extension a map repeats is declared once.** A title line such as
  `# hologram ·.py` states the corpus extension, and the file nodes that carry
  it render bare; a leaf that states an extension has exactly the one it
  states. Worth 1.2–1.5% of a real map (57 tokens on a 4,936-token TypeScript
  corpus, 95 on a 6,220-token Java one) and nothing where it would not pay: a
  stem another node already owns (`extract/` beside `extract.py`), a stem still
  holding a dot (`shell.component`), and corpora too small or too mixed to earn
  the declaration all keep their extensions. Grouped landmarks
  (`{ItemId,OrderId}.java(R{value})`) keep theirs too, which is what tells a
  reader the bare node above them is a directory.
- **The test index states landmarks and support, never call targets.** The
  `> target +N` coverage edge is gone: it spent tokens naming one arbitrary
  target and counting the rest, and what a test file exercises is already
  legible from the case names it carries. `? tests` now holds exactly test
  classes, test functions and methods, helper classes, helper functions, and
  declared fixtures.
- **Fixtures and shared helper functions are named.** A fixture qualifies by
  declaration (`@pytest.fixture`, `@BeforeEach`, `@Rule`, …) because the
  framework injects it by name rather than calling it; a plain function has to
  be used by another test file. Teardown markers earn no name. Both render in
  the existing `*` group, whose legend clause is now `*=helper/fixture`.

## [0.14.0] - 2026-08-21

Theme: the map states what it costs.

### Added

- **The footer carries the corpus and map token counts.**
  `· 19,311 LOC · input 215,433 · output 5,999 tokens · state … · budget …` —
  `input` is the estimated token count of the scanned sources, `output` the
  estimated count of the map itself. The block is always loaded, so what it costs
  and what reading the sources instead would cost now travel with it. `output` is
  measured on text that contains `output`, so the render iterates the footer to a
  fixed point; where a token boundary makes two candidates alternate, the larger is
  stated — the map never claims to be cheaper than it is.

`render_simple` gained an optional `source_tokens` parameter alongside `loc`,
precomputable by callers and computed locally when omitted. Freshness and settings
readers are unchanged: LOC stays the footer's first field, which is how the
metadata line is located, and legacy metadata-bearing headers still parse.

The new fields cost nine tokens per map, flat.

## [0.13.0] - 2026-08-21

Theme: the floor is business logic only.

### Changed

- **The semantic floor drops the test index entirely.** At the deepest budget level
  the `? tests` section is absent — header, file landmarks, helper names, and
  test-to-business edges alike. Test file landmarks used to be unconditional floor
  content, so the tightest budgets paid for test orientation before business API.
- **Test file landmarks are now individually restorable facts** (`test-files`), ranked
  and admitted like any other optional fact above the floor. Admitting a suite/case
  label or a coverage edge also admits the landmark line it renders on, so a restored
  name never arrives without the file it belongs to.

Nothing changes for unlimited maps or for budgets that comfortably fit the index; the
difference appears only when a budget binds. On this repository the floor drops from
759 to 701 estimated tokens; corpora with hundreds of test files recover far more,
since the index is proportional to the test tree.

`hologram stats` reports the new `test-files` category alongside the existing ones —
the schema grows additively.

## [0.12.0] - 2026-08-20

Theme: the test index costs what it is worth.

### Changed

- **`? tests` renders through the same path-compressed trie as the source section.**
  Test files used to carry their whole relative path on every line, so a deep package
  tree restated one prefix hundreds of times. Directories are now stated once and the
  file landmark sits under them.
- **Wrapped case names no longer pad to the length of their path.** Continuation lines
  indent one level instead of aligning under an eighty-character prefix, which on a
  deep tree made the section half whitespace.

Both are representation changes: file landmarks, suite names, case names, helper
names, and test-to-business edges are byte-for-byte what they were, and the source
section is unchanged. On a 114k-LOC Java corpus with 293 test files the `? tests`
section drops 56% (140,913 to 61,788 characters) and the whole map goes from about
60,600 to about 40,900 estimated tokens. Repos whose tests sit in one shallow
directory see little change.

## [0.11.0] - 2026-08-16

Theme: more business-logic signal per always-loaded token.

### Added

- **Semantic whole-fact budgeting v2.** Optional facts now compete globally above a
  compact pushed floor. Tested and cross-file call paths, test-to-business edges,
  high-fan-in APIs, and breadth across source files outrank local private leaves.
  Method-call facts retain their owning method as a dependency, and every admission is
  still checked by rendering the complete digest against the hard digest ceiling.
- **Managed-context accounting.** CLI output, `stats`/`stats --json`, and benchmark
  rows distinguish digest, wrapper, coaching, and total managed-block estimates. This
  keeps the historical digest budget contract while exposing the full context cost an
  agent actually receives.
- **Inspectable selection reasons.** Budget statistics add deterministic retained and
  dropped reason counts while preserving existing fields additively.

### Changed

- Production landmarks now resolve to exact files. Conventional one-type files retain
  lossless cross-file shape grouping (`{A,B}.java(...)`), while multi-entity modules
  use explicit file nodes.
- Repeated names are factored in signatures, fields, relationships, re-exports, and
  call targets only when the notation is shorter. A private inventory entry is omitted
  only when the same exact target remains visible in a selected call chain.
- Tests retain one actionable landmark per file plus compact coverage/helper hints.
  Every recognized test function/method name and suite is shown by default to
  discourage duplicate coverage. Names are factored losslessly, same-named methods in
  different suites gain their suite owner, and every additional nonredundant label
  remains individually droppable when a tight budget needs to preserve business logic
  first. Root `tools` and `benchmark` code gets separate compact orientation instead of
  competing with business internals.
- Volatile LOC, freshness state, filters, targets, and budget metadata moved to the
  final digest line. Existing header-form maps remain readable, while unchanged
  semantic prefixes can now be reused by prompt caches.
- The fixed embedding note and budget omission warning are shorter and make no claim
  that an optional fact survived selection.

### Validation

- On Hologram's own test-heavy repository, retaining all 633 recognized case/suite
  names raises the deterministic full estimate from v0.10's 3,621 to about 8,260
  digest tokens and from 3,769 to about 8,370 managed-context tokens. This is an
  explicit duplicate-avoidance tradeoff; the compact floor still falls from about
  1,444 to about 760, and explicit budgets can drop every additional test-name label
  independently while keeping file landmarks. Full-grammar fixture estimates remain
  below v0.10 managed-context baselines. These are representation measurements, not a
  claim of improved model outcomes; the earlier matched evaluation predates this
  test-inventory change.

## [0.10.0] - 2026-08-14

Theme: make token choices and effectiveness experiments inspectable.

### Added

- **Adaptive whole-fact budgeting.** After the least-degraded complete L0–L7
  map fits, Hologram deterministically restores individual facts from the next
  quality boundary. Every trial measures the complete output, facts remain
  indivisible, the entrypoint skeleton stays mandatory, and a fixed trial
  bound keeps hook latency predictable. Adaptive headers use `A<level>` and
  structured stats identify the exact selection.
- **`hologram stats [--json]`** reports the budget policy version, selected,
  full, and skeleton token estimates, fit/utilization against the deterministic
  characters-per-four estimator, effective detail,
  and retained/dropped fact bundles without modifying a context file.
- **Structured review findings.** `hologram review --json` emits stable finding
  IDs plus check, kind, subject, path, and detail. The Python API can compare a
  baseline and final review as `seen`, `attempted`, or `resolved`; normal human
  review output remains unchanged. API drift now preserves overload sets and
  covers public kind, signature, field, relationship, mapped decorator/route,
  throw, constructor, and constant-value changes.
- **Resumable benchmark blocks.** Schema-v3 results identify the immutable
  experiment, treatment/control pair, cell, task, judge configuration, corpus, tool, and
  runner mode. `--resume` skips only when every planned cell in a task/repetition
  block is terminal, evidence-intact, and from the same wave/model; otherwise it
  reruns the whole block. Failed attempts remain append-only evidence. Referenced
  stdout and stderr artifacts are fsynced, sized, hashed, and verified before a
  block can satisfy resume.

### Changed

- Benchmark condition order is seeded and counterbalanced. Reports compare
  every selected treatment (`A`, `AC`, or `AR`) with `B` on matched
  task/repetition pairs, list incomplete pairs, summarize numeric fields
  with median ± MAD, and keep fresh, cache-created, and cache-read input tokens
  separate. Structural `accept_cmd` evidence is labelled as such; tasks can
  carry `manual_only` and versioned judge metadata.
- Eligible AR rows capture sanitized finding IDs during the existing
  post-commit review pass, compare their deduplicated union with the final
  working tree, and report resolved, persisting, and new-final counts. This is
  an identity-based final-state measure, not a correctness or attempted-action
  claim; finding content and IDs never enter the aggregate report.
- Real benchmark sessions require `--allow-unsafe-host`, an explicit
  acknowledgement that the agent runner is not a filesystem, credential,
  process, or network sandbox. Dry and real cells cannot share resume IDs.
- Acceptance commands declare disjoint pass/fail exit codes; undeclared exits
  are judge infrastructure errors. Provider terminal events must satisfy the
  metrics protocol, dry runs execute no acceptance shell, unknown task fields
  fail before spending, and repeated global or same-condition infrastructure
  failures open a circuit breaker.

### Security

- Release preparation audits index blobs and non-ignored working-tree files
  separately, plus built archives, against private-only paths and an optional
  external denylist.
  Release-artifact publication remains hard-disabled until repository-history remediation
  and a clean-clone privacy audit are complete.

### Validation

- Adaptive selection, structured review schemas, exact resume compatibility,
  atomic result persistence, matched reporting, parser completeness, and the
  release privacy gate have synthetic/public-only regression coverage. This
  release makes no unmeasured claim that a prompt or hook timing improves agent
  behavior.

## [0.9.1] - 2026-08-14

Theme: integrity before the next effectiveness experiment.

### Security

- Recalled `targets` metadata is resolved against the supported context-file
  set and must remain inside the repository. Context targets with any symlink
  path component or multiple hard links are rejected, so neither a tampered
  header nor a filesystem alias can redirect a map write into source or an
  outside file. Benchmark clone setup applies the same check before reading or
  rewriting corpus context files.

### Fixed

- **Quoted Python annotations no longer break call chains.** A PEP 484 string
  forward reference kept its quotes, so the declared type never matched its
  class, receiver resolution failed, and the edge silently vanished from the
  map — `def run(self, e: "Engine")` lost the `> evaluate` that
  `def run(self, e: Engine)` kept. References now resolve, including nested
  (`list["Engine"]`) and whole-string (`"Engine | None"`) forms, while
  `Literal[...]` members and `Annotated[...]` metadata stay verbatim because
  those strings are values, not type names.
- **An unreadable source file no longer crashes the build.** `_gather` skipped
  the `OSError` guard that `_state_hash` and `_total_loc` both have, so a
  permission-denied or dangling-symlink file left `check` reporting stale
  forever while `build` died on a traceback. Such a file is now skipped with a
  warning — never silently.
- **The legend no longer claims prefix factoring the map never used.** A type
  header carrying fields (`Config(T{host,port})`) was not stripped before the
  factoring check, so `p{a,b}=pa,pb` appeared spuriously and told readers to
  expand `T{host,port}` into `Thost,Tport`. The `T{fields}` form is now
  documented in the legend's own first clause.
- **`build --if-stale` honours a changed `--budget`.** The state hash covers
  sources, not settings, so a new budget left every stamp fresh and the
  rebuild was skipped with the old budget still in place. `check` stays
  source-only, since its own `--budget` is an accept-and-ignore option.
- **Whole-map budget selection** now compares complete L0–L7 candidates,
  including their stamps, legends, and loss disclosures. It chooses the
  least-degraded candidate that fits; when none fits, it emits the actual
  smallest candidate rather than blindly ending at L7. A generous budget
  still renders only L0.
- **External entrypoints survive pressure.** Framework routes/listeners
  (including non-public handlers) and public Make targets remain visible at L5
  and at the skeleton floor. Method ownership and cold-type identity prefer an
  exact file, then an unambiguous package/directory owner: sibling classes no
  longer exchange methods or fan-in, while split Go receivers, Rust impls, and
  C++ header/source definitions remain attached. Declaration/definition facts
  merge into one stable line.
- **Information-loss corrections.** Ordinary class constructors remain visible
  because fields do not prove public constructibility; only record constructors
  whose components fully restate the header are folded. `__init__` and
  `__repr__` stay omitted from private inventories, while meaningful protocol
  methods such as `__iter__`, `__enter__`, and `__call__` remain.
- **Makefile correctness.** Ordinary `=`, `:=`, `+=`, and `?=` assignments are
  caller-overridable as GNU Make specifies; only explicit `override`
  assignments pin a variable. Repeated/double-colon rules merge without
  duplicate symbols, source locations survive `define` bodies, continued
  prerequisites/recipes, custom `.RECIPEPREFIX`, escaped dollar parity, inline
  recipes, and Make comment boundaries are handled. Prerequisite targets form
  call edges (`deploy > build`), including under test-named directories, and
  Make entrypoints no longer receive `×0`.
- `init` and `uninstall` remove the managed review-only pre-commit line from the
  reverted 0.8 experiment while preserving every foreign hook command.
- Managed hook markers remain recognizable after a repository moves, so stale
  absolute roots cannot leave a second reviewer installed. Uninstall strips
  managed blocks but conservatively preserves every context/rule file because
  its basename and canonical seed are not durable proof of ownership.

### Dev

- **Benchmark acceptance is clean.** Setup state now lives under Git metadata,
  a no-op run cannot satisfy an any-diff gate, and setup asserts a pristine
  baseline captured before the agent runs. New files participate in map-diff
  judging. Runner and acceptance outcomes record status, exit code, timeout,
  duration, stderr/stdout, and immutable run-ID artifacts; missing terminal
  results, provider failures, and timed-out process groups cannot be accepted.
- Review metrics recognize the shipped `HEAD~1` output only when a tool result
  is correlated to the actual commit/review command. `review_action_proxy`
  names the edit-plus-commit heuristic honestly; `acted_on_findings` remains as
  a compatibility alias.
- Benchmark rows record the requested budget and effective map level/token
  estimate. Task IDs, conditions, regexes, formats, turn limits, budgets, reps,
  and selections are validated before spending tokens. Reports split compatible
  model/effort/budget/corpus/task-config/tool revisions and result schema, and
  exclude infrastructure failures from quality and cost means. CI now has an
  explicit dependency-free `core` profile and a `full` profile that preflights
  every grammar and rejects unexpected
  skips; tag publication runs the full profile and map-freshness gate too.

### Benchmark integrity

- The old workspace marker contaminated any-diff acceptance commands. This
  release removes the marker and rejects no-op runs.
- Budget selection now evaluates complete output rather than fact bodies alone.
- Private-corpus measurements and derived aggregates are not included in
  publishable documentation or release artifacts.

## [0.9.0] - 2026-08-14

Theme: deterministic token budgeting. Makefile support intentionally adds new
L0 facts in repositories that contain Make rules.

### Added

- **Makefile support** — `Makefile`/`makefile`/`GNUmakefile`/`*.mk` render
  as one node with each target as a command carrying its caller-settable
  variables: `deploy(ENV,MANIFEST)` means the recipe consumes `$(ENV)`
  (overridable `?=` or undefined) and `$(MANIFEST)`; variables the file
  pins with `=`/`:=` are internal and excluded. `.PHONY` and friends,
  pattern rules, and `define` bodies are skipped; `_name` targets are
  private. No parser dependency — a line scanner, like Helm. (0.9.1 corrects
  the assignment semantics: ordinary assignments are also CLI-overridable;
  only explicit `override` pins.)

### Changed

- **The degradation ladder is repaired and deepened to a skeleton floor.**
  The old ladder had ineffective transitions, retained private fragments, and
  could miss practical budgets. The new ladder removes coverage edges, helper
  signatures, private inventories, untested call chains, zero-fan-in methods,
  remaining chains, and finally non-entrypoint method lines. The skeleton keeps
  type headers with fields, external entrypoints, top-level signatures, const
  names, and test filenames. Nothing is cut mid-fact. (0.9.1 corrects selection
  for disclosure text that can make a deeper complete candidate larger.)
- **Const values survive to the skeleton, and degraded maps disclose
  their losses.** Scalars ride through L6, with their names retained at L7,
  and every degraded map carries a
  header disclosure naming the dropped fact classes with an instruction
  to read the source instead of guessing.
- The budget ladder no longer re-reads every source file per level
  (level-invariant work — LOC count, call resolution, helper detection —
  is computed once).

### Dev

- Benchmark task files accept a top-level `budget`, applied to the
  map-bearing conditions' build.

### Validation

- Determinism, fact-order degradation, and skeleton reachability are covered by
  regression tests.

## [0.8.0] - 2026-08-14

Theme: cheaper without being smaller. A pre-commit review variant was reverted
before release; the post-commit form remains.

### Changed

- **Token diet** — three repeated representations removed:
  constructors whose argument list restates the type header's field list
  (`PricingEngine(basePrices)` under `PricingEngine(C{basePrices})`) are
  suppressed, including the grouped `Self(fields)` form — any constructor
  carrying notes, ✓, sizes, throws, calls, or typed args stays; dunder
  methods (`__init__`, `__repr__`) leave the private inventories;
  the test index folds a file whose only test class matches its name
  (`ThemeTest>loadTheme+2`, no braces) and states a shared file
  extension once in the header (`? tests ·.java`).
- **The `· deps` block is gone.** It was a coarser restatement of the call
  chains, degenerated on shallow repos, and outlived richer facts under
  `--budget`.
- **Coaching sentence** now says what to do with review findings (reuse
  the named original, consolidate re-covered tests) instead of just naming
  the command.

### Reverted

- **Pre-commit review timing.** Review was moved to a pre-commit hook so
  findings would land *before* the commit. The post-commit form ships
  unchanged; the `acted_on_findings` bench metric remains available.

### Dev

- Benchmark: `acted_on_findings` transcript metric (review report →
  later edit of a file the findings named → later commit).

## [0.7.0] - 2026-08-14

Theme: the map talks back. `hologram review` adds a deterministic map-diff
engine that runs from the post-commit hook, so findings land inside the
committing agent's own context.

### Added

- **`hologram review [REV]`** — compares the map-level facts of the working
  tree against any revision (default `HEAD`) and reports drift findings:
  near-duplicate additions (with the original's map line as the pointer),
  tests re-covering already-covered paths, dead-on-arrival public symbols,
  orphaned test references to deleted production code, an API drift
  summary, and placement advice when a new symbol's call affinity points at
  a different module. Findings are advisory and do not make a successful
  invocation fail; invalid revisions and setup errors still exit nonzero.
  A clean diff prints nothing with `--quiet-if-clean`. `--brief K` prints just the API drift
  of the last K commits. Findings are never embedded into context files —
  the map stays a pure function of tracked sources, generator, and settings.
- **Review in the post-commit hook** — `init` now installs
  `build && review HEAD~1 --quiet-if-clean` after each commit, so a
  committing agent sees what its own commit drifted. Existing installs keep
  working; run `hologram init` again to upgrade the hook line in place.
- **Coaching sentence in the embed note** — the map's in-band note now
  tells the agent to check `? tests` before writing tests or helpers and to
  run `hologram review` before finishing.
- **`--budget N`** — optional token target for `build`/`init`/`print`. When
  the full map exceeds the budget, a deterministic degradation ladder drops
  whole fact categories (const values → test extras → private inventories →
  `×0` call chains → methods of unreferenced types) until it fits; the
  applied level is stamped in the header (`· budget N L2`), recalled on
  flagless rebuilds, cleared with `--budget 0`. Never truncates mid-fact;
  if even the last level exceeds the budget it emits anyway with a warning
  pointing at `--lang`.

### Fixed

- Annotation arguments with string literals keep their interior spacing —
  `@DisplayName("a, b")` no longer extracts as `"a,b"`.

### Dropped before release

- Class-level `@DisplayName` sub-lines in the test index were built and removed
  before shipping. The
  extraction (decorators on test classes) remains; only the rendering is
  gone. `review`'s *dead* check was de-noised in the same pass: a new
  public symbol that any test file mentions is treated as exercised even
  when the call arrives through framework indirection with no static edge.

### Dev

- Benchmark harness: `AC` (map + coaching note) and `AR` (map + live
  review hook) conditions, a `review_seen` transcript metric, and
  workspace confinement — every condition now uses a local clone with the
  origin remote removed, and absolute paths in corpus context files that
  point outside the workspace are rewritten.

## [0.6.0] - 2026-08-13

Theme: test placement, helper reuse, and scope-aware benchmark judging.

### Added

- **Per-class coverage edges in the test index** — the data behind the ✓
  marker, un-flattened: `{WorkspaceTest>make_workspace+1,ReportTest}` names
  the first non-obvious production symbol each test class exercises (targets
  guessable from the test's own name fold into `+N`; bare `+N` of 2 or less
  is suppressed). Answers "is this path covered, and by whom" without
  opening a test file.
- **Test helpers in the map** — reusable drivers, builders, and shared
  bases render under `? tests` with a `*` sigil; helpers referenced from
  other test files get their full public method signatures (with resolved
  production call chains), unreferenced ones are named only. Detection:
  classes on test paths whose file and name are both non-test-shaped, plus
  classes referenced by two or more other test files.
- **Benchmark scope judge** (dev-facing) — `expect_in_new_code` checks the
  workspace git diff for required collaborators in newly written code
  (optionally test files only), making scope a first-class result alongside
  command acceptance.
- **Benchmark effort knob** (dev-facing) — `effort` per task file/task and
  `--effort` CLI, passed to the claude CLI natively or via a thinking-token
  mapping, no-op when unsupported.

### Changed

- Self-map: every test class gains coverage edges; this repo has no helper
  classes, so no `*` lines should appear here — a surprise `*` line in a
  rebuild is a detector regression.
- All maps generated by 0.5.0 go stale on upgrade; rebuild with
  `hologram build --root .`.

## [0.5.0] - 2026-08-13

Theme: the 0.3.0 semantics, everywhere. Routes, annotations, and constants
were Java/Python/TS-only; this release levels them across C#, Kotlin, PHP,
Rust, Go, and Bash, closes the Angular template gap, and adds target
selection. Token cost is near-neutral: the rendering already existed, the
other languages now feed it.

### Added

- **Depth leveling** — C# attributes with ASP.NET routes (`[HttpGet("{id}")]`
  → `@GET/{id}`) and `const`/`static readonly` constants; Kotlin annotations
  with Spring routes and `const val` constants (top-level, objects, companion
  objects); PHP 8 attributes with Symfony routes (`#[Route('/x', methods:
  ['GET'])]`) and class constants; Rust attribute macros with actix routes
  (`#[get("/x")]`) and `const`/`static` items; Go constants (`iota` renders
  name-only); Swift and Scala throw extraction; Ruby `attr_*`/`@ivar` fields.
- **Bash scripts get a root node and variables** — each script renders as one
  node with its functions nested under it; top-level `VAR=`, `export`, and
  `readonly` assignments with literal values render inline, with the same
  secret redaction constants get (shell scripts are a prime secret habitat);
  command substitutions render name-only.
- **Angular template→component edges** — custom elements in inline
  `template:` strings and `templateUrl`-referenced html files resolve through
  a selector→class map, so a component line reads `AppComponent(C{…})
  @app-root > UserListComponent` — the same render-tree-as-call-graph shape
  React gets from JSX. Duplicate selectors are ambiguous and produce no edge.
- **`--target`** — choose which agent context files carry the map
  (`--target CLAUDE.md`); the restriction is stamped into the map header and
  recalled by later rebuilds and `check`, like `--lang`; restricting removes
  the managed block from deselected files (prose survives); a named target
  that doesn't exist yet is created and seeded; `--target all` restores
  auto-detection.
- **Benchmark harness** (dev-facing) — `expect_answer` regex grading of
  navigate-task answers, per-task `max_turns` (the short-vs-long-session
  dial), distinct-files-read metric, per-(condition, kind) report rows, and
  `report --anon` for identifier-free local inspection;
  `benchmark/tasks/local-*.json` is gitignored for private task files, and
  task files can pin a `lang` filter for condition-A maps. Private results
  still require explicit publication permission even when anonymized.

### Changed

- All maps generated by 0.4.0 go stale on upgrade (fingerprint covers the
  generator); rebuild with `hologram build --root .`.

## [0.4.0] - 2026-08-13

### Security

- **Secret-shaped constant values are redacted from the map.** The map is
  copied into context files that get committed, so constants whose *name*
  looks secret-bearing (`KEY`/`SECRET`/`TOKEN`/`PASSWORD`/`SALT`/…) or whose
  *value* matches a known credential prefix (`sk-`, `ghp_`, `AKIA`, `eyJ`,
  `-----BEGIN`, …) now render name-only. One shared gate
  (`symbols.const_signature`) is used by every extractor.
- **Git-hook lines escape shell-active characters** in interpolated paths
  (`$`, backtick, backslash); a repository path like `x$(cmd)` no longer
  executes when the hook runs, and paths containing a double quote are
  refused outright. Re-`init` still recognizes and replaces pre-existing
  unescaped lines.

## [0.3.0] - 2026-08-13

Theme: more business-logic semantics with compact rendering. Maps now carry
routes, constants, implementor lists, and framework wiring while preserving
lossless compression.

### Added

- **Decorators/annotations with business meaning** — new `Symbol.decorators`
  captured verbatim for Python, Java, and TypeScript; the render layer
  allowlists what earns tokens: HTTP routes as `@GET/users/{id}` (Spring,
  JAX-RS `@GET`+`@Path` pairing, Flask/FastAPI with `methods=[...]`, NestJS),
  class-level prefixes hoisted to the type header, Angular `@Component`
  collapsed to its selector, and curated markers (`@Transactional`,
  `@Scheduled`, `@Injectable`, `@property`, …). `@Override`-class noise never
  renders.
- **React support** — JSX elements become call edges (capitalized names only),
  so the component render tree appears as the existing call graph with zero
  new notation; `memo`/`forwardRef`/`observer`-wrapped components are
  extracted (previously they produced no symbol at all); `React.FC<Props>`
  type arguments replace untyped destructured params.
- **Angular support** — constructor-DI receiver resolution (including
  `@Inject(...)`-decorated and `readonly` parameter properties),
  `@Output() x = new EventEmitter<T>()` fields, route-config arrays (inline
  or via `RouterModule.forRoot`) rendered as `routes=/users→UserListComponent`
  lines, and lifecycle hooks exempt from `×0`.
- **Constants with values** — `= config.py: MAX_RETRIES=3,BASE_URL` lines for
  UPPER_SNAKE / `static final` constants with literal values (scalars ≤24
  chars inline, longer values and containers name-only — omission, never
  truncation). Python module-level constants were previously invisible
  entirely.
- **Interface→implementors index** — `PricePort(I) ←PricingEngine|MockPricer`
  states the relation once on the interface instead of `: PricePort` on every
  implementor; >6 implementors summarize to a count. Sealed hierarchies keep
  `sealed:A|B`.
- **`--lang` filters persist** — the filter is stamped into the map header
  (`· langs java`) and recalled by every later `build`/`check`/`print`/`diff`
  that doesn't pass `--lang`, so hooks and manual rebuilds keep a narrowed
  map narrowed; `--lang all` clears it. Staleness is scoped too: edits to
  out-of-filter files don't stale the map.
- **`tools/measure_tokens.py`** — dev-only o200k measurement harness (tiktoken
  in a scratch venv; the runtime stays dependency-free).

### Changed

- **The legend lists only the notation the map actually uses** — small maps
  carry a small legend (pymini's whole map: −36% from this alone).
- **Suffix factoring** — `{TaskLoader,Workspace,Report}Test` joins the
  existing prefix form; the test index shrinks ~27% on this repo.
- **ASCII where the tokenizer punishes glyphs** — body-size marker `~N`
  (was `⋮N`, 3 tokens/occurrence), own-name hole `Self` (was `⟨X⟩`,
  7 tokens); `Self` also reads natively in Rust/Swift/Python.
- **The embed note is 233 chars** (was 511) — ~120 tokens saved per context
  file.
- **`×0` no longer marks framework entry points** — route handlers,
  schedulers, listeners, and Angular lifecycle hooks are invoked by the
  framework; flagging every live endpoint as unused was misleading.
- **Python `@dataclass` renders as a record (`R`)**, matching Java records and
  Kotlin data classes.
- All maps generated by 0.2.0 go stale on upgrade (fingerprint covers the
  generator); rebuild with `hologram build --root .`.

### Fixed

- **Generic supers no longer split on type-argument commas** —
  `extends React.Component<Props, State>` used to produce the supers
  `[Component, State>]`.
- **TS interface methods are extracted** — the port/contract layer of a
  TypeScript codebase was invisible (Java interfaces already worked).
- **Untyped TS class fields bind through `new`-expression initializers**, and
  parameter decorators no longer defeat parameter parsing.

## [0.2.0] - 2026-08-13

### Added

- **Four new languages** — Ruby, PHP, Swift, and Scala. PHP gets the C#-level
  treatment (typed params, fields, supers, `$x = new T()` receiver bindings,
  throw extraction); Swift and Scala the Kotlin-level treatment (protocols /
  traits, inheritance, local-binding receiver resolution); Ruby gets untyped
  calls with `private`/`protected` section tracking.
- **`hologram print`** — write the map to stdout without modifying context or
  source files (missing-parser bootstrap may create its managed environment).
- **`hologram uninstall`** — remove the managed git-hook lines and strip the
  embedded map blocks (deleting the rule files hologram itself created);
  `--keep-blocks` limits it to hooks.
- **Oversized-map warning** — `build`/`init`/`print` warn on stderr when the
  map exceeds `--warn-tokens` (default 25,000; `0` disables). The map is still
  written exactly — size remains a representation decision, never truncation.
- **Five more agent context targets** — `AGENT.md` (Amp), `CONVENTIONS.md`
  (Aider), `.junie/guidelines.md` (JetBrains Junie), `.continue/rules/`
  (Continue, seeded with front matter), `.kiro/steering/` (Kiro).
- **Single-file zipapp** — releases now ship `hologram.pyz`; `python3
  hologram.pyz init --root .` works exactly like the old copy-one-file flow,
  including the grammar-venv bootstrap.

### Changed

- **Package layout** — the single 3,100-line `hologram.py` is now the
  `hologram/` package (`symbols` / `treesitter` / `extract/<lang>` / `gather` /
  `render` / `embed` / `bootstrap` / `cli`). The refactor was gated on
  byte-identical digests over the fixture corpora. A `hologram.py` shim remains
  at the repo root, so checkout-installed hooks keep working unchanged.
- **Generator fingerprint** now hashes every package source via
  `importlib.resources`, so checkout, wheel, and zipapp builds of the same code
  agree on freshness. **All maps generated by 0.1.0 go stale on upgrade** (by
  design — extraction and rendering changed); run `hologram init --root .`
  once per repo, which also heals hook lines pointing at the old install
  (`build` warns when it finds one).
- Hook lines now use the delivery mode that installed them: checkout shim path,
  `python -m hologram` for pip installs, or the `.pyz` path.

### Fixed

- **Public constructors are rendered.** Extractors always produced them; the
  renderer's kind filters silently dropped every public one.
- **The legend covers all emitted notation** — `:T` supers, `sealed:`, `»`
  re-exports, `⟨X⟩` grouping, and `deps a→b` were used but never explained.
- **`--lang` with an unknown language errors** instead of silently emitting an
  empty map.
- **Kotlin local-variable bindings** — `val e = Engine(); e.run()` now resolves
  to `Engine.run` in call chains.
- **`!E` throws extraction for Kotlin, C#, and C++** (`@Throws` annotations and
  throw statements/expressions); previously Java and Python only.
- **Rust supertraits** — `trait X: Y` bounds now land in the relations.

## [0.1.0] - 2026-08-13

First release. One self-contained Python file that compresses a codebase into a
compact, deterministic markdown map and embeds it into the context files coding
agents already read — so the shape of the code is in context from turn zero.

### Added

- **Compact map rendering** — a single-layout digest of the codebase: public
  callables with name-based signatures, named type fields, enum values,
  supertype/target relations, project-internal call chains (receiver-resolved
  through declared bindings, transitively reduced), prefix-factored private
  identifiers, and a file/class-level test index.
- **Inline markers** — `✓` covered by tests, `×0` no statically observed project
  use, `⋮N` body size, `!E` raised/thrown exceptions.
- **16 languages** — Python (stdlib `ast`), Java, TypeScript, JavaScript, TSX,
  Vue, Svelte, Go, Rust, C#, Kotlin, C, C++, Lua, Bash, CSS, HTML (including
  nested `<script>`/`<style>` blocks), and Helm templates. Extraction depth
  varies by language; see the README table.
- **Embed-only delivery** — the map is spliced between managed markers into
  every agent context file the repo already uses (`CLAUDE.md`, `AGENTS.md`,
  `GEMINI.md`, `QWEN.md`, `.cursorrules`, `.clinerules`, `.windsurfrules`,
  `.roorules`, `.rules`, `.github/copilot-instructions.md`, and managed files
  inside rule directories such as `.cursor/rules/`). Hand-written content
  outside the markers is preserved; repos with no context file get `CLAUDE.md`.
- **CLI** — `hologram build` (with `--if-stale`), `hologram init` (installs git
  hooks, then builds), `hologram check` (exit 0 fresh / 1 stale), `hologram
  diff <rev>` (API drift against another git revision), `--lang` filters,
  `--version`.
- **Freshness stamps** — every map carries a state hash covering the source
  files and the generator itself; `check` recomputes it without parsing, so
  staleness detection is cheap and any change to the tool invalidates old maps.
- **Git hooks** — post-commit, post-merge, and post-checkout hooks rebuild the
  embedded map automatically.
- **Parser bootstrap** — when a scanned language needs a missing tree-sitter
  grammar, hologram re-execs into a `.venv` next to itself, or offers to create
  one and install the grammars.
- **Determinism** — no LLM, no timestamps, no unordered iteration in output:
  under a fixed Python/parser toolchain, the same sources always produce the
  same map, so a map diff means the code changed.
- **Benchmark harness** — `benchmark/bench.py` runs headless agent sessions
  over task sets in map-embedded vs. control workspaces and reports effort and
  reuse metrics.
- **Packaging** — installable as `hologram-map` (`pip install
  "hologram-map[grammars]"` for all parsers), or copy `hologram.py` into a repo
  and run it with plain `python3`.

[Unreleased]: https://github.com/lazymaniac/hologram/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/lazymaniac/hologram/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/lazymaniac/hologram/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/lazymaniac/hologram/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/lazymaniac/hologram/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/lazymaniac/hologram/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/lazymaniac/hologram/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/lazymaniac/hologram/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/lazymaniac/hologram/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/lazymaniac/hologram/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/lazymaniac/hologram/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/lazymaniac/hologram/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/lazymaniac/hologram/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lazymaniac/hologram/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lazymaniac/hologram/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lazymaniac/hologram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lazymaniac/hologram/releases/tag/v0.1.0
