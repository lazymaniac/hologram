# Hologram v2 Language Extractors and Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every advertised language extractor out of `legacy.py`, emit complete immutable raw facts from the scanner’s byte snapshot, resolve imports/references/calls conservatively, and expose one exact project-build orchestration API.

**Architecture:** Each language module converts one immutable `SourceFile.raw` snapshot into `FileIR`; it never opens a path and never filters, truncates, or resolves facts. A lazy parser registry owns optional Tree-sitter loading and version reporting. `resolve_project()` builds deterministic project indexes and records resolved, ambiguous, external, and unresolved outcomes without discarding candidates; `pipeline.build_project()` composes config, scan, extraction, state, and resolution into one `BuildSnapshot`.

**Tech Stack:** Python 3.11+ stdlib `ast`, Tree-sitter 0.26 with pinned grammar wheels, immutable dataclasses from `hologram.model`, `importlib.metadata`, and `unittest` fixture/characterization tests.

---

## Fixed file responsibilities

- `src/hologram/parsers/api.py` — parser/extractor protocols, registry dispatch, file/project extraction.
- `src/hologram/parsers/common.py` — text/type normalization, stable IDs/spans, ordered deduplication, and raw-fact builders.
- `src/hologram/parsers/treesitter.py` — lazy grammar loading and Tree-sitter-only node helpers.
- `src/hologram/parsers/<language>.py` — one language family to `FileIR`, including module/import/call/reference/body facts.
- `src/hologram/resolve.py` — project module indexes and conservative resolution records.
- `src/hologram/pipeline.py` — the only public scan/extract/state/resolve orchestration function.
- `src/hologram/legacy.py` — rendering/CLI compatibility only; extractor implementations are deleted as their replacements land.

## Cross-language extraction rules

Every extractor must obey these rules:

1. Read only `source.raw`/`source.text`; never call `Path.read_*`.
2. Emit `SourceSpan` for every declaration, call, import, reference, and body.
3. Build line-independent `SymbolId(language, file, container_path, kind, name, signature_key)` values. A callable signature key is `(<normalized parameter types>)`; non-overloadable declarations use `""`.
4. Emit every named module/type/member/callable declaration in production, test, and generated indexed files, including named nested functions/classes and supported fields/properties/constants with their full stable container path. Parameters and local variables are bindings plus `PARAM`/`LOCAL` body events, not symbols. Anonymous expressions do not receive invented names.
5. Keep calls/references in source order, deduplicate only identical `(owner, span, raw fact)` values, and never cap their count.
6. Every `ReferenceRef` records both `ReferenceContext` (`CODE`, `TYPE`, `ANNOTATION`, `STRING`, `CONFIG`, or `REFLECTION`) and `ReferenceConfidence` (`DEFINITE` or `POSSIBLE`). Annotation/string/config/reflection reachability is retained but can never become strong dead-code evidence.
7. Preserve annotations/decorators and modifiers on `Symbol`. Emit override/entrypoint annotations and recognized framework registration, configuration, and reflection references. An arbitrary string or comment is not a reference; an exact callback name in a recognized registration/configuration/reflection construct is a `POSSIBLE` reference.
8. Emit direct syntax facts only. Platform filtering, ubiquitous-call filtering, transitive reduction, marker thresholds, and duplicate analysis belong to later phases.
9. Preserve source bytes in `FileIR.source` and emit `BodyIR(owner, body_span, events)` for every callable with a body. `events` is the complete source-ordered tuple of `BodyEvent` values (`PARAM`, `LOCAL`, `NAME`, `TYPE`, `CALL`, `CONSTRUCT`, `MEMBER`, `LITERAL`, `OPERATOR`, `KEYWORD`, `CONTROL_ENTER`, `CONTROL_EXIT`). Control events are balanced and nest in syntax order with text `if`, `loop`, `try`, `catch`, `finally`, `match`, or the closest language-independent category; phase 3 reconstructs indexed control paths without reparsing or reopening the file.
10. A missing grammar, parser exception, or syntax-error tree produces an error `Diagnostic`; partial facts may be retained, but `ProjectIR.complete` is false.
11. Every `CallRef` whose span lies inside its caller's `BodyIR.span` has a `BodyEvent` with the identical `SourceSpan` and kind `CALL` or `CONSTRUCT` matching `CallRef.kind`. Every body-produced `ReferenceRef` has an identical-span `NAME` or `TYPE` event matching `ReferenceRef.kind`. Analysis joins resolved targets to body syntax by `(BodyEventKind, SourceSpan)` and must never reparse to recover this correspondence.

### Task 1: Add lazy parser infrastructure and the extraction contract

**Files:**
- Create: `src/hologram/parsers/__init__.py`
- Create: `src/hologram/parsers/api.py`
- Create: `src/hologram/parsers/common.py`
- Create: `src/hologram/parsers/treesitter.py`
- Create: `tests/test_parser_runtime.py`
- Create: `tests/parser_assertions.py`
- Create: `tests/extractor_characterization.py`
- Create: `tests/fixtures/expected/extractors-v1.json`
- Modify: `src/hologram/__init__.py`

- [ ] **Step 1: Write runtime contract tests**

Create `tests/test_parser_runtime.py`:

```python
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from hologram.model import DiagnosticSeverity, FileIR, Language, SourceFile, SourceRole
from hologram.parsers.api import ParserRegistry, extract_file, extract_project


def source(language: Language = Language.JAVA) -> SourceFile:
    raw = b"class Broken {"
    return SourceFile(
        Path("/repo/Broken.java"),
        "Broken.java",
        language,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


class ParserRuntimeTest(unittest.TestCase):
    def test_missing_grammar_is_a_diagnostic_not_process_exit(self):
        registry = ParserRegistry(module_loader=lambda name: None)
        result = extract_file(source(), registry=registry)
        self.assertEqual(result.symbols, ())
        self.assertEqual(result.diagnostics[0].code, "missing-parser")
        self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)

    def test_extractor_never_reads_source_path(self):
        raw = b"def answer():\n    return 42\n"
        snapshot = SourceFile(
            Path("/repo/answer.py"),
            "answer.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        with patch.object(Path, "read_bytes", side_effect=AssertionError("disk reread")):
            result = extract_file(snapshot, registry=ParserRegistry())
        self.assertIs(result.source, snapshot)

    def test_project_is_incomplete_when_any_file_has_error(self):
        project = extract_project(
            Path("/repo"),
            (source(),),
            registry=ParserRegistry(module_loader=lambda name: None),
        )
        self.assertFalse(project.complete)
        self.assertEqual(len(project.files), 1)


if __name__ == "__main__":
    unittest.main()
```

Create `tests/parser_assertions.py` with the invariant used by every language fixture test added in Tasks 2–6:

