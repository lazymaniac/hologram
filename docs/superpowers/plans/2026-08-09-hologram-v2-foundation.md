# Hologram v2 Package and Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the repository-root script boundary with an installable `src/hologram` package and establish strict configuration, immutable canonical IR, a complete source ledger, one-read source snapshots, and versioned SHA-256 state hashing.

**Architecture:** Move the current implementation intact to `src/hologram/legacy.py` before extracting behavior. All new boundaries use frozen dataclasses and relative POSIX paths; the scanner reads each included supported source exactly once into immutable bytes, and every later phase consumes that snapshot rather than the filesystem. `.hologram.toml` schema 2 is validated before scanning, and the state value commits only to supported candidate facts, source bytes, canonical configuration, and the active IR/extractor/parser versions.

**Tech Stack:** Python 3.11+, standard-library `dataclasses`, `enum.StrEnum`, `tomllib`, `hashlib`, `subprocess`, setuptools src-layout packaging, and `unittest`.

---

## Fixed file responsibilities

- `pyproject.toml` — package metadata, pinned parser extras, development tools, and the temporary legacy console entry point.
- `src/hologram/legacy.py` — the moved v1 implementation; it remains the compatibility implementation until later phase plans remove sections from it.
- `src/hologram/__init__.py` — stable package exports plus temporary legacy attribute compatibility.
- `src/hologram/__main__.py` — `python -m hologram` adapter.
- `src/hologram/model.py` — immutable canonical source, identity, span, raw-fact, body, file, project, and diagnostic types.
- `src/hologram/config.py` — strict `.hologram.toml` schema 2 loading and canonical serialization through `render_config()`.
- `src/hologram/scan.py` — Git/filesystem candidate discovery, include/exclude classification, and the single source-byte read.
- `src/hologram/state.py` — deterministic SHA-256 state framing over the scanner snapshot and tool/config versions.

Do not create a repository-root `hologram.py` compatibility shim. Once Task 1 moves the file, package imports and module execution are the only supported development paths.

### Task 1: Move the monolith into an installable src-layout package

**Files:**
- Create: `pyproject.toml`
- Move: `hologram.py` → `src/hologram/legacy.py`
- Create: `src/hologram/__init__.py`
- Create: `src/hologram/__main__.py`
- Create: `tests/test_package_layout.py`
- Modify: `.gitignore`
- Modify: `src/hologram/legacy.py`
- Modify: `benchmark/bench.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`

- [ ] **Step 1: Write the package-boundary regression tests**

Create `tests/test_package_layout.py`:

```python
import importlib.metadata
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTest(unittest.TestCase):
    def test_repository_root_has_no_compatibility_shim(self):
        self.assertFalse((ROOT / "hologram.py").exists())

    def test_import_resolves_to_src_package(self):
        import hologram

        self.assertEqual(
            Path(hologram.__file__).resolve(),
            ROOT / "src" / "hologram" / "__init__.py",
        )

    def test_module_entry_point_exposes_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "hologram", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: hologram", result.stdout)

    def test_console_script_targets_package(self):
        scripts = {
            entry.name: entry.value
            for entry in importlib.metadata.entry_points(group="console_scripts")
        }
        self.assertEqual(scripts["hologram"], "hologram.legacy:run_cli")

    def test_editable_install_metadata_is_ignored(self):
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(".egg-info/", result.stdout)


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_cli.py`, change the existing hook assertion to:

```python
self.assertEqual(content.count("-m hologram"), 1)
self.assertNotIn("hologram.py", content)
```

- [ ] **Step 2: Run the package test to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_package_layout.py -v
```

Expected: FAIL because `hologram` resolves to the root `hologram.py`, it is not a package with `__main__`, and no installed distribution exposes the console entry point.

- [ ] **Step 3: Move the script before creating package adapters**

Run exactly:

```bash
mkdir -p src/hologram
git mv hologram.py src/hologram/legacy.py
```

Do not copy the file and do not leave a root shim.

- [ ] **Step 4: Add package metadata and pinned optional parser dependencies**

Add `*.egg-info/` to `.gitignore`, then create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "hologram-code-map"
version = "0.2.0"
description = "Deterministic whole-codebase symbol maps for coding agents"
readme = "README.md"
requires-python = ">=3.11"
license = { file = "LICENSE" }
dependencies = []

[project.optional-dependencies]
parsers = [
  "tree-sitter==0.26.0",
  "tree-sitter-c==0.24.2",
  "tree-sitter-c-sharp==0.23.5",
  "tree-sitter-cpp==0.23.4",
  "tree-sitter-go==0.25.0",
  "tree-sitter-html==0.23.2",
  "tree-sitter-java==0.23.5",
  "tree-sitter-kotlin==1.1.0",
  "tree-sitter-lua==0.5.0",
  "tree-sitter-rust==0.24.2",
  "tree-sitter-typescript==0.23.2",
]
dev = [
  "mypy>=1.17,<2",
  "ruff>=0.12,<1",
  "tree-sitter==0.26.0",
  "tree-sitter-c==0.24.2",
  "tree-sitter-c-sharp==0.23.5",
  "tree-sitter-cpp==0.23.4",
  "tree-sitter-go==0.25.0",
  "tree-sitter-html==0.23.2",
  "tree-sitter-java==0.23.5",
  "tree-sitter-kotlin==1.1.0",
  "tree-sitter-lua==0.5.0",
  "tree-sitter-rust==0.24.2",
  "tree-sitter-typescript==0.23.2",
]

[project.scripts]
hologram = "hologram.legacy:run_cli"

[tool.setuptools.packages.find]
where = ["src"]
```

The complete advertised language profile requires installing `.[parsers]`.
Python's stdlib-AST path and the built-in Helm path can each run individually
without that extra; neither path performs runtime installation. Keep the full
extra in the package and phase-gate commands because those commands verify the
complete advertised profile.

- [ ] **Step 5: Add the temporary package adapters**

Create `src/hologram/__init__.py`:

```python
"""Public Hologram package."""

from . import legacy as _legacy
from .legacy import (
    Symbol,
    build_digest,
    embed_digest,
    extract_file,
    has_parser,
    render_simple,
    run_cli,
    scan_files,
)

__all__ = [
    "Symbol",
    "build_digest",
    "embed_digest",
    "extract_file",
    "has_parser",
    "render_simple",
    "run_cli",
    "scan_files",
]


def __getattr__(name: str):
    """Temporary compatibility for tests of legacy private helpers."""
    return getattr(_legacy, name)
```

Create `src/hologram/__main__.py`:

```python
from .legacy import run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli())
```

- [ ] **Step 6: Remove runtime package installation from the moved legacy CLI**

