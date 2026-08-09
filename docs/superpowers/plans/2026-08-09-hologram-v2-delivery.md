# Hologram v2 Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one complete canonical map through atomic managed blocks in Claude, Codex, and Gemini context files, with strict configuration, read-only freshness checking, model-based revision diffing, and a read-only pre-commit guard.

**Architecture:** `context.py` owns pure byte-preserving managed-block transforms and staged atomic file replacement. `cli.py` composes the frozen scan/extract/resolve/analyze/render pipeline and maps typed failures to exit codes. `hooks.py` installs only a managed read-only `pre-commit` check. `diff.py` compares canonical `RenderIR` models and emits deterministic new-code advisories; it never reconstructs facts by scraping rendered Markdown.

**Tech Stack:** Python 3.11+, stdlib `argparse`/`tempfile`/`os`/`shlex`/`subprocess`/`tarfile`, strict schema-2 TOML from `config.py`, Git CLI, immutable v2 analysis/render models, `unittest`, Ruff, mypy.

---

## Frozen inputs and file structure

Consume, do not redefine, the foundation configuration API:

```python
ProjectConfig(schema_version, agents, languages, include, exclude,
              hot_threshold, output)
load_config(root: Path, path: Path | None = None) -> ProjectConfig
```

Schema version is exactly `2`. Omitted or empty languages mean auto-detect. The scanner's complete candidate ledger, extraction completeness diagnostics, state result, `ProjectIR`, `ResolutionResult`, `AnalyzedProject`, and `RenderIR` are the authoritative data flow. Delivery never rereads sources after the captured extraction snapshot.

Create:

- `src/hologram/context.py` — managed blocks, output preflight, staged atomic writes.
- `src/hologram/cli.py` — command parser, complete build artifact, command orchestration, exit mapping.
- `src/hologram/diff.py` — revision materialization, semantic model diff, new-code advisories.
- `src/hologram/hooks.py` — idempotent read-only pre-commit block and deliberate v1 hook cleanup.
- `src/hologram/__main__.py` — `raise SystemExit(main())` only.
- `tests/test_context.py` — byte preservation, malformed markers, atomic failure behavior.
- `tests/test_delivery_cli.py` — init/build/check config and exit-code behavior.
- `tests/test_diff.py` — model changes, revision failures, and new-code advisories.
- `tests/test_hooks.py` — hook preservation, idempotency, and executed read-only checks.

Modify:

- `src/hologram/__init__.py` — export version and phase entry points only.
- `tests/test_cli.py` — replace legacy monolith flag tests with package command smoke tests.
- `tests/test_freshness_and_markers.py` — test canonical context freshness instead of a standalone header substring.
- `benchmark/bench.py` and `tests/test_bench.py` — deliberate migration from removed flags to schema-2 temporary configs and the v2 decoder.
- `README.md` — config, commands, managed files, exit codes, hook behavior, and advisory caveats.

The only supported CLI is:

```text
hologram init [--root PATH] [--config PATH] [--quiet]
              [--agent claude|codex|gemini ...] [--no-hook]
hologram build [--root PATH] [--config PATH] [--quiet]
hologram check [--root PATH] [--config PATH] [--quiet]
hologram diff [REV] [--root PATH] [--config PATH] [--quiet]
```

Do not retain `--embed`, `--embed-max-tokens`, `--out`, `--lang`, `--private`, `--behaviors`, or `--if-stale` aliases. Deliberate migration is limited to exact old managed markers and exact generated Hologram hook lines.

## Fixed delivery contracts

```python
EXIT_OK = 0
EXIT_STALE = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

AGENT_PATHS = {
    "claude": Path("CLAUDE.md"),
    "codex": Path("AGENTS.md"),
    "gemini": Path("GEMINI.md"),
}
```

- Exit `0`: build/init/check success and advisory diff findings.
- Exit `1`: missing, stale, malformed, or noncanonical managed context/output.
- Exit `2`: argparse usage, root/configuration, unsafe path, or unsupported existing-hook error.
- Exit `3`: incomplete scan/extraction/state, unavailable parser, unreadable candidate, or invalid/incomplete revision.
- Build generates a complete in-memory artifact and preflights every target before any replacement.
- Each target replacement uses a same-directory temporary file, `flush`, `fsync`, mode preservation, and `os.replace`.
- Existing authored bytes outside a managed pair remain byte-for-byte identical.
- Identical content is not replaced, so mode and mtime remain unchanged.
- No budget, ranked omission, degradation, or truncation exists.
- `check` never writes, bootstraps, installs, chmods, or stages.
- `diff` findings are advisory and never authorize deletion.