```python
import unittest

from hologram.model import (
    BodyEventKind,
    CallKind,
    FileIR,
    ReferenceKind,
    SourceSpan,
)


def _position(span: SourceSpan, *, end: bool) -> tuple[int, int]:
    return (
        (span.end_line, span.end_column)
        if end else (span.start_line, span.start_column)
    )


def _inside(inner: SourceSpan, outer: SourceSpan) -> bool:
    return (
        inner.file == outer.file
        and _position(outer, end=False) <= _position(inner, end=False)
        and _position(inner, end=True) <= _position(outer, end=True)
    )


def assert_body_fact_events(test: unittest.TestCase, file_ir: FileIR) -> None:
    bodies = {body.owner: body for body in file_ir.bodies}
    events = {
        owner: {(event.kind, event.span) for event in body.events}
        for owner, body in bodies.items()
    }
    for call in file_ir.calls:
        body = bodies.get(call.caller)
        if body is None or not _inside(call.span, body.span):
            continue
        kind = (
            BodyEventKind.CONSTRUCT
            if call.kind is CallKind.CONSTRUCT else BodyEventKind.CALL
        )
        test.assertIn((kind, call.span), events[call.caller])
    for reference in file_ir.references:
        body = bodies.get(reference.owner)
        if body is None or not _inside(reference.span, body.span):
            continue
        kind = (
            BodyEventKind.TYPE
            if reference.kind is ReferenceKind.TYPE else BodyEventKind.NAME
        )
        test.assertIn((kind, reference.span), events[reference.owner])
```

Each parser test calls `assert_body_fact_events(self, result)` for every complete or partial `FileIR` it extracts. Decorator/config facts outside the body span remain raw facts but are intentionally excluded from this body-event join.

- [ ] **Step 2: Run the runtime tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_runtime.py -v
```

Expected: ERROR because `hologram.parsers.api` does not exist.

- [ ] **Step 3: Implement parser/extractor protocols and deterministic dispatch**

Create `src/hologram/parsers/api.py` with the exact imports, protocol, constants, and frozen concrete signatures below. Protocol ellipses are valid protocol bodies; the concrete entries are explicitly signatures whose complete behavior is specified immediately after the block.

```python
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from hologram.model import FileIR, Language, ProjectIR, SourceFile


class ParserProvider(Protocol):
    def has_parser(self, language: Language) -> bool: ...
    def parser_for(self, language: Language) -> object | None: ...
    def versions(self) -> Mapping[str, str]: ...


Extractor = Callable[[SourceFile, object | None], FileIR]
EXTRACTOR_VERSIONS: Mapping[Language, str] = MappingProxyType(
    {language: "2" for language in Language}
)
```

Frozen concrete signatures:

```text
ParserRegistry.__init__(
    *, module_loader: Callable[[str], object | None] | None = None
) -> None
ParserRegistry.has_parser(language: Language) -> bool
ParserRegistry.parser_for(language: Language) -> object | None
ParserRegistry.versions() -> Mapping[str, str]

DEFAULT_REGISTRY: ParserRegistry
extract_file(
    source: SourceFile,
    *,
    registry: ParserProvider = DEFAULT_REGISTRY,
) -> FileIR
extract_project(
    root: Path,
    sources: Iterable[SourceFile],
    *,
    registry: ParserProvider = DEFAULT_REGISTRY,
) -> ProjectIR
```

Implement `ParserRegistry` with per-language parser and version caches. The default loader is `importlib.import_module`; a supplied loader is used instead in tests. `has_parser()` returns `True` for Python and Helm, otherwise delegates to `parser_for()`. `parser_for()` returns `None` for Python/Helm, lazily imports only the requested grammar module, constructs one Tree-sitter parser, caches success or absence, and never installs anything. `versions()` returns a new key-sorted immutable mapping on each call without changing either cache.

Keep a private immutable mapping from `Language` to extractor module name. `_extractors(language)` imports only that requested module and returns its `extract` callable, so importing `hologram` never imports every extractor or grammar. During Tasks 1–5, a not-yet-ported module returns a source-retaining `missing-extractor` error diagnostic; Task 6's dispatch test proves that temporary outcome is gone for every advertised language. Instantiate `DEFAULT_REGISTRY` only after `ParserRegistry` is defined. Python and Helm receive `parser=None`; Tree-sitter languages request one parser from the registry. When a grammar is absent, return a `FileIR` retaining the source and one `missing-parser` error diagnostic. Catch exceptions raised while executing an available extractor at this boundary, return an `extractor-crash` diagnostic with exception type/message, and do not catch `KeyboardInterrupt` or `SystemExit`.

`extract_project()` sorts sources by `file`, extracts each once, concatenates diagnostics in file order, and sets `complete=False` when any diagnostic severity is `ERROR`.

In `src/hologram/parsers/__init__.py`, re-export `ParserProvider`,
`ParserRegistry`, `DEFAULT_REGISTRY`, `EXTRACTOR_VERSIONS`, `extract_file`, and
`extract_project` from `api`, plus canonical `Symbol` from `hologram.model`.
Do not replace the package-root legacy `extract_file` or `Symbol` names during
this task; the explicit `hologram.parsers` surface is the canonical extractor
API until the delivery plan finishes the compatibility cutover.

- [ ] **Step 4: Implement lazy Tree-sitter loading and shared fact helpers**

Copy `_load_parser`, grammar module/attribute metadata, `_ast_text`, `_ast_field`, `_ast_collect`, `_body_lines`, top-level comma splitting, type tightening, base-type normalization, and heritage parsing into `parsers/treesitter.py` and `parsers/common.py`. Do not delete or rewrite the legacy copies in Task 1: the characterization step below must execute against the byte-for-byte intact v1 extractor. Later language tasks delete each legacy helper with its final consumer.

The central call helper must return all direct raw calls:

```python
from collections.abc import Iterable
from typing import TypeVar

from hologram.model import SourceFile, SymbolId, SymbolKind


T = TypeVar("T")


def ordered_unique(values: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(values))


def signature_key(params: Iterable[str]) -> str:
    return f"({','.join(params)})"


def symbol_id(
    source: SourceFile,
    container_path: tuple[str, ...],
    kind: SymbolKind,
    name: str,
    params: Iterable[str] = (),
) -> SymbolId:
    key = signature_key(params) if kind in {
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.CONSTRUCTOR,
    } else ""
    return SymbolId(source.language, source.file, container_path, kind, name, key)
```

Tree-sitter spans use zero-based node columns converted to `SourceSpan`’s zero-based columns and one-based lines. Parser versions come from `importlib.metadata.version()` for both `tree-sitter` and the selected grammar distribution. Return values are sorted mappings and never mutate global metadata.

`ParserRegistry.versions()` reports one deterministic entry per advertised language: Python uses `stdlib-ast-<major>.<minor>`, Helm uses `builtin`, and each Tree-sitter language uses `<tree-sitter version>/<grammar distribution version>` or the literal `missing` when its wheel is absent. This lets state hashing cover the parser that actually interpreted every active source while the pipeline still filters out inactive languages.

Add `body_events()` helpers for stdlib AST and Tree-sitter nodes. They walk only the owning callable body and emit `PARAM` and `LOCAL` declarations plus normalized literals (`<string>`, `<number>`, `<bool>`, `<null>`), identifiers, types, members, calls/constructions, operators, and keywords. A call node emits a `CALL`/`CONSTRUCT` event in addition to its child name/member events. Structured control nodes emit `CONTROL_ENTER` before their contained events and a matching `CONTROL_EXIT` afterward; branch arms such as `catch` get their own balanced pair. Preserve this traversal order—do not globally sort after adding control events—and assert a stack walk never underflows and ends empty. Every language extractor calls this helper or an equivalent language-specific AST walker; no later phase reparses source.

All `SourceSpan` values use one-based lines and zero-based UTF-8 byte columns with an end-exclusive endpoint. Python AST and Tree-sitter already expose UTF-8 byte offsets; do not convert them to Unicode code-point columns. Add `x = "ż"; target()`/equivalent fixtures and assert `target` starts two bytes later than a character-count calculation would report.

Add a `reference()` builder that requires callers to choose context and confidence explicitly. It must not default annotation/string/config/reflection facts to `DEFINITE`.

- [ ] **Step 5: Capture the v1 extractor oracle before deleting any extractor**

Create `tests/extractor_characterization.py`. It must scan each of `javamini`, `pymini`, `tsmini`, and `polyglot` with the still-intact legacy API and write `tests/fixtures/expected/extractors-v1.json`. Normalize every symbol to:

```python
def normalize(symbol) -> dict[str, object]:
    return {
        "file": symbol.file,
        "line": symbol.line,
        "name": symbol.name,
        "kind": symbol.kind,
        "signature": symbol.signature,
        "params": list(symbol.params),
        "returns": symbol.returns,
        "visibility": symbol.visibility,
        "container": symbol.container,
        "lang": symbol.lang,
        "calls": list(symbol.calls),
        "supers": list(symbol.supers),
        "permits": list(symbol.permits),
        "raises": list(symbol.raises),
        "bindings": sorted(symbol.bindings.items()),
        "body_lines": symbol.size,
    }
