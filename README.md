# hologram

hologram reads your codebase and writes one compact map of it — public callables, type
field names, relationships, project-internal calls, private identifiers, and the test
files/classes that cover the project — directly into the context files your coding
agents already read. The map is in context from turn zero, before any exploration
begins.

It ships as a pip package and as a single runnable file (`hologram.pyz`). It
installs its own parsers the first time it needs them,
and git hooks keep the map up to date after every commit. Generation is fully
deterministic — no LLM involved — so the same code always produces the same map,
and a map diff always means the code changed.

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
  project references, and the `· deps` lines show which modules are coupled, before
  you start pulling threads.
- **Debugging** — call chains, private-name lists, and `~N` body-size marks point at
  the right file before you open a single one.
- **Onboarding** — a new teammate, human or agent, reads one block and knows the
  territory: the modules, the vocabulary, the patterns.

## What the output looks like

The map of a small Java fixture:

```
# hologram · 186 LOC · state de55ba22cc9d
· C/R/I{fields} E{values} · f(args):Ret > project calls · ?=tests · ×0=no static use · !E=throws · p{a,b}=pa,pb · :T=supers · sealed:A|B · ←A|B=implementors · Self=own type · deps a→b=a uses b
· deps .→ids | engine→ids
src
 App(C) ×0
  main(args) ×0 > PricingEngine,evaluate,OrderId.of,ItemId.of
 delta
  AddOp,RemoveOp(R{nodeId})
   weight():int ×0
  DeltaOp(I) sealed:AddOp|RemoveOp
   weight():int ×0
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
? tests
 src/test
  PricingEngineTest.java{PricingEngineTest,BulkDiscounts}
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
  (framework entry points — route handlers, schedulers, listeners, Angular
  lifecycle hooks — are exempt) · `!UnknownItem` = throws (`Exception` suffix
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
  one token (`PricingEngineTest>applyDelta+2` — no braces).
- **Test helpers** — reusable drivers/builders/shared bases — render with a `*`
  sigil and, when referenced by other test files, their full public signatures:
  the reuse targets agents otherwise re-invent. Helpers living under directories
  named `fixtures`, `testdata`, or `resources` are never scanned (denylist).
- **`· deps a→b`** = module `a` uses types from module `b`: the import architecture
  without reading imports.
- **`state`** hashes the exact sources plus the generator, so source or extraction/
  rendering changes make old maps stale.

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
hologram review --root .                   # map-level drift check of uncommitted work
hologram review HEAD~3 --root .            # …or of the last three commits
hologram print --root .                    # write the map to stdout, touch nothing
hologram build --root . --budget 8000      # fit the map into a token budget
hologram uninstall --root .                # remove the hooks and embedded blocks
```

(Substitute `python3 hologram.pyz` or `python3 hologram.py` for `hologram` when
running the single-file form.)

A successful build prints the map's token cost and where it went:

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
- **API drift** — `+added −removed ~changed` in one line (`--brief K` prints just
  this for the last K commits);
- **placement** — a new symbol whose calls point overwhelmingly at a different
  module than the one it landed in.

Everything is advisory: `review` always exits 0, and `--quiet-if-clean` prints
nothing when there's nothing to say. `init` wires it into the post-commit hook,
which is the interesting part: when a *coding agent* commits, the findings print
into the agent's own session — the map answers back at exactly the moment the
mistake is cheapest to undo. Findings are heuristic (name similarity, call
affinity), so expect the occasional false positive; they point, you decide.

Review output is never embedded into context files — the embedded map stays a pure
function of the tracked sources, so a map diff always means the code changed.

## Fitting a token budget

The map is already compact (facts are chosen, never truncated), but if you need a
hard ceiling, `--budget N` applies a deterministic degradation ladder — const
values first, then test extras, private inventories, `×0` call chains, finally
methods of unreferenced types — stopping at the first level that fits. The level
is stamped in the header (`· budget 8000 L2`) and reused by every later rebuild
until you clear it with `--budget 0`. The same code and budget always produce the
same map. If even the deepest level doesn't fit, the map is emitted anyway with a
warning suggesting `--lang` filters — hologram never cuts a fact in half.

## Does it actually help? An honest take

hologram exists because of one specific failure: an agent lands in a repo with no
map, greps its way to a partial picture, and writes code that already exists.

