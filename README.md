# hologram

Hologram builds a deterministic, whole-codebase map for coding agents. The map
records files, symbols, signatures, relationships, resolved project calls,
dependencies, tests, reachability evidence, and conservative duplicate candidates.
It is generated entirely from source analysis; no LLM is involved.

Canonical v2 maps are complete inventories. Hologram does not rank, omit, budget,
degrade, or truncate the map to fit a context window.

## Install

```bash
python3 -m pip install -e '.[parsers]'
```

The `parsers` extra installs every supported tree-sitter grammar. Python extraction
uses the standard library and works without the extra.

## Quick start

In a repository that already has one or more agent context files:

```bash
hologram init --root /path/to/repo
```

`init` detects regular `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md` files, writes a
strict `.hologram.toml`, delivers the first complete map, and installs a read-only
pre-commit check. If none of those files exists, select at least one target:

```bash
hologram init --root /path/to/repo --agent claude --agent codex
```

Use `--no-hook` when a hook is unwanted or the existing pre-commit hook cannot be
safely managed.

## Configuration

Hologram reads schema-2 TOML. Unknown keys and invalid values are errors. This is the
canonical default configuration:

```toml
schema_version = 2
agents = ["claude", "codex", "gemini"]
languages = []
include = ["**/*"]
exclude = ["**/.git/**", "**/.venv/**", "**/__pycache__/**", "**/bin/**", "**/build/**", "**/dist/**", "**/generated/**", "**/node_modules/**", "**/obj/**", "**/out/**", "**/target/**", "**/vendor/**"]
hot_threshold = 10
output = "PROJECT_DIGEST.md"
```

The keys are:

- `schema_version`: exactly `2`.
- `agents`: any unique subset of `claude`, `codex`, and `gemini`.
- `languages`: supported language names; an empty list auto-detects languages.
- `include` and `exclude`: root-relative POSIX glob patterns.
- `hot_threshold`: a positive integer controlling the `×N` marker threshold.
- `output`: an optional root-relative standalone map. Omit it to deliver only to
  agent contexts.

An empty `agents = []` is valid only when `output` is set. That is useful for CI,
benchmarks, and repositories that want a standalone canonical map without creating
agent instruction files.

Agent names map to root files as follows:

| Agent | Managed target |
|---|---|
| `claude` | `CLAUDE.md` |
| `codex` | `AGENTS.md` |
| `gemini` | `GEMINI.md` |

An explicit `--config PATH` may be supplied to every command. A relative path is
interpreted relative to `--root`; an absolute configuration may live elsewhere.
Hook installation requires a root-relative configuration, so use `--no-hook` with
an external config.

## Commands

The complete command surface is:

```text
hologram init [--root PATH] [--config PATH] [--quiet]
              [--agent claude|codex|gemini ...] [--no-hook]
hologram build [--root PATH] [--config PATH] [--quiet]
hologram check [--root PATH] [--config PATH] [--quiet]
hologram diff [REV] [--root PATH] [--config PATH] [--quiet]
```

`diff` defaults to `HEAD~1`. `--quiet` suppresses success output only; diagnostics
remain visible on stderr.

| Exit | Meaning |
|---:|---|
| `0` | init/build success, fresh check, or a completed advisory diff |
| `1` | missing, stale, malformed, or noncanonical managed output |
| `2` | usage, configuration, unsafe path, or unsupported-hook error |
| `3` | incomplete scan/extraction/state or invalid/incomplete revision |

## Managed delivery and freshness

Agent maps live between full-line `hologram:v2:start` and `hologram:v2:end`
markers. Authored bytes outside the managed pair are preserved exactly, including
CRLF and bytes that are not valid UTF-8. A fresh build is idempotent and does not
replace a target, so its inode, mode, and modification time remain unchanged.

Build creates one immutable source snapshot, requires it to be complete, analyzes
and renders that snapshot without rereading sources, then preflights every target
before committing any write. Atomic replacement is per target, not across the set
of independent context files. A failed later replacement does not roll back an
earlier completed target; `check` detects any interrupted multi-target delivery.

`check` runs the complete pipeline and compares every configured or retained managed
target. It never writes, creates directories, bootstraps configuration, installs a
hook, or changes permissions. It returns `0` only when every expected byte is fresh.

## Pre-commit check

`init` can install one managed block immediately after the shebang in an executable
`sh`, `bash`, or `zsh` pre-commit hook. The block resolves the worktree root at
execution time and runs:

```text
python -B -m hologram check --root ... --config ... --quiet
```

The hook is read-only. Stale output blocks the commit with exit `1`; usage and unsafe
hook states return `2`; incomplete extraction or state returns `3`. Authored hook
bytes outside the managed block are preserved. Use `hologram init --no-hook` to skip
all Git and hook work.