### Task 1: Implement byte-preserving managed context blocks

**Files:**

- Create: `src/hologram/context.py`
- Create: `tests/test_context.py`

- [ ] **Step 1: Write failing managed-block tests**

```python
class ManagedBlockTest(unittest.TestCase):
    def test_refresh_preserves_authored_bytes_exactly(self):
        authored = b"# Rules\r\nUse tabs.\r\n\r\n"
        old = authored + render_managed_block("old\n")
        new = replace_managed_block(old, render_managed_block("new\n"))
        self.assertTrue(new.startswith(authored))
        self.assertEqual(new[:len(authored)], authored)
        self.assertIn(b"new\n", new)
        self.assertNotIn(b"old\n", new)

    def test_missing_block_appends_one_canonical_pair(self):
        updated = replace_managed_block(b"# Rules\n", render_managed_block("map\n"))
        self.assertEqual(updated.count(CONTEXT_START), 1)
        self.assertEqual(updated.count(CONTEXT_END), 1)

    def test_duplicate_reversed_and_unbalanced_markers_are_malformed(self):
        for existing in malformed_contexts():
            with self.subTest(existing=existing):
                self.assertEqual(inspect_managed_block(existing, b"expected"),
                                 ContextStatus.MALFORMED)
                with self.assertRaises(ManagedBlockError):
                    replace_managed_block(existing, b"expected")

    def test_all_three_agent_paths_use_identical_full_block(self):
        block = render_managed_block(
            "# hologram:2 state=" + "a" * 64 + " · regen: hologram build\n")
        self.assertEqual({agent: block for agent in AGENT_PATHS},
                         {"claude": block, "codex": block, "gemini": block})
```

- [ ] **Step 2: Run tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_context.ManagedBlockTest -v
```

Expected: import failure for `hologram.context`.

- [ ] **Step 3: Implement the pure byte transform**

Use these exact APIs:

```python
CONTEXT_START = b"<!-- hologram:v2:start -->"
CONTEXT_END = b"<!-- hologram:v2:end -->"
LEGACY_START = b"<!-- hologram:start \xe2\x80\x94 generated, do not edit; refreshed by git hooks -->"
LEGACY_END = b"<!-- hologram:end -->"


class ContextStatus(StrEnum):
    FRESH = "fresh"
    MISSING = "missing"
    STALE = "stale"
    MALFORMED = "malformed"


class ManagedBlockError(ValueError):
    pass


def render_managed_block(rendered_map: str) -> bytes:
    payload = rendered_map.encode("utf-8")
    return (CONTEXT_START + b"\n"
            + b"## Project map (generated by Hologram)\n\n"
            + b"Use this complete map for orientation, placement, reuse, and review. "
              b"Read source bodies before changing behavior.\n\n"
            + b"```text\n" + payload + b"```\n"
            + b"Regenerate with: `hologram build`\n"
            + CONTEXT_END + b"\n")
```

The other exact context APIs are
`inspect_managed_block(existing: bytes, expected: bytes) -> ContextStatus` and
`replace_managed_block(existing: bytes, expected: bytes) -> bytes`.

Recognize exactly one canonical pair or exactly one legacy pair. Mixed, nested, repeated, reversed, and unbalanced markers are malformed. Refreshing a legacy pair replaces that exact byte range with the v2 pair. For a missing block, preserve existing bytes and append `\n` only when needed to begin the canonical block on a new line. Never call `decode()` on authored bytes.

- [ ] **Step 4: Run managed-block tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_context.ManagedBlockTest -v
```

Expected: all managed-block tests pass.

- [ ] **Step 5: Commit context transformation**

```bash
git add src/hologram/context.py tests/test_context.py
git commit -m "feat: manage agent context blocks losslessly"
```

### Task 2: Stage and atomically replace every preflighted target

**Files:**

- Modify: `src/hologram/context.py`
- Modify: `tests/test_context.py`

- [ ] **Step 1: Write failing atomic-output tests**