**The good.** An agent normally burns thousands of tokens re-discovering project
structure every single session, and most of what it reads gets discarded. The map
replaces that exploration. Duplication gets a real counterweight: "does this already
exist?" becomes something the agent can see rather than something it only catches by
grepping the exact right word. And because the map shows your conventions — all your
ID types are one-field records, your services take dependencies through constructors —
a model tends to extend the patterns it sees rather than invent parallel ones. Factored
private names, concise call lines, and the test index tell it which file to open first
without a raw symbol dump.

**The caveats.** None of this is enforced. The map competes for the model's
attention like everything else in context, and an agent can ignore it and reimplement
a helper anyway — it shifts the odds, it is not a guardrail. Function bodies stay
invisible: a 500-line algorithm and a one-liner expose the same signature, so the
map tells an agent what exists, never how well it's built. `✓` means a test
mentions the function, not that the function is correct. If your naming is misleading,
the map compresses and transmits the misleading names with perfect fidelity. Depth
varies by language — the table above is honest about which ones get the full
treatment.

**What's been measured.** Two rounds on private codebases the models had never seen,
map-in-context vs matched control, headless sessions, transcripts and written code
reviewed ([full tables](benchmark/README.md)).

*Navigation and lookup* (constants, implementors, route→handler): with the map the
agent answers **in ~1 turn with zero file reads, straight off the map** — the control
reaches the same answers in ~3.5 turns, 2 searches, and +80% input tokens. Both are
100% correct; the map's win here is pure effort.

*A long generative task across model tiers* (one 60-turn test-writing task, 3 reps ×
map/control × haiku/sonnet/opus, all 18 runs passing acceptance):

| model | map turns | control turns | saving |
|---|---|---|---|
| haiku | 33.7 | 48.0 | −30% |
| sonnet | 28.3 | 37.0 | −24% |
| opus | 26.7 | 34.0 | −21% |

The saving replicates at every tier and grows as models get weaker; the map also
*stabilizes* sessions (map runs varied by ±1–3 turns, control runs by ±10–17). A
cheaper model with the map matched a stronger model without it on effort. The
sharpest result was about quality, not speed: the task required testing a real
implementation rather than a stub, and the mid-tier model did so in **1/3 control
runs vs 3/3 map runs** — the map's implementor lists steered it to the right
collaborator. The top tier didn't need the help (6/6 either way); the bottom tier
couldn't use it (0/6 either way). The map changes what a mid-tier model *does*, not
just how fast it does it.

A follow-up round at pinned low effort on the 0.6.0 map (test helpers +
coverage edges) produced the measurement program's only duplication event —
in the weakest model's control condition, re-inventing a helper the map
names; every map-equipped run reused it.

A third round measured the 0.7.0 review loop (map vs map+coaching vs
map+live post-commit review, two write-task shapes, sonnet + haiku, n=3):
zero duplication in every map-bearing condition, and the reviewer fired on
exactly the commits that drifted — naming the classes that already covered
what a parallel test file re-covered, and flagging dead-on-arrival
additions — while staying silent on clean commits. The honest half of the
result: at low effort the weakest tier *read* the findings but didn't
restructure already-committed work, and placement quality split by model
tier, not by condition. The reviewer surfaces drift the moment it happens;
what the agent does next still scales with capability. Caveats stay honest:
one private corpus per round (numbers published, corpus withheld), n=3 per
cell, quality judged on narrow task shapes. Classic
AI-slop markers (mock storms, duplicate test bodies, comment chatter) were largely
absent in *all* conditions — a strict corpus CLAUDE.md sets that floor, map or not.
On a famous OSS corpus the model has memorized, expect no benefit at all — a control
agent walks straight to the right API from training memory.

## How it works

One file, one pipeline: scan (only git-tracked files when inside a repo), extract,
render, embed. Each language has its own small extractor and they all produce the same
`Symbol` records, so everything downstream — receiver resolution, transitive
reduction, shape grouping, the final tree — is language-neutral and written once.
Formatting decisions were measured with a real tokenizer (o200k), not guessed.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

Runs under plain `python3` too — tests for languages whose grammar isn't installed
just skip.

## License

MIT — see [LICENSE](LICENSE).
