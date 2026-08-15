# hologram

hologram reads your codebase and writes one compact map of it — public callables, type
field names, relationships, project-internal calls, private identifiers, and the test
files/classes that cover the project — directly into the context files your coding
agents already read. The map is in context from turn zero, before any exploration
begins.

It ships as a pip package and as a single runnable file (`hologram.pyz`). It
installs its own parsers the first time it needs them,
and git hooks keep the map up to date after every commit. Generation uses no
LLM and is deterministic under a fixed Python/parser toolchain: the same
sources, Hologram version, settings, runtime, and grammar versions produce the
same map. Under those fixed inputs, a map diff means the code changed.

The name: like a hologram, every fragment of the output carries the shape of the
whole. Token cost stays low by choosing compact facts instead of truncating them.

## What it's for

- **Feature planning** — plan against the real surface of the code: what already
  exists, which module the new thing belongs in, which family of types it should
  extend. Plans written this way survive contact with the codebase.
- **Implementation** — the agent (or you) finds the existing helper before writing a
  second one, follows the house conventions, and places code where it belongs.
- **Code review** — `hologram diff` shows a pull request's API drift on one screen;
  `hologram review` goes further and names the near-duplicate helpers, re-covered
  test paths, and misplaced additions that sneak in quietly — from the post-commit
  hook, straight into the committing agent's context.
- **Refactoring** — `×0` flags functions and classes with no statically observed
  project references, and the call chains show which modules are coupled, before
  you start pulling threads.
- **Debugging** — call chains, private-name lists, and `~N` body-size marks point at
  the right file before you open a single one.
- **Onboarding** — a new teammate, human or agent, reads one block and knows the
  territory: the modules, the vocabulary, the patterns.

## What the output looks like

The map of a small Java fixture:

```
# hologram · 186 LOC · state 2c3a5cf0b580
· C/R/I{fields} E{values} · f(args):Ret > project calls · ?=tests · ×0=no static use · !E=throws · p{a,b}=pa,pb · :T=supers · sealed:A|B · ←A|B=implementors · Self=own type
src
 App(C) ×0
  main(args) ×0 > PricingEngine,evaluate,OrderId.of,ItemId.of
 engine
  OrderStatus(E{NEW,PAID,SHIPPED})
   isTerminal():boolean ×0
  PricePort(I) ←PricingEngine
   quoteFor(order):Quote ×0
   supports(order):boolean ×0
  PricingEngine(C{basePrices})
   PricingEngine(basePrices)
   quoteFor(order):Quote ×0 > evaluate
   supports(order):boolean ×0
   evaluate(order,items):Quote !UnknownItem > UnknownItemException,Quote
  Quote(R{order,totalCents})
  UnknownItemException(C) : RuntimeException
   UnknownItemException(item)
 ids
  ItemId,OrderId,UserId(R{value})
   of(raw):Self > Self
 transport
  Bicycle,Scooter(R{serial})
   wheels():int ×0
  Vehicle(I) sealed:Bicycle|Scooter
   wheels():int ×0
? tests ·.java
 src/test
  PricingEngineTest{PricingEngineTest,BulkDiscounts}
```

Reading it is easier than it looks, and the legend on line 2 teaches the notation to
any LLM:

- **The tree** mirrors your directory layout, shared path prefixes stated once.
- **The legend on line 2 lists only the notation this particular map uses**, so
  small maps carry a small legend.
- **Types** expose field names rather than redundant field types.
  `PricingEngine(C{basePrices})` is a class with a `basePrices` field.
  Records/interfaces use the same braces, enums list values, aliases retain their
  target, and sealed interfaces retain permitted types. Python `@dataclass`
  renders as a record (`R`).
- **Interface relations are stated once, on the interface**:
  `PricePort(I) ←PricingEngine` names the implementors, so the domain's
  variation points read off one line. Non-interface supers keep the `: T` suffix.