```python
class AtomicOutputTest(unittest.TestCase):
    def test_identical_write_preserves_inode_mode_and_mtime(self):
        path = self.root / "CLAUDE.md"
        path.write_bytes(b"same")
        path.chmod(0o640)
        before = path.stat()
        changed = atomic_write(path, b"same")
        after = path.stat()
        self.assertFalse(changed)
        self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                         (before.st_ino, before.st_mode, before.st_mtime_ns))

    def test_replace_failure_keeps_original_and_cleans_temp(self):
        path = self.root / "AGENTS.md"
        path.write_bytes(b"original")
        with mock.patch("hologram.context.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(AtomicWriteError):
                atomic_write(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".hologram-tmp-*")), [])

    def test_preflight_rejects_one_malformed_target_before_any_write(self):
        targets = three_agent_target_fixture(one_malformed=True)
        before = snapshot_paths(targets)
        with self.assertRaises(ManagedBlockError):
            preflight_context_writes(targets, b"block")
        self.assertEqual(snapshot_paths(targets), before)
```

- [ ] **Step 2: Run tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_context.AtomicOutputTest -v
```

Expected: missing `atomic_write` and `preflight_context_writes`.

- [ ] **Step 3: Implement preflight and atomic replacement**

```python
@dataclass(frozen=True, slots=True)
class PlannedWrite:
    path: Path
    content: bytes
    mode: int | None


class AtomicWriteError(OSError):
    pass
```

The exact write APIs are
`preflight_context_writes(targets: Mapping[str, Path], expected_block: bytes) -> tuple[PlannedWrite, ...]`,
`atomic_write(path: Path, content: bytes, *, mode: int | None = None) -> bool`,
and `commit_writes(writes: Sequence[PlannedWrite]) -> tuple[Path, ...]`.

Preflight reads all target bytes, validates markers, computes replacements, rejects symlinks and non-files, and returns paths sorted by absolute path. `atomic_write()` uses `tempfile.mkstemp(prefix=".hologram-tmp-", dir=path.parent)`, writes through `os.fdopen`, flushes, calls `os.fsync`, applies the original permission bits or `0o644`, and then `os.replace`. Clean the temp in `finally`. Create parent directories only during commit, after all preflight checks succeed.

Atomicity is per target. Do not claim a multi-file filesystem transaction; a later `check` detects interruption between replacements.

- [ ] **Step 4: Run atomic tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_context.AtomicOutputTest -v
```

Expected: all tests pass and no temp files remain.

- [ ] **Step 5: Commit atomic delivery**

```bash
git add src/hologram/context.py tests/test_context.py
git commit -m "feat: write preflighted outputs atomically"
```

### Task 3: Compose complete build artifacts and read-only freshness checks

**Files:**

- Create: `src/hologram/cli.py`
- Create: `tests/test_delivery_cli.py`

- [ ] **Step 1: Write failing build/check service tests**

```python
class BuildCheckServiceTest(unittest.TestCase):
    def test_build_updates_all_agents_and_optional_output_from_one_artifact(self):
        root = configured_project(self.tmp, agents=("claude", "codex", "gemini"),
                                  output="PROJECT_DIGEST.md")
        result = command_build(root, root / ".hologram.toml", quiet=True)
        self.assertEqual(result, EXIT_OK)
        blocks = [managed_payload((root / name).read_bytes())
                  for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md")]
        self.assertEqual(blocks[0], blocks[1])
        self.assertEqual(blocks[1], blocks[2])
        self.assertEqual((root / "PROJECT_DIGEST.md").read_text(),
                         blocks[0].decode("utf-8"))

    def test_incomplete_extraction_refuses_every_output(self):
        root = configured_project_with_failed_candidate(self.tmp)
        before = snapshot_tree(root)
        self.assertEqual(command_build(root, root / ".hologram.toml", quiet=True),
                         EXIT_INCOMPLETE)
        self.assertEqual(snapshot_tree(root), before)

    def test_check_is_read_only_for_fresh_stale_missing_and_malformed(self):
        for state, expected in check_state_fixtures(self.tmp):
            with self.subTest(state=state):
                before = snapshot_tree_with_metadata(state.root)
                self.assertEqual(command_check(state.root, state.config, quiet=True),
                                 expected)
                self.assertEqual(snapshot_tree_with_metadata(state.root), before)
```

- [ ] **Step 2: Run tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_delivery_cli.BuildCheckServiceTest -v
```

Expected: import failure for `hologram.cli`.

- [ ] **Step 3: Implement one complete artifact pipeline**

In `cli.py` implement:

```python
EXIT_OK = 0
EXIT_STALE = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    config: ProjectConfig
    snapshot: BuildSnapshot
    analyzed: AnalyzedProject
    render_ir: RenderIR
    rendered: str
