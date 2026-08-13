# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/lazymaniac/hologram/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lazymaniac/hologram/releases/tag/v0.1.0