- **Functions** show parameter names and return types: `evaluate(order,items):Quote`.
  Types appear beside names only when overloads would otherwise collide.
- **Routes and annotations** that carry business meaning render after the
  signature: `find(id):User @GET/users/{id}` (Spring, JAX-RS, Flask/FastAPI,
  NestJS), `@app-user-list` (Angular selector), `@Transactional`-style markers.
  Noise annotations (`@Override`, Lombok, …) never appear. Angular route configs
  render as `routes=/users→UserListComponent` lines; in React/TSX, JSX usage
  becomes call edges, so the component render tree is the call graph.
- **Constants are business rules**: `= config.py: MAX_RETRIES=3,BASE_URL` lists
  UPPER_SNAKE/static-final constants, with scalar literal values inline.
- **Call chains** follow the `>`: what a function calls, in order. Variables resolve
  to their declared types (`PricingEngine.evaluate`, not `engine.evaluate`), standard
  library calls are dropped, and chains are transitively reduced — if `a > b` and
  `b > c`, then `a`'s line doesn't repeat `c`.
- **Same-shape types group.** `ItemId,OrderId,UserId(R{value})` is a family in one
  entry; `Self` stands for each member's own name in the methods they share.
- **Markers**: `✓` = resolved call from a test · `~120` = the body is 120 lines ·
  `×0` = no statically observed project reference to a function/class/method
  (external entry points — route handlers, schedulers, listeners, Angular
  lifecycle hooks, and Make targets — are exempt) · `!UnknownItem` = throws (`Exception` suffix
  implied) · no `:Ret` = returns void · `» index.ts: A,B` = barrel re-exports.
- **Private members** always appear as names. Repeated prefixes and suffixes
  factor losslessly: `_extract_{java,python,typescript}` and
  `{TaskLoader,Workspace}Test` each mean those exact identifiers.
- **Tests** list every detected test file and its classes, each class carrying a
  coverage edge to the first non-obvious production symbol it exercises
  (`{WorkspaceTest>make_workspace+1,…}`; `+N` = more targets). Test functions are
  omitted because their names cost tokens without improving placement guidance.
  When every test file shares one extension it is stated once in the header
  (`? tests ·.java`), and a file whose only test class matches its name folds to
  one token (`ThemeTest>loadTheme+2` — no braces).
- **Test helpers** — reusable drivers/builders/shared bases — render with a `*`
  sigil and, when referenced by other test files, their full public signatures:
  the reuse targets agents otherwise re-invent. Helpers living under directories
  named `fixtures`, `testdata`, or `resources` are never scanned (denylist).
- **`state`** hashes the exact sources plus the generator, so source or Hologram
  extraction/rendering changes make old maps stale. It does not fingerprint the
  Python runtime or optional parser package versions; rebuild after upgrading
  that toolchain even when `check` reports fresh.

## Languages