```

The exact service APIs are
`create_artifact(root: Path, config: ProjectConfig) -> BuildArtifact`,
`command_build(root: Path, config_path: Path, *, quiet: bool) -> int`, and
`command_check(root: Path, config_path: Path, *, quiet: bool) -> int`.

`create_artifact()` executes
`snapshot = pipeline.build_project(root, config)` exactly once, then calls
`snapshot.require_complete()` so scan, state, project, and resolution diagnostics
share the one foundation-owned incomplete-build boundary. Then call
`analyze_project(snapshot.project, snapshot.resolution,
hot_threshold=config.hot_threshold)`,
`project_render_ir(analyzed, state=snapshot.state.value,
hot_threshold=config.hot_threshold)`, and `render_project(render_ir)`.
Each `BodyIR.events` tuple is the only body-analysis input. Source bytes were
consumed at the extraction boundary; do not reparse or reread mutable
working-tree bytes later.

`command_build()` creates the artifact before preflighting any agent/output target. It then preflights every managed file plus the optional root-relative standalone output and commits planned writes. `command_check()` creates the same artifact, reads and classifies each configured target, and returns `1` for any nonfresh target without invoking write or hook APIs. Consume the foundation config module's canonical schema-2 bytes for init and hashing; delivery must not introduce a second TOML serializer or defaulting path.

Add an integration test that first captures a complete `BuildSnapshot`, then
mutates a source path, mocks the single `pipeline.build_project()` call to return
that snapshot, and creates the artifact. Analysis, render IR, rendered bytes, and
state must remain identical to the pre-mutation result because they consume only
the snapshot. Mutation during the scanner's initial read may fail the scan, but
delivery never rereads or detects a change after `BuildSnapshot` exists.

- [ ] **Step 4: Run build/check tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_delivery_cli.BuildCheckServiceTest -v
```

Expected: all service tests pass; incomplete builds leave every output untouched.

- [ ] **Step 5: Commit build/check services**

```bash
git add src/hologram/cli.py tests/test_delivery_cli.py
git commit -m "feat: build and check complete hologram artifacts"
```

### Task 4: Expose init/build/check/diff with exact exit semantics

**Files:**

- Modify: `src/hologram/cli.py`
- Modify: `src/hologram/__main__.py`
- Modify: `tests/test_delivery_cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing parser and exit-code tests**

```python
class CliContractTest(unittest.TestCase):
    def test_supported_command_surface(self):
        self.assertEqual(invoke(["build", "--root", str(self.root), "--quiet"]), 0)
        self.assertEqual(invoke(["check", "--root", str(self.root), "--quiet"]), 0)
        self.assertEqual(invoke(["diff", "HEAD", "--root", str(self.root), "--quiet"]), 0)

    def test_removed_flags_are_usage_errors(self):
        for flag in ("--embed", "--out", "--lang", "--private",
                     "--behaviors", "--if-stale"):
            with self.subTest(flag=flag):
                self.assertEqual(invoke(["build", flag]), EXIT_USAGE)

    def test_error_classes_map_to_1_2_3(self):
        self.assertEqual(invoke(stale_args(self.root)), EXIT_STALE)
        self.assertEqual(invoke(invalid_config_args(self.root)), EXIT_USAGE)
        self.assertEqual(invoke(incomplete_args(self.root)), EXIT_INCOMPLETE)

    def test_missing_selected_config_exits_two_before_scan_or_write(self):
        missing = self.root / "missing.toml"
        with mock.patch("hologram.cli.pipeline.build_project") as build:
            self.assertEqual(invoke(["build", "--root", str(self.root),
                                     "--config", str(missing)]), EXIT_USAGE)
            self.assertEqual(invoke(["check", "--root", str(self.root),
                                     "--config", str(missing)]), EXIT_USAGE)
        build.assert_not_called()
```

- [ ] **Step 2: Run CLI tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_delivery_cli.CliContractTest tests.test_cli -v
```

Expected: missing `main`/`__main__` or legacy parser accepts removed flags.

- [ ] **Step 3: Implement the parser and typed failure boundary**

The exact command APIs are `build_parser() -> argparse.ArgumentParser` and
`main(argv: Sequence[str] | None = None) -> int`.

Use a shared parent parser containing only `--root`, `--config`, and `--quiet`. `init` additionally owns repeatable `--agent` with choices from `AGENT_PATHS` and `--no-hook`; `diff` owns optional `rev`, default `HEAD~1`. Resolve a relative `--config` against `--root`.

Catch and print one concise diagnostic to stderr:

- managed freshness/malformed errors -> `1`;
- `ConfigError`, unsafe root/path, and unsupported hook -> `2`;
- `IncompleteBuildError`, parser/extraction failure, and `RevisionError` -> `3`.