Replace `_bootstrap_or_die` in `src/hologram/legacy.py` with a fail-fast dependency message:

```python
def _bootstrap_or_die(missing: set[str], argv: list[str]) -> None:
    del argv
    packages = " ".join(_grammar_pkgs(missing))
    raise SystemExit(
        f"missing tree-sitter parser for: {', '.join(sorted(missing))}\n"
        f"install the parser extra with: {sys.executable} -m pip install "
        "'hologram-code-map[parsers]'\n"
        f"required packages: {packages}"
    )
```

Change hook generation to invoke the installed module rather than an absolute source file:

```python
hook_line = (
    f'{_hook_python()} -m hologram build --root "{repo.resolve()}"'
    f"{lang_args}{embed_arg} --quiet || true\n"
)
```

Remove `_venv_has_grammars`, `_reexec`, `os.execv`, venv creation, and pip installation paths. Keep `_hook_python()` temporarily returning `sys.executable`; the delivery plan replaces the hook subsystem.

- [ ] **Step 7: Change benchmark subprocesses to module execution**

In `benchmark/bench.py`, replace the `HOLOGRAM` path constant with:

```python
def _hologram_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "hologram", *args]
```

In all three benchmark subprocesses, replace the command prefix `[sys.executable, str(HOLOGRAM), "build"]` with `_hologram_command("build")` while preserving each call's existing trailing arguments. In `README.md`, replace direct `hologram.py` examples with `hologram` and add this installation command before first use:

```bash
python3 -m pip install -e '.[parsers]'
```

- [ ] **Step 8: Install editable and verify GREEN**

Run:

```bash
.venv/bin/python -m pip install -e '.[parsers]'
.venv/bin/python -m unittest tests/test_package_layout.py tests/test_cli.py tests/test_bench.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: package, CLI, benchmark, and complete legacy suite pass; `python -m hologram` works; the root `hologram.py` path no longer exists; and `git status --short --untracked-files=all` contains no `.egg-info/` entry (the intended staged rename may still mention the old path before commit).

- [ ] **Step 9: Commit the package boundary**

```bash
git add .gitignore pyproject.toml src/hologram README.md benchmark/bench.py tests/test_package_layout.py tests/test_cli.py
git commit -m "refactor: move hologram into src package"
```

### Task 2: Define immutable canonical IR and diagnostic contracts

**Files:**
- Create: `src/hologram/model.py`
- Create: `tests/test_model.py`
- Modify: `src/hologram/__init__.py`

- [ ] **Step 1: Write tests for stable identity, source ownership, and deep immutability**

Create `tests/test_model.py` with these cases:

```python
import dataclasses
import hashlib
import unittest
from pathlib import Path

from hologram.model import (
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    FileIR,
    Language,
    ProjectIR,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)


