# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-08-14

Theme: clever token budgeting. `--budget` maps are unchanged at L0 — this
release only changes what happens under pressure. Unbudgeted maps are
byte-identical to 0.8.0 (verified on three corpora, modulo the state
stamp).

### Added

- **Makefile support** — `Makefile`/`makefile`/`GNUmakefile`/`*.mk` render
  as one node with each target as a command carrying its caller-settable
  variables: `deploy(ENV,MANIFEST)` means the recipe consumes `$(ENV)`
  (overridable `?=` or undefined) and `$(MANIFEST)`; variables the file
  pins with `=`/`:=` are internal and excluded. `.PHONY` and friends,
  pattern rules, and `define` bodies are skipped; `_name` targets are
  private. No parser dependency — a line scanner, like Helm.

### Changed

- **The degradation ladder is repaired and deepened to a skeleton floor.**
  The old ladder was lumpy (one level dropped −1,921 tokens on the
  reference corpus while another dropped −116), partly broken (the private
  wipe left per-class `- name` lines at every level), and shallow (floor
  −17%; an 8k budget was unreachable). The new ladder (measured per-level
  on the reference corpus): L1 coverage edges (−5%), L2 helper signatures
  (−8%), L3 all private inventories (−8%, bug fixed), L4 chains of
  *untested* functions (−6%, replaces the no-op), L5 methods of types with
  zero real fan-in (−3%, replaces the name-keyed heuristic), L6 all chains
  (−4%), L7 the **skeleton** — no method lines, const values gone, type
  headers with fields and top-level signatures stay (−21%). Floor on the
  reference corpus: **7,138 tokens, −54%** from the full map. Every level
  is monotone; facts degrade in usefulness order; the legend only explains
  what survived; nothing is ever cut mid-fact.
- **Const values survive to the skeleton, and degraded maps disclose
  their losses.** The first gate round dropped const values at L1 — the
  smallest saving (−0.3%) — and produced the worst failure: agents
  confidently answered value questions off a map that silently omitted
  the values. Now scalars ride to L7, and every degraded map carries a
  header disclosure naming the dropped fact classes with an instruction
  to read the source instead of guessing — measured to flip wrong
  zero-read route answers into correct one-read answers.
- The budget ladder no longer re-reads every source file per level
  (level-invariant work — LOC count, call resolution, helper detection —
  is computed once).

### Dev

- Benchmark task files accept a top-level `budget`, applied to the
  map-bearing conditions' build.

### Gates (measured before merge)

- Determinism, monotonicity, floor reachability: pass (numbers above).
- Refactor invariance: unbudgeted digests identical to v0.8.0 on the
  reference corpus, this repo, and the polyglot fixtures.
- Navigation at the skeleton floor: see benchmark/README.md.

## [0.8.0] - 2026-08-14

Theme: cheaper without being smaller. Every change was built behind a
measured merge gate (see benchmark/README.md); one feature — pre-commit
review timing — was built, measured, and reverted for showing no
behavioral gain.

### Changed

- **Token diet, same facts** — three same-fact-twice redundancies removed
  (reference corpus −3.7%, self −0.7%, zero information loss):
  constructors whose argument list restates the type header's field list
  (`PricingEngine(basePrices)` under `PricingEngine(C{basePrices})`) are
  suppressed, including the grouped `Self(fields)` form — any constructor
  carrying notes, ✓, sizes, throws, calls, or typed args stays; dunder
  methods (`__init__`, `__repr__`) leave the private inventories;
  the test index folds a file whose only test class matches its name
  (`PricingEngineTest>applyDelta+2`, no braces) and states a shared file
  extension once in the header (`? tests ·.java`).
- **The `· deps` block is gone.** It was a coarser restatement of the call
  chains, degenerated on shallow repos, and outlived richer facts under
  `--budget`. Gate evidence: all navigation/architecture answers stayed
  correct at 1 turn without it.
- **Coaching sentence** now says what to do with review findings (reuse
  the named original, consolidate re-covered tests) instead of just naming
  the command.

### Measured and reverted

- **Pre-commit review timing.** Review was moved to a pre-commit hook so
  findings would land *before* the commit; the gate round showed the
  reviewer firing identically — and, at low effort, agents acting on the
  findings exactly as often as post-commit: never. The post-commit form
  ships unchanged; the new `acted_on_findings` bench metric stays for 0.9.