Successful/advisory messages go to stdout unless `--quiet`; errors remain visible. `__main__.py` contains only:

```python
from .cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Run CLI tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_delivery_cli.CliContractTest tests.test_cli -v
```

Expected: all tests pass and invalid argparse syntax exits `2`.

- [ ] **Step 5: Commit CLI contract**

```bash
git add src/hologram/cli.py src/hologram/__main__.py tests/test_delivery_cli.py tests/test_cli.py
git commit -m "feat: expose hologram v2 lifecycle commands"
```

### Task 5: Initialize detected agents and install a read-only pre-commit hook

**Files:**

- Create: `src/hologram/hooks.py`
- Create: `tests/test_hooks.py`
- Modify: `src/hologram/cli.py`
- Modify: `tests/test_delivery_cli.py`

- [ ] **Step 1: Write failing init and hook tests**

```python
class InitTest(unittest.TestCase):
    def test_detects_existing_root_agent_files(self):
        root = git_project(self.tmp, files=("CLAUDE.md", "GEMINI.md"))
        self.assertEqual(command_init(root, root / ".hologram.toml",
                                      agents=(), no_hook=False, quiet=True), 0)
        config = load_config(root)
        self.assertEqual(config.agents, ("claude", "gemini"))
        expected = dataclasses.replace(
            default_config(), agents=("claude", "gemini"))
        self.assertEqual(config, expected)
        self.assertEqual((root / CONFIG_NAME).read_text(),
                         render_config(expected))

    def test_requires_agent_when_none_exists_and_writes_nothing(self):
        root = git_project(self.tmp, files=())
        before = snapshot_tree(root)
        self.assertEqual(command_init(root, root / ".hologram.toml",
                                      agents=(), no_hook=False, quiet=True), 2)
        self.assertEqual(snapshot_tree(root), before)

    def test_explicit_agent_creates_target_and_no_hook_escape_skips_hook(self):
        root = git_project(self.tmp, files=())
        self.assertEqual(command_init(root, root / ".hologram.toml",
                                      agents=("codex",), no_hook=True, quiet=True), 0)
        self.assertTrue((root / "AGENTS.md").exists())
        self.assertFalse((root / ".git/hooks/pre-commit").exists())


class PreCommitHookTest(unittest.TestCase):
    def test_hook_is_idempotent_preserves_shell_body_and_runs_check_only(self):
        hook = existing_shell_hook(self.root, b"#!/bin/sh\necho authored\n")
        install_precommit(self.root, self.config_path, self.command)
        install_precommit(self.root, self.config_path, self.command)
        content = hook.read_bytes()
        self.assertIn(b"echo authored", content)
        self.assertEqual(content.count(HOOK_START), 1)
        self.assertIn(b" check ", content)
        self.assertIn(b" -B -m hologram ", content)
        self.assertNotIn(b" build ", content)

    def test_executed_hook_blocks_stale_without_writing(self):
        fresh_project_with_hook(self.root)
        mutate_source(self.root)
        before = snapshot_tree_with_metadata(self.root)
        result = subprocess.run([str(self.root / ".git/hooks/pre-commit")],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, EXIT_STALE)
        self.assertEqual(snapshot_tree_with_metadata(self.root), before)
```

- [ ] **Step 2: Run init/hook tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_delivery_cli.InitTest tests.test_hooks -v
```

Expected: missing `hologram.hooks`, `command_init`, or hook installer.

- [ ] **Step 3: Implement canonical init and hook management**

In `hooks.py`:

```python
HOOK_START = b"# hologram:v2:start"
HOOK_END = b"# hologram:v2:end"


class UnsupportedHookError(ValueError):
    pass
```

The exact hook APIs are
`render_precommit_command(*, root: Path, config_path: Path, python: Path, module: str = "hologram") -> bytes`,
`preflight_precommit(repo: Path, command: bytes) -> PlannedWrite`, and
`remove_legacy_post_hook_lines(repo: Path) -> tuple[PlannedWrite, ...]`.

Use `shlex.quote` for every argument. The managed shell block runs:

```text
<python> -B -m hologram check --root <root> --config <config> --quiet
```

Resolve the effective hook directory without assuming `<root>/.git/hooks`: handle
a normal `.git` directory, a worktree `.git` indirection file, and local/global
`core.hooksPath` through `git rev-parse --git-path hooks`. Reject a resolved path
outside the Git administrative directory unless it is the explicit Git-configured
hooks path. No `|| true`, build, add, or chmod occurs at hook execution. A missing
hook gets `#!/bin/sh\n` and mode `0o755`; an existing POSIX `sh`/`bash`/`zsh` hook
retains all bytes outside the block and preserves its existing executable mode.
Reject nonexecutable existing hooks and other shebangs with `UnsupportedHookError`
and instruct the caller to use `--no-hook`; never wrap or rename an authored hook
silently. `-B` prevents repository-local bytecode creation during the read-only
check.