```

Sort records by `(file, line, kind, container or "", name, signature)`. Build `payload = {fixture.name: records for fixture, records in sorted(results.items())}` and write exactly `json.dumps(payload, indent=2, sort_keys=True) + "\n"` as UTF-8. Assert the existing fixture set represents exactly Java, Python, TypeScript, TSX, Vue, C#, Kotlin, C, C++, Go, Lua, Rust, HTML, and Helm before writing, so a missing optional grammar cannot silently shrink the oracle. JavaScript and Svelte provenance are added as new tests in Task 4 rather than fabricated in the v1 oracle.

Run:

```bash
.venv/bin/python tests/extractor_characterization.py
git add -N tests/fixtures/expected/extractors-v1.json
git diff -- tests/fixtures/expected/extractors-v1.json
```

Expected: the new reviewed JSON records current calls, bindings, and body sizes as well as declarations/signatures. Do not regenerate it after Task 2 starts.

- [ ] **Step 6: Verify runtime GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_runtime.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: runtime tests pass, missing parsers are structured diagnostics, source paths are never reopened, and legacy tests remain green.

The complete advertised language profile requires installing `.[parsers]`.
Python's stdlib-AST extractor and the built-in Helm extractor can each run
individually without that extra, but a distribution must not claim the complete
non-Python profile unless the extra is installed. Parser discovery never
installs packages or creates a virtual environment at runtime.

- [ ] **Step 7: Commit parser infrastructure and the v1 oracle**

```bash
git add src/hologram/parsers src/hologram/__init__.py tests/test_parser_runtime.py tests/parser_assertions.py tests/extractor_characterization.py tests/fixtures/expected/extractors-v1.json
git commit -m "feat: add immutable extraction runtime"
```

### Task 2: Move Python and Helm extractors

**Files:**
- Create: `src/hologram/parsers/python.py`
- Create: `src/hologram/parsers/helm.py`
- Create: `tests/test_parser_python_helm.py`
- Modify: `src/hologram/parsers/api.py`
- Modify: `src/hologram/legacy.py`

- [ ] **Step 1: Write Python/Helm raw-fact tests**

Create `tests/test_parser_python_helm.py`. Reuse `tests/fixtures/pymini` and the Helm chart, and add this inline Python source:

```python
PYTHON_IMPORTS = b"""\
import shop.pricing as pricing
from shop.ids import OrderId as Oid

@register("on_ready")
def on_ready() -> None:
    pass

def quote(order: Oid) -> int:
    client = pricing.Client()
    note = "on_ready"
    return client.fetch(order)
"""
```

Assert exact facts:

```python
self.assertEqual(
    [(i.module, i.name, i.alias, i.wildcard) for i in result.imports],
    [("shop.pricing", None, "pricing", False), ("shop.ids", "OrderId", "Oid", False)],
)
self.assertEqual(
    [(c.receiver, c.name, c.kind, c.arity) for c in result.calls],
    [
        (None, "register", CallKind.CALL, 1),
        ("pricing", "Client", CallKind.CONSTRUCT, 0),
        ("client", "fetch", CallKind.CALL, 1),
    ],
)
quote = next(symbol for symbol in result.symbols if symbol.name == "quote")
self.assertEqual(quote.id.signature_key, "(Oid)")
self.assertEqual(len(result.bodies), 2)
self.assertIs(result.source, snapshot)
```

Assert the decorator callback produces one `ReferenceRef("on_ready", context=ANNOTATION, confidence=POSSIBLE)`, while the ordinary `note = "on_ready"` string produces no reference. Assert `on_ready` retains decorator text in `Symbol.annotations`.

Also assert Python syntax errors yield `python-syntax-error`; a named closure and class nested inside a function are symbols with the outer function in `container_path`; parameters/locals are not symbols; calls are source ordered; more than 12 calls survive; raises, annotations/decorators, modifiers, bindings, components, and enum members remain ordered. Retain current Helm assertions for chart name, top-level values, template definitions, and ordinary chart-layout YAML producing an indexed, possibly empty, complete `FileIR`.

Add one body with a parameter, local, `for`, nested `if`, operator, Unicode string before a call, and return. Assert it contains `PARAM`, `LOCAL`, identifier/member/operator/literal/keyword events; `CONTROL_ENTER(loop)`, `CONTROL_ENTER(if)`, `CONTROL_EXIT(if)`, `CONTROL_EXIT(loop)` are balanced in that order; and the call span column is the zero-based UTF-8 byte offset, not the Unicode character index. For every emitted Python/Helm symbol assert `symbol.id.file == symbol.span.file == source.file` and exact declaration start lines; inserting a leading blank line changes spans but not IDs.

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_python_helm.py -v
```

Expected: FAIL because the registry has no Python/Helm modules and no imports, references, or bodies are emitted.

- [ ] **Step 3: Port Python extraction without filesystem access**

In `parsers/python.py`, parse `source.text` with `ast.parse`. Use one declaration visitor that descends into named nested functions/classes and gives them full container paths; use separate owner-scoped fact/body visitors that stop when they reach a nested declaration so facts are not attributed twice. Emit:

- module identity from extensionless relative path, with `pkg/__init__.py` becoming `pkg`;
- `ImportRef` for every `ast.Import` alias and `ast.ImportFrom` imported name;
- class/enum, method, async method, function, and async function symbols;
- ordered `CallRef` values for `Name` and one-level `Attribute` callees, with constructors identified by a capitalized final name;
- load-context `ReferenceRef` values, excluding declaration/store positions, plus decorator/annotation refs and recognized registration/config/reflection callback refs with explicit context/confidence;
- annotated parameter/local and capitalized-constructor bindings;
- raised exception types;
- `BodyIR` spanning the first through last body statement with complete balanced ordered events.

Use `ast.unparse()` only on the in-memory AST. Convert `lineno`/`col_offset` and `end_lineno`/`end_col_offset` directly to `SourceSpan`.

- [ ] **Step 4: Port Helm extraction and delete the legacy cluster**

In `parsers/helm.py`, operate only on `source.text`, preserve the existing chart-layout gate, and emit stable generic `CLASS`/`FUNCTION` symbols for chart names, top-level values, and template definitions. A named template `FUNCTION` has a body: emit `BodyIR` from its parsed template actions, including ordered name/call/literal events and balanced `if`/`loop` control events. Chart/value name-only declarations have no body. Add Helm `include`/`template` invocations as raw calls owned by their enclosing definition when present.

