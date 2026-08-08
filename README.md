# hologram

Compresses a codebase into **one token-tight markdown file** — every type, signature,
relation, and call chain — that an LLM agent can read for instant whole-project context
instead of re-grepping the repo every session. A 77k-LOC / 727-file Java codebase
digests to ~20k tokens.

Deterministic (no LLM in generation), single-file, self-sufficient: it installs its own
parser grammars on first contact with a language and keeps itself fresh through git
hooks. Named hologram because each fragment of the output carries the shape of the
whole system.

## What the output looks like

Real output for a small Java fixture — signatures only, no prose; the LLM reads
semantics from names:

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
  DeltaOp(I sealed: AddOp|RemoveOp)
 engine
  OrderStatus(E: NEW,PAID,SHIPPED)
   isTerminal():boolean
  PricingEngine(C: Map<ItemId,Long>) : PricePort
   quoteFor(OrderId):Quote > evaluate
   evaluate(OrderId,List<ItemId>):Quote !UnknownItem > UnknownItemException,Quote
 ids
  ItemId,OrderId,UserId(R: String)
   of(String):⟨X⟩ > ⟨X⟩
```

How to read it (the one-line legend at the top of every digest teaches this to any LLM):

- **Tree** — a path-compressed package trie; walk it to reconstruct any file's directory.
- **Types** — `PricingEngine(C: Map<ItemId,Long>) : PricePort` = a class whose
  constructor takes that map, implementing PricePort. `(R: …)` record components,
  `(E: …)` enum values, `(T: string)` type alias, `(I sealed: A|B)` permitted subtypes.
- **Same-shape grouping** — `ItemId,OrderId,UserId(R: String)` collapses a family into
  one entry; shared methods print once with `⟨X⟩` standing for each member's own name;
  a member's divergent methods print on its own `Name: …` line.
- **Call chains** — after `>`: what the function calls, in first-call order. Receivers
  are **type-resolved** from declared params/fields/locals (`engine.evaluate` renders as
  `PricingEngine.evaluate`; calls through platform-typed variables are dropped, so no
  `bigint.signum` noise). Chains are **transitively reduced** (an entry reachable through
  a sibling is omitted; SCC-safe) and filtered to project-defined names minus
  project-wide ubiquitous helpers.
- **Markers** — `!UnknownItem` = throws (`Exception` suffix implied) · no `:Ret` = void ·
  `×N` = referenced from N other files (shown at ≥10) · `✓` = name appears in test
  files · `⋮N` = body is N lines (shown at ≥40, where implementation weight hides) ·
  `» index.ts: A,B` = barrel re-exports.
- **Private members** — packed name lists by default (`- evict,rebalance` under a class,
  `- util.py: _parse,_walk` per file): names alone say a lot about internals at ~⅓ the
  cost of signatures. `--private` upgrades them to full `-`-prefixed signatures.
- **`· deps a→b`** — module `a`'s code references types defined in `b`: the import
  architecture without reading imports.
- **`state`** — a content hash of the scanned sources; the freshness mechanism below.

## Languages

| Language | Depth |
|---|---|
| Java, C#, TypeScript/JS, TSX/JSX | types, signatures, relations, resolved call chains, ctor deps, privates, type aliases, object-literal APIs, barrel re-exports |
| Python (stdlib `ast`, zero deps) | same |
| Kotlin | classes/data/enums/interfaces, ctor deps, supers, calls, visibility |
| Go, Rust, C, C++ | types/traits/structs, signatures, calls, receiver bindings, visibility |
| Vue, Svelte | component symbol + everything in `<script>` blocks |
| Lua | functions/methods with call chains (params by name — untyped) |
| HTML | element ids + custom-element tags, names only |
| Helm | `{{ define }}` names, `values.yaml` keys, chart name (regex, chart-layout-gated) |

## Install / use

Standalone — clone anywhere and point it at any repo. No setup: on first contact with a
language it offers to create a `.venv` next to `hologram.py` and pip-install the needed
tree-sitter grammar (one `y`), then transparently re-execs into that venv on every later
run. Non-interactive contexts get the exact install command instead. Python-only repos
need no dependencies at all.

```bash
python3 ~/workspace/hologram/hologram.py init --root /path/to/repo    # once per repo: git hooks + .gitignore + first build
python3 ~/workspace/hologram/hologram.py build --root /path/to/repo   # manual rebuild (hooks do this automatically)
```

Output: `PROJECT_DIGEST.md` at the repo root, gitignored. After `init`,
post-commit/merge/checkout hooks keep it fresh.

More:

```bash
hologram.py build --root . --lang java,kotlin --out DIGEST.md  # restrict languages, custom output
hologram.py build --root . --private     # full signatures for private members
hologram.py build --root . --behaviors   # append test names as behavior specs (can be large)
hologram.py build --root . --if-stale    # rebuild only when sources changed (instant when fresh)
hologram.py check --root .               # exit 0 fresh / 1 stale — for agent harnesses and CI
hologram.py diff HEAD~3 --root .         # API drift between revisions, as a digest diff
```

## Freshness

The header's `state` stamp is a hash of the scanned sources. `check` recomputes it in
milliseconds without parsing anything and exits 0/1; `build --if-stale` uses the same
probe to make "rebuild when unsure" free. `diff <rev>` builds the digest for another
revision in a temporary git worktree and prints the body diff — a PR's API drift in one
screen, including near-duplicate helpers quietly appearing.

## Telling your agent about it

The digest only helps if the agent knows when to reach for it. Copy this into your
project's `CLAUDE.md` / `AGENTS.md` (adjust the filename if you changed `--out`):

```markdown
## Project map: PROJECT_DIGEST.md