`remove_legacy_post_hook_lines()` removes only exact old generated Hologram command lines from `post-commit`, `post-merge`, and `post-checkout`, deleting an old hook only when its remaining bytes are exactly `#!/bin/sh\n`. It preserves every unrelated byte.

`command_init()` behavior:

1. If a valid config already exists, use it; explicit `--agent` must match its agents or exit `2`.
2. Otherwise detect existing root agent files in fixed order Claude, Codex, Gemini. If none, require explicit agents.
3. Create exactly
   `dataclasses.replace(default_config(), agents=selected_agents)` and serialize
   through `render_config()`. This preserves schema `2`, auto-detected languages,
   canonical include/exclude patterns, hot threshold `10`, and the default
   `PROJECT_DIGEST.md` output without reconstructing defaults in delivery.
4. Build a complete artifact and preflight config, context targets, and hook changes before committing any write.
5. Skip hook work entirely under `--no-hook`.
6. Repeated init with identical inputs changes no bytes or mtimes.

- [ ] **Step 4: Run init/hook tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_delivery_cli.InitTest tests.test_hooks -v
```

Expected: all tests pass; the executed stale hook returns `1` and changes no metadata.

- [ ] **Step 5: Commit init and hooks**

```bash
git add src/hologram/hooks.py src/hologram/cli.py tests/test_hooks.py tests/test_delivery_cli.py
git commit -m "feat: install read-only hologram precommit checks"
```

### Task 6: Diff canonical models and report new-code advisories

**Files:**

- Create: `src/hologram/diff.py`
- Create: `tests/test_diff.py`
- Modify: `src/hologram/cli.py`

- [ ] **Step 1: Write failing semantic diff and advisory tests**

```python
class DiffCommandTest(unittest.TestCase):
    def test_reports_added_changed_removed_symbols_by_provenance(self):
        report = diff_fixture_report()
        self.assertIn("+ src/new.py:4 new_api", report.text)
        self.assertIn("~ src/service.py:10 run", report.text)
        self.assertIn("- src/old.py:3 old_api", report.text)
        self.assertIn("~ module src/service.py: app→application", report.text)
        self.assertIn("+ dependency application→core", report.text)

    def test_reports_new_strong_uncertain_and_duplicate_advisories(self):
        report = new_code_advisory_fixture_report()
        self.assertIn("new strong ×0: src/new.py:4 unused_private", report.text)
        self.assertIn("new uncertain ×0?: src/new.py:8 exported_api", report.text)
        self.assertRegex(report.text,
                         r"new duplicate candidate: src/new.py:12 clone .* "
                         r"src/core.py:20 canonical .* ast=0\.9[0-9] total=0\.8[0-9]")

    def test_invalid_revision_and_incomplete_revision_exit_three(self):
        self.assertEqual(command_diff(self.root, self.config_path,
                                      "missing-rev", quiet=True), 3)
        self.assertEqual(command_diff(self.incomplete_root, self.config_path,
                                      "HEAD~1", quiet=True), 3)

    def test_diff_never_writes_delivery_files(self):
        before = snapshot_tree_with_metadata(self.root)
        self.assertEqual(command_diff(self.root, self.config_path,
                                      "HEAD~1", quiet=True), EXIT_OK)
        self.assertEqual(snapshot_tree_with_metadata(self.root), before)
```

- [ ] **Step 2: Run diff tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_diff -v
```

Expected: import failure for `hologram.diff` or missing `command_diff`.

- [ ] **Step 3: Implement revision analysis and deterministic advisories**

```python
@dataclass(frozen=True, slots=True)
class SymbolChange:
    kind: str
    before: RenderSymbol | None
    after: RenderSymbol | None


@dataclass(frozen=True, slots=True)
class FileTopology:
    path: str
    language: str
    role: str
    module: str | None
    reexports: tuple[RenderReexport, ...]


@dataclass(frozen=True, slots=True)
class FileChange:
    kind: str
    before: FileTopology | None
    after: FileTopology | None


@dataclass(frozen=True, slots=True)
class DependencyChange:
    kind: str
    dependency: str


@dataclass(frozen=True, slots=True)
class DiffAdvisory:
    kind: str
    symbol: SymbolId
    span: SourceSpan
    peer: SymbolId | None
    peer_span: SourceSpan | None
    score: DuplicateScore | None


@dataclass(frozen=True, slots=True)
class DiffInput:
    analyzed: AnalyzedProject
    render_ir: RenderIR


@dataclass(frozen=True, slots=True)
class DiffReport:
    symbol_changes: tuple[SymbolChange, ...]
    file_changes: tuple[FileChange, ...]
    dependency_changes: tuple[DependencyChange, ...]
    advisories: tuple[DiffAdvisory, ...]
    text: str
```

