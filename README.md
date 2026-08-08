# hologram

hologram reads your codebase and writes one small markdown file that describes all of
it: every type, every signature, every relationship, and who calls what. Give that
file to an LLM agent and it knows the shape of your project without grepping through
it first.

It's a single Python file. It installs its own parsers the first time it needs them,
and git hooks keep the output up to date after every commit. Generation is fully
deterministic — no LLM involved — so the same code always produces the same digest,
and a digest diff always means the code changed.

The name: like a hologram, every fragment of the output carries the shape of the
whole. For scale: a real 77,000-line Java project with 727 files compresses to about
20,000 tokens — small enough to hand an agent whole.

## What it's for

- **Feature planning** — plan against the real surface of the code: what already
  exists, which module the new thing belongs in, which family of types it should
  extend. Plans written this way survive contact with the codebase.
- **Implementation** — the agent (or you) finds the existing helper before writing a
  second one, follows the house conventions, and places code where it belongs.
- **Code review** — `hologram diff` shows a pull request's API drift on one screen,
  including the near-duplicate helpers that sneak in quietly.
- **Refactoring** — `×N` fan-in marks show a symbol's blast radius, and the
  `· deps` lines show which modules are coupled, before you start pulling threads.
- **Debugging** — call chains, private-name lists, and `⋮N` body-size marks point at
  the right file before you open a single one.
- **Onboarding** — a new teammate, human or agent, reads one file and knows the
  territory: the modules, the vocabulary, the patterns.

## What the output looks like

The digest of a small Java fixture:

```
# javamini @30ab133 2026-08-08 · 200 LOC · state ca50854aec7c · regen: …/hologram.py build
· legend: (C)lass (R)ecord (I)nterface (E)num (F)n (T)ype-alias · (R: …)=components · …
· deps .→ids | engine→ids
src
 App(C)
  main(String[]) > PricingEngine,PricingEngine.evaluate,OrderId.of,ItemId.of
 delta
  AddOp,RemoveOp(R: String) : DeltaOp
   weight():int
 engine
  OrderStatus(E: NEW,PAID,SHIPPED)
  PricingEngine(C: Map<ItemId,Long>) : PricePort
   evaluate(OrderId,List<ItemId>):Quote !UnknownItem > UnknownItemException,Quote
 ids
  ItemId,OrderId,UserId(R: String)
   of(String):⟨X⟩ > ⟨X⟩
```

Reading it is easier than it looks, and the legend on line 2 teaches the notation to
any LLM:

- **The tree** mirrors your directory layout, shared path prefixes stated once.
- **Types** say what they are and what they're made of.
  `PricingEngine(C: Map<ItemId,Long>) : PricePort` is a class, constructed from that
  map, implementing `PricePort`. Records list components (`R:`), enums their values
  (`E:`), type aliases their target (`T:`), sealed interfaces their permitted
  subtypes.
- **Call chains** follow the `>`: what a function calls, in order. Variables resolve
  to their declared types (`PricingEngine.evaluate`, not `engine.evaluate`), standard
  library calls are dropped, and chains are transitively reduced — if `a > b` and
  `b > c`, then `a`'s line doesn't repeat `c`.
- **Same-shape types group.** `ItemId,OrderId,UserId(R: String)` is a family in one
  entry; `⟨X⟩` stands for each member's own name in the methods they share.
- **Markers**: `✓` = the name appears in the test suite · `⋮120` = the body is 120
  lines · `×N` = referenced from N other files · `!UnknownItem` = throws
  (`Exception` suffix implied) · no `:Ret` = returns void · `» index.ts: A,B` =
  barrel re-exports.
- **Private members** appear as packed name lists: `- rebalance,evict,writeThrough`
  under a class. Names alone reveal a lot of the internals at a fraction of the cost;
  `--private` upgrades them to full signatures.
- **`· deps a→b`** = module `a` uses types from module `b`: the import architecture
  without reading imports.
- **`state`** in the header is a hash of the exact sources the digest was built from —
  the freshness mechanism described below.

## Languages