Register both extractors in `parsers/api.py`. Route Python and Helm branches of `legacy.extract_file()` through one canonical-to-v1 conversion, remove their entries from the legacy `EXTRACTORS` mapping, and delete `_extract_python`, `_extract_helm`, and their now-unused helpers in the same commit. Do not introduce new `_extract_*` adapters; Task 6's no-legacy-extractor assertion must already hold for this cluster.

- [ ] **Step 5: Verify Python/Helm GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_python_helm.py tests/test_extract_langs.py tests/test_more_langs.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all current Python/Helm behavior plus raw imports, references, bodies, diagnostics, and uncapped calls pass.

- [ ] **Step 6: Commit Python and Helm**

```bash
git add src/hologram/parsers/python.py src/hologram/parsers/helm.py src/hologram/parsers/api.py src/hologram/legacy.py tests/test_parser_python_helm.py
git commit -m "refactor: move Python and Helm extractors"
```

### Task 3: Move and harden the Java extractor

**Files:**
- Create: `src/hologram/parsers/java.py`
- Create: `tests/test_parser_java.py`
- Modify: `src/hologram/parsers/api.py`
- Modify: `src/hologram/legacy.py`
- Modify: `tests/test_treesitter.py`

- [ ] **Step 1: Add Java package/import/resolution-input tests**

Create `tests/test_parser_java.py` using the current Java fixture plus this source:

```java
package shop.app;
import shop.engine.PricingEngine;
import shop.ids.OrderId as InvalidJavaAlias;
import static shop.ids.OrderId.of;
import java.util.*;

final class App {
  @Bean
  Handler onRefresh() { return new Handler(); }
  @Override
  Quote run(PricingEngine engine, String raw) {
    var id = OrderId.of(raw);
    return engine.evaluate(id);
  }
}
```

Do not parse the deliberately invalid alias line as a valid import: assert the file has a `tree-sitter-syntax-error` diagnostic and makes the project incomplete while retaining facts from valid nodes. Add a separate valid version and assert:

```python
self.assertEqual(result.module, "shop.app")
self.assertIn(("shop.engine", "PricingEngine", None, False), import_tuples)
self.assertIn(("shop.ids.OrderId", "of", None, False), import_tuples)
self.assertIn(("java.util", None, None, True), import_tuples)
self.assertEqual(
    [(c.receiver, c.name) for c in result.calls],
    [(None, "Handler"), ("OrderId", "of"), ("engine", "evaluate")],
)
self.assertIn(Binding("engine", "PricingEngine"), run.bindings)
self.assertIn(Binding("id", "OrderId"), run.bindings)
```

Retain exact assertions for classes, interfaces, records, enums, constructors, bodyless interface methods, sealed permits, supers, throws, visibility, field/record/parameter/local bindings, and constructor calls.

Add a valid framework source containing `@Bean`, `@Override`, `@EventListener("onRefresh")`, and `public static void main(String[] args)`. Assert annotations/modifiers are stored on symbols, `main` is recognizable from modifiers/name, the event-listener string becomes an `ANNOTATION/POSSIBLE` reference to `onRefresh`, and an unrelated string literal `"onRefresh"` emits no reference.

Add nested classes plus named fields/constants. Assert each is a symbol with exact file/span and full container path, while parameters/locals appear only as bindings/body events. Assert the Java body-event stream includes all event categories present in the fixture and balanced `if`/`loop`/`try`/`catch` enter/exit pairs. Insert a leading blank line and assert IDs stay equal while spans shift by one.

- [ ] **Step 2: Run Java tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_java.py -v
```

Expected: FAIL because `parsers/java.py` and Java import/reference facts do not exist.

- [ ] **Step 3: Port Java declarations and raw facts**

Move the Java node-kind maps and helpers into `parsers/java.py`. Parse the immutable bytes, obtain package/import declarations from syntax nodes, and split a normal class import into `module=<package>` and `name=<type>`. Static imports use `module=<declaring type>` and `name=<member>`; on-demand imports set `wildcard=True`.

Emit nested type container paths, overload signature keys, complete `SourceSpan` values, all calls with argument arity, identifier/type references, bindings, raises, and `BodyIR`. Do not mutate symbols when discovering relations; accumulate supers/permits/components first and construct the frozen `Symbol` once.

- [ ] **Step 4: Register Java and remove its legacy implementation**

Register `Language.JAVA`, migrate `tests/test_treesitter.py` from private `hologram._extract_java(text, rel)` to `hologram.parsers.java.extract(source, parser)`, and delete the Java extractor/helper block from `legacy.py`. Keep only the temporary `legacy.extract_file()` compatibility conversion.

- [ ] **Step 5: Verify Java GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_java.py tests/test_treesitter.py tests/test_simple_mode.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: valid Java emits complete immutable facts, invalid syntax is diagnosed without crashing, all existing Java digest assertions pass, and no Java extractor remains in `legacy.py`.

- [ ] **Step 6: Commit Java extraction**

```bash
git add src/hologram/parsers/java.py src/hologram/parsers/api.py src/hologram/legacy.py tests/test_parser_java.py tests/test_treesitter.py
git commit -m "refactor: move Java extractor"
```

### Task 4: Move TypeScript, JavaScript, TSX, JSX, Vue, and Svelte

**Files:**
- Create: `src/hologram/parsers/typescript.py`
- Create: `tests/test_parser_typescript.py`
- Modify: `src/hologram/parsers/api.py`
- Modify: `src/hologram/legacy.py`
- Modify: `tests/test_extract_langs.py`
- Modify: `tests/test_more_langs.py`

- [ ] **Step 1: Write TypeScript-family coverage before porting**

Create `tests/test_parser_typescript.py`. Keep existing fixtures and add temporary `.js`, `.mjs`, `.jsx`, and `.svelte` files. Assert:

- `.js`/`.mjs` symbols have `Language.JAVASCRIPT`, never `Language.TYPESCRIPT`;
- `.tsx`/`.jsx` dispatch through the TSX grammar and preserve `Language.TSX`;
- Vue and Svelte emit the component plus each `<script>` declaration at original file line/column;
- named/default/namespace imports and aliases become exact `ImportRef` values;
- `export { A, B as C } from "./api"` produces reexport imports and current reexport symbols;
- interfaces include method signatures;
- functions, async functions, named nested functions/classes, top-level arrows, class-field arrows, object-literal APIs, constructors, type aliases, enum values, fields/properties/constants, visibility, bindings, calls, references, bodies, annotations, and modifiers match current behavior.

Use this exact alias case:

```typescript
import Client, { Quote as PriceQuote } from "./api";
import * as ids from "./ids";
export { OrderId as PublicOrderId } from "./ids";
export const load = (id: ids.OrderId): PriceQuote => Client.fetch(id);
export const onReady = (): void => {};
register({ handler: "onReady" });
const note = "onReady";
```

Assert `Client.fetch` retains receiver `Client`, `ids.OrderId` is a type reference, all four import/reexport bindings remain raw, recognized registration key `handler` emits one `CONFIG/POSSIBLE` reference to `onReady`, and the ordinary `note` string emits none.

For TS/JS/TSX/Vue/Svelte, assert exact declaration spans/container paths, ID stability after leading-line insertion, complete body event categories, balanced control enter/exit events, and UTF-8 byte columns after a non-ASCII character.

- [ ] **Step 2: Run TypeScript-family tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_typescript.py -v
```