The pure `diff.py` APIs are
`compare_projects(before: DiffInput, after: DiffInput) -> DiffReport` and
`analyze_revision(root: Path, config: ProjectConfig, rev: str) -> DiffInput`.
The CLI-owned API is
`command_diff(root: Path, config_path: Path, rev: str, *, quiet: bool) -> int`.

Keep `BuildArtifact`, exit constants, `create_artifact()`, and `command_diff()`
owned by `cli.py`. `diff.py` imports none of them. `command_diff()` loads the
config, builds the current `BuildArtifact`, wraps its analyzed/render values in
`DiffInput`, obtains the old `DiffInput` through `analyze_revision()`, calls
`compare_projects()`, prints unless quiet, and returns `EXIT_OK`. This is the
only dependency direction: `cli.py` may import pure `diff.py`; never the reverse.

Resolve the revision with `git rev-parse --verify <rev>^{commit}`. Materialize
its tracked tree in a temporary directory using `git archive` plus stdlib
`tarfile`; reject absolute paths, `..`, links, and devices while extracting.
Apply the current config's language/include/exclude projection to both sides.
Pure revision analysis raises `RevisionError` or `IncompleteBuildError` for an
invalid revision, archive failure, failed candidate, missing parser, or
incomplete old artifact; `command_diff()` maps those and incomplete current
artifacts to exit `3`.

Compare stable `SymbolId`s and every canonical `RenderSymbol` field. Compare
file topology (`path`, language, role, module, and file-level reexports) through
`FileChange`, and exact module-coupling strings through `DependencyChange`.
Ignore `RenderIR.state` and the derived intern table: they are freshness and
presentation metadata, not semantic changes. Sort symbol changes/advisories by
`(file, start_line, start_column, language, container_path, kind, name,
signature_key)` and file/dependency changes lexically.

For symbols newly introduced relative to the revision:

- emit `strong-zero` when analysis says `×0`;
- emit `uncertain-zero` when analysis says `×0?`;
- run the broad diff duplicate policy (`AST >= .72`, `total >= .78`) against every eligible current symbol and emit all matches with both spans and all component scores.

These are advisory; findings and ordinary API drift return `0`. Output must state: “Static analysis cannot guarantee semantic deadness or authorize deletion; inspect source and runtime/framework reachability.”

- [ ] **Step 4: Run diff tests to verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_diff -v
```

Expected: all tests pass; new-code advisories are stable and return `0`.

- [ ] **Step 5: Commit model diff**

```bash
git add src/hologram/diff.py src/hologram/cli.py tests/test_diff.py
git commit -m "feat: report new-code advisories in hologram diff"
```

### Task 7: Migrate in-repository consumers, document delivery, and verify the phase

**Files:**

- Modify: `benchmark/bench.py`
- Modify: `tests/test_bench.py`
- Modify: `tests/test_freshness_and_markers.py`
- Modify: `tests/test_delivery_cli.py`
- Modify: `src/hologram/__init__.py`
- Modify: `README.md`
- Create: `.hologram.toml`

- [ ] **Step 1: Write failing consumer-migration tests**

Update benchmark tests so condition B has no Hologram context, condition C creates a schema-2 config with `agents = ["claude"]`, and standalone before/after maps use an external temporary schema-2 config with `agents = []` plus `output = ".bench-digest.md"`. Add:

```python
class V2ConsumerMigrationTest(unittest.TestCase):
    def test_benchmark_reads_symbols_through_decoder(self):
        before, after = v2_before_after_maps()
        verdict = bench.judge_reuse(before, after, ["normalize"])
        self.assertEqual(verdict["reused"], ["normalize"])

    def test_condition_c_uses_managed_claude_block_without_legacy_flags(self):
        ws = prepared_condition_c_workspace(self.tmp)
        self.assertIn(b"hologram:v2:start", (ws / "CLAUDE.md").read_bytes())
        self.assertNotIn("--embed", bench_condition_command(ws))