| Language | What you get |
|---|---|
| Java, C#, TypeScript/JS, TSX/JSX | the full treatment: types, signatures, relations, resolved call chains, constructor deps, privates, type aliases, object-literal APIs, barrel re-exports |
| Python | same, via the standard library's `ast` — zero dependencies |
| Kotlin | classes, data classes, enums, interfaces, constructor deps, supers, calls |
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

That installs git hooks, adds a `.gitignore` entry, and writes the first
`PROJECT_DIGEST.md` at the repo root. From then on the hooks rebuild it after every
commit, merge, and checkout. You never touch it again.

The first time it meets a language it has no parser for, it offers to set one up: it
creates a `.venv` next to itself and pip-installs the right tree-sitter grammar. You
type `y` once. Every later run finds that venv on its own, so plain
`python3 hologram.py …` always works. Python-only repos skip all of this — the
standard library is enough.

Everything it can do:

```bash
hologram.py build --root .                                 # manual rebuild
hologram.py build --root . --lang java --out DIGEST.md     # limit languages, pick the filename
hologram.py build --root . --private                       # full signatures for private members
hologram.py build --root . --behaviors                     # include test names as behavior specs
hologram.py build --root . --if-stale                      # rebuild only if the code changed
hologram.py check --root .                                 # is the digest current? exit 0 yes / 1 no
hologram.py diff HEAD~3 --root .                           # how did the API change since then?
```

## Staying fresh

A stale digest is worse than none — an agent trusting a description of deleted code
is confidently wrong. Three commands make freshness a non-issue:

- `check` recomputes the header's `state` hash in milliseconds, without parsing
  anything, and answers yes or no. Wire it into CI or an agent harness.
- `build --if-stale` uses the same probe, so "rebuild just in case" costs nothing
  when nothing changed.
- `diff <rev>` points the same machinery backwards: it rebuilds the digest as it
  looked at an older revision and prints the difference — a pull request's API drift
  on one screen.

## Telling your agent about it

The digest only helps if the agent knows when to look at it. Copy this into your
project's `CLAUDE.md` or `AGENTS.md` (adjust the filename if you changed `--out`):

```markdown
## Project map: PROJECT_DIGEST.md

`PROJECT_DIGEST.md` at the repo root is a generated inventory of this codebase:
every public signature, type relation, and call chain, plus private member names.
Line 2 is the legend. Git hooks rebuild it on every commit; the `state` stamp in
its header makes freshness checkable.

Consult it BEFORE:
- writing any new function, class, or helper — search for an existing one first
  and reuse it (`×N` marks widely-used utilities, `✓` marks test-exercised ones)
- placing new code — the package tree, the `· deps a→b` lines, and grouped
  families (`AId,BId(R: UUID)`) are the house structure; extend it rather than
  inventing a parallel one
- exploratory grepping — look here first, then grep for the specific thing this
  file says exists
- opening files while debugging — `- name,name` private lists and `⋮N` body-size
  marks say where behavior and weight live

Rules:
- Read on demand; do not paste this file into every prompt. It pays off on
  unfamiliar territory, not on surgical fixes in files you already know.
- It says what exists, not what works. `✓` means a test references the symbol,
  nothing more. Read the source before wiring anything critical.
- Verify freshness first: rerun the regen command from the header with
  `--if-stale` (instant when fresh), or run `check` for a yes/no.
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
patterns it sees rather than invent parallel ones. Private-name lists and `⋮` weight
marks tell it which file to open first when debugging. All of this arrives at around
20k tokens for a mid-sized codebase, paid only when the agent actually reads the file.

**The caveats.** None of this is enforced. The digest competes for the model's
attention like everything else in context, and an agent can ignore it and reimplement
a helper anyway — it shifts the odds, it is not a guardrail. Function bodies stay
invisible: a 500-line algorithm and a one-liner expose the same signature, so the
digest tells an agent what exists, never how well it's built. `✓` means a test
mentions the function, not that the function is correct. If your naming is misleading,
the digest compresses and transmits the misleading names with perfect fidelity. Depth
varies by language — the table above is honest about which ones get the full
treatment. And the biggest caveat: the core claim, that agents with a digest duplicate
less and navigate faster, has not been rigorously benchmarked yet. The design is
argued from failure modes seen in practice, not measured against a control. That is
the largest open item.

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