Expected: FAIL due to the missing module, incorrect JavaScript language tag, absent import/reference facts, and absent interface methods.

- [ ] **Step 3: Port the TypeScript-family extractor**

Move all TS helpers into `parsers/typescript.py`. Select `language_typescript` for TS/JS/Vue/Svelte script blocks and `language_tsx` for TSX/JSX. Pass the original `SourceFile.language` into every ID. Extract imports/exports from syntax fields rather than regular expressions. Preserve script byte/line offsets when translating embedded SFC node spans; emit one component symbol even when no script exists.

Build frozen symbols once. Keep all calls, including optional calls and constructors, with arity. Emit named nested functions/classes as symbols with full container paths; parameters and lexical locals remain bindings/body events.

- [ ] **Step 4: Register aliases and delete legacy TS/SFC code**

Register `TYPESCRIPT`, `JAVASCRIPT`, `TSX`, `VUE`, and `SVELTE`. Delete `_extract_ts`, `_extract_tsx`, `_extract_sfc`, their maps/regexes, and their helpers from `legacy.py`; update current tests to call the package extractor API where they directly inspect extraction.

- [ ] **Step 5: Verify TypeScript-family GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_typescript.py tests/test_extract_langs.py tests/test_more_langs.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all six file families pass, aliases/reexports remain raw and ordered, SFC spans point into the original file, and legacy digest output remains compatible.

- [ ] **Step 6: Commit the TypeScript family**

```bash
git add src/hologram/parsers/typescript.py src/hologram/parsers/api.py src/hologram/legacy.py tests/test_parser_typescript.py tests/test_extract_langs.py tests/test_more_langs.py
git commit -m "refactor: move TypeScript family extractors"
```

### Task 5: Move Go, Rust, C#, and Kotlin extractors

**Files:**
- Create: `src/hologram/parsers/go.py`
- Create: `src/hologram/parsers/rust.py`
- Create: `src/hologram/parsers/csharp.py`
- Create: `src/hologram/parsers/kotlin.py`
- Create: `tests/test_parser_go_rust.py`
- Create: `tests/test_parser_csharp_kotlin.py`
- Modify: `src/hologram/parsers/api.py`
- Modify: `src/hologram/legacy.py`
- Modify: `tests/test_more_langs.py`

- [ ] **Step 1: Write Go/Rust tests and verify RED**

Create `tests/test_parser_go_rust.py` retaining current fixture assertions and adding:

```go
package app
import p "shop/pricing"
import . "shop/ids"
func Run(id OrderId) Quote { client := p.New(); return client.Get(id) }
```

```rust
use crate::pricing::Client as PricingClient;
use crate::ids::{OrderId, UserId as Uid};
fn run(id: OrderId) -> Quote { PricingClient::new().get(id) }
```

Assert package/module identity, import aliases, dot/grouped imports, receiver/self bindings, struct construction, trait/impl supers, calls with arity, references, and body spans. Assert the Rust trait relation is present without assigning to `symbol.supers` after construction.

Add Go/Rust nested named declarations where supported, fields, properties/associated constants, and module constants. Assert exact spans/container paths, stable IDs across line shifts, parameters/locals only as body events/bindings, all applicable body event categories, balanced control pairs, annotations/attributes/modifiers, and UTF-8 byte columns.

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_go_rust.py -v
```

Expected: FAIL because the new modules and import facts are absent.

- [ ] **Step 2: Port Go and Rust with immutable assembly**

Move the existing helpers into their modules. For Go, derive module identity from `package`, represent each import spec, bind receivers/parameters/fields/locals, and treat composite literals as construction calls. For Rust, extract `use_declaration` trees into one `ImportRef` per local binding, accumulate trait relations in temporary dictionaries, then construct final symbols exactly once. Preserve current structs, enums, traits, interface methods, impl methods, visibility, returns, and calls.

- [ ] **Step 3: Write C#/Kotlin tests and verify RED**

Create `tests/test_parser_csharp_kotlin.py` retaining current assertions and add alias forms:

```csharp
using Pricing = Shop.Engine.PricingEngine;
using Shop.Ids;
```

```kotlin
package shop.app
import shop.engine.PricingEngine as Engine
import shop.ids.*
```

Assert namespace/package module identity, alias and wildcard imports, expression-bodied method bodies, data/record components, enums/interfaces, constructors, supers, visibility, calls, bindings, references, and body spans.

Add C# and Kotlin nested types/callables, fields/properties/constants, annotations/attributes, overrides, and entrypoints. Assert exact provenance, stable IDs across line shifts, complete body event categories, balanced control pairs, and UTF-8 byte columns.

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_csharp_kotlin.py -v
```

Expected: FAIL because the new modules and import facts are absent.

- [ ] **Step 4: Port C# and Kotlin, register all four, and delete legacy blocks**

Port syntax-node extraction into the four modules and register their languages. C# aliases split the final type from its namespace; Kotlin aliases retain the fully qualified module plus local alias. Accumulate all collections before creating frozen values. Delete the corresponding helper/extractor sections from `legacy.py`.

- [ ] **Step 5: Verify both clusters GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_go_rust.py tests/test_parser_csharp_kotlin.py tests/test_more_langs.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all four language families emit complete raw IR and all current fixture assertions pass without mutation of frozen records.

- [ ] **Step 6: Commit the four extractors**

```bash
git add src/hologram/parsers/go.py src/hologram/parsers/rust.py src/hologram/parsers/csharp.py src/hologram/parsers/kotlin.py src/hologram/parsers/api.py src/hologram/legacy.py tests/test_parser_go_rust.py tests/test_parser_csharp_kotlin.py tests/test_more_langs.py
git commit -m "refactor: move Go Rust CSharp and Kotlin extractors"
```

### Task 6: Move C, C++, Lua, and HTML extractors

**Files:**
- Create: `src/hologram/parsers/c_family.py`
- Create: `src/hologram/parsers/lua.py`
- Create: `src/hologram/parsers/html.py`
- Create: `tests/test_parser_c_family.py`
- Create: `tests/test_parser_lua_html.py`
- Modify: `src/hologram/parsers/api.py`
- Modify: `src/hologram/legacy.py`
- Modify: `tests/test_more_langs.py`

- [ ] **Step 1: Write C-family tests and verify RED**

Create `tests/test_parser_c_family.py` retaining current C/C++ assertions and adding quoted/system includes, overloaded methods, header extensions, and an out-of-line definition whose source line differs from its declaration. Assert:

```python
self.assertEqual(compute.id.signature_key, "(int)")
self.assertEqual(compute.id, out_of_line_compute.id)
self.assertEqual(len([s for s in result.symbols if s.id == compute.id]), 1)
self.assertIn(("engine.h", None), [(i.module, i.name) for i in result.imports])
```

Add namespaces, nested named types/callables, global/member fields and constants, annotations/attributes where represented by the grammar, exact provenance, stable IDs across line shifts, complete body event categories, balanced control pairs, and UTF-8 byte columns. Parameters/locals remain bindings/events.

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_c_family.py -v
```

Expected: FAIL because the module/import facts do not exist and the legacy C++ implementation mutates `params` and `calls`.

- [ ] **Step 2: Port C/C++ with immutable declaration-definition merging**

Move shared declarator machinery to `parsers/c_family.py`. Represent `#include` as module imports, preserve C prototypes, static visibility, C++ access sections, constructors, bases, fields, namespaces, calls, references, and bodies. Key declarations and definitions by stable `SymbolId`; merge them in a temporary builder dictionary, prefer the definition span/body/calls, preserve declared visibility, then construct one frozen symbol.