class SelfConfigTest(unittest.TestCase):
    def test_tracked_self_config_is_canonical_digest_only_config(self):
        expected = dataclasses.replace(default_config(), agents=())
        path = Path(__file__).resolve().parents[1] / CONFIG_NAME
        self.assertEqual(path.read_bytes(),
                         render_config(expected).encode("utf-8"))
        self.assertEqual(load_config(path.parent, path), expected)
        self.assertEqual(expected.output, "PROJECT_DIGEST.md")
```

Place `V2ConsumerMigrationTest` in `tests/test_bench.py` and `SelfConfigTest` in
`tests/test_delivery_cli.py`; the combined block above freezes both exact tests.

- [ ] **Step 2: Run migration tests to verify RED**

```bash
.venv/bin/python -m unittest tests.test_bench.V2ConsumerMigrationTest tests.test_delivery_cli.SelfConfigTest tests.test_freshness_and_markers -v
```

Expected: old benchmark helpers invoke removed flags or parse signature lines
heuristically, and `SelfConfigTest` fails because the tracked canonical config
does not exist yet.

- [ ] **Step 3: Migrate consumers and document the final contract**

Change benchmark map inspection to `decode_render()` and compare `RenderSymbol` values, not `_sig_lines()` text scraping. Build temporary maps through schema-2 config files; do not add CLI aliases.

Add a tracked self-configuration so the roadmap's root build/check commands have
a delivery target without creating agent instruction files. Its bytes must be
exactly:

```python
render_config(dataclasses.replace(default_config(), agents=()))
```

This keeps the default `PROJECT_DIGEST.md` output and emits all canonical keys,
including the nonempty include list and full exclude defaults. Make the Step 1
regression green before any root build verification. Never hand-maintain a
shorter equivalent TOML spelling.

The foundation validator must accept `agents = []` only when `output` is set and
must reject a configuration with neither an agent nor standalone output. Delivery
consumes that invariant and never special-cases it.

Update README with:

- the strict `.hologram.toml` keys and defaults;
- root `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md` target mapping;
- agent auto-detection and required `--agent` fallback;
- optional standalone `output`;
- atomic per-target writes and authored-byte preservation;
- exact command surface and exit table;
- read-only pre-commit behavior and `--no-hook`;
- full-map delivery with no budget/truncation;
- `diff` new strong `×0`, uncertain `×0?`, and duplicate candidate advisories;
- the explicit warning that static evidence cannot prove semantic deadness or authorize deletion;
- one deliberate migration paragraph for exact v1 blocks/post hooks and a statement that removed flags are not supported.

Export `main` and the exit constants from the package only if tests or documented library use require them; keep implementation helpers module-local.

- [ ] **Step 4: Run complete phase verification to verify GREEN**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests benchmark
.venv/bin/mypy src/hologram
.venv/bin/python -m hologram build --root . --quiet
.venv/bin/python -m hologram check --root . --quiet
.venv/bin/python -m hologram diff HEAD~1 --root . --quiet
git diff --check
```

Expected: all tests pass; Ruff and mypy are clean; build/check/diff return `0`;
diff check is silent. Review the generated standalone map, confirm it is fresh,
and confirm no root agent file was created by the empty-agent self-config.

- [ ] **Step 5: Commit delivery documentation and consumer migration**

```bash
git add .hologram.toml benchmark/bench.py tests/test_bench.py tests/test_delivery_cli.py tests/test_freshness_and_markers.py src/hologram/__init__.py README.md
git commit -m "docs: publish hologram v2 delivery workflow"
```

## Phase risks and handoff

- Atomic replacement is guaranteed per target, not across three independent files. Complete preflight plus `check` makes interrupted multi-target builds detectable.
- Existing non-shell hooks cannot safely accept shell fragments. Fail closed and require `--no-hook`; never rename or wrap authored hooks automatically.
- Revision archives omit untracked worktree files by definition. `diff REV` compares the committed revision with the complete current worktree and labels that asymmetry in output.
- Source mutation during scanner capture may fail the build with exit `3`; after
  `BuildSnapshot` exists, every downstream phase deterministically uses its
  immutable bytes/events and never probes the path again.
- Managed-block parsing operates on bytes to preserve authored encodings and line endings. Only generated UTF-8 block bytes are decoded.
- Schema 2 and removed legacy flags are intentional breaking changes. Support only exact managed-block/post-hook migration, not a second compatibility CLI.
- New-code `×0`, `×0?`, and duplicate findings are review prompts. They do not prove unused code, semantic equivalence, or safe deletion.
