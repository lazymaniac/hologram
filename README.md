# hologram

hologram reads your codebase and writes one compact markdown map: public callables,
type field names, relationships, project-internal calls, private identifiers, and the
test files/classes that cover the project. Embed it in an agent's context to provide
the project vocabulary and shape before exploration begins.

It's a single Python file. It installs its own parsers the first time it needs them,
and git hooks keep the output up to date after every commit. Generation is fully
deterministic — no LLM involved — so the same code always produces the same digest,
and a digest diff always means the code changed.

The name: like a hologram, every fragment of the output carries the shape of the
whole. Token cost stays low by choosing compact facts instead of truncating them.

## What it's for

- **Feature planning** — plan against the real surface of the code: what already
  exists, which module the new thing belongs in, which family of types it should
  extend. Plans written this way survive contact with the codebase.
- **Implementation** — the agent (or you) finds the existing helper before writing a
  second one, follows the house conventions, and places code where it belongs.
- **Code review** — `hologram diff` shows a pull request's API drift on one screen,
  including the near-duplicate helpers that sneak in quietly.
- **Refactoring** — `×0` flags functions and classes with no statically observed
  project references, and the
  `· deps` lines show which modules are coupled, before you start pulling threads.
- **Debugging** — call chains, private-name lists, and `⋮N` body-size marks point at
  the right file before you open a single one.
- **Onboarding** — a new teammate, human or agent, reads one file and knows the
  territory: the modules, the vocabulary, the patterns.

## What the output looks like

The digest of a small Java fixture:

```
# hologram · 186 LOC · state 817a0445a77f
· C/R/I{fields} E{values} T:target · f(args):Ret > project calls · -=private · ?=tests · ×0=no static use · ✓=tested · ⋮N=lines · !E=throws · p{a,b}=pa,pb
· deps .→ids | engine→ids
src
 App(C) ×0
  main(args) ×0 > PricingEngine,evaluate,OrderId.of,ItemId.of
 delta
  AddOp,RemoveOp(R{nodeId}) : DeltaOp
   weight():int ×0
  DeltaOp(I) sealed:AddOp|RemoveOp
   weight():int ×0
 engine
  OrderStatus(E{NEW,PAID,SHIPPED})
   isTerminal():boolean ×0
  PricePort(I)
   quoteFor(order):Quote ×0
   supports(order):boolean ×0
  PricingEngine(C{basePrices}) : PricePort
   quoteFor(order):Quote ×0 > evaluate
   supports(order):boolean ×0
   evaluate(order,items):Quote !UnknownItem > UnknownItemException,Quote
  Quote(R{order,totalCents})
  UnknownItemException(C) : RuntimeException
 ids
  ItemId,OrderId,UserId(R{value})
   of(raw):⟨X⟩ > ⟨X⟩
? tests
 src/test
  PricingEngineTest.java{PricingEngineTest,BulkDiscounts}
```

Reading it is easier than it looks, and the legend on line 2 teaches the notation to
any LLM:

- **The tree** mirrors your directory layout, shared path prefixes stated once.
- **Types** expose field names rather than redundant field types.
  `PricingEngine(C{basePrices}) : PricePort` is a class with a `basePrices` field
  implementing `PricePort`. Records/interfaces use the same braces, enums list
  values, aliases retain their target, and sealed interfaces retain permitted types.
- **Functions** show parameter names and return types: `evaluate(order,items):Quote`.
  Types appear beside names only when overloads would otherwise collide.
- **Call chains** follow the `>`: what a function calls, in order. Variables resolve
  to their declared types (`PricingEngine.evaluate`, not `engine.evaluate`), standard
  library calls are dropped, and chains are transitively reduced — if `a > b` and
  `b > c`, then `a`'s line doesn't repeat `c`.
- **Same-shape types group.** `ItemId,OrderId,UserId(R{value})` is a family in one
  entry; `⟨X⟩` stands for each member's own name in the methods they share.
- **Markers**: `✓` = resolved call from a test · `⋮120` = the body is 120 lines ·
  `×0` = no statically observed project reference to a function/class/method ·
  `!UnknownItem` = throws
  (`Exception` suffix implied) · no `:Ret` = returns void · `» index.ts: A,B` =
  barrel re-exports.