- [ ] **Step 3: Write Lua/HTML tests and verify RED**

Create `tests/test_parser_lua_html.py` retaining current assertions and add:

```lua
local pricing = require("shop.pricing")
local quote = require "shop.quote"
function M.run(id) return pricing.get(id) end
```

Assert two module imports, the `pricing.get` call, local/private functions, module/method containers, calls, references, and body spans. For HTML assert stable source-order deduplication of IDs and custom elements across opening/closing tags. Add `.h`, `.hpp`, `.yml`, and `.tpl` dispatch assertions to close extension gaps.

Assert Lua named nested functions and module constants have full container paths and exact spans; parameters/locals remain bindings/events. Assert Lua bodies provide complete event categories and balanced controls. HTML and Helm chart/value name-only declarations still get exact spans and stable IDs without invented bodies; named Helm template functions retain the `BodyIR` required by Task 2.

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_lua_html.py -v
```

Expected: FAIL because the modules/import facts are absent.

- [ ] **Step 4: Port Lua/HTML, register all four, and delete remaining legacy extractors**

Port Lua and HTML syntax-node extraction, register `C`, `CPP`, `LUA`, and `HTML`, then remove the corresponding blocks from `legacy.py`. Delete any remaining compatibility extractor names and the legacy `EXTRACTORS` mapping; compatibility now flows only through `legacy.extract_file()` and the canonical-to-v1 converter. At this point `legacy.py` must contain no `_extract_<language>` function and no `EXTRACTORS` mapping.

- [ ] **Step 5: Verify all extractor modules GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_parser_c_family.py tests/test_parser_lua_html.py tests/test_more_langs.py -v
.venv/bin/python -m unittest discover -s tests -v
rg '^def _extract_|^EXTRACTORS =' src/hologram/legacy.py
```

Expected: all tests pass and `rg` exits 1 with no matches, proving language extraction has left the monolith.

- [ ] **Step 6: Commit the final extractor moves**

```bash
git add src/hologram/parsers/c_family.py src/hologram/parsers/lua.py src/hologram/parsers/html.py src/hologram/parsers/api.py src/hologram/legacy.py tests/test_parser_c_family.py tests/test_parser_lua_html.py tests/test_more_langs.py
git commit -m "refactor: move C family Lua and HTML extractors"
```

### Task 7: Resolve imports, references, and calls without guessing

**Files:**
- Create: `src/hologram/resolve.py`
- Create: `tests/test_resolve.py`
- Create: `tests/fixtures/resolution/python/app.py`
- Create: `tests/fixtures/resolution/python/a/client.py`
- Create: `tests/fixtures/resolution/python/b/client.py`
- Create: `tests/fixtures/resolution/java/shop/app/App.java`
- Create: `tests/fixtures/resolution/java/shop/engine/Client.java`
- Create: `tests/fixtures/resolution/typescript/app.ts`
- Create: `tests/fixtures/resolution/typescript/api.ts`
- Modify: `src/hologram/__init__.py`

- [ ] **Step 1: Write synthetic resolution tests first**

Create `tests/test_resolve.py` with small `ProjectIR` builders and assert these exact cases:

```python
def test_alias_and_typed_receiver_select_exact_target(self):
    result = resolve_project(self.alias_project())
    call = next(item for item in result.calls if item.fact.name == "fetch")
    self.assertEqual(call.status, ResolutionStatus.RESOLVED)
    self.assertEqual(call.target.container_path, ("a", "Client"))
    self.assertEqual(call.candidates, (call.target,))
    self.assertEqual(call.display_name, "Client.fetch")

def test_same_name_without_import_stays_ambiguous(self):
    result = resolve_project(self.two_clients_project())
    call = result.calls[0]
    self.assertEqual(call.status, ResolutionStatus.AMBIGUOUS)
    self.assertIsNone(call.target)
    self.assertEqual(len(call.candidates), 2)

def test_external_and_unresolved_facts_are_retained(self):
    result = resolve_project(self.external_project())
    self.assertEqual([c.status for c in result.calls], [
        ResolutionStatus.EXTERNAL,
        ResolutionStatus.UNRESOLVED,
    ])
    self.assertEqual(len(result.calls), len(self.external_project().files[0].calls))

def test_recognized_dynamic_callback_keeps_possible_resolution(self):
    result = resolve_project(self.annotation_callback_project())
    reference = result.references[0]
    self.assertEqual(reference.status, ResolutionStatus.RESOLVED)
    self.assertEqual(reference.fact.context, ReferenceContext.ANNOTATION)
    self.assertEqual(reference.fact.confidence, ReferenceConfidence.POSSIBLE)
    self.assertEqual(reference.target.name, "onRefresh")

def test_possible_config_callback_keeps_all_ambiguous_candidates(self):
    result = resolve_project(self.config_callback_project())
    reference = result.references[0]
    self.assertEqual(reference.status, ResolutionStatus.AMBIGUOUS)
    self.assertEqual(reference.fact.context, ReferenceContext.CONFIG)
    self.assertEqual(reference.fact.confidence, ReferenceConfidence.POSSIBLE)
    self.assertEqual([candidate.name for candidate in reference.candidates], ["handler", "handler"])
```

Add tests for relative imports, declared Java packages, TS reexports, same-file functions, same-container `this`/`self`, static type receivers, constructor calls, overload arity, transitive supers with an inheritance cycle, wildcard imports, same-name cross-language declarations, and deterministic ordering. Include extracted `@Bean`, override, `main`, annotation-string callback, recognized config-string callback, reflection registration, arbitrary string, and comment cases: annotations/modifiers remain symbol provenance; recognized dynamic refs resolve as `POSSIBLE`; ordinary strings/comments emit no `ReferenceRef`. For every project assert one `ResolvedCall` per `CallRef`, one `ResolvedImport` per `ImportRef`, and one `ResolvedReference` per `ReferenceRef`.

- [ ] **Step 2: Run resolver tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_resolve.py -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'hologram.resolve'`.

- [ ] **Step 3: Implement immutable resolution records**

Create `src/hologram/resolve.py` with:

```python
from dataclasses import dataclass
from enum import StrEnum

from hologram.model import (
    CallRef,
    Diagnostic,
    ImportRef,
    ProjectIR,
    ReferenceRef,
    SymbolId,
)


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    source_file: str
    fact: ImportRef
    status: ResolutionStatus
    target_files: tuple[str, ...]
    target_symbols: tuple[SymbolId, ...]


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    fact: CallRef
    status: ResolutionStatus
    target: SymbolId | None
    candidates: tuple[SymbolId, ...]
    display_name: str | None


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    fact: ReferenceRef
    status: ResolutionStatus
    target: SymbolId | None
    candidates: tuple[SymbolId, ...]


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    imports: tuple[ResolvedImport, ...]
    calls: tuple[ResolvedCall, ...]
    references: tuple[ResolvedReference, ...]
    diagnostics: tuple[Diagnostic, ...]