class CanonicalModelTest(unittest.TestCase):
    def test_symbol_id_is_line_independent(self):
        identity = SymbolId(
            Language.JAVA,
            "src/shop/Price.java",
            ("shop", "Price"),
            SymbolKind.METHOD,
            "quote",
            "(OrderId)",
        )
        before = Symbol(
            id=identity,
            span=SourceSpan("src/shop/Price.java", 8, 4, 10, 5),
            visibility=Visibility.PUBLIC,
            signature="quote(OrderId):Quote",
            params=("OrderId",),
            returns="Quote",
        )
        after = dataclasses.replace(
            before,
            span=SourceSpan("src/shop/Price.java", 108, 4, 110, 5),
        )
        self.assertEqual(before.id, after.id)

    def test_source_snapshot_owns_immutable_bytes(self):
        raw = b"def f():\n    return 1\n"
        source = SourceFile(
            path=Path("/repo/f.py"),
            file="f.py",
            language=Language.PYTHON,
            role=SourceRole.PRODUCTION,
            raw=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(source.text, raw.decode("utf-8"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            source.raw = b"changed"

    def test_body_span_retains_source_for_later_analysis(self):
        symbol_id = SymbolId(
            Language.PYTHON, "f.py", (), SymbolKind.FUNCTION, "f", "()"
        )
        body = BodyIR(
            symbol_id,
            SourceSpan("f.py", 1, 0, 2, 12),
            (
                BodyEvent(
                    BodyEventKind.LITERAL,
                    "<number>",
                    SourceSpan("f.py", 2, 11, 2, 12),
                ),
            ),
        )
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            b"def f():\n    return 1\n",
            hashlib.sha256(b"def f():\n    return 1\n").hexdigest(),
        )
        file_ir = FileIR(source=source, module="f", bodies=(body,))
        project = ProjectIR(Path("/repo"), (file_ir,), (), True)
        self.assertIs(project.files[0].source.raw, raw := source.raw)
        self.assertEqual(raw[0:3], b"def")

    def test_raw_call_is_not_capped_or_resolved_in_ir(self):
        caller = SymbolId(
            Language.PYTHON, "f.py", (), SymbolKind.FUNCTION, "f", "()"
        )
        calls = tuple(
            CallRef(
                caller,
                SourceSpan("f.py", line, 4, line, 10),
                f"call_{line}",
                None,
                CallKind.CALL,
                0,
            )
            for line in range(1, 15)
        )
        self.assertEqual(len(calls), 14)
        self.assertEqual(calls[-1].name, "call_14")

    def test_dynamic_reference_keeps_context_and_confidence(self):
        owner = SymbolId(
            Language.JAVA, "App.java", ("App",), SymbolKind.METHOD, "config", "()"
        )
        reference = ReferenceRef(
            owner,
            SourceSpan("App.java", 4, 10, 4, 19),
            "onRefresh",
            None,
            ReferenceKind.NAME,
            ReferenceContext.ANNOTATION,
            ReferenceConfidence.POSSIBLE,
        )
        self.assertEqual(reference.context, ReferenceContext.ANNOTATION)
        self.assertEqual(reference.confidence, ReferenceConfidence.POSSIBLE)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the model tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_model.py -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'hologram.model'`.

- [ ] **Step 3: Implement the exact canonical types**

Create `src/hologram/model.py` with `@dataclass(frozen=True, slots=True)` on every record and these exact public fields:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath


IR_SCHEMA_VERSION = 2


class Language(StrEnum):
    JAVA = "java"
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    TSX = "tsx"
    VUE = "vue"
    SVELTE = "svelte"
    KOTLIN = "kotlin"
    GO = "go"
    RUST = "rust"
    CSHARP = "csharp"
    C = "c"
    CPP = "cpp"
    LUA = "lua"
    HTML = "html"
    HELM = "helm"


class SymbolKind(StrEnum):
    CLASS = "class"
    INTERFACE = "interface"
    RECORD = "record"
    ENUM = "enum"
    TYPE = "type"
    FUNCTION = "fn"
    METHOD = "method"
    CONSTRUCTOR = "ctor"
    REEXPORT = "reexport"
    FIELD = "field"
    PROPERTY = "property"
    CONSTANT = "constant"
    MODULE = "module"


class Visibility(StrEnum):
    PUBLIC = "pub"
    PROTECTED = "protected"
    INTERNAL = "internal"
    PRIVATE = "private"


class SourceRole(StrEnum):
    PRODUCTION = "production"
    TEST = "test"
    GENERATED = "generated"


class CallKind(StrEnum):
    CALL = "call"
    CONSTRUCT = "construct"


class ReferenceKind(StrEnum):
    NAME = "name"
    TYPE = "type"


class ReferenceContext(StrEnum):
    CODE = "code"
    TYPE = "type"
    ANNOTATION = "annotation"
    STRING = "string"
    CONFIG = "config"
    REFLECTION = "reflection"


class ReferenceConfidence(StrEnum):
    DEFINITE = "definite"
    POSSIBLE = "possible"


class BodyEventKind(StrEnum):
    PARAM = "param"
    LOCAL = "local"
    NAME = "name"
    TYPE = "type"
    CALL = "call"
    CONSTRUCT = "construct"
    MEMBER = "member"
    LITERAL = "literal"
    OPERATOR = "operator"
    KEYWORD = "keyword"
    CONTROL_ENTER = "control-enter"
    CONTROL_EXIT = "control-exit"


class DiagnosticSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True, order=True)
class SourceSpan:
    """One-based lines and zero-based UTF-8 byte columns, end-exclusive."""

    file: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        _validate_relative_file(self.file)
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("source lines must be positive and ordered")
        if self.start_column < 0 or self.end_column < 0:
            raise ValueError("source columns must be non-negative")
        if self.end_line == self.start_line and self.end_column < self.start_column:
            raise ValueError("source columns must be ordered on one line")


@dataclass(frozen=True, slots=True, order=True)
class SymbolId:
    language: Language
    file: str
    container_path: tuple[str, ...]
    kind: SymbolKind
    name: str
    signature_key: str

    def __post_init__(self) -> None:
        _validate_relative_file(self.file)
        if not self.name:
            raise ValueError("symbol name must not be empty")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    file: str
    language: Language
    role: SourceRole
    raw: bytes
    sha256: str

    def __post_init__(self) -> None:
        _validate_relative_file(self.file)
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("source sha256 must be 64 lowercase hex characters")

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", errors="strict")


@dataclass(frozen=True, slots=True)
class Binding:
    name: str
    type_name: str


@dataclass(frozen=True, slots=True)
class CallRef:
    caller: SymbolId
    span: SourceSpan
    name: str
    receiver: str | None
    kind: CallKind
    arity: int | None


@dataclass(frozen=True, slots=True)
class ImportRef:
    span: SourceSpan
    module: str
    name: str | None
    alias: str | None
    wildcard: bool = False
    reexport: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceRef:
    owner: SymbolId | None
    span: SourceSpan
    name: str
    qualifier: str | None
    kind: ReferenceKind
    context: ReferenceContext
    confidence: ReferenceConfidence


@dataclass(frozen=True, slots=True)
class BodyEvent:
    kind: BodyEventKind
    text: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BodyIR:
    owner: SymbolId
    span: SourceSpan
    events: tuple[BodyEvent, ...]


@dataclass(frozen=True, slots=True)
class Symbol:
    id: SymbolId
    span: SourceSpan
    visibility: Visibility
    signature: str
    params: tuple[str, ...] = ()
    returns: str | None = None
    supers: tuple[str, ...] = ()
    permits: tuple[str, ...] = ()
    raises: tuple[str, ...] = ()
    bindings: tuple[Binding, ...] = ()
    components: tuple[str, ...] = ()
    annotations: tuple[str, ...] = ()
    modifiers: tuple[str, ...] = ()
    body_lines: int = 0

    @property
    def name(self) -> str:
        return self.id.name

    @property
    def kind(self) -> SymbolKind:
        return self.id.kind

    @property
    def file(self) -> str:
        return self.id.file

    @property
    def lang(self) -> Language:
        return self.id.language

    @property
    def container(self) -> str | None:
        return self.id.container_path[-1] if self.id.container_path else None


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: DiagnosticSeverity
    message: str
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class FileIR:
    source: SourceFile
    module: str | None = None
    symbols: tuple[Symbol, ...] = ()
    calls: tuple[CallRef, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    references: tuple[ReferenceRef, ...] = ()
    bodies: tuple[BodyIR, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    extractor_version: str = ""
    parser_version: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectIR:
    root: Path
    files: tuple[FileIR, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool


def _validate_relative_file(file: str) -> None:
    path = PurePosixPath(file)
    if not file or path.is_absolute() or ".." in path.parts or "\\" in file:
        raise ValueError(f"not a normalized relative POSIX path: {file!r}")
```

`SymbolId.signature_key` is the overload discriminator, not rendered text: functions/methods use `(<comma-separated normalized parameter types>)`; constructors use the same form; declarations that cannot overload use an empty string. Never include line numbers, columns, body hashes, or return types in an ID.

- [ ] **Step 4: Export canonical types and verify GREEN**

Add the model types to `src/hologram/__init__.py` without replacing the legacy `Symbol` export yet; export the canonical class as `CanonicalSymbol` until the extractor cutover. Re-export every enum and record in the public model surface, including `SourceRole`, `Visibility`, `SymbolKind`, `BodyEventKind`, `BodyEvent`, `BodyIR`, the three raw reference records, `Diagnostic`, `FileIR`, and `ProjectIR`:

```python
from .model import (
    Binding,
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    ImportRef,
    Language,
    ProjectIR,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceRole,
    SourceSpan,
    Symbol as CanonicalSymbol,
    SymbolId,
    SymbolKind,
    Visibility,
)

__all__ += [
    "Binding",
    "BodyEvent",
    "BodyEventKind",
    "BodyIR",
    "CallKind",
    "CallRef",
    "CanonicalSymbol",
    "Diagnostic",
    "DiagnosticSeverity",
    "FileIR",
    "ImportRef",
    "Language",
    "ProjectIR",
    "ReferenceConfidence",
    "ReferenceContext",
    "ReferenceKind",
    "ReferenceRef",
    "SourceFile",
    "SourceRole",
    "SourceSpan",
    "SymbolId",
    "SymbolKind",
    "Visibility",
]
```

Run:

```bash
.venv/bin/python -m unittest tests/test_model.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: model tests and all legacy tests pass; moving a declaration between lines leaves `SymbolId` unchanged, while `SourceSpan` changes.

- [ ] **Step 5: Commit the canonical model**

```bash
git add src/hologram/model.py src/hologram/__init__.py tests/test_model.py
git commit -m "feat: define immutable canonical IR"
```

### Task 3: Load strict `.hologram.toml` schema 2

**Files:**
- Create: `src/hologram/config.py`
- Create: `tests/test_config.py`
- Modify: `src/hologram/__init__.py`
- Modify: `src/hologram/legacy.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_freshness_and_markers.py`
- Modify: `tests/test_simple_mode.py`

- [ ] **Step 1: Write strict loader tests**

Create `tests/test_config.py` around this complete valid manifest:

```python
VALID = """\
schema_version = 2
agents = ["claude", "codex", "gemini"]
languages = ["java", "python", "typescript"]
include = ["src/**", "tests/**"]
exclude = ["**/generated/**"]
hot_threshold = 10
output = "PROJECT_DIGEST.md"
"""
```

The test class must assert:

```python
def test_loads_exact_schema(self):
    config = self.load(VALID)
    self.assertEqual(config.schema_version, 2)
    self.assertEqual(config.agents, ("claude", "codex", "gemini"))
    self.assertEqual(
        config.languages,
        (Language.JAVA, Language.PYTHON, Language.TYPESCRIPT),
    )
    self.assertEqual(config.include, ("src/**", "tests/**"))
    self.assertEqual(config.exclude, ("**/generated/**",))
    self.assertEqual(config.hot_threshold, 10)
    self.assertEqual(config.output, "PROJECT_DIGEST.md")

def test_missing_manifest_is_an_error(self):
    with self.assertRaisesRegex(ConfigError, r"missing .*\.hologram\.toml"):
        load_config(self.root)

def test_omitted_languages_auto_detect_and_output_is_optional(self):
    text = """\
schema_version = 2
agents = ["claude"]
"""
    config = self.load(text)
    self.assertEqual(config.languages, ())
    self.assertIsNone(config.output)
    self.assertEqual(config.include, ("**/*",))
    self.assertTrue(config.exclude)

def test_rejects_unknown_and_missing_keys(self):
    for text in (
        VALID + "mispelled = true\n",
        VALID.replace("schema_version = 2\n", ""),
    ):
        with self.subTest(text=text), self.assertRaises(ConfigError):
            self.load(text)

def test_rejects_wrong_schema_types_and_values(self):
    replacements = (
        ("schema_version = 2", "schema_version = 1"),
        ("hot_threshold = 10", "hot_threshold = true"),
        ("hot_threshold = 10", "hot_threshold = 0"),
        ("languages = [\"java\", \"python\", \"typescript\"]", "languages = [\"brainfuck\"]"),
        ("agents = [\"claude\", \"codex\", \"gemini\"]", "agents = [\"unknown\"]"),
        ("output = \"PROJECT_DIGEST.md\"", "output = \"../escape.md\""),
        ("output = \"PROJECT_DIGEST.md\"", "output = \"CLAUDE.md\""),
        ("include = [\"src/**\", \"tests/**\"]", "include = [\"/absolute/**\"]"),
    )
    for old, new in replacements:
        with self.subTest(new=new), self.assertRaises(ConfigError):
            self.load(VALID.replace(old, new))

def test_agents_may_be_empty_only_with_digest_output(self):
    digest_only = VALID.replace(
        'agents = ["claude", "codex", "gemini"]',
        "agents = []",
    )
    self.assertEqual(self.load(digest_only).agents, ())
    with self.assertRaises(ConfigError):
        self.load(digest_only.replace('output = "PROJECT_DIGEST.md"\n', ""))

def test_render_config_round_trips_canonical_defaults(self):
    config = default_config()
    rendered = render_config(config)
    path = self.root / ".hologram.toml"
    path.write_text(rendered)
    self.assertEqual(load_config(self.root), config)
    self.assertEqual(rendered, render_config(config))
```

Also assert `canonical_config_bytes()` is identical for manifests whose `agents` and nonempty `languages` contain the same unique values in a different order.

In `tests/test_cli.py`, add a temporary legacy-boundary characterization: `build` and `check` with no selected `.hologram.toml` raise `ConfigError` before scanning, while `init` creates exactly `render_config(default_config())` when the file is absent and then proceeds. An existing manifest is loaded and never overwritten. The delivery plan later maps this typed error to exit `2` and replaces the temporary CLI; do not add a second defaulting path here.

Add this helper to `tests/test_cli.py`, `tests/test_freshness_and_markers.py`, and `tests/test_simple_mode.py`, and call it before every temporary-root `run_cli(["build", ...])` or `run_cli(["check", ...])` invocation that is not specifically testing the missing-manifest error:

```python
def write_manifest(root: Path) -> ProjectConfig:
    config = default_config()
    (root / CONFIG_NAME).write_text(render_config(config), encoding="utf-8")
    return config
```

Direct `build_digest()` characterization calls remain library calls and may pass `default_config()` explicitly; they must not invoke `load_config()` implicitly.

- [ ] **Step 2: Run the config tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_config.py tests/test_cli.py -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'hologram.config'`; once the module exists but before the legacy adapter is changed, the new CLI cases still fail because build/check do not require a manifest and init does not create one.

- [ ] **Step 3: Implement the immutable schema and defaults**

Create `src/hologram/config.py` with the immutable records above and these frozen public signatures. This is a signature block, not an implementation placeholder; implement the complete validation/defaulting/serialization algorithm immediately below it.

```text
CONFIG_NAME = ".hologram.toml"
CONFIG_SCHEMA_VERSION = 2
ALLOWED_AGENTS = frozenset({"claude", "codex", "gemini"})


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    schema_version: int
    agents: tuple[str, ...]
    languages: tuple[Language, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    hot_threshold: int
    output: str | None


class ConfigError(ValueError):
    pass

load_config(root: Path, path: Path | None = None) -> ProjectConfig
default_config() -> ProjectConfig
render_config(config: ProjectConfig) -> str
canonical_config_bytes(config: ProjectConfig) -> bytes
```

Use these exact values for `default_config()`. In a present manifest, omitted `agents`, `languages`, `include`, `exclude`, and `hot_threshold` follow the rules below; omitted `output` intentionally becomes `None` rather than inheriting the init default. These values are never a missing-manifest fallback: `load_config()` still raises when its selected path does not exist.

```python
def default_config() -> ProjectConfig:
    return ProjectConfig(
        schema_version=2,
        agents=("claude", "codex", "gemini"),
        languages=(),
        include=("**/*",),
        exclude=(
            "**/.git/**",
            "**/.venv/**",
            "**/__pycache__/**",
            "**/bin/**",
            "**/build/**",
            "**/dist/**",
            "**/generated/**",
            "**/node_modules/**",
            "**/obj/**",
            "**/out/**",
            "**/target/**",
            "**/vendor/**",
        ),
        hot_threshold=10,
        output="PROJECT_DIGEST.md",
    )
```

`load_config()` must raise `ConfigError` when the selected manifest is missing. A present manifest requires `schema_version = 2`; the other six keys are optional. Omitted or empty `languages` means scanner auto-detection. Omitted `agents` uses the three-agent default; omitted `include`, `exclude`, and `hot_threshold` use `default_config()` values; omitted `output` means `None`. Explicit `agents = []` is valid only when `output` is non-`None`, so every configuration has at least one delivery target.

Reject booleans where integers are required, duplicates, unknown agents/languages, absolute paths, backslashes, and any `..` path component. Require nonempty `include`; allow empty `exclude`. A non-`None` output must be one root-relative `.md` path distinct from `.hologram.toml`, `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md`. Normalize agents and nonempty languages into sorted tuples; preserve include/exclude order for matching.

`render_config()` emits all seven keys in the order used by `VALID`, using `languages = []` for auto-detect and omitting only `output` when it is `None`; it ends with one newline. `canonical_config_bytes()` is exactly `render_config(config).encode("utf-8")`, so init, hashing, and tests share one canonical representation.

At the temporary `legacy.run_cli()` boundary, resolve the manifest before any build/check scan. For `init`, write `render_config(default_config())` only when the selected file is absent, then load it; for `build` and `check`, call `load_config(root)` and let `ConfigError` propagate. Pass that loaded `ProjectConfig` through legacy build/state helpers. Direct legacy library tests may opt into `default_config()` explicitly, but commands must never do so implicitly.

Export `CONFIG_NAME`, `CONFIG_SCHEMA_VERSION`, `ConfigError`, `ProjectConfig`,
`load_config`, `default_config`, `render_config`, and
`canonical_config_bytes` from `src/hologram/__init__.py` in this task.

- [ ] **Step 4: Run focused and complete tests to verify GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_config.py tests/test_cli.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: valid schema and defaults pass; every malformed variant raises `ConfigError` containing the manifest path and offending field.

- [ ] **Step 5: Commit strict configuration**

```bash
git add src/hologram/config.py src/hologram/__init__.py src/hologram/legacy.py tests/test_config.py tests/test_cli.py tests/test_freshness_and_markers.py tests/test_simple_mode.py
git commit -m "feat: add strict hologram config schema"
```

### Task 4: Build a complete one-read source candidate ledger

**Files:**
- Create: `src/hologram/scan.py`
- Create: `tests/test_scan.py`
- Modify: `src/hologram/legacy.py`
- Modify: `src/hologram/__init__.py`

- [ ] **Step 1: Write scanner tests for Git, exclusions, failures, and snapshot bytes**

Create `tests/test_scan.py` with temporary repositories and these assertions:

```python
def test_git_scan_includes_tracked_and_untracked_nonignored(self):
    self.git("init", "-q")
    self.write("tracked.py", "def tracked(): pass\n")
    self.git("add", "tracked.py")
    self.write("new.py", "def new(): pass\n")
    self.write("ignored.py", "def ignored(): pass\n")
    self.write(".gitignore", "ignored.py\n")
    result = scan_project(self.root, self.config())
    indexed = {entry.file for entry in result.entries if entry.status is ScanStatus.INDEXED}
    self.assertEqual(indexed, {"new.py", "tracked.py"})

def test_indexed_entry_owns_the_only_byte_snapshot(self):
    self.write("svc.py", "def run(): return 1\n")
    result = scan_project(self.root, self.config())
    entry = next(e for e in result.entries if e.file == "svc.py")
    self.assertEqual(entry.source.raw, b"def run(): return 1\n")
    self.assertEqual(entry.source.sha256, hashlib.sha256(entry.source.raw).hexdigest())
    (self.root / "svc.py").write_text("changed after scan\n")
    self.assertEqual(entry.source.raw, b"def run(): return 1\n")

def test_roles_are_explicit_for_marker_analysis(self):
    self.write("src/main.py", "x = 1\n")
    self.write("test/helper.py", "x = 1\n")
    self.write("tests/test_x.py", "x = 1\n")
    self.write("spec/helper.ts", "export const x = 1\n")
    self.write("specs/helper.py", "x = 1\n")
    self.write("web/foo.spec.ts", "export const x = 1\n")
    self.write("go/x_test.go", "package go\n")
    self.write("java/XTest.java", "class XTest {}\n")
    self.write("generated/schema.py", "x = 1\n")
    config = self.config(exclude=())
    result = scan_project(self.root, config)
    roles = {entry.file: entry.source.role for entry in result.entries if entry.source}
    self.assertEqual(roles["src/main.py"], SourceRole.PRODUCTION)
    self.assertEqual(roles["test/helper.py"], SourceRole.TEST)
    self.assertEqual(roles["tests/test_x.py"], SourceRole.TEST)
    self.assertEqual(roles["spec/helper.ts"], SourceRole.TEST)
    self.assertEqual(roles["specs/helper.py"], SourceRole.TEST)
    self.assertEqual(roles["web/foo.spec.ts"], SourceRole.TEST)
    self.assertEqual(roles["go/x_test.go"], SourceRole.TEST)
    self.assertEqual(roles["java/XTest.java"], SourceRole.TEST)
    self.assertEqual(roles["generated/schema.py"], SourceRole.GENERATED)

def test_every_discovered_path_has_a_status_and_reason(self):
    self.write("src/keep.py", "x = 1\n")
    self.write("src/generated/drop.py", "x = 2\n")
    self.write("notes.txt", "not source\n")
    result = scan_project(self.root, self.config(include=("**/*",), exclude=("**/generated/**",)))
    ledger = {entry.file: (entry.status, entry.reason) for entry in result.entries}
    self.assertEqual(ledger["src/keep.py"], (ScanStatus.INDEXED, None))
    self.assertEqual(ledger["src/generated/drop.py"], (ScanStatus.EXCLUDED, "exclude-pattern"))
    self.assertEqual(ledger["notes.txt"], (ScanStatus.EXCLUDED, "unsupported-language"))

def test_invalid_utf8_source_retains_bytes_and_fails_closed(self):
    (self.root / "bad.py").write_bytes(b"\xff\xfe")
    result = scan_project(self.root, self.config())
    entry = next(e for e in result.entries if e.file == "bad.py")
    self.assertEqual(entry.status, ScanStatus.FAILED)
    self.assertEqual(entry.reason, "invalid-utf8")
    self.assertEqual(entry.source.raw, b"\xff\xfe")
    self.assertEqual([d.code for d in result.diagnostics], ["scan-invalid-utf8"])
    self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)
    self.assertFalse(result.complete)

def test_unreadable_source_has_no_snapshot_and_fails_closed(self):
    self.write("locked.py", "x = 1\n")
    original = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path.name == "locked.py":
            raise OSError("permission denied")
        return original(path)

    with patch.object(Path, "read_bytes", read_bytes):
        result = scan_project(self.root, self.config())
    entry = next(e for e in result.entries if e.file == "locked.py")
    self.assertEqual(entry.status, ScanStatus.FAILED)
    self.assertEqual(entry.reason, "read-error")
    self.assertIsNone(entry.source)
    self.assertEqual([d.code for d in result.diagnostics], ["scan-read-error"])
    self.assertFalse(result.complete)
```

Add cases for a linked worktree (`.git` is a file), non-Git filesystem fallback, deterministic POSIX sorting, include mismatch, paths excluded by the configured default patterns, missing tracked files, symlinks escaping root, and a mocked `git ls-files` error. Assert the exact Git discovery command contains:

```python
[
    "git", "-C", str(root), "ls-files",
    "--cached", "--others", "--exclude-standard", "-z",
]
```

- [ ] **Step 2: Run the scanner tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_scan.py -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'hologram.scan'`.

- [ ] **Step 3: Implement scanner records and language detection**

Create `src/hologram/scan.py` with these immutable records and frozen public signatures. The two final lines are signatures only; implement them with the discovery/classification algorithm below.

```text
class ScanStatus(StrEnum):
    INDEXED = "indexed"
    EXCLUDED = "excluded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScanEntry:
    path: Path
    file: str
    language: Language | None
    status: ScanStatus
    reason: str | None
    source: SourceFile | None


@dataclass(frozen=True, slots=True)
class ScanResult:
    entries: tuple[ScanEntry, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool

    @property
    def sources(self) -> tuple[SourceFile, ...]:
        return tuple(
            entry.source
            for entry in self.entries
            if entry.status is ScanStatus.INDEXED and entry.source is not None
        )


detect_language(path: Path) -> Language | None
scan_project(root: Path, config: ProjectConfig) -> ScanResult
```

Use this exact immutable suffix map:

```python
LANGUAGE_BY_SUFFIX = MappingProxyType({
    ".java": Language.JAVA,
    ".py": Language.PYTHON,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TSX,
    ".jsx": Language.TSX,
    ".js": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".vue": Language.VUE,
    ".svelte": Language.SVELTE,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".cs": Language.CSHARP,
    ".c": Language.C,
    ".h": Language.C,
    ".cpp": Language.CPP,
    ".cc": Language.CPP,
    ".cxx": Language.CPP,
    ".hpp": Language.CPP,
    ".hh": Language.CPP,
    ".lua": Language.LUA,
    ".html": Language.HTML,
    ".htm": Language.HTML,
    ".yaml": Language.HELM,
    ".yml": Language.HELM,
    ".tpl": Language.HELM,
})
```

For a Git worktree, discover the union of tracked and untracked nonignored paths with the exact command tested above. For a non-Git directory, use a sorted `os.walk` without following symlink directories. Normalize every relative path through `PurePosixPath` and sort entries by `entry.file`.

Classify in this order: unsafe/outside-root → failed; unsupported language → excluded; language disabled when `config.languages` is nonempty → excluded; include miss → excluded; exclude hit → excluded; non-regular/missing → failed; read error → failed; invalid UTF-8 → failed; otherwise indexed. A successful byte read creates `SourceFile(path, file, language, role, raw, sha256(raw))` before UTF-8 validation, so an invalid-UTF-8 failed entry retains its immutable raw snapshot while an unreadable/missing entry has `source=None`. Empty `config.languages` auto-detects every supported language found. Every `FAILED` entry contributes exactly one `Diagnostic(DiagnosticSeverity.ERROR)` whose code is `scan-<reason>` and whose span is `None`; preserve entry order when collecting diagnostics.

Implement glob matching once for both include and exclude. A leading `**/` also matches at the project root, so the default `**/*` includes `svc.py` as well as `src/svc.py`; test both forms rather than relying on platform-specific `Path.match()` edge behavior.

Role classification is deterministic and path-only. Compare lowercase path segments: any segment equal to `test`, `tests`, `spec`, or `specs` is `TEST`. Otherwise remove the one recognized language suffix and compare the resulting stem: its lowercase form starts with `test_` or ends with `_test`, `.test`, or `.spec`, or its original case ends with `Test` or `Tests`; each is `TEST`. This recognizes `test_x.py`, `x_test.go`, `foo.spec.ts`, `foo.test.tsx`, `XTest.java`, and `XTests.cs` without classifying an unrelated lowercase word such as `contest.java`. A lowercase `generated` path segment is `GENERATED` unless the test rule matched; everything else is `PRODUCTION`. Do not read indexed files again in this function or any later phase.

If Git reports that the root is not a worktree, use filesystem discovery without a diagnostic. If Git identifies a worktree but `ls-files` fails or times out, add one failed ledger entry with file `"<git>"`, reason `"git-list-failed"`, an error diagnostic, and `complete=False`; do not silently substitute a filesystem walk.

Export `ScanEntry`, `ScanResult`, `ScanStatus`, `detect_language`, and
`scan_project` from `src/hologram/__init__.py` in this task. The explicit
canonical `detect_language` export replaces temporary legacy attribute lookup;
`scan_files` remains the v1 compatibility adapter.

- [ ] **Step 4: Delegate the legacy scanner to the new ledger**

Replace `legacy.detect_language()` and `legacy.scan_files()` with adapters:

```python
def detect_language(path: Path) -> str | None:
    language = scan.detect_language(path)
    return language.value if language is not None else None


def scan_files(root: Path, config: ProjectConfig | None = None) -> list[Path]:
    result = scan.scan_project(root.resolve(), config or default_config())
    if not result.complete:
        messages = "; ".join(d.message for d in result.diagnostics)
        raise SystemExit(messages or "source scan incomplete")
    return [source.path for source in result.sources]
```

The optional default exists only for direct v1 library compatibility. `legacy.run_cli()` must pass the manifest-backed config loaded in Task 3. Task 8 of the extractor plan replaces this adapter with the exact pipeline.

- [ ] **Step 5: Verify scanner and legacy behavior GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_scan.py tests/test_simple_mode.py tests/test_cli.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all ledger cases pass, tracked and untracked nonignored sources are indexed, byte snapshots survive later disk mutation, and the legacy digest suite remains green.

- [ ] **Step 6: Commit the scanner boundary**

```bash
git add src/hologram/scan.py src/hologram/legacy.py src/hologram/__init__.py tests/test_scan.py
git commit -m "feat: add complete source scan ledger"
```

### Task 5: Hash the exact source snapshot and semantic tool inputs

**Files:**
- Create: `src/hologram/state.py`
- Create: `tests/test_state.py`
- Modify: `src/hologram/legacy.py`
- Modify: `tests/test_freshness_and_markers.py`
- Modify: `src/hologram/__init__.py`

- [ ] **Step 1: Write state tests that forbid filesystem rereads**

Create `tests/test_state.py` with helpers that construct `SourceFile` and `ScanResult` values directly. `self.scan()` returns a `ScanResult`; `self.compute()` accepts a `scan_result` plus version overrides and returns `StateResult`; `self.state()` returns the corresponding `.value`. Cover these exact invariants:

Use this complete fixture factory; import `dataclasses`, `hashlib`, `Mapping` from `collections.abc`, `Path`, `cast` from `typing`, and the referenced foundation types. `_UNSET` distinguishes an omitted output override from `output=None`:

```python
_UNSET = object()


def source(self, file: str, raw: bytes) -> SourceFile:
    return SourceFile(
        path=self.root / file,
        file=file,
        language=detect_language(Path(file)) or Language.PYTHON,
        role=SourceRole.PRODUCTION,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def scan_with_source(self, file: str, raw: bytes) -> ScanResult:
    snapshot = self.source(file, raw)
    return ScanResult(
        entries=(ScanEntry(
            snapshot.path,
            snapshot.file,
            snapshot.language,
            ScanStatus.INDEXED,
            None,
            snapshot,
        ),),
        diagnostics=(),
        complete=True,
    )


def scan(self, *files: str) -> ScanResult:
    entries = tuple(
        ScanEntry(
            (snapshot := self.source(file, b"x = 1\n")).path,
            snapshot.file,
            snapshot.language,
            ScanStatus.INDEXED,
            None,
            snapshot,
        )
        for file in files
    )
    return ScanResult(entries, (), True)


def failed_scan(self) -> ScanResult:
    diagnostic = Diagnostic(
        "scan-read-error",
        DiagnosticSeverity.ERROR,
        "bad.py: permission denied",
    )
    entry = ScanEntry(
        self.root / "bad.py",
        "bad.py",
        Language.PYTHON,
        ScanStatus.FAILED,
        "read-error",
        None,
    )
    return ScanResult((entry,), (diagnostic,), False)


def compute(
    self,
    scan_result: ScanResult | None = None,
    *,
    raw: bytes = b"x = 1\n",
    output: str | None | object = _UNSET,
    extractor_version: str = "2",
    parser_version: str = "stdlib-ast-3.11",
    parser_versions: Mapping[str, str] | None = None,
    extra_unsupported: tuple[str, bytes] | None = None,
    extra_excluded: tuple[str, bytes] | None = None,
) -> StateResult:
    config = self.config
    if output is not _UNSET:
        config = dataclasses.replace(config, output=cast(str | None, output))
    if scan_result is None:
        entries = list(self.scan_with_source("main.py", raw).entries)
        if extra_unsupported is not None:
            file, _ignored_raw = extra_unsupported
            entries.append(ScanEntry(
                self.root / file, file, None,
                ScanStatus.EXCLUDED, "unsupported-language", None,
            ))
        if extra_excluded is not None:
            file, _ignored_raw = extra_excluded
            entries.append(ScanEntry(
                self.root / file, file, detect_language(Path(file)),
                ScanStatus.EXCLUDED, "exclude-pattern", None,
            ))
        scan_result = ScanResult(tuple(entries), (), True)
    return compute_state(
        self.root,
        config,
        scan_result,
        extractor_versions={"python": extractor_version},
        parser_versions=(
            parser_versions
            if parser_versions is not None
            else {"python": parser_version}
        ),
    )


def state(self, **overrides: object) -> str:
    return self.compute(**overrides).value
```

```python
def test_state_uses_snapshot_after_disk_changes(self):
    scan = self.scan_with_source("svc.py", b"before\n")
    first = compute_state(
        self.root,
        self.config,
        scan,
        extractor_versions={"python": "2"},
        parser_versions={"python": "stdlib-ast-3.11"},
    )
    (self.root / "svc.py").write_bytes(b"after\n")
    second = compute_state(
        self.root,
        self.config,
        scan,
        extractor_versions={"python": "2"},
        parser_versions={"python": "stdlib-ast-3.11"},
    )
    self.assertEqual(first.value, second.value)

def test_state_changes_for_every_semantic_input(self):
    baseline = self.state()
    self.assertNotEqual(baseline, self.state(raw=b"changed\n"))
    self.assertNotEqual(baseline, self.state(output="OTHER.md"))
    self.assertNotEqual(baseline, self.state(extractor_version="3"))
    self.assertNotEqual(baseline, self.state(parser_version="stdlib-ast-3.12"))

def test_entry_order_does_not_change_state(self):
    left = self.compute(scan_result=self.scan("a.py", "b.py"))
    right = self.compute(scan_result=self.scan("b.py", "a.py"))
    self.assertEqual(left.value, right.value)

def test_incomplete_scan_produces_incomplete_state(self):
    result = self.compute(scan_result=self.failed_scan())
    self.assertFalse(result.complete)
    self.assertEqual(len(result.value), 64)

def test_unsupported_files_do_not_change_state(self):
    baseline = self.state(extra_unsupported=("README.md", b"first\n"))
    changed = self.state(extra_unsupported=("README.md", b"second\n"))
    self.assertEqual(baseline, changed)

def test_excluded_supported_source_does_not_change_state(self):
    baseline = self.state(extra_excluded=("generated/Model.java", b"first\n"))
    changed_bytes = self.state(extra_excluded=("generated/Model.java", b"second\n"))
    changed_path = self.state(extra_excluded=("generated/Renamed.java", b"first\n"))
    self.assertEqual(baseline, changed_bytes)
    self.assertEqual(baseline, changed_path)

def test_moving_an_indexed_source_behind_exclusion_changes_state(self):
    indexed = self.compute(
        scan_result=self.scan("main.py", "generated/model.py")
    )
    excluded = self.compute(
        extra_excluded=("generated/model.py", b"x = 1\n")
    )
    self.assertNotEqual(indexed.value, excluded.value)

def test_only_active_language_versions_are_hashed(self):
    baseline = self.state(parser_versions={"python": "3.11", "java": "one"})
    irrelevant = self.state(parser_versions={"python": "3.11", "java": "two"})
    active = self.state(parser_versions={"python": "3.12", "java": "one"})
    self.assertEqual(baseline, irrelevant)
    self.assertNotEqual(baseline, active)
```

Also test that `read_digest_state()` accepts exactly the constant header field `state=<64 lowercase hex characters>` and rejects old 12-character, uppercase, missing, spaced, and malformed stamps.

- [ ] **Step 2: Run state tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_state.py -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'hologram.state'`.

- [ ] **Step 3: Implement length-framed SHA-256 hashing**

Create `src/hologram/state.py` with the immutable result record and frozen signatures below. The signatures are not implementations; implement them using the exact framing algorithm that follows.

```text
STATE_FORMAT_VERSION = "hologram-state-v2"


@dataclass(frozen=True, slots=True)
class StateResult:
    value: str
    diagnostics: tuple[Diagnostic, ...]
    complete: bool


def compute_state(
    root: Path,
    config: ProjectConfig,
    scan_result: ScanResult,
    *,
    extractor_versions: Mapping[str, str],
    parser_versions: Mapping[str, str],
) -> StateResult


read_digest_state(path: Path) -> str | None
```

Use this unambiguous framing helper:

```python
def _feed(hasher: "hashlib._Hash", label: str, value: bytes) -> None:
    label_bytes = label.encode("utf-8")
    hasher.update(len(label_bytes).to_bytes(4, "big"))
    hasher.update(label_bytes)
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)
```

Hash, in this exact order:

1. `format` → `b"hologram-state-v2"`.
2. `ir-schema` → ASCII `IR_SCHEMA_VERSION`.
3. `config` → `canonical_config_bytes(config)`.
4. Determine active languages from ledger entries whose status is `INDEXED` or `FAILED` and whose `language is not None`; `extractors` → compact, key-sorted JSON containing only active keys from `extractor_versions`.
5. `parsers` → compact, key-sorted JSON containing only active keys from `parser_versions`.
6. For every `INDEXED` or `FAILED` ledger entry with `language is not None`, sorted by `file`, `entry-status:<file>` → `f"{entry.status.value}\0{entry.reason or ''}".encode("utf-8")`.
7. Include a scanner-fatal sentinel such as `file="<git>"` even though its language is `None`.
8. For every included entry with a `SourceFile`, `source-path` → relative POSIX UTF-8 and `source-bytes` → immutable `source.raw`.

Never hash `EXCLUDED` entries or entries whose `language is None` except the scanner-fatal sentinel: README/documentation, unsupported Scala/Bash, config-disabled languages, include misses, and excluded paths must not stale the code map. Moving a formerly indexed file behind an exclusion still changes state because its previously framed source disappears. Ordinary chart-layout YAML remains `INDEXED` and hashed even when its extractor emits zero symbols. Never reread `ScanEntry.path` or `SourceFile.path` in `state.py`; `read_digest_state()` may read only its explicitly supplied rendered-artifact path. Preserve scan diagnostics, add no duplicate diagnostics, and set `complete=scan_result.complete`.

Define and use one header parser:

```python
STATE_HEADER_RE = re.compile(r"(?:^|[ ·])state=([0-9a-f]{64})(?=$|[ ·])")
```

Export `STATE_FORMAT_VERSION`, `StateResult`, `compute_state`, and
`read_digest_state` from `src/hologram/__init__.py` in this task.

- [ ] **Step 4: Replace legacy freshness helpers with strict adapters**

Change `_digest_state()` to delegate to `read_digest_state()`. Change `_state_hash()` to require the manifest-backed config supplied by `run_cli()`, scan once, and call `compute_state()` with the temporary legacy version maps:

```python
LEGACY_EXTRACTOR_VERSIONS = {language.value: "legacy-1" for language in Language}


def _state_hash(
    root: Path,
    config: ProjectConfig,
    langs: set[str] | None = None,
) -> str:
    config = dataclasses.replace(
        config,
        languages=(
            tuple(Language(value) for value in sorted(langs))
            if langs is not None else config.languages
        ),
    )
    scan_result = scan_project(root.resolve(), config)
    state = compute_state(
        root.resolve(),
        config,
        scan_result,
        extractor_versions=LEGACY_EXTRACTOR_VERSIONS,
        parser_versions={language.value: "legacy" for language in Language},
    )
    return state.value
```

Update `legacy._gather()` in the same step: accept the caller's `ProjectConfig`, derive the same effective config for `langs` as `_state_hash()`, create one `ScanResult`, iterate `scan_result.sources`, decode and extract from `source.raw`, compute file tokens from `source.text`, and call `compute_state()` on that exact scan and effective config for the header value. It must not call `scan_files()`, `_state_hash()`, or otherwise scan/read each path a second time. Freeze direct compatibility as:

```text
build_digest(
    root: Path,
    regen_cmd: str = "hologram build",
    langs: set[str] | None = None,
    private_sigs: bool = False,
    behaviors: bool = False,
    config: ProjectConfig | None = None,
) -> str
```

`config=None` selects `default_config()` only for direct library calls, while `run_cli()` always supplies its loaded manifest. Render `state=<value>` and make `_state_hash()` use the same version maps, so build and check cannot mix legacy MD5 with v2 SHA-256 or silently ignore the manifest.

Update `tests/test_freshness_and_markers.py` fixture headers and assertions from the old 12-character state stamp to the constant `state=<64hex>` form. Replace every direct `_state_hash(root)` assertion with `_state_hash(root, default_config())`; CLI cases continue to use the manifest written by `write_manifest()` from Task 3.

- [ ] **Step 5: Verify state and complete suite GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_state.py tests/test_freshness_and_markers.py -v
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

Expected: state tests prove no disk reread, each semantic input changes the digest, ordering does not, freshness tests pass with 64-character stamps, and the complete suite passes.

- [ ] **Step 6: Commit versioned state hashing**

```bash
git add src/hologram/state.py src/hologram/legacy.py src/hologram/__init__.py tests/test_state.py tests/test_freshness_and_markers.py
git commit -m "feat: hash versioned source snapshots"
```

## Foundation phase gate

Before starting the extractor plan, run:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m hologram --help
.venv/bin/python -m unittest tests.test_cli -v
git diff --check
git status --short
```

Expected: package imports resolve under `src/hologram`, no root `hologram.py`
or `.egg-info/` path is reported by Git, module help and the temporary-directory
CLI tests pass, and only intentional phase files are changed. Do not run `init`
against a tracked fixture or install a repository hook during this phase gate.