## Canonical map

The renderer emits explicit file leaves and source positions. A small fragment looks
like this:

```text
# hologram:2 state=0000000000000000000000000000000000000000000000000000000000000000 · regen: hologram build
· deps ["app→core"]
@ "src/core.py" "python" "production" "core"
  :4:0 [[],"fn","public_surface","(int)"] "pub"
    signature "public_surface(int):int"
    param ["int"]
    return "int"
    body 2
    mark ["×0?"]
```

`decode_render()` strictly validates canonical spelling, structure, ordering, intern
tables, ownership, and a byte-for-byte rerender.

Markers are evidence, not verdicts:

- `×N`: resolved references from `N` other files.
- `×0`: strong static zero-reference candidate.
- `×0?`: reachability is uncertain or may be external.
- `✓`: referenced from test code.
- `≈N`: `N` conservative duplicate peers.

## Semantic revision diff

```bash
hologram diff HEAD~3 --root .
```

Diff builds the current artifact first, reads the selected revision through Git's
object database, and compares canonical models rather than rendered Markdown. It
reports symbol fields, file topology, and dependency changes. For newly introduced
symbols it reports strong `×0`, uncertain `×0?`, and all broad duplicate-candidate
matches. Findings are advisory and still return `0`.

Static evidence cannot prove semantic deadness or authorize deletion. Read the
source, runtime registration, framework conventions, and tests before removing or
consolidating anything.

## Migration from v1

The schema and CLI are intentionally breaking. Exact old managed context marker
pairs are migrated to canonical v2 blocks, and `init` removes only exact generated
Hologram post-commit, post-merge, and post-checkout command lines. Authored text,
near-matches, and unrelated hooks are preserved.

The removed flags `--embed`, `--embed-max-tokens`, `--out`, `--lang`, `--private`,
`--behaviors`, and `--if-stale` are not supported and have no aliases. Put language,
agent, and standalone-output choices in `.hologram.toml`; canonical delivery always
includes full symbol, body-size, and behavior evidence.

## Library API

`hologram.__version__` reports the package version. The six lazy canonical phase
exports are `AnalyzedProject`, `analyze_project`, `RenderIR`, `project_render_ir`,
`render_project`, and `decode_render`:

```python
snapshot = hologram.build_project(root, config).require_complete()
analyzed = hologram.analyze_project(
    snapshot.project,
    snapshot.resolution,
    hot_threshold=config.hot_threshold,
)
render_ir = hologram.project_render_ir(
    analyzed,
    state=snapshot.state.value,
    hot_threshold=config.hot_threshold,
)
text = hologram.render_project(render_ir)
assert hologram.decode_render(text) == render_ir
```

The v2 CLI orchestration, atomic writers, hook helpers, and semantic-diff internals
are not package-root exports. The retired v1 library entry points—including
`run_cli`, `build_digest`, `render_simple`, and the legacy path-based
`extract_file`—have been removed. Use `hologram.cli`, the canonical phase APIs
above, and `hologram.parsers.extract_file` instead.

## Languages

Hologram supports Java, Python, TypeScript, JavaScript, TSX, JSX, Vue, Svelte, C#,
Kotlin, C, C++, Go, Lua, Rust, HTML, and Helm. Extraction depth follows what each
language can state honestly. Unsupported candidates are excluded.
A supported source that cannot be read or parsed, or whose required parser is
unavailable, makes the build incomplete rather than silently shrinking the map.

## Benchmark status

The current public benchmark is the frozen
[`codecompanion.json`](benchmark/tasks/codecompanion.json) matrix: condition B has
no Hologram context, while condition C receives the managed canonical v2 map from
turn zero. It pins `claude-sonnet-5`, Claude Code `2.1.224`, a 40-turn limit, one
repetition, one seed, four task capabilities, and exact answer verifiers. Acceptance
requires both terminal success and verifier success; navigation answers are
automatically checked. Public reports compare provenance-matched B/C pairs and
partition them by model/version, tier, and capability. Private manifests, corpora,
assets, and external private results stay outside this repository and private
reports expose condition totals only. No paid sessions are run by the implementation
suite.

Historical condition A measured a legacy on-disk pull model. Its Spring figures are
legacy, exploratory, pre-tier evidence with n=1 per cell. That experiment used
`sonnet`, a mutable model alias, pooled task metrics, change-only reuse checks, and
did not gate turn-limit errors; navigation acceptance was not automated. Its token,
navigation, reuse, and duplication figures are historical observations only and
cannot be compared with future v2 reports or used to validate the hardened harness.
See the [current runbook](benchmark/README.md) for preparation, zero-cost dry runs,
static thresholds, privacy boundaries, and manual paid-run instructions.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## License

MIT — see [LICENSE](LICENSE).