```

Freeze the orchestration signature; its implementation is the indexing and precedence algorithm in Steps 4–5:

```text
resolve_project(project: ProjectIR) -> ResolutionResult
```

Export `ResolutionStatus`, `ResolvedImport`, `ResolvedCall`,
`ResolvedReference`, `ResolutionResult`, and `resolve_project` from
`src/hologram/__init__.py` in this task.

- [ ] **Step 4: Implement deterministic module and symbol indexes**

Index symbols by full `SymbolId`, `(language family, name)`, `(file, name)`, `(container path, name)`, and callable arity. Index files by extensionless relative path and declared module. Compatible families are exactly:

```python
LANGUAGE_FAMILIES = {
    Language.TYPESCRIPT: "typescript",
    Language.JAVASCRIPT: "typescript",
    Language.TSX: "typescript",
    Language.VUE: "typescript",
    Language.SVELTE: "typescript",
    Language.C: "c-family",
    Language.CPP: "c-family",
}
```

Every other language is its own family. Normalize only syntax the extractor made explicit: Python dots, TS relative paths, Java/Kotlin packages, C# namespaces, Go import paths, Rust `crate/self/super`, C/C++ includes, and Lua module dots. Never use arbitrary suffix/name similarity to force a match.

- [ ] **Step 5: Apply the conservative resolution order**

Resolve imports first and retain one `ResolvedImport` per raw fact. Match only an
exact normalized declared module/path. For a module-only or wildcard import, one
project module file is `RESOLVED`, multiple exact module files are `AMBIGUOUS`,
and no project module is `EXTERNAL`; a resolved wildcard retains all public
symbols in `target_symbols` without making the import ambiguous. For a named
import, filter the exact module's declarations by imported name: one symbol is
`RESOLVED`, multiple overload/declaration candidates are `AMBIGUOUS`, and an
exact project module with no such declaration is `UNRESOLVED`. Reexports follow
the same rule and populate the alias index for downstream facts. Sort target
files and IDs and never discard candidates.

For calls/references, filter candidates in this order:

1. Current container for `this`, `self`, `cls`, or bare same-container methods.
2. Receiver `Binding.type_name`, following explicit project supers cycle-safely.
3. Exact import/reexport local alias.
4. Same-file declaration.
5. Same declared package/module.
6. All same-name declarations in the compatible language family.
7. Arity match for callables when `CallRef.arity` is not `None`.

Treat steps 1–6 as precedence scopes: choose the first nonempty scope, then apply arity as a final narrowing step without falling through to a weaker scope. An explicit import/reexport that names an external module terminates as `EXTERNAL`; never fall through from that evidence to an unrelated same-name project declaration. Exactly one remaining candidate is `RESOLVED`; multiple candidates are `AMBIGUOUS`; no candidate and no external module evidence is `UNRESOLVED`. Sort candidates by `SymbolId`. Preserve raw facts, context, confidence, and candidates in every outcome. Definite and possible references use the same candidate lookup, but resolution never upgrades confidence. Token occurrences, arbitrary strings/comments, capitalization, and ubiquitous-name frequency are not resolver evidence; only extractor-emitted recognized dynamic references participate. A later analysis may use token occurrence solely as a false-positive guard, never as the primary resolver.

- [ ] **Step 6: Verify resolver GREEN**

Run:

```bash
.venv/bin/python -m unittest tests/test_resolve.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: every raw fact has one deterministic resolution record, ambiguity is explicit, aliases/receivers resolve exact targets, recognized dynamic callbacks retain possible targets, and arbitrary string/comment decoys do not become facts.

- [ ] **Step 7: Commit conservative resolution**

```bash
git add src/hologram/resolve.py src/hologram/__init__.py tests/test_resolve.py tests/fixtures/resolution
git commit -m "feat: resolve project facts conservatively"
```

### Task 8: Cut over through one exact `BuildSnapshot` pipeline

**Files:**
- Create: `src/hologram/pipeline.py`
- Create: `tests/test_pipeline.py`
- Create: `tests/test_extractor_parity.py`
- Test: `tests/fixtures/expected/extractors-v1.json`
- Modify: `src/hologram/parsers/api.py`
- Modify: `src/hologram/legacy.py`
- Modify: `src/hologram/__init__.py`
- Modify: `tests/test_extract_langs.py`
- Modify: `tests/test_more_langs.py`
- Modify: `tests/test_treesitter.py`
- Modify: `tests/test_simple_mode.py`

- [ ] **Step 1: Consume the pre-migration characterization fixture**

Load the reviewed `tests/fixtures/expected/extractors-v1.json` captured in Task 1. `tests/test_extractor_parity.py` must normalize canonical IR back to every captured v1 field, including calls, bindings, and body size. Use this exact projection:

```python
def legacy_call(call: CallRef) -> str:
    return f"{call.receiver}.{call.name}" if call.receiver else call.name


def normalize_v2(file_ir: FileIR, symbol: Symbol) -> dict[str, object]:
    return {
        "file": symbol.file,
        "line": symbol.span.start_line,
        "name": symbol.name,
        "kind": symbol.kind.value,
        "signature": symbol.signature,
        "params": list(symbol.params),
        "returns": symbol.returns,
        "visibility": symbol.visibility.value,
        "container": symbol.container,
        "lang": symbol.lang.value,
        "calls": [
            legacy_call(call)
            for call in file_ir.calls
            if call.caller == symbol.id
        ],
        "supers": list(symbol.supers),
        "permits": list(symbol.permits),
        "raises": list(symbol.raises),
        "bindings": [
            [binding.name, binding.type_name]
            for binding in sorted(
                symbol.bindings,
                key=lambda item: (item.name, item.type_name),
            )
        ],
        "body_lines": symbol.body_lines,
    }


def legacy_key(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record["file"],
        record["line"],
        record["kind"],
        record["container"] or "",
        record["name"],
        record["signature"],
    )
```

Sort with the Task 1 key. For each fixture root, build `expected_keys`, retain every normalized v2 record whose `legacy_key()` is in that set, and assert that complete sorted list equals the oracle list. This makes every captured v1 record mandatory and compares every captured field exactly; no captured call, binding, or body size may disappear or change. Additional canonical records are allowed only for the explicitly new module, named-nested-declaration, field, property, and constant categories asserted by the language tests in Tasks 2–6. Imports, contextual references, ordered body events, exact end spans, annotations, modifiers, and diagnostics are new non-symbol facts and are tested directly rather than projected into the v1 object. The test fails if the fixture is missing or if its fixture-root keys differ, never rewrites the oracle, and calls `assert_body_fact_events()` for every extracted `FileIR`.

- [ ] **Step 2: Write pipeline and parity tests**

Create `tests/test_pipeline.py`:

