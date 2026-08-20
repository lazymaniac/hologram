# hologram

Hologram gives coding agents a compact map of a repository before they start
exploring it. The map keeps exact source paths, public callables, named fields,
relationships, project-internal calls, constants, useful private names, and
pointers to tests, tools, and benchmarks in the context files agents already
read.

There is no LLM, query step, or separate index service. Generation runs locally
and is deterministic for the same sources, Hologram build, settings, runtime,
and parser versions.

## Quick start

Hologram requires Python 3.11 or newer. For a polyglot repository, install it
with all tree-sitter grammars:

```bash
python3 -m pip install "hologram-map[grammars]"
hologram init --root .
```

`init` builds the map, embeds it in the repository's agent context files, and
installs hooks that refresh it after commits, merges, and checkouts. Content
outside Hologram's managed block is left untouched.

Python-only repositories can use the dependency-free base package:

```bash
python3 -m pip install hologram-map
```

You can also download the single-file `hologram.pyz` from the
[latest release](https://github.com/lazymaniac/hologram/releases):

```bash
python3 hologram.pyz init --root .
```

If a parser is missing, an interactive run can create a nearby `.venv` and
install only the grammars it needs. Non-interactive runs print the equivalent
install command instead.

## What the map looks like

An abridged Java map looks like this:

```text
# hologram
· C/R/I{fields} · f(args):Ret > calls · ×0=unused · !E=throws · p{a,b}s=pas,pbs · ←A|B=implementors
src
 App.java(C) ×0
  main(args) ×0 > PricingEngine,evaluate,{Order,Item}Id.of
 engine
  PricePort.java(I) ←PricingEngine
   quoteFor(order):Quote ×0
  PricingEngine.java(C{basePrices})
   evaluate(order,items):Quote !UnknownItem > UnknownItemException,Quote
  Quote.java(R{order,totalCents})
 ids
  {ItemId,OrderId,UserId}.java(R{value})
? tests ·.java
 src/test
  PricingEngineTest:BulkDiscounts,ordersOverTenItemsGetTenPercentOff,smallOrdersPayFullPrice,
    unknownItemIsRejected
· 186 LOC · state 0123456789ab
```

The second line is generated with the map and explains its applicable core
notation. Section-specific layout such as `? tests` is described below. The
essentials are:

- The tree mirrors the repository and names exact source files. Similar files
  may be grouped losslessly with braces.
- `f(args):Ret > calls` shows a callable, its return type, and retained internal
  calls. `←` shows implementors.
- `×0` means no static project reference was found; `!E` means a callable
  throws. These are navigation hints, not correctness claims.
- `? tests` uses the same path-compressed tree as the source section and
  retains a compact, reconstructable landmark for every detected test file,
  plus suite names and every recognized function/method case name,
  so an agent can inspect existing coverage before recreating it. Same-named
  methods in different suites gain their suite owner. `*` marks reusable test
  helpers; separate tool/benchmark landmarks stay compact. The footer carries
  freshness and saved settings.

## Common commands

| Command | Purpose |
|---|---|
| `hologram build --root .` | Rebuild the embedded map |
| `hologram build --root . --if-stale` | Skip extraction when the map is fresh |
| `hologram check --root .` | Exit 0 when every target is fresh, otherwise 1 |
| `hologram print --root .` | Print without modifying context or source files |
| `hologram diff HEAD~3 --root .` | Show the semantic map diff from a revision |
| `hologram review --root .` | Review the working tree against `HEAD` |
| `hologram review --root . --json` | Emit structured findings with stable IDs |
| `hologram stats --root . --budget 8000` | Explain a token-budget decision |
| `hologram uninstall --root .` | Remove managed hooks and map blocks |

`build` and `init` remember their settings in the map itself:

- `--lang java,python` limits extraction; clear it with `--lang all`.
- `--target AGENTS.md` selects context files; clear it with `--target all`.
- `--budget 8000` sets an estimated digest-token target; clear it with
  `--budget 0`.

Other useful options include `--warn-tokens N`, `review --brief K`,
`review --quiet-if-clean`, and `uninstall --keep-blocks`. Run
`hologram <command> --help` for the full CLI.

## Review changes

`hologram review [REV]` looks for near-duplicate callables, repeated test
coverage, new public symbols with no static references, tests that name removed
code, public API drift, and additions that appear misplaced. Findings are
deterministic heuristics and advisory: a successful review still exits zero.
The post-commit hook runs the same review against the previous commit.

Review scans Git-indexed files. Use `git add -N path/to/file` to include a
completely untracked addition. JSON output contains project paths and symbols,
so treat it as repository-derived data.

## Token budgets

When a full map exceeds `--budget N`, Hologram starts with a compact semantic
floor: retained business types, fields, and top-level signatures with exact
file ownership, plus external entrypoints, test landmarks, and tool/benchmark
orientation. Additional nonredundant test-suite and function/method labels are
default facts but can be dropped individually while their file landmarks
remain. Hologram then restores ranked whole facts,
prioritizing tested and cross-file paths, widely used APIs, and breadth across
files. If even the floor cannot fit, Hologram warns and emits the smallest
complete candidate instead of cutting facts in half.

The budget applies to the digest. `hologram stats` separately reports the
wrapper, coaching text, and total managed-block estimate. `--warn-tokens`
checks that managed block for `build`/`init`, while `print` checks its printed
digest. Estimates use `ceil(characters / 4)` for deterministic planning; they
are not tokenizer counts from a particular model.

## Language support

- Application code: Java, Python, TypeScript, JavaScript, TSX/JSX, C#, Kotlin,
  Go, Rust, C, C++, PHP, Swift, Scala, Ruby, and Lua.
- Components and web assets: Angular, React, Vue, Svelte, HTML, and CSS.
- Project files: Bash/zsh scripts, Helm charts, and Makefiles.

Extraction depth varies by language. Where supported, maps retain types,
fields, signatures, relations, resolved calls, constants, throws, routes,
annotations, component usage, and framework entrypoints. Python uses the
standard library's `ast`; Helm and Make support are also built in. Most other
languages use optional tree-sitter grammars.

## Context files and freshness

When `--root` is a Git worktree root, Hologram scans indexed source files.
Otherwise, it walks the tree while pruning hidden, generated, vendored, and
fixture directories.

Hologram recognizes the instruction files used by Claude Code, Codex, opencode,
Jules, Zed, Amp, Gemini CLI, Qwen Code, Aider, GitHub Copilot, Cline, Cursor,
Windsurf, Roo Code, JetBrains Junie, Continue, and Kiro. Auto-detection updates
supported files that already exist; supported rule directories receive one
managed rule file. If no target exists, Hologram creates `CLAUDE.md`. Use
`--target` when you want an explicit destination.

The `state` stamp covers source content and Hologram's generator code. It does
not fingerprint the Python runtime or installed grammar versions, so rebuild
after upgrading that toolchain even if `check` still reports fresh.

## Limits

Hologram is static context, not proof:

- `×0` means no static project reference was observed, not that code is safe to
  delete.
- A test edge means a test references a symbol, not that the behavior is
  correct.
- Test inventories are declaration-based. Cases named only through strings,
  framework DSL calls, or macros are not extracted as function/method names.
- Function bodies are summarized rather than embedded.
- Extraction depth varies, and review findings can produce false positives.

## Development

Run the dependency-free test profile:

```bash
python3 tools/run_tests.py --profile core
```

With every optional grammar installed, run the complete profile with no allowed
skips:

```bash
.venv/bin/python tools/run_tests.py --profile full
```

See [CHANGELOG.md](CHANGELOG.md) for release details. The
[benchmark guide](benchmark/README.md) describes matched map/control experiments
and their privacy boundary.

## License

MIT — see [LICENSE](LICENSE).