`PROJECT_DIGEST.md` at the repo root is a generated inventory of this codebase:
every public signature, type relation, and call chain, plus private member names.
Line 2 is its legend. It is regenerated by git hooks; the `@hash` in its header
says which commit it describes.

Read it BEFORE:
- writing any new function, class, or helper — search the digest for an existing
  one first, and reuse instead of reimplementing (`×N` marks widely-used utilities)
- placing new code — the package tree and grouped shape families
  (`AId,BId(R: UUID)`) are the house conventions; extend a family, don't invent
  a parallel one
- exploratory grepping — check the digest first, then grep for the specific
  thing it says exists
- opening files while debugging — `- name,name` lines under a class list its
  private internals, so you open the right file first

Rules:
- Read this file on demand — do not paste it into every prompt. It pays off on
  tasks touching unfamiliar parts of the codebase; skip it for surgical fixes
  in files you already know.
- The digest says what exists, not what works. `✓` means a symbol is at least
  referenced from tests; absence of `✓` means no test names it. Read the source
  before wiring anything critical.
- Before trusting it, run the regen command from its header with `--if-stale`
  appended (instant when fresh), or `check` to just test freshness.
```

## Working with coding agents: an honest assessment

This tool exists because of a specific failure mode: an LLM agent lands in a repo with
no map, greps its way to a partial picture, and starts writing code that already exists.

**Where it genuinely helps**

- **Session economics.** An agent re-derives project structure every session: dozens of
  grep/read round-trips, easily 50–100k tokens of file content on a mid-sized repo, most
  of it discarded. The digest front-loads that map for ~20k tokens, read on demand, and
  the agent's searches become targeted lookups instead of exploration.
- **Duplication has a real counterweight.** The classic agent failure — writing a second
  `normalize()` because it never saw the first one — happens because existence of code is
  invisible until you grep the exact right word. A complete signature inventory with call
  chains makes "does this already exist?" answerable by reading. `×N` fan-in points at
  the canonical utilities; `hologram diff` shows a reviewer the near-duplicate helpers a
  PR quietly adds.
- **Convention transmission dampens bloat.** Generated code bloats worst when the model
  invents its own patterns. The digest shows the house grammar — all IDs are
  one-component records, services take deps by constructor, errors are sealed
  hierarchies — and a model shown a family extends it rather than inventing a parallel
  one.
- **Private names orient debugging.** `- rebalance,evict,writeThrough` under a cache
  class says where behavior lives before any file is opened; `⋮N` says where the
  implementation weight hides.
- **Freshness is machine-checkable.** `check` / `--if-stale` cost milliseconds, so an
  agent harness can gate on digest freshness mechanically instead of trusting prose.

**Where honesty is due**

- **It prevents nothing by itself.** The digest competes for the model's attention like
  any other context; an agent can ignore the inventory and reimplement a helper anyway.
  It shifts probabilities. It is not a guardrail.
- **Bodies stay invisible.** `⋮N` flags weight and `✓` flags test exercise, but a
  500-line algorithm and a one-liner still expose the same signature. It tells an LLM
  *what exists*, not *how it works* — and verbose implementation inside bodies is only
  flagged, never fought.
- **Existence ≠ correctness.** `✓` is evidence of exercise, not proof; an unmarked
  function is a warning, a marked one can still be wrong. `--behaviors` adds test names
  as specs, but on a test-heavy repo that can double the digest — hence opt-in.
- **It amplifies naming, including bad naming.** `doStuff2(Object):Object` compresses to
  exactly the nothing it says. On misleadingly-named codebases the digest transmits the
  misdirection more efficiently than reading the code would.
- **The residual staleness risk is the agent that never checks.** The stamp makes
  freshness checkable, not checked.
- **Language depth is uneven.** Java/C#/TS/Python get full treatment; Go/Rust/C/C++ skip
  idioms like Go `const`/`iota` enums and flatten C++ templates; Lua is untyped so
  params are names; Helm is regex, not a parser; Kotlin rests on a community grammar.
- **The core claim is unbenchmarked.** No rigorous eval yet shows agents with the digest
  duplicate less or navigate faster. The design is argued from observed failure modes,
  not measured against a control. This is the largest open item.

## Architecture

Single file, one pipeline: scan (git-tracked files only when in a repo) → extract →
render. Language-specific code is confined to per-language extractors that all produce
the same `Symbol` records; everything downstream — receiver resolution, transitive
reduction, shape grouping, rendering — is language-neutral. Format choices are measured
with a real tokenizer (o200k), not guessed.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

(Runs under plain `python3` too; grammar-dependent tests skip when tree-sitter grammars
are absent.)

## License

MIT — see [LICENSE](LICENSE).