```python
def test_build_project_returns_one_complete_snapshot(self):
    config = default_config()
    snapshot = build_project(PYMINI, config)
    self.assertTrue(snapshot.complete)
    self.assertIs(snapshot.project.files[0].source, snapshot.scan.sources[0])
    self.assertEqual(len(snapshot.state.value), 64)
    self.assertEqual(len(snapshot.resolution.calls), sum(len(f.calls) for f in snapshot.project.files))
    self.assertIs(snapshot.require_complete(), snapshot)

def test_parse_error_finalizes_ledger_as_failed_and_exit_contract_is_shared(self):
    self.write("broken.py", "def broken(:\n")
    snapshot = build_project(self.root, self.config)
    entry = next(e for e in snapshot.scan.entries if e.file == "broken.py")
    self.assertEqual(entry.status, ScanStatus.FAILED)
    self.assertEqual(entry.reason, "parse-error")
    self.assertIsNotNone(entry.source)
    self.assertFalse(snapshot.complete)
    with self.assertRaises(IncompleteBuildError) as ctx:
        snapshot.require_complete()
    self.assertIs(ctx.exception.snapshot, snapshot)

def test_build_uses_scanned_bytes_after_path_mutation(self):
    scanned = scan_project(self.root, self.config)
    original = scanned.sources[0].raw
    scanned.sources[0].path.write_bytes(b"changed after scan\n")
    with patch("hologram.pipeline.scan_project", return_value=scanned):
        snapshot = build_project(self.root, self.config)
    self.assertEqual(snapshot.project.files[0].source.raw, original)

def test_pipeline_passes_only_active_versions_to_state(self):
    with patch("hologram.pipeline.compute_state", wraps=compute_state) as state:
        build_project(PYMINI, default_config())
    self.assertEqual(set(state.call_args.kwargs["extractor_versions"]), {"python"})
    self.assertEqual(set(state.call_args.kwargs["parser_versions"]), {"python"})
```

Create `tests/test_extractor_parity.py` using the exact projection and addition boundary from Step 1. Add a table-driven pipeline test for `missing-parser`, `extractor-crash`, and an unfamiliar stable error code; assert the final ledger reasons are respectively `missing-parser`, `extractor-crash`, and that unfamiliar code. Captured v1 call strings, bindings, and body sizes are never exempt.

- [ ] **Step 3: Run pipeline/parity tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_pipeline.py tests/test_extractor_parity.py -v
```

Expected: ERROR because `hologram.pipeline` and `BuildSnapshot` do not exist; parity also exposes any remaining extractor drift.

- [ ] **Step 4: Implement the frozen orchestration API**

Create `src/hologram/pipeline.py` with this exact public contract:

```python
from dataclasses import dataclass
from pathlib import Path

from hologram.config import ProjectConfig
from hologram.model import ProjectIR
from hologram.resolve import ResolutionResult
from hologram.scan import ScanResult
from hologram.state import StateResult


@dataclass(frozen=True, slots=True)
class BuildSnapshot:
    scan: ScanResult
    state: StateResult
    project: ProjectIR
    resolution: ResolutionResult
    complete: bool

    def require_complete(self) -> "BuildSnapshot":
        if not self.complete:
            raise IncompleteBuildError(self)
        return self


class IncompleteBuildError(RuntimeError):
    def __init__(self, snapshot: BuildSnapshot) -> None:
        self.snapshot = snapshot
        self.diagnostics = tuple(dict.fromkeys(
            snapshot.scan.diagnostics
            + snapshot.state.diagnostics
            + snapshot.project.diagnostics
            + snapshot.resolution.diagnostics
        ))
        messages = [
            (
                f"{d.code} ({d.span.file}:{d.span.start_line}): {d.message}"
                if d.span else f"{d.code}: {d.message}"
            )
            for d in self.diagnostics
        ]
        super().__init__("; ".join(messages) or "project extraction incomplete")
```

Frozen function signature (the complete algorithm follows):

```text
build_project(root: Path, config: ProjectConfig) -> BuildSnapshot
```

Export `BuildSnapshot`, `IncompleteBuildError`, and `build_project` from
`src/hologram/__init__.py` in this task.

Implement it in this order:

1. Resolve `root` and call `scan_project(root, config)` exactly once.
2. Call `extract_project(root, provisional_scan.sources)`; extractors use only snapshot bytes.
3. For every file with an error diagnostic, replace its immutable ledger entry with `status=FAILED` while retaining its `SourceFile`. Choose `reason` deterministically from that file's error diagnostics: any syntax-error code maps to `parse-error`; otherwise `missing-parser` maps to `missing-parser`; otherwise `extractor-crash` maps to `extractor-crash`; otherwise use the first error diagnostic's stable code in emitted order. Rebuild the final `ScanResult` in original order, preserve scanner diagnostics, and force `complete=False` when any entry is failed.
4. Determine `active: set[Language]` from final-ledger `INDEXED`/`FAILED` entries whose `language is not None`. Pass `extractor_versions={language.value: EXTRACTOR_VERSIONS[language] for language in sorted(active, key=lambda item: item.value)}` and the equivalently key-filtered result of `DEFAULT_REGISTRY.versions()` to `compute_state()`. State consumes retained bytes, including failed parse snapshots. Installing or upgrading an inactive grammar must not change `state.value`.
5. Call `resolve_project(project)` exactly once.
6. Set `complete` only when scan, state, and project are complete and resolution has no error diagnostic.

`build_project()` always returns diagnostics; it does not raise for incomplete input. Callers that require a usable map call `snapshot.require_complete()`. The delivery CLI maps `IncompleteBuildError` to exit code 3; no downstream phase defines a second incomplete-build exception.

- [ ] **Step 5: Adapt legacy build/render without reintroducing extraction logic**

Change `legacy._gather()` to accept the manifest-backed `ProjectConfig` already loaded by the temporary CLI, call `build_project()`, call `require_complete()`, and adapt canonical symbols/resolved calls into its private render-only legacy record. Direct compatibility tests pass `default_config()` explicitly; build/check commands never silently substitute it. Populate file tokens from `file_ir.source.text`, not the filesystem. Use `snapshot.state.value` in the constant `state=<64hex>` header. Remove legacy parser registries, parser bootstrapping, language dispatch, receiver-binding resolution, and import guessing.

Keep transitive call reduction temporarily as rendering analysis; the next plan moves it to `analysis.py`. Keep the package-root legacy `Symbol` constructor until render-unit tests migrate in that plan, but export canonical `Symbol` from `hologram.model` under `hologram.parsers` and through `FileIR`.

- [ ] **Step 6: Verify cutover GREEN and absence of duplicate implementations**

Run:

```bash
.venv/bin/python -m unittest tests/test_pipeline.py tests/test_extractor_parity.py -v
.venv/bin/python -m unittest discover -s tests -v
rg '^def _extract_|^EXTRACTORS =|tree_sitter_' src/hologram/legacy.py
```

Expected: parity and full suite pass; `rg` exits 1 with no extractor/parser
implementation matches; pipeline tests prove build/check adapters use the same
64-character state snapshot. Direct fixture commands remain deferred until the
delivery phase creates the tracked root schema-2 self-config.

- [ ] **Step 7: Commit the pipeline cutover**

```bash
git add src/hologram/pipeline.py src/hologram/parsers/api.py src/hologram/legacy.py src/hologram/__init__.py tests/test_pipeline.py tests/test_extractor_parity.py tests/test_extract_langs.py tests/test_more_langs.py tests/test_treesitter.py tests/test_simple_mode.py
git commit -m "refactor: cut over to modular project pipeline"
```

## Extractor phase gate

Run:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src/hologram/parsers src/hologram/resolve.py src/hologram/pipeline.py tests/test_parser_*.py tests/test_resolve.py tests/test_pipeline.py
.venv/bin/mypy src/hologram/model.py src/hologram/config.py src/hologram/scan.py src/hologram/state.py src/hologram/parsers src/hologram/resolve.py src/hologram/pipeline.py
git diff --check
git status --short
```

Expected: all advertised extensions dispatch; all parsers emit immutable raw
facts from scanner bytes; ambiguity remains explicit; the complete suite, Ruff,
and mypy pass; and no language extractor remains in `legacy.py`. Root
self-build/check starts in the delivery phase after `.hologram.toml` exists.