- **Private members** always appear as names. Repeated prefixes factor losslessly:
  `_extract_{java,python,typescript}` means those three exact identifiers.
- **Tests** list every detected test file and its classes. Test functions are omitted
  because their names cost tokens without improving placement guidance.
- **`· deps a→b`** = module `a` uses types from module `b`: the import architecture
  without reading imports.
- **`state`** hashes the exact sources plus the generator, so source or extraction/
  rendering changes make old maps stale.

## Languages

| Language | What you get |
|---|---|
| Java, C#, TypeScript/JS, TSX/JSX | types with named fields, name-based signatures, relations, resolved calls, privates, aliases, object APIs, re-exports |
| Python | same, via the standard library's `ast` — zero dependencies |
| Kotlin | classes, data classes, enums, interfaces, named fields, supers, calls |
| Go, Rust, C, C++ | types, traits, structs, signatures, calls, receiver bindings |
| Vue, Svelte | the component plus everything in its `<script>` block |
| Lua | functions and methods with call chains (params by name — it's untyped) |
| HTML | element ids and custom-element tags, names only |
| Helm | template `define` names, `values.yaml` keys, chart name |

## Getting started

Clone it anywhere and point it at a repo:

```bash
python3 ~/workspace/hologram/hologram.py init --root /path/to/repo
```

That installs git hooks, adds a `.gitignore` entry, writes `PROJECT_DIGEST.md`, and
embeds the same map in `CLAUDE.md`. From then on the hooks rebuild both after every
commit, merge, and checkout. You never touch them again.

The first time it meets a language it has no parser for, it offers to set one up: it
creates a `.venv` next to itself and pip-installs the right tree-sitter grammar. You
type `y` once. Every later run finds that venv on its own, so plain
`python3 hologram.py …` always works. Python-only repos skip all of this — the
standard library is enough.

Everything it can do:

```bash
hologram.py build --root .                                 # rebuild file + embedded map
hologram.py build --root . --lang java --out DIGEST.md     # limit languages, pick the filename
hologram.py build --root . --if-stale                      # rebuild only if the code changed
hologram.py check --root .                                 # are file + embed current? exit 0 yes / 1 no
hologram.py diff HEAD~3 --root .                           # how did the API change since then?
hologram.py build --root . --no-embed                      # digest file only
```

## Staying fresh

A stale digest is worse than none — an agent trusting a description of deleted code
is confidently wrong. Three commands make freshness a non-issue:

- `check` recomputes the header's `state` hash in milliseconds, without parsing
  anything, and verifies both copies (`--no-embed` checks only the file). Wire it
  into CI or an agent harness.
- `build --if-stale` uses the same probe, so "rebuild just in case" costs nothing
  when nothing changed.
- `diff <rev>` points the same machinery backwards: it rebuilds the digest as it
  looked at an older revision and prints the difference — a pull request's API drift
  on one screen.

## Putting it in front of your agent

There are two delivery modes, and the difference matters more than anything else
about this tool.

**Embed it (the default) — the holistic view, always in context:**

```bash
hologram.py init --root /path/to/repo
```

This injects the digest directly into `CLAUDE.md` between managed markers, and
the git hooks keep the block fresh. Every agent session now *starts* with the
same exact map as `PROJECT_DIGEST.md` in its context window — no alternate compact
tier and no semantic truncation. Output size is controlled by representation:
parameter names, field names, resolved project calls, prefix-factored private names,
and file/class-only test indexing.

**Or keep it on disk** with `--no-embed` — the digest stays a file the agent
queries when it chooses to. Weaker (the benchmark below showed agents mostly don't
choose to), but free of context cost. If you use this mode, teach the query patterns
— copy this into `CLAUDE.md` / `AGENTS.md`:

```bash
hologram.py init --root /path/to/repo --no-embed
```

```markdown
## Project index: PROJECT_DIGEST.md

`PROJECT_DIGEST.md` indexes public signatures, named type fields, resolved project
calls, private identifiers, and test locations. Line 2 explains the notation.
Query it with grep when it is not embedded.

The queries and when to run them:
- **Who calls X** (before changing or removing X):
  `grep "> .*X" PROJECT_DIGEST.md` — one line per caller, receivers resolved
  to types. Source grep cannot answer this.
- **Does something like this already exist** (before writing ANY new helper):
  grep concept synonyms over the inventory, e.g.
  `grep -i "trim\|blank\|strip" PROJECT_DIGEST.md` — then reuse what you find.
- **Which candidate is canonical**: prefer lines marked `✓` (called by tests).
- **Where does new code belong**: the tree shows packages, `· deps a→b` shows
  module coupling, grouped families (`AId,BId(R{value})`) are the house
  conventions — extend a family, don't invent a parallel one.
- **Where does behavior live** (debugging): a class's `- name,name` line lists
  its private internals and `⋮N` marks heavy bodies; `? tests` gives test files
  and classes.

Rules:
- It says what exists, not what works — read the source before wiring anything
  critical. `✓` means a test names the symbol, nothing more.
- Freshness: run `hologram.py build --if-stale` (instant when fresh), or `check`
  for exit 0/1.
```

## Does it actually help? An honest take

hologram exists because of one specific failure: an agent lands in a repo with no
map, greps its way to a partial picture, and writes code that already exists.

**The good.** An agent normally burns thousands of tokens re-discovering project
structure every single session, and most of what it reads gets discarded. The digest
replaces that exploration with one file it consults when needed. Duplication gets a
real counterweight: "does this already exist?" becomes something the agent answers by
reading, instead of something it only catches by grepping the exact right word. And
because the digest shows your conventions — all your ID types are one-field records,
your services take dependencies through constructors — a model tends to extend the
patterns it sees rather than invent parallel ones. Factored private names, concise
call lines, and the test index tell it which file to open first without a raw symbol
dump.

**The caveats.** None of this is enforced. The digest competes for the model's
attention like everything else in context, and an agent can ignore it and reimplement
a helper anyway — it shifts the odds, it is not a guardrail. Function bodies stay
invisible: a 500-line algorithm and a one-liner expose the same signature, so the
digest tells an agent what exists, never how well it's built. `✓` means a test
mentions the function, not that the function is correct. If your naming is misleading,
the digest compresses and transmits the misleading names with perfect fidelity. Depth
varies by language — the table above is honest about which ones get the full
treatment.

**What's been measured — and it's a negative.** Two controlled runs
(34 headless agent sessions, n=1 per cell, sonnet + Claude Code tooling):
[spring-framework](benchmark/results-spring-2026-08-08.md) — 1.5M LOC, memorized
from training — showed identical outcomes at **+80% token cost** for the digest
condition. The decisive run, a private 133k-LOC codebase
the model had never seen (results withheld — private corpus), showed **A ≈ B on every metric**: same reuse (3/4 tasks
both conditions), zero duplicated helpers anywhere, identical navigation turns.
Control agents found the right existing APIs in 2–3 greps; the map couldn't beat
three greps. The duplication failure mode this tool was designed against did not
reproduce in 18 scored bait sessions.

Those two runs tested the **pull model** — digest on disk, agent instructed to
consult it. It mostly didn't, and when it did, it paid retrieval costs. The pull
model is measured-false at these scales.

The **push model** — `--embed`, the whole map in context from turn zero — was
then run as its own condition on the private corpus (10 sessions vs the same
baselines, transcript-reviewed): **outcomes stayed equal while effort dropped
~36% in turns and ~55% in searches, with navigation tasks 40% faster — one
answered in 4 turns with zero file reads, straight from the embedded map. Total
tokens came out level: the embed's per-turn cost was fully offset by fewer
turns.** That is the holistic thesis doing what it was supposed to do — the map
in context replaces exploration — and it's why `--embed` is the recommended
delivery. Caveats stay honest: n=1 per cell, one model, one corpus; duplication
was zero in every condition, so embed's win here is orientation speed, not
duplication prevention; and larger-repo tiers, weaker models, and chat-only
contexts remain unmeasured.

## How it works

One file, one pipeline: scan (only git-tracked files when inside a repo), extract,
render. Each language has its own small extractor and they all produce the same
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