| Language | What you get |
|---|---|
| Java, C#, TypeScript/JS, TSX/JSX | types with named fields, name-based signatures, relations, resolved calls, privates, aliases, object APIs, re-exports; Java additionally annotations/routes and static-final constants; C# additionally attributes/routes (ASP.NET) and constants |
| TypeScript (Angular) | `@Component` selectors, `@Injectable`, constructor DI receiver resolution, `@Input`/`@Output` fields, route configs (`routes=/path→Component`), template→component usage edges (inline `template:` and `templateUrl`) |
| TSX/JSX (React) | JSX usage as call edges (the render tree is the call graph), `memo`/`forwardRef`-wrapped components, `React.FC<Props>` prop types |
| Python | same as Java tier, via the standard library's `ast` — zero dependencies; decorators/routes (Flask, FastAPI), module constants, `@dataclass` as record |
| Kotlin | classes, data classes, enums, interfaces, named fields, supers, calls, local-variable receiver bindings, `@Throws`/throw extraction, annotations/routes (Spring), `const val` constants |
| Go | structs, interfaces, signatures, calls, receiver bindings, constants |
| Rust | structs, traits (with supertraits), enums, signatures, calls, receiver bindings, attribute macros/routes (actix), constants |
| C, C++ | types, structs, signatures, calls, receiver bindings; C++ additionally throw extraction |
| PHP | classes, interfaces, traits, enums, typed params, fields, supers, `$x = new T()` bindings, throw extraction, PHP 8 attributes/routes (Symfony), class constants |
| Swift | classes, structs, enums, protocols, inheritance, typed params, fields, `let x = T()` bindings, throw extraction |
| Scala | classes, case classes, traits, objects, extends, typed params, fields, `val x = new T()` bindings, throw extraction |
| Ruby | classes, modules, methods with param names and call chains, `attr_*`/`@ivar` fields; `private`/`protected` sections respected (untyped — no receiver resolution) |
| Vue, Svelte | the component plus everything in its `<script>` block |
| Lua | functions and methods with call chains (params by name — it's untyped) |
| Bash/zsh (`.sh`, `.bash`, `.zsh`) | one node per script with its functions nested under it, command-call chains, variables with literal values (secret-redacted); `_name` = private |
| HTML | element ids and custom-element tags, plus nested `<script>`/`<style>` blocks run through the JS/CSS extractors (when those grammars are installed) |
| CSS | class/id selectors, custom properties (`--x`), `@keyframes` names — names only |
| Helm | template `define` names, `values.yaml` keys, chart name |
| Makefile (`Makefile`, `*.mk`) | targets as external commands with caller-settable recipe variables (`deploy(ENV,MANIFEST)`) and prerequisite call edges (`deploy > build`); ordinary `=`, `:=`, `+=`, and `?=` values are overridable, explicit `override` values are internal; repeated/double-colon rules merge; `.PHONY`/pattern rules skipped; `_name` = private |

## Getting started

Install from PyPI (the `grammars` extra pulls in every tree-sitter parser up front):

```bash
pip install "hologram-map[grammars]"
```

```bash
hologram init --root /path/to/repo
```

Or skip installation entirely — download the single-file `hologram.pyz` from the
[latest release](https://github.com/lazymaniac/hologram/releases) (or clone the
repo and use `hologram.py`) and point it at a repo:

```bash
python3 hologram.pyz init --root /path/to/repo
```

That installs git hooks and embeds the map in every agent context file the repo
already has. From then on the hooks refresh it after every commit, merge, and
checkout. You never touch them again.

The first time it meets a language it has no parser for, it offers to set one up: it
creates a `.venv` next to itself and pip-installs the right tree-sitter grammar. You
type `y` once. Every later run finds that venv on its own, so plain
`python3 hologram.py …` always works. Python-only repos skip all of this — the
standard library is enough.

Everything it can do:

```bash
hologram build --root .                    # refresh the embedded map
hologram build --root . --lang java        # limit to one or more languages;
                                           # the filter is stamped into the map and
                                           # reused by every later rebuild/check
                                           # (clear with --lang all)
hologram build --root . --if-stale         # rebuild only if the code changed
hologram check --root .                    # is every context file current? exit 0 yes / 1 no
hologram diff HEAD~3 --root .              # how did the API change since then?
hologram review --root .                   # map-level drift in tracked/staged work
hologram review HEAD~3 --root .            # …or of the last three commits
hologram review --root . --json            # stable finding IDs for automation
hologram print --root .                    # stdout only; context/source files stay untouched
hologram build --root . --budget 8000      # fit the map into a token budget
hologram stats --root . --budget 8000      # inspect that decision; add --json for tooling
hologram uninstall --root .                # remove the hooks and embedded blocks
```

(Substitute `python3 hologram.pyz` or `python3 hologram.py` for `hologram` when
running the single-file form.)

A successful build prints the map's estimated token cost and where it went:

```
hologram: 1193 tokens embedded in CLAUDE.md, AGENTS.md
```

## Which agents get the map

`init`/`build` detect the context files a repo already uses and attach the map to each
one — the same map, everywhere, so Claude Code and Codex and Cursor can't drift apart:

| Agent | File it reads |
|---|---|
| Claude Code | `CLAUDE.md` |
| Codex, opencode, Jules, Zed | `AGENTS.md` |
| Amp | `AGENT.md` |
| Gemini CLI | `GEMINI.md` |
| Qwen Code | `QWEN.md` |
| Aider | `CONVENTIONS.md` |
| GitHub Copilot | `.github/copilot-instructions.md`, `.github/instructions/` |
| Cline | `.clinerules` (file or directory) |
| Cursor | `.cursorrules`, `.cursor/rules/` |
| Windsurf | `.windsurfrules`, `.windsurf/rules/` |
| Roo Code | `.roorules`, `.roo/rules/` |
| JetBrains Junie | `.junie/guidelines.md` |
| Continue | `.continue/rules/` |
| Kiro | `.kiro/steering/` |

Existing files are attached to, never invented: hologram only writes a context file
that already exists. Rule *directories* get one managed file of hologram's own
(`.cursor/rules/hologram.mdc`, `.clinerules/hologram.md`, …), created with whatever
front matter that agent needs to load it. A repo with none of these gets a `CLAUDE.md`.

Inside each file the map lives between two HTML-comment markers, and the block opens
with a short note telling the agent what it is looking at. Everything you wrote around
the block is preserved on every rebuild — the map is a block in your instructions
file, not a replacement for it.

## Staying fresh

A stale map is worse than none — an agent trusting a description of deleted code
is confidently wrong. Three commands make freshness a non-issue:

- `check` recomputes the `state` hash in milliseconds, without parsing anything, and
  compares it against the stamp in every context file. Any target lagging means exit
  1. Wire it into CI or an agent harness.
- `build --if-stale` uses the same probe, so "rebuild just in case" costs nothing
  when nothing changed.
- `diff <rev>` points the same machinery backwards: it rebuilds the map as it
  looked at an older revision and prints the difference — a pull request's API drift
  on one screen.

## Reviewing changes — the map talks back

`hologram review [REV]` compares the map-level facts of your working tree against a
revision (default `HEAD`) and reports what drifted:

- **near-duplicates** — a new function whose name is suspiciously close to an
  existing one in another file, *with the original's map line as the pointer*
  (delegating to the original doesn't count — that's reuse);
- **re-covered paths** — a new test edge to a production symbol some other test
  class already covers;
- **dead on arrival** — a new public symbol with zero static references;
- **orphaned tests** — a test still naming production code this change deleted;
- **API drift** — `+added −removed ~changed` in one line across public symbol
  kinds, overloads, callable signatures, fields, relationships, mapped
  routes/annotations, throws, constructors, and constants (`--brief K` prints
  just this for the last K commits);
- **placement** — a new symbol whose calls point overwhelmingly at a different
  module than the one it landed in.

Findings are advisory and do not change a successful invocation's exit status;
invalid revisions or setup failures still exit nonzero. `--quiet-if-clean`
prints nothing when there's nothing to say, and `--json` exposes stable finding IDs and
structured metadata for harnesses that verify whether a finding remains in the
final state. Review JSON includes source paths, subjects, details, and
corpus-derived IDs; do not publish it without explicit authorization from the
corpus owner. `init` wires review into the post-commit hook,
which is the interesting part: when a *coding agent* commits, the findings print
into the agent's own session — the map answers back at exactly the moment the
mistake is cheapest to undo. Findings are heuristic (name similarity, call
affinity), so expect the occasional false positive; they point, you decide.
Foreign hook scripts that `exec` another tool will skip an appended hologram
line — a limitation every appended hook line has.

Review output is never embedded into context files — under a fixed runtime/parser
toolchain, the embedded map stays a pure function of tracked sources plus the
Hologram version and settings. Review scans
Git-indexed files; use `git add -N path/to/new-file` before reviewing a wholly
untracked addition.

## Fitting a token budget

The map is already compact (facts are chosen, never truncated), but `--budget N`
can target an estimated ceiling with a deterministic degradation ladder. Budget,
fit, and utilization use the dependency-free `ceil(characters / 4)` estimate;
they are deterministic planning values, not a hard limit from a model tokenizer.
Hologram compares
the complete candidates — stamp, legend, disclosure, and facts — and selects the
least-degraded one that fits:

| level | drops |
|---|---|
| L1 | test-index coverage edges |
| L2 | test-helper method signatures |
| L3 | private-name inventories |
| L4 | call chains of untested functions |
| L5 | methods of types with zero static fan-in, except external entrypoints |
| L6 | all remaining call chains |
| L7 | non-entrypoint method lines and const values — the **skeleton**: type headers with fields, external route/listener/Make commands, top-level signatures, const names, test file names |

The applied level is
stamped in the header (`· budget 8000 L2`) and reused by every later rebuild until
you clear it with `--budget 0`; the same code and budget always produce the same
map, and the legend only explains notation that survived. Facts degrade in
usefulness order — untested paths lose chains before tested ones, cold types lose
methods before referenced ones, and externally invoked routes, listeners, and Make
targets survive every level. High-value scalar values ride through L6 and their
names remain in the skeleton. Every degraded map carries a disclosure line naming exactly which
fact classes were dropped, with an instruction to read the source instead of
guessing. Disclosure text can make a deeper candidate larger on tiny maps;
selection therefore uses total size, not level order. If no complete candidate
fits, the smallest candidate is emitted with a warning suggesting `--lang`
filters — hologram never cuts a fact in half.

When a complete level fits, Hologram uses remaining room to restore whole facts
from the next quality boundary. It tries smaller rendered payloads first within
each semantic category and interleaves categories deterministically, so a long
high-priority fact cannot consume the search cap without testing small facts in
the other categories. Adaptive output is stamped `A<level>` (for example,
`· budget 8000 A3`), while `stats` reports its exact selection and effective
detail. Selection stops once the
complete map is within one percent of the target or reaches its fixed trial
bound, keeping post-commit rebuild time predictable on large repositories.
`hologram stats --budget N` explains the choice without modifying context or
source files (missing-parser bootstrap may still create its managed environment);
`--json` includes the policy version, full/selected/skeleton estimates, fit and
utilization, effective detail, retained/dropped bundle IDs, trial count, search
truncation, and stop reason. Bundle IDs contain source paths and symbols, so
treat JSON statistics as corpus-derived data rather than publication-safe output.

## Evaluating effectiveness

Hologram targets a common failure mode: an agent develops a partial picture of
a repository and writes code that already exists. The map gives duplication a
counterweight and makes project structure available without a full exploratory
scan. It remains context, not enforcement: agents can ignore it, bodies stay
invisible, `✓` means a test references a symbol rather than proving correctness,
and extraction depth varies by language.

The benchmark harness supports matched map/control experiments with immutable
artifacts and revision-aware reports. Private-corpus prompts, transcripts,
results, and derived aggregates are not published. See
[benchmark/README.md](benchmark/README.md) for the privacy boundary and for
instructions on running an authorized evaluation.

## How it works

One file, one pipeline: scan (only git-tracked files when inside a repo), extract,
render, embed. Each language has its own small extractor and they all produce the same
`Symbol` records, so everything downstream — receiver resolution, transitive
reduction, shape grouping, the final tree — is language-neutral and written once.
Formatting decisions were measured with a real tokenizer (o200k), not guessed.

## Tests

```bash
python3 tools/run_tests.py --profile core       # no optional grammars required
.venv/bin/python tools/run_tests.py --profile full  # every grammar, zero skips
```

The full profile preflights every registered parser and fails on any unexpected
skip, so a broken grammar install cannot silently turn CI green.

## License

MIT — see [LICENSE](LICENSE).