### Dev

- Benchmark: `acted_on_findings` transcript metric (review report →
  later edit of a file the findings named → later commit).

## [0.7.0] - 2026-08-14

Theme: the map talks back. Until now the map was a passive briefing; the
0.6.0 rounds showed the remaining failures happen at *write time* — an
agent duplicates a helper or re-covers a tested path in the same session
that read the map. `hologram review` closes that loop: a deterministic
map-diff engine that runs from the post-commit hook, so its findings land
inside the committing agent's own context.

### Added

- **`hologram review [REV]`** — compares the map-level facts of the working
  tree against any revision (default `HEAD`) and reports drift findings:
  near-duplicate additions (with the original's map line as the pointer),
  tests re-covering already-covered paths, dead-on-arrival public symbols,
  orphaned test references to deleted production code, an API drift
  summary, and placement advice when a new symbol's call affinity points at
  a different module. Advisory only — always exits 0, prints nothing on a
  clean diff with `--quiet-if-clean`. `--brief K` prints just the API drift
  of the last K commits. Findings are never embedded into context files —
  the map stays a pure function of the tracked sources.
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

### Dropped during the release round

- Class-level `@DisplayName` sub-lines in the test index were built,
  measured at ~+7% map cost on a heavily annotated corpus with no
  demonstrated behavioral benefit, and removed before shipping. The
  extraction (decorators on test classes) remains; only the rendering is
  gone. `review`'s *dead* check was de-noised in the same pass: a new
  public symbol that any test file mentions is treated as exercised even
  when the call arrives through framework indirection with no static edge.

### Dev

- Benchmark harness: `AC` (map + coaching note) and `AR` (map + live
  review hook) conditions, a `review_seen` transcript metric, and
  workspace confinement — every condition now uses a local clone with the
  origin remote removed, and absolute paths in corpus context files that
  point outside the workspace are rewritten (an agent once followed one
  into the real corpus).

## [0.6.0] - 2026-08-13

Theme: the 0.5.0 benchmark findings become features. Measured misses —
agents re-inventing an existing test driver (17/18 runs), blindly
re-covering tested paths, and a saturated grep acceptance gate — each got
a structural answer.

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
  (optionally test files only); grep acceptance saturated while scope
  differences hid, so scope is now first-class.
- **Benchmark effort knob** (dev-facing) — `effort` per task file/task and
  `--effort` CLI, passed to the claude CLI natively or via a thinking-token
  mapping, no-op when unsupported.

### Changed

- Self-map: every test class gains coverage edges; this repo has no helper
  classes, so no `*` lines should appear here — a surprise `*` line in a
  rebuild is a detector regression.
- Token cost on the private measurement corpus: +6.4% digest against a +6%
  planning estimate — accepted and documented; the additions are precisely
  the facts the 0.5.0 round proved agents lack.
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
  `report --anon` for sharing runs over private corpora without leaking
  symbol names; `benchmark/tasks/local-*.json` is gitignored for private
  task files, and task files can pin a `lang` filter for condition-A maps.

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

Theme: more business-logic semantics for fewer tokens. Every fact added in
this release is paid for by lossless compression in the same release — the
same sources render to ~3.5% fewer o200k tokens than 0.2.0 while now carrying
routes, constants, implementor lists, and framework wiring. Small maps shrink
much more (the pymini fixture: −42%).

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
- **`hologram print`** — write the map to stdout without touching any file.
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
  the same sources always produce the same map, so a map diff always means the
  code changed.
- **Benchmark harness** — `benchmark/bench.py` runs headless agent sessions
  over task sets in map-embedded vs. control workspaces and reports effort and
  reuse metrics.
- **Packaging** — installable as `hologram-map` (`pip install
  "hologram-map[grammars]"` for all parsers), or copy `hologram.py` into a repo
  and run it with plain `python3`.

[Unreleased]: https://github.com/lazymaniac/hologram/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/lazymaniac/hologram/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/lazymaniac/hologram/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lazymaniac/hologram/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lazymaniac/hologram/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lazymaniac/hologram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lazymaniac/hologram/releases/tag/v0.1.0
