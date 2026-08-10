import ast
import dataclasses
import hashlib
import inspect
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hologram.model import (
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    Language,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    SourceFile,
    SourceRole,
    SourceSpan,
    SymbolKind,
)
from hologram.parsers import api as api_runtime
from hologram.parsers import common as common_runtime
from hologram.parsers import treesitter as treesitter_runtime
from hologram.parsers.api import (
    EXTRACTOR_VERSIONS,
    ParserRegistry,
    extract_file,
    extract_project,
)
from hologram.parsers.common import (
    ast_body_events,
    ast_span,
    base_type,
    body_lines,
    heritage,
    ordered_unique,
    reference,
    signature_key,
    split_top_commas,
    symbol_id,
    tight_type,
    utf8_byte_column,
    validate_body_events,
)
from hologram.parsers.treesitter import ast_collect, body_events, node_span
from tests.parser_assertions import assert_body_fact_events

ROOT = Path(__file__).resolve().parents[1]


def source(
    language: Language = Language.JAVA,
    *,
    file: str = "Broken.java",
    raw: bytes = b"class Broken {",
) -> SourceFile:
    return SourceFile(
        Path("/repo") / file,
        file,
        language,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def token_span(
    snapshot: SourceFile,
    line: int,
    token: str,
    *,
    occurrence: int = 1,
) -> SourceSpan:
    raw_line = snapshot.raw.splitlines()[line - 1]
    needle = token.encode("utf-8")
    start = -1
    for _ in range(occurrence):
        start = raw_line.find(needle, start + 1)
        if start < 0:
            raise AssertionError(f"{token!r} occurrence {occurrence} not found")
    return SourceSpan(snapshot.file, line, start, line, start + len(needle))


def tree_body_fixture(
    test: unittest.TestCase,
    language: Language,
    raw: bytes,
    callable_kinds: tuple[str, ...],
) -> tuple[SourceFile, Any, Any, tuple[Any, ...]]:
    parser = ParserRegistry().parser_for(language)
    if parser is None:
        test.skipTest(f"tree-sitter grammar for {language.value} is not installed")
    snapshot = source(language, file=f"probe.{language.value}", raw=raw)
    tree = cast(Any, parser).parse(snapshot.raw)
    test.assertFalse(tree.root_node.has_error, str(tree.root_node))
    callables = ast_collect(tree.root_node, callable_kinds)
    test.assertTrue(callables, f"no callable node for {language.value}")
    callable_node = callables[0]
    events = body_events(snapshot, callable_node)
    validate_body_events(events)
    return snapshot, tree, callable_node, events


class _FakeLanguage:
    def __init__(self, capsule: object) -> None:
        self.capsule = capsule


class _FakeParser:
    def __init__(self, language: object) -> None:
        self.language = language


class _DiscoveryGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.entered = threading.Event()
        self.duplicate = threading.Event()
        self.release = threading.Event()

    def block(self) -> None:
        with self._lock:
            self.calls += 1
            if self.calls > 1:
                self.duplicate.set()
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("discovery gate timed out")


class ParserRuntimeTest(unittest.TestCase):
    def run_discovery_pair(
        self,
        operation: Any,
        gate: _DiscoveryGate,
    ) -> tuple[object, object]:
        ready = threading.Barrier(3)

        def worker() -> object:
            ready.wait(timeout=5)
            return operation()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(worker), executor.submit(worker))
            ready.wait(timeout=5)
            self.assertTrue(gate.entered.wait(5))
            duplicate = gate.duplicate.wait(0.2)
            gate.release.set()
            results = tuple(future.result(timeout=5) for future in futures)
        self.assertFalse(duplicate)
        return cast(tuple[object, object], results)

    def test_missing_grammar_is_a_diagnostic_not_process_exit(self) -> None:
        snapshot = source()
        registry = ParserRegistry(module_loader=lambda name: None)

        result = extract_file(snapshot, registry=registry)

        self.assertIs(result.source, snapshot)
        self.assertEqual(result.symbols, ())
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0].code, "missing-parser")
        self.assertEqual(
            result.diagnostics[0].severity,
            DiagnosticSeverity.ERROR,
        )

    def test_extractor_never_reads_source_path(self) -> None:
        raw = b"def answer():\n    return 42\n"
        snapshot = source(Language.PYTHON, file="answer.py", raw=raw)

        with (
            patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("disk reread"),
            ),
            patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("disk reread"),
            ),
        ):
            result = extract_file(snapshot, registry=ParserRegistry())

        self.assertIs(result.source, snapshot)

    def test_project_is_incomplete_when_any_file_has_error(self) -> None:
        project = extract_project(
            Path("/repo"),
            (source(),),
            registry=ParserRegistry(module_loader=lambda name: None),
        )

        self.assertFalse(project.complete)
        self.assertEqual(len(project.files), 1)
        self.assertEqual(project.diagnostics, project.files[0].diagnostics)

    def test_project_sorts_files_extracts_once_and_orders_diagnostics(self) -> None:
        calls: list[str] = []

        def fake_extract(snapshot: SourceFile, parser: object | None) -> FileIR:
            calls.append(snapshot.file)
            return FileIR(
                snapshot,
                diagnostics=(
                    Diagnostic(
                        f"error-{snapshot.file}",
                        DiagnosticSeverity.ERROR,
                        snapshot.file,
                    ),
                ),
            )

        def load(name: str) -> object | None:
            if name == "hologram.parsers.python":
                return SimpleNamespace(extract=fake_extract)
            return None

        registry = ParserRegistry(module_loader=load)
        snapshots = (
            source(Language.PYTHON, file="z.py", raw=b"z = 1\n"),
            source(Language.PYTHON, file="a.py", raw=b"a = 1\n"),
        )

        project = extract_project(Path("/repo"), snapshots, registry=registry)

        self.assertEqual(calls, ["a.py", "z.py"])
        self.assertEqual([item.source.file for item in project.files], ["a.py", "z.py"])
        self.assertEqual(
            [item.code for item in project.diagnostics],
            ["error-a.py", "error-z.py"],
        )
        self.assertFalse(project.complete)

    def test_project_materializes_once_and_rejects_duplicate_file_keys(self) -> None:
        loader_calls: list[str] = []
        yielded: list[str] = []
        first = source(Language.PYTHON, file="duplicate.py", raw=b"first = 1\n")
        second_raw = b"second = 2\n"
        second = SourceFile(
            Path("/other/duplicate.py"),
            "duplicate.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            second_raw,
            hashlib.sha256(second_raw).hexdigest(),
        )

        def duplicates() -> Any:
            for snapshot in (first, second):
                yielded.append(snapshot.file)
                yield snapshot

        registry = ParserRegistry(module_loader=lambda name: loader_calls.append(name))
        with self.assertRaisesRegex(
            ValueError,
            r"^duplicate SourceFile\.file 'duplicate\.py'$",
        ):
            extract_project(Path("/repo"), duplicates(), registry=registry)

        self.assertEqual(yielded, ["duplicate.py", "duplicate.py"])
        self.assertEqual(loader_calls, [])

        extracted: list[str] = []

        def fake_extract(snapshot: SourceFile, parser: object | None) -> FileIR:
            extracted.append(snapshot.file)
            return FileIR(snapshot)

        def load(name: str) -> object | None:
            if name == "hologram.parsers.python":
                return SimpleNamespace(extract=fake_extract)
            return None

        distinct = (
            source(Language.PYTHON, file="b/item.py", raw=b"b = 1\n"),
            source(Language.PYTHON, file="a/item.py", raw=b"a = 1\n"),
        )
        iterations = 0

        def generated() -> Any:
            nonlocal iterations
            iterations += 1
            yield from distinct

        project = extract_project(
            Path("/repo"),
            generated(),
            registry=ParserRegistry(module_loader=load),
        )

        self.assertEqual(iterations, 1)
        self.assertEqual(extracted, ["a/item.py", "b/item.py"])
        self.assertEqual(
            [file_ir.source.file for file_ir in project.files],
            ["a/item.py", "b/item.py"],
        )

    def test_extractor_exception_becomes_source_retaining_diagnostic(self) -> None:
        snapshot = source(Language.PYTHON, file="answer.py", raw=b"answer = 42\n")

        def crash(snapshot: SourceFile, parser: object | None) -> FileIR:
            raise RuntimeError("broken extraction")

        registry = ParserRegistry(
            module_loader=lambda name: (
                SimpleNamespace(extract=crash)
                if name == "hologram.parsers.python"
                else None
            )
        )

        result = extract_file(snapshot, registry=registry)

        self.assertIs(result.source, snapshot)
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0].code, "extractor-crash")
        self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)
        self.assertIn("RuntimeError", result.diagnostics[0].message)
        self.assertIn("broken extraction", result.diagnostics[0].message)

    def test_parser_loader_exception_is_cached_parser_crash_diagnostic(self) -> None:
        snapshot = source()
        calls = 0

        def load(name: str) -> object | None:
            nonlocal calls
            if name == "tree_sitter_java":
                calls += 1
                raise RuntimeError("grammar discovery broke")
            return None

        registry = ParserRegistry(module_loader=load)

        first = extract_file(snapshot, registry=registry)
        second = extract_file(snapshot, registry=registry)

        self.assertEqual(calls, 1)
        for result in (first, second):
            self.assertIs(result.source, snapshot)
            self.assertEqual(len(result.diagnostics), 1)
            self.assertEqual(result.diagnostics[0].code, "parser-crash")
            self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)
            self.assertIn("RuntimeError", result.diagnostics[0].message)
            self.assertIn("grammar discovery broke", result.diagnostics[0].message)

    def test_parser_construction_exception_is_parser_crash_diagnostic(self) -> None:
        snapshot = source()

        def broken_grammar() -> object:
            raise RuntimeError("grammar capsule broke")

        modules = {
            "tree_sitter_java": SimpleNamespace(language=broken_grammar),
            "tree_sitter": SimpleNamespace(Language=_FakeLanguage, Parser=_FakeParser),
        }
        registry = ParserRegistry(module_loader=modules.get)

        result = extract_file(snapshot, registry=registry)

        self.assertEqual(result.diagnostics[0].code, "parser-crash")
        self.assertIn("RuntimeError", result.diagnostics[0].message)
        self.assertIn("grammar capsule broke", result.diagnostics[0].message)

    def test_extractor_loader_exception_is_cached_extractor_crash_diagnostic(
        self,
    ) -> None:
        snapshot = source(Language.PYTHON, file="answer.py", raw=b"answer = 42\n")
        calls = 0

        def load(name: str) -> object | None:
            nonlocal calls
            if name == "hologram.parsers.python":
                calls += 1
                raise RuntimeError("extractor discovery broke")
            return None

        registry = ParserRegistry(module_loader=load)

        first = extract_file(snapshot, registry=registry)
        second = extract_file(snapshot, registry=registry)

        self.assertEqual(calls, 1)
        for result in (first, second):
            self.assertIs(result.source, snapshot)
            self.assertEqual(len(result.diagnostics), 1)
            self.assertEqual(result.diagnostics[0].code, "extractor-crash")
            self.assertIn("RuntimeError", result.diagnostics[0].message)
            self.assertIn("extractor discovery broke", result.diagnostics[0].message)

    def test_falsey_module_loader_is_honored(self) -> None:
        snapshot = source(Language.PYTHON, file="answer.py", raw=b"answer = 42\n")

        def fake_extract(snapshot: SourceFile, parser: object | None) -> FileIR:
            return FileIR(snapshot)

        class FalseyLoader:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __bool__(self) -> bool:
                return False

            def __call__(self, name: str) -> object | None:
                self.calls.append(name)
                if name == "hologram.parsers.python":
                    return SimpleNamespace(extract=fake_extract)
                return None

        loader = FalseyLoader()

        result = extract_file(snapshot, registry=ParserRegistry(module_loader=loader))

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(loader.calls, ["hologram.parsers.python"])

    def test_process_control_exceptions_from_extractors_propagate(self) -> None:
        snapshot = source(Language.PYTHON, file="answer.py", raw=b"answer = 42\n")

        for exception in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(exception=type(exception).__name__):

                def crash(
                    snapshot: SourceFile,
                    parser: object | None,
                    error: BaseException = exception,
                ) -> FileIR:
                    raise error

                registry = ParserRegistry(
                    module_loader=lambda name: (
                        SimpleNamespace(extract=crash)
                        if name == "hologram.parsers.python"
                        else None
                    )
                )
                with self.assertRaises(type(exception)):
                    extract_file(snapshot, registry=registry)

    def test_process_control_exceptions_from_module_loaders_propagate(self) -> None:
        cases = (
            (Language.JAVA, "tree_sitter_java", KeyboardInterrupt()),
            (Language.JAVA, "tree_sitter_java", SystemExit(3)),
            (Language.PYTHON, "hologram.parsers.python", KeyboardInterrupt()),
            (Language.PYTHON, "hologram.parsers.python", SystemExit(4)),
        )
        for language, target, exception in cases:
            with self.subTest(language=language, exception=type(exception).__name__):
                snapshot = source(
                    language,
                    file="probe.py" if language is Language.PYTHON else "Probe.java",
                )

                def load(
                    name: str,
                    error: BaseException = exception,
                    target_name: str = target,
                ) -> object | None:
                    if name == target_name:
                        raise error
                    return None

                with self.assertRaises(type(exception)):
                    extract_file(snapshot, registry=ParserRegistry(module_loader=load))

    def test_importing_package_does_not_import_optional_grammars_or_extractors(
        self,
    ) -> None:
        code = """
import sys
for name in tuple(sys.modules):
    if name == 'hologram' or name.startswith('hologram.'):
        del sys.modules[name]
import hologram
from hologram import parsers
forbidden = {
    'tree_sitter_java',
    'tree_sitter_typescript',
    'tree_sitter_go',
    'tree_sitter_rust',
    'tree_sitter_c_sharp',
    'tree_sitter_kotlin',
    'tree_sitter_c',
    'tree_sitter_cpp',
    'tree_sitter_lua',
    'tree_sitter_html',
}
loaded = sorted(forbidden.intersection(sys.modules))
legacy = sorted(name for name in sys.modules if name == 'hologram.legacy')
extractors = sorted(
    name for name in sys.modules
    if name.startswith('hologram.parsers.')
    and name.rsplit('.', 1)[-1] not in {'api', 'common', 'treesitter'}
)
if loaded or legacy or extractors:
    raise SystemExit(f'eager imports: {loaded!r} {legacy!r} {extractors!r}')
if parsers.ParserRegistry is not hologram.parsers.ParserRegistry:
    raise SystemExit('canonical parser package mismatch')
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parser_success_and_absence_are_cached_per_language(self) -> None:
        calls: list[str] = []
        java_capsule = object()

        def load(name: str) -> object | None:
            calls.append(name)
            modules = {
                "tree_sitter": SimpleNamespace(
                    Language=_FakeLanguage,
                    Parser=_FakeParser,
                ),
                "tree_sitter_java": SimpleNamespace(language=lambda: java_capsule),
            }
            return modules.get(name)

        registry = ParserRegistry(module_loader=load)

        first = registry.parser_for(Language.JAVA)
        second = registry.parser_for(Language.JAVA)
        self.assertIs(first, second)
        self.assertEqual(calls.count("tree_sitter_java"), 1)
        self.assertEqual(calls.count("tree_sitter"), 1)
        self.assertNotIn("tree_sitter_go", calls)

        self.assertIsNone(registry.parser_for(Language.GO))
        self.assertIsNone(registry.parser_for(Language.GO))
        self.assertEqual(calls.count("tree_sitter_go"), 1)

    def test_optional_imports_only_suppress_the_requested_module(self) -> None:
        direct_cases = (
            (Language.JAVA, "tree_sitter_java", "missing-parser"),
            (Language.PYTHON, "hologram.parsers.python", "missing-extractor"),
        )
        for language, target, expected_code in direct_cases:
            with self.subTest(direct=target):
                calls = 0

                def missing(name: str, target: str = target) -> object | None:
                    nonlocal calls
                    if name == target:
                        calls += 1
                        raise ModuleNotFoundError(
                            f"No module named {name!r}",
                            name=name,
                        )
                    return None

                file = "probe.py" if language is Language.PYTHON else "Probe.java"
                snapshot = source(language, file=file)
                registry = ParserRegistry(module_loader=missing)
                results = (
                    extract_file(snapshot, registry=registry),
                    extract_file(snapshot, registry=registry),
                )

                self.assertEqual(calls, 1)
                self.assertEqual(
                    [result.diagnostics[0].code for result in results],
                    [expected_code, expected_code],
                )

        failure_cases = (
            (
                Language.JAVA,
                "tree_sitter_java",
                ModuleNotFoundError("missing dependency", name="grammar_dependency"),
                "parser-crash",
            ),
            (
                Language.JAVA,
                "tree_sitter_java",
                ImportError("broken ABI"),
                "parser-crash",
            ),
            (
                Language.PYTHON,
                "hologram.parsers.python",
                ModuleNotFoundError("missing dependency", name="extractor_dependency"),
                "extractor-crash",
            ),
            (
                Language.PYTHON,
                "hologram.parsers.python",
                ImportError("broken ABI"),
                "extractor-crash",
            ),
        )
        for language, target, failure, expected_code in failure_cases:
            with self.subTest(failure=type(failure).__name__, target=target):
                calls = 0

                def broken(
                    name: str,
                    target: str = target,
                    failure: Exception = failure,
                ) -> object | None:
                    nonlocal calls
                    if name == target:
                        calls += 1
                        raise failure
                    return None

                file = "probe.py" if language is Language.PYTHON else "Probe.java"
                snapshot = source(language, file=file)
                registry = ParserRegistry(module_loader=broken)
                results = (
                    extract_file(snapshot, registry=registry),
                    extract_file(snapshot, registry=registry),
                )

                self.assertEqual(calls, 1)
                for result in results:
                    diagnostic = result.diagnostics[0]
                    self.assertEqual(diagnostic.code, expected_code)
                    self.assertIn(type(failure).__name__, diagnostic.message)
                    self.assertIn(str(failure), diagnostic.message)

    def test_concurrent_parser_success_is_constructed_once(self) -> None:
        gate = _DiscoveryGate()
        capsule = object()
        runtime_calls = 0

        def load(name: str) -> object | None:
            nonlocal runtime_calls
            if name == "tree_sitter_java":
                gate.block()
                return SimpleNamespace(language=lambda: capsule)
            if name == "tree_sitter":
                runtime_calls += 1
                return SimpleNamespace(Language=_FakeLanguage, Parser=_FakeParser)
            return None

        registry = ParserRegistry(module_loader=load)
        with patch.object(
            api_runtime, "grammar_version", return_value="runtime/grammar"
        ):
            first, second = self.run_discovery_pair(
                lambda: registry.parser_for(Language.JAVA),
                gate,
            )

        self.assertEqual(gate.calls, 1)
        self.assertEqual(runtime_calls, 1)
        self.assertIs(first, second)
        self.assertIs(first, registry.parser_for(Language.JAVA))
        self.assertIsNone(registry._parser_error(Language.JAVA))

    def test_concurrent_parser_error_is_published_once(self) -> None:
        gate = _DiscoveryGate()

        def load(name: str) -> object | None:
            if name == "tree_sitter_java":
                gate.block()
                raise RuntimeError("gated parser failure")
            return None

        registry = ParserRegistry(module_loader=load)
        first, second = self.run_discovery_pair(
            lambda: registry.parser_for(Language.JAVA),
            gate,
        )

        self.assertEqual(gate.calls, 1)
        self.assertIsNone(first)
        self.assertIsNone(second)
        error = registry._parser_error(Language.JAVA)
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error, registry._parser_error(Language.JAVA))
        self.assertIsNone(registry.parser_for(Language.JAVA))

    def test_concurrent_extractor_success_is_imported_once(self) -> None:
        gate = _DiscoveryGate()

        def fake_extract(snapshot: SourceFile, parser: object | None) -> FileIR:
            return FileIR(snapshot)

        def load(name: str) -> object | None:
            if name == "hologram.parsers.python":
                gate.block()
                return SimpleNamespace(extract=fake_extract)
            return None

        registry = ParserRegistry(module_loader=load)
        first, second = self.run_discovery_pair(
            lambda: registry._extractor_for(Language.PYTHON),
            gate,
        )

        self.assertEqual(gate.calls, 1)
        self.assertIs(first, fake_extract)
        self.assertIs(second, fake_extract)
        self.assertIs(first, registry._extractor_for(Language.PYTHON))
        self.assertIsNone(registry._extractor_error(Language.PYTHON))

    def test_concurrent_extractor_error_is_published_once(self) -> None:
        gate = _DiscoveryGate()

        def load(name: str) -> object | None:
            if name == "hologram.parsers.python":
                gate.block()
                raise RuntimeError("gated extractor failure")
            return None

        registry = ParserRegistry(module_loader=load)
        first, second = self.run_discovery_pair(
            lambda: registry._extractor_for(Language.PYTHON),
            gate,
        )

        self.assertEqual(gate.calls, 1)
        self.assertIsNone(first)
        self.assertIsNone(second)
        error = registry._extractor_error(Language.PYTHON)
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error, registry._extractor_error(Language.PYTHON))
        self.assertIsNone(registry._extractor_for(Language.PYTHON))

    def test_parser_initialization_locks_are_per_language(self) -> None:
        grammar_gate = threading.Barrier(2)
        capsules = {
            "tree_sitter_java": object(),
            "tree_sitter_go": object(),
        }

        def load(name: str) -> object | None:
            if name in capsules:
                grammar_gate.wait(timeout=5)
                return SimpleNamespace(language=lambda: capsules[name])
            if name == "tree_sitter":
                return SimpleNamespace(Language=_FakeLanguage, Parser=_FakeParser)
            return None

        registry = ParserRegistry(module_loader=load)
        with (
            patch.object(
                api_runtime, "grammar_version", return_value="runtime/grammar"
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = (
                executor.submit(registry.parser_for, Language.JAVA),
                executor.submit(registry.parser_for, Language.GO),
            )
            results = tuple(future.result(timeout=5) for future in futures)

        self.assertTrue(all(result is not None for result in results))

    def test_parser_version_failure_is_atomically_fail_closed(self) -> None:
        capsule = object()
        version_entered = threading.Event()
        version_release = threading.Event()
        version_calls = 0

        def load(name: str) -> object | None:
            modules = {
                "tree_sitter_java": SimpleNamespace(language=lambda: capsule),
                "tree_sitter": SimpleNamespace(
                    Language=_FakeLanguage,
                    Parser=_FakeParser,
                ),
            }
            return modules.get(name)

        def broken_version(language: Language) -> str:
            nonlocal version_calls
            version_calls += 1
            version_entered.set()
            if not version_release.wait(5):
                raise AssertionError("version gate timed out")
            raise RuntimeError("metadata discovery broke")

        registry = ParserRegistry(module_loader=load)

        def discover() -> object:
            try:
                return registry.parser_for(Language.JAVA)
            except RuntimeError as error:  # captured to expose partial publication
                return error

        second_done = threading.Event()

        def second_discover() -> object:
            try:
                return discover()
            finally:
                second_done.set()

        with (
            patch.object(api_runtime, "grammar_version", side_effect=broken_version),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first_future = executor.submit(discover)
            self.assertTrue(version_entered.wait(5))
            second_future = executor.submit(second_discover)
            published_early = second_done.wait(0.2)
            version_release.set()
            results = (
                first_future.result(timeout=5),
                second_future.result(timeout=5),
            )

        self.assertFalse(published_early)
        self.assertEqual(results, (None, None))
        self.assertEqual(version_calls, 1)
        error = registry._parser_error(Language.JAVA)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(registry.versions()["java"], "missing")
        self.assertIsNone(registry.parser_for(Language.JAVA))

    def test_builtin_parsers_do_not_load_modules(self) -> None:
        calls: list[str] = []
        registry = ParserRegistry(module_loader=lambda name: calls.append(name))

        self.assertTrue(registry.has_parser(Language.PYTHON))
        self.assertTrue(registry.has_parser(Language.HELM))
        self.assertIsNone(registry.parser_for(Language.PYTHON))
        self.assertIsNone(registry.parser_for(Language.HELM))
        self.assertEqual(calls, [])

    def test_versions_are_complete_sorted_fresh_immutable_and_nonloading(self) -> None:
        calls: list[str] = []
        registry = ParserRegistry(module_loader=lambda name: calls.append(name))

        first = registry.versions()
        second = registry.versions()

        expected_keys = sorted(language.value for language in Language)
        self.assertEqual(list(first), expected_keys)
        self.assertEqual(list(second), expected_keys)
        self.assertIsInstance(first, MappingProxyType)
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        self.assertEqual(
            first["python"],
            f"stdlib-ast-{sys.version_info.major}.{sys.version_info.minor}",
        )
        self.assertEqual(first["helm"], "builtin")
        self.assertRegex(first["java"], r"^(missing|[^/]+/[^/]+)$")
        self.assertEqual(calls, [])
        with self.assertRaises(TypeError):
            first["python"] = "changed"  # type: ignore[index]

    def test_extractor_version_mapping_is_complete_and_immutable(self) -> None:
        self.assertEqual(set(EXTRACTOR_VERSIONS), set(Language))
        self.assertEqual(set(EXTRACTOR_VERSIONS.values()), {"2"})
        with self.assertRaises(TypeError):
            EXTRACTOR_VERSIONS[Language.PYTHON] = "3"  # type: ignore[index]


class ParserHelperTest(unittest.TestCase):
    def test_identity_and_stable_text_helpers(self) -> None:
        snapshot = source(Language.JAVA, file="Price.java", raw=b"class Price {}\n")

        method = symbol_id(
            snapshot,
            ("Price",),
            SymbolKind.METHOD,
            "quote",
            ["Map<K, V>", "int"],
        )
        field = symbol_id(
            snapshot,
            ("Price",),
            SymbolKind.FIELD,
            "cache",
            ["ignored"],
        )

        self.assertEqual(ordered_unique(["a", "b", "a"]), ("a", "b"))
        self.assertEqual(signature_key(["Map<K,V>", "int"]), "(Map<K,V>,int)")
        self.assertEqual(method.signature_key, "(Map<K, V>,int)")
        self.assertEqual(field.signature_key, "")
        self.assertEqual(
            split_top_commas("Map<K,V>, List<X>, int"), ("Map<K,V>", " List<X>", " int")
        )
        self.assertEqual(tight_type("Map<K, V>"), "Map<K,V>")
        self.assertEqual(base_type("shop.Map<K,V>[]"), "shop.Map")
        self.assertEqual(
            heritage(" extends Base implements One, Two permits Three "),
            (("Base", "One", "Two"), ("Three",)),
        )

    def test_reference_requires_explicit_context_and_confidence(self) -> None:
        parameters = inspect.signature(reference).parameters
        self.assertIs(parameters["context"].default, inspect.Parameter.empty)
        self.assertIs(parameters["confidence"].default, inspect.Parameter.empty)

        snapshot = source(Language.PYTHON, file="a.py", raw=b"target\n")
        owner = symbol_id(snapshot, (), SymbolKind.FUNCTION, "run")
        span = SourceSpan("a.py", 1, 0, 1, 6)
        result = reference(
            owner,
            span,
            "target",
            qualifier=None,
            kind=ReferenceKind.NAME,
            context=ReferenceContext.CODE,
            confidence=ReferenceConfidence.DEFINITE,
        )

        self.assertEqual(result.context, ReferenceContext.CODE)
        self.assertEqual(result.confidence, ReferenceConfidence.DEFINITE)

    def test_stdlib_ast_spans_are_utf8_bytes_and_control_events_are_balanced(
        self,
    ) -> None:
        raw = (
            "def run(flag, value: Thing):\n"
            '    x = "ż"; target()\n'
            "    enabled = True\n"
            "    missing = None\n"
            "    if flag:\n"
            "        return value.member + 2\n"
            "    def nested():\n"
            "        hidden()\n"
        ).encode()
        snapshot = source(Language.PYTHON, file="run.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]

        events = ast_body_events(snapshot, callable_node)
        validate_body_events(events)

        target_event = next(
            event
            for event in events
            if event.kind is BodyEventKind.CALL and event.text == "target"
        )
        target_line = snapshot.text.splitlines()[1]
        character_column = target_line.index("target")
        self.assertEqual(target_event.span.start_column, character_column + 1)
        self.assertEqual(
            target_event.span.start_column,
            utf8_byte_column(target_line, character_column),
        )
        self.assertEqual(ast_span(snapshot, callable_node).file, "run.py")
        self.assertEqual(body_lines(callable_node), 8)
        self.assertEqual(
            sum(event.kind is BodyEventKind.CONTROL_ENTER for event in events),
            sum(event.kind is BodyEventKind.CONTROL_EXIT for event in events),
        )
        self.assertTrue(any(event.kind is BodyEventKind.PARAM for event in events))
        self.assertTrue(any(event.kind is BodyEventKind.LOCAL for event in events))
        self.assertTrue(any(event.text == "<string>" for event in events))
        self.assertTrue(any(event.text == "<bool>" for event in events))
        self.assertTrue(any(event.text == "<null>" for event in events))
        self.assertNotIn("hidden", {event.text for event in events})

    def test_stdlib_parameter_defaults_are_emitted_in_source_order(self) -> None:
        raw = (
            b"def run(first=alpha(), second=beta(), *args, "
            b"third=gamma(), fourth: Kind=delta(), **kwargs):\n"
            b"    body(*args, value=first, **kwargs)\n"
        )
        snapshot = source(Language.PYTHON, file="defaults.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]

        events = ast_body_events(snapshot, callable_node)
        relevant = tuple(
            (event.kind, event.text)
            for event in events
            if event.kind
            in {BodyEventKind.PARAM, BodyEventKind.TYPE, BodyEventKind.CALL}
        )

        self.assertEqual(
            relevant,
            (
                (BodyEventKind.PARAM, "first"),
                (BodyEventKind.CALL, "alpha"),
                (BodyEventKind.PARAM, "second"),
                (BodyEventKind.CALL, "beta"),
                (BodyEventKind.PARAM, "args"),
                (BodyEventKind.PARAM, "third"),
                (BodyEventKind.CALL, "gamma"),
                (BodyEventKind.PARAM, "fourth"),
                (BodyEventKind.TYPE, "Kind"),
                (BodyEventKind.CALL, "delta"),
                (BodyEventKind.PARAM, "kwargs"),
                (BodyEventKind.CALL, "body"),
            ),
        )
        for name in ("first", "second", "args", "third", "fourth", "kwargs"):
            event = next(
                item
                for item in events
                if item.kind is BodyEventKind.PARAM and item.text == name
            )
            self.assertEqual(event.span, token_span(snapshot, 1, name))

        operators = tuple(
            (event.text, event.span)
            for event in events
            if event.kind is BodyEventKind.OPERATOR
        )
        self.assertEqual(
            operators,
            (
                ("=", token_span(snapshot, 1, "=", occurrence=1)),
                ("=", token_span(snapshot, 1, "=", occurrence=2)),
                ("*", token_span(snapshot, 1, "*", occurrence=1)),
                ("=", token_span(snapshot, 1, "=", occurrence=3)),
                ("=", token_span(snapshot, 1, "=", occurrence=4)),
                ("**", token_span(snapshot, 1, "**")),
                ("*", token_span(snapshot, 2, "*", occurrence=1)),
                ("=", token_span(snapshot, 2, "=")),
                ("**", token_span(snapshot, 2, "**")),
            ),
        )

    def test_python_physical_lines_only_split_cr_and_lf(self) -> None:
        special = "\v\f\x1c\x1d\x1e\x85\u2028\u2029"
        raw = (f'def run():\n    text = "before{special}after"; target()\n').encode()
        cases = (
            ("special.py", raw, 2, raw.split(b"\n")[1].find(b"target")),
            (
                "cr-only.py",
                b"def run():\r    value = 1\r    target()\r",
                3,
                4,
            ),
            (
                "crlf.py",
                b"def run():\r\n    value = 1\r\n    target()\r\n",
                3,
                4,
            ),
        )
        for file, case_raw, line, column in cases:
            with self.subTest(file=file):
                snapshot = source(Language.PYTHON, file=file, raw=case_raw)
                callable_node = ast.parse(snapshot.text).body[0]
                events = ast_body_events(snapshot, callable_node)
                target = next(
                    event
                    for event in events
                    if event.kind is BodyEventKind.CALL and event.text == "target"
                )

                self.assertEqual(
                    target.span,
                    SourceSpan(file, line, column, line, column + len("target()")),
                )
                context = common_runtime._python_source_context(snapshot)
                self.assertEqual(context.lines[-1], "")

    def test_parenthesized_dict_unpack_uses_the_entry_prefix_span(self) -> None:
        raw = (
            b"def run(left, right, base, exponent):\n"
            b"    first = {**(left)}\n"
            b"    second = {**(((right)))}\n"
            b"    third = {**((base ** exponent))}\n"
        )
        snapshot = source(Language.PYTHON, file="unpack.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]

        operators = tuple(
            (event.text, event.span)
            for event in ast_body_events(snapshot, callable_node)
            if event.kind is BodyEventKind.OPERATOR and event.text == "**"
        )

        self.assertEqual(
            operators,
            (
                ("**", token_span(snapshot, 2, "**")),
                ("**", token_span(snapshot, 3, "**")),
                ("**", token_span(snapshot, 4, "**", occurrence=1)),
                ("**", token_span(snapshot, 4, "**", occurrence=2)),
            ),
        )

    def test_nested_format_spec_keeps_one_literal_and_exact_call_facts(self) -> None:
        raw = b'def run(value):\n    rendered = f"{value:{width()}}"\n'
        snapshot = source(Language.PYTHON, file="format_spec.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]
        width_call = next(
            node for node in ast.walk(callable_node) if isinstance(node, ast.Call)
        )
        width_name = cast(ast.Name, width_call.func)
        events = ast_body_events(snapshot, callable_node)
        owner = symbol_id(snapshot, (), SymbolKind.FUNCTION, "run", ("value",))
        width_span = ast_span(snapshot, width_call)
        name_span = ast_span(snapshot, width_name)
        file_ir = FileIR(
            snapshot,
            calls=(
                CallRef(
                    owner,
                    width_span,
                    "width",
                    None,
                    CallKind.CALL,
                    0,
                ),
            ),
            references=(
                reference(
                    owner,
                    name_span,
                    "width",
                    None,
                    ReferenceKind.NAME,
                    context=ReferenceContext.CODE,
                    confidence=ReferenceConfidence.DEFINITE,
                ),
            ),
            bodies=(BodyIR(owner, ast_span(snapshot, callable_node), events),),
        )

        assert_body_fact_events(self, file_ir)
        self.assertEqual(
            [event.text for event in events if event.kind is BodyEventKind.LITERAL],
            ["<string>"],
        )
        event_pairs = {(event.kind, event.span) for event in events}
        self.assertIn((BodyEventKind.CALL, width_span), event_pairs)
        self.assertIn((BodyEventKind.NAME, name_span), event_pairs)

    def test_python_source_context_is_single_flight_and_bounded(self) -> None:
        raw = b"\n".join(
            b"def function_%d(value):\n    return value + %d\n" % (index, index)
            for index in range(8)
        )
        snapshot = source(Language.PYTHON, file="context-cache.py", raw=raw)
        with common_runtime._PYTHON_SOURCE_CONTEXT_LOCK:
            for cached_source in tuple(common_runtime._PYTHON_SOURCE_CONTEXTS):
                if cached_source.file == snapshot.file:
                    common_runtime._PYTHON_SOURCE_CONTEXTS.pop(cached_source)
        callables = tuple(
            node
            for node in ast.parse(snapshot.text).body
            if isinstance(node, ast.FunctionDef)
        )
        ready = threading.Barrier(len(callables))

        def walk(callable_node: ast.FunctionDef) -> tuple[Any, ...]:
            ready.wait(timeout=5)
            return ast_body_events(snapshot, callable_node)

        original_generate = common_runtime.tokenize.generate_tokens
        with (
            patch.object(
                common_runtime.tokenize,
                "generate_tokens",
                wraps=original_generate,
            ) as generate_tokens,
            ThreadPoolExecutor(max_workers=len(callables)) as executor,
        ):
            results = tuple(executor.map(walk, callables))
            first_context = common_runtime._python_source_context(snapshot)
            equivalent = SourceFile(
                snapshot.path,
                snapshot.file,
                snapshot.language,
                snapshot.role,
                snapshot.raw,
                snapshot.sha256,
            )
            second_context = common_runtime._python_source_context(equivalent)
            changed = source(
                Language.PYTHON,
                file="context-cache.py",
                raw=raw + b"\n# changed\n",
            )
            changed_callable = ast.parse(changed.text).body[0]
            ast_body_events(changed, changed_callable)
            changed_context = common_runtime._python_source_context(changed)

        self.assertTrue(all(result for result in results))
        self.assertEqual(generate_tokens.call_count, 2)
        self.assertIs(first_context, second_context)
        self.assertIsNot(first_context, changed_context)
        self.assertLessEqual(
            len(common_runtime._PYTHON_SOURCE_CONTEXTS),
            common_runtime._PYTHON_SOURCE_CONTEXT_LIMIT,
        )

    def test_python_token_queries_only_touch_the_callable_window(self) -> None:
        prefix = b"\n".join(
            b"def prefix_%d(value):\n    return value + %d\n" % (index, index)
            for index in range(300)
        )
        raw = (
            prefix
            + b"\ndef target(value=default()):\n"
            + b"    result = call(keyword=value)\n"
            + b"    return obj.member and result\n"
        )
        snapshot = source(Language.PYTHON, file="token-locality.py", raw=raw)
        callable_node = cast(ast.FunctionDef, ast.parse(snapshot.text).body[-1])
        context = common_runtime._python_source_context(snapshot)

        class CountingTokens:
            def __init__(self, values: tuple[Any, ...]) -> None:
                self.values = values
                self.touches = 0

            def __len__(self) -> int:
                return len(self.values)

            def __getitem__(self, index: Any) -> Any:
                if isinstance(index, slice):
                    start, stop, step = index.indices(len(self.values))
                    self.touches += len(range(start, stop, step))
                else:
                    self.touches += 1
                return self.values[index]

            def __iter__(self) -> Any:
                for index in range(len(self.values)):
                    yield self[index]

        counting = CountingTokens(context.tokens)
        instrumented = dataclasses.replace(
            context,
            tokens=cast(Any, counting),
        )
        with common_runtime._PYTHON_SOURCE_CONTEXT_LOCK:
            common_runtime._PYTHON_SOURCE_CONTEXTS[snapshot] = instrumented
        try:
            events = ast_body_events(snapshot, callable_node)
        finally:
            with common_runtime._PYTHON_SOURCE_CONTEXT_LOCK:
                common_runtime._PYTHON_SOURCE_CONTEXTS[snapshot] = context

        self.assertGreater(len(context.tokens), 2_000)
        self.assertTrue(events)
        self.assertLess(counting.touches, 100)

    def test_stdlib_operator_events_use_each_exact_source_token_span(self) -> None:
        raw = (
            b"def run(a, b, c, middle_x):\n"
            b"    assigned = alias = a\n"
            b"    assigned += b\n"
            b"    named = (captured := c)\n"
            b"    binary = a + b\n"
            b"    compared = a < b <= c\n"
            b"    if a:\n"
            b"        return a and middle_x and c\n"
            b"    return not a or b\n"
            b'    text = f"{a + b}"\n'
            b'    annotated: Literal["="] = a\n'
            b"    relation = a is not b\n"
            b"    membership = a not in c\n" + "    Kvalue = a + b\n".encode()
        )
        snapshot = source(Language.PYTHON, file="operators.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]

        operators = tuple(
            (event.text, event.span)
            for event in ast_body_events(snapshot, callable_node)
            if event.kind is BodyEventKind.OPERATOR
        )
        expected = (
            ("=", token_span(snapshot, 2, "=", occurrence=1)),
            ("=", token_span(snapshot, 2, "=", occurrence=2)),
            ("+=", token_span(snapshot, 3, "+=")),
            ("=", token_span(snapshot, 4, "=")),
            (":=", token_span(snapshot, 4, ":=")),
            ("=", token_span(snapshot, 5, "=")),
            ("+", token_span(snapshot, 5, "+")),
            ("=", token_span(snapshot, 6, "=")),
            ("<", token_span(snapshot, 6, "<")),
            ("<=", token_span(snapshot, 6, "<=")),
            ("and", token_span(snapshot, 8, "and", occurrence=1)),
            ("and", token_span(snapshot, 8, "and", occurrence=2)),
            ("not", token_span(snapshot, 9, "not")),
            ("or", token_span(snapshot, 9, "or")),
            ("=", token_span(snapshot, 10, "=")),
            ("+", token_span(snapshot, 10, "+")),
            ("=", token_span(snapshot, 11, "=", occurrence=2)),
            ("=", token_span(snapshot, 12, "=")),
            (
                "is not",
                SourceSpan(
                    snapshot.file,
                    12,
                    token_span(snapshot, 12, "is").start_column,
                    12,
                    token_span(snapshot, 12, "not").end_column,
                ),
            ),
            ("=", token_span(snapshot, 13, "=")),
            (
                "not in",
                SourceSpan(
                    snapshot.file,
                    13,
                    token_span(snapshot, 13, "not").start_column,
                    13,
                    token_span(snapshot, 13, "in").end_column,
                ),
            ),
            ("=", token_span(snapshot, 14, "=")),
            ("+", token_span(snapshot, 14, "+")),
        )

        self.assertEqual(operators, expected)
        self.assertEqual(expected[10][1].start_column, 17)
        self.assertEqual(expected[10][1].end_column, 20)
        self.assertEqual(expected[11][1].start_column, 30)
        self.assertEqual(expected[11][1].end_column, 33)

    def test_stdlib_import_match_and_delete_binding_roles_are_exact(self) -> None:
        raw = (
            b"def run(subject, existing):\n"
            b"    import package.module\n"
            b"    import other.module as renamed\n"
            b"    from source import first, second as alias\n"
            b"    match subject:\n"
            b"        case [head, *tail]:\n"
            b"            use(head, tail)\n"
            b'        case {"first": value, "second": other, **rest}:\n'
            b"            use(value, other, rest)\n"
            b"        case Point(left=left_value, right=right_value) as point:\n"
            b"            use(left_value, right_value, point)\n"
            b"    del existing\n"
            b"    use(existing)\n"
        )
        snapshot = source(Language.PYTHON, file="bindings.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]
        events = ast_body_events(snapshot, callable_node)
        event_pairs = {(event.kind, event.span) for event in events}

        bindings = (
            (2, "package", 1),
            (3, "renamed", 1),
            (4, "first", 1),
            (4, "alias", 1),
            (6, "head", 1),
            (6, "tail", 1),
            (8, "value", 1),
            (8, "other", 1),
            (8, "rest", 1),
            (10, "left_value", 1),
            (10, "right_value", 1),
            (10, "point", 1),
        )
        for line, name, occurrence in bindings:
            with self.subTest(binding=name):
                span = token_span(snapshot, line, name, occurrence=occurrence)
                self.assertIn((BodyEventKind.LOCAL, span), event_pairs)
                self.assertNotIn((BodyEventKind.NAME, span), event_pairs)

        for line, name, occurrence in (
            (2, "module", 1),
            (3, "other", 1),
            (3, "module", 1),
            (4, "source", 1),
            (4, "second", 1),
        ):
            self.assertNotIn(
                (
                    BodyEventKind.LOCAL,
                    token_span(snapshot, line, name, occurrence=occurrence),
                ),
                event_pairs,
            )

        point_span = token_span(snapshot, 10, "Point")
        deleted_span = token_span(snapshot, 12, "existing")
        used_span = token_span(snapshot, 13, "existing")
        self.assertIn((BodyEventKind.NAME, point_span), event_pairs)
        self.assertIn((BodyEventKind.NAME, deleted_span), event_pairs)
        self.assertNotIn((BodyEventKind.LOCAL, deleted_span), event_pairs)
        self.assertIn((BodyEventKind.NAME, used_span), event_pairs)

        mapping_events = tuple(
            event.text
            for event in events
            if event.span.start_line == 8
            and event.kind in {BodyEventKind.LITERAL, BodyEventKind.LOCAL}
        )
        self.assertEqual(
            mapping_events,
            ("<string>", "value", "<string>", "other", "rest"),
        )

    def test_stdlib_string_binders_use_unicode_source_token_spans(self) -> None:
        raw = (
            "def run(Kelvin):\n"
            "    import module as Kalias\n"
            "    try:\n"
            "        visible()\n"
            "    except Error as Error:\n"
            "        visible(Error)\n"
            "    def nested():\n"
            "        hidden()\n"
            "    class Nested:\n"
            "        hidden()\n"
            "    match Kelvin:\n"
            '        case {"key": Kcapture}:\n'
            "            visible(Kcapture)\n"
            "    visible(nested, Nested)\n"
        ).encode()
        snapshot = source(Language.PYTHON, file="unicode_binders.py", raw=raw)
        callable_node = ast.parse(snapshot.text).body[0]
        events = ast_body_events(snapshot, callable_node)
        event_pairs = {(event.kind, event.span) for event in events}

        expected = (
            (BodyEventKind.PARAM, 1, "Kelvin"),
            (BodyEventKind.LOCAL, 2, "Kalias"),
            (BodyEventKind.LOCAL, 5, "Error", 2),
            (BodyEventKind.LOCAL, 7, "nested"),
            (BodyEventKind.LOCAL, 9, "Nested"),
            (BodyEventKind.LOCAL, 12, "Kcapture"),
        )
        for expected_binding in expected:
            kind, line, name, *rest = expected_binding
            occurrence = rest[0] if rest else 1
            self.assertIn(
                (kind, token_span(snapshot, line, name, occurrence=occurrence)),
                event_pairs,
            )

        self.assertNotIn("hidden", {event.text for event in events})
        self.assertIn(
            (BodyEventKind.TYPE, token_span(snapshot, 5, "Error")),
            event_pairs,
        )

    def test_stdlib_callable_scope_excludes_external_and_comprehension_binders(
        self,
    ) -> None:
        raw = (
            b"def enclosing():\n"
            b"    outer = 0\n"
            b"    def run(items, subject):\n"
            b"        global shared\n"
            b"        nonlocal outer\n"
            b"        shared = 1\n"
            b"        outer = 2\n"
            b"        local = 3\n"
            b"        values = [transform(item) for item in items]\n"
            b"        match subject:\n"
            b'            case ("a", choice) | ("b", choice):\n'
            b"                visible(choice)\n"
            b"        return local\n"
        )
        snapshot = source(Language.PYTHON, file="scope.py", raw=raw)
        enclosing = ast.parse(snapshot.text).body[0]
        callable_node = enclosing.body[1]
        events = ast_body_events(snapshot, callable_node)
        event_pairs = {(event.kind, event.span) for event in events}

        for line, name in ((6, "shared"), (7, "outer")):
            span = token_span(snapshot, line, name)
            self.assertIn((BodyEventKind.NAME, span), event_pairs)
            self.assertNotIn((BodyEventKind.LOCAL, span), event_pairs)
        for line, name in ((8, "local"), (9, "values")):
            span = token_span(snapshot, line, name)
            self.assertIn((BodyEventKind.LOCAL, span), event_pairs)
        for occurrence in (1, 2):
            span = token_span(snapshot, 9, "item", occurrence=occurrence)
            self.assertIn((BodyEventKind.NAME, span), event_pairs)
            self.assertNotIn((BodyEventKind.LOCAL, span), event_pairs)
        for occurrence in (1, 2):
            span = token_span(snapshot, 11, "choice", occurrence=occurrence)
            self.assertIn((BodyEventKind.LOCAL, span), event_pairs)

    def test_tree_sitter_spans_and_call_events_share_utf8_byte_coordinates(
        self,
    ) -> None:
        raw = b'class A { void run() { String x = "\xc5\xbc"; target(); } }\n'
        snapshot = source(Language.JAVA, file="A.java", raw=raw)
        parser = ParserRegistry().parser_for(Language.JAVA)
        self.assertIsNotNone(parser)
        tree = parser.parse(snapshot.raw)
        method = ast_collect(tree.root_node, ("method_declaration",))[0]
        call = ast_collect(method, ("method_invocation",))[0]

        span = node_span(snapshot, call)
        events = body_events(snapshot, method)
        validate_body_events(events)

        line = snapshot.text.splitlines()[0]
        character_column = line.index("target")
        self.assertEqual(span.start_column, character_column + 1)
        self.assertIn(
            (BodyEventKind.CALL, span),
            {(event.kind, event.span) for event in events},
        )

    def test_interpolated_strings_keep_one_literal_and_embedded_facts(self) -> None:
        cases = (
            (
                Language.TYPESCRIPT,
                (
                    b"function run(value: string) {\n"
                    b"  const text = `prefix ${target(value)} suffix`;\n"
                    b"}\n"
                ),
                ("function_declaration",),
                "template_string",
            ),
            (
                Language.KOTLIN,
                (
                    b"fun run(value: String) {\n"
                    b'  val text = "prefix ${target(value)} suffix"\n'
                    b"}\n"
                ),
                ("function_declaration",),
                "string_literal",
            ),
        )
        for language, raw, callable_kinds, string_kind in cases:
            with self.subTest(language=language):
                snapshot, tree, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                string_node = ast_collect(tree.root_node, (string_kind,))[0]
                target_call = next(
                    node
                    for node in ast_collect(tree.root_node, ("call_expression",))
                    if b"target" in node.text
                )
                target_name = next(
                    node
                    for node in ast_collect(target_call, ("identifier",))
                    if node.text == b"target"
                )

                literals = [
                    event for event in events if event.kind is BodyEventKind.LITERAL
                ]
                self.assertEqual(
                    [(event.text, event.span) for event in literals],
                    [("<string>", node_span(snapshot, string_node))],
                )
                event_pairs = {(event.kind, event.span) for event in events}
                self.assertIn(
                    (BodyEventKind.CALL, node_span(snapshot, target_call)),
                    event_pairs,
                )
                self.assertIn(
                    (BodyEventKind.NAME, node_span(snapshot, target_name)),
                    event_pairs,
                )

    def test_tree_sitter_callable_shapes_bind_params_and_locals_exactly(self) -> None:
        cases = (
            (
                Language.KOTLIN,
                (
                    b"fun run(param: Int) {\n"
                    b"  val local = param\n"
                    b"  fun nested() { hidden() }\n"
                    b"  target(local)\n"
                    b"}\n"
                ),
                ("function_declaration",),
                1,
                2,
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"func run(param int) {\n"
                    b"  local := param\n"
                    b"  nested := func() { hidden() }\n"
                    b"  target(local)\n"
                    b"}\n"
                ),
                ("function_declaration",),
                2,
                3,
            ),
            (
                Language.RUST,
                (
                    b"fn run(param: i32) {\n"
                    b"  let local = param;\n"
                    b"  let nested = || hidden();\n"
                    b"  target(local);\n"
                    b"}\n"
                ),
                ("function_item",),
                1,
                2,
            ),
        )
        for language, raw, callable_kinds, parameter_line, local_line in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                parameter_span = token_span(snapshot, parameter_line, "param")
                local_span = token_span(snapshot, local_line, "local")
                event_pairs = {(event.kind, event.span) for event in events}

                self.assertIn((BodyEventKind.PARAM, parameter_span), event_pairs)
                self.assertIn((BodyEventKind.LOCAL, local_span), event_pairs)
                self.assertNotIn((BodyEventKind.NAME, parameter_span), event_pairs)
                self.assertNotIn((BodyEventKind.NAME, local_span), event_pairs)
                self.assertIn(
                    "target",
                    {
                        event.text
                        for event in events
                        if event.kind is BodyEventKind.CALL
                    },
                )
                self.assertNotIn("hidden", {event.text for event in events})

    def test_tree_sitter_extended_parameter_roles_use_exact_identifier_spans(
        self,
    ) -> None:
        cases = (
            (
                Language.JAVA,
                (
                    b"class Probe { void run() {\n"
                    b"  Factory action = (left, right) -> visible(left, right);\n"
                    b"} }\n"
                ),
                ("lambda_expression",),
                ((2, "left", 1), (2, "right", 1)),
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"type Probe struct{}\n"
                    b"func (first, second *Probe) run(left, right int) "
                    b"(value int, err error) {\n"
                    b"  visible(first, second, left, right, value, err)\n"
                    b"  return\n"
                    b"}\n"
                ),
                ("method_declaration",),
                (
                    (3, "first", 1),
                    (3, "second", 1),
                    (3, "left", 1),
                    (3, "right", 1),
                    (3, "value", 1),
                    (3, "err", 1),
                ),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run(params int[] rest) {\n"
                    b"  System.Func<int, int> action = value => visible(value);\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                ((1, "rest", 1),),
            ),
            (
                Language.CSHARP,
                b"class Probe { void run() { System.Func<int, int> action = value => visible(value); } }\n",
                ("lambda_expression",),
                ((1, "value", 1),),
            ),
            (
                Language.JAVA,
                b"class Probe { void run(Probe this, int value) { visible(value); } }\n",
                ("method_declaration",),
                ((1, "this", 1), (1, "value", 1)),
            ),
            (
                Language.RUST,
                b"fn run() { let action = |left, right| visible(left, right); }\n",
                ("closure_expression",),
                ((1, "left", 1), (1, "right", 1)),
            ),
            (
                Language.RUST,
                (
                    b"fn run() { let action = |(left, right), mut value| "
                    b"visible(left, right, value); }\n"
                ),
                ("closure_expression",),
                ((1, "left", 1), (1, "right", 1), (1, "value", 1)),
            ),
            (
                Language.RUST,
                b"impl Probe { fn run(self: Self, value: i32) { visible(self, value); } }\n",
                ("function_item",),
                ((1, "self", 1), (1, "value", 1)),
            ),
            (
                Language.RUST,
                b"impl Probe { fn run(&self, value: i32) { visible(self, value); } }\n",
                ("function_item",),
                ((1, "self", 1), (1, "value", 1)),
            ),
            (
                Language.RUST,
                b"impl Probe { fn run(&mut self, value: i32) { visible(self, value); } }\n",
                ("function_item",),
                ((1, "self", 1), (1, "value", 1)),
            ),
        )
        for language, raw, callable_kinds, parameters in cases:
            with self.subTest(language=language, raw=raw):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                event_pairs = {(event.kind, event.span) for event in events}
                for line, name, occurrence in parameters:
                    span = token_span(
                        snapshot,
                        line,
                        name,
                        occurrence=occurrence,
                    )
                    self.assertIn((BodyEventKind.PARAM, span), event_pairs)
                    self.assertNotIn((BodyEventKind.NAME, span), event_pairs)
                    self.assertNotIn((BodyEventKind.KEYWORD, span), event_pairs)

    def test_tree_sitter_declaration_bindings_exclude_initializer_uses(self) -> None:
        cases = (
            (Language.C, b"int run(int param) {\n  int local = param;\n}\n"),
            (Language.CPP, b"int run(int param) {\n  int local = param;\n}\n"),
        )
        for language, raw in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    ("function_definition",),
                )
                local_span = token_span(snapshot, 2, "local")
                initializer_span = token_span(snapshot, 2, "param")
                event_pairs = {(event.kind, event.span) for event in events}

                self.assertIn((BodyEventKind.LOCAL, local_span), event_pairs)
                self.assertNotIn((BodyEventKind.NAME, local_span), event_pairs)
                self.assertIn((BodyEventKind.NAME, initializer_span), event_pairs)
                self.assertNotIn((BodyEventKind.LOCAL, initializer_span), event_pairs)

    def test_lua_direct_parameters_and_local_lists_are_bindings(self) -> None:
        raw = (
            b"function run(first, second)\n"
            b"  local local_one, local_two = first, second\n"
            b"  target(local_one, local_two)\n"
            b"end\n"
        )
        snapshot, _, _, events = tree_body_fixture(
            self,
            Language.LUA,
            raw,
            ("function_declaration",),
        )
        event_pairs = {(event.kind, event.span) for event in events}

        for name, line in (
            ("first", 1),
            ("second", 1),
            ("local_one", 2),
            ("local_two", 2),
        ):
            expected_kind = BodyEventKind.PARAM if line == 1 else BodyEventKind.LOCAL
            span = token_span(snapshot, line, name)
            self.assertIn((expected_kind, span), event_pairs)
            self.assertNotIn((BodyEventKind.NAME, span), event_pairs)

    def test_tree_sitter_nested_callable_shape_matrix_is_owner_scoped(self) -> None:
        cases = (
            (
                Language.JAVA,
                (
                    b"class Probe { void run() {\n"
                    b"  Runnable nested = () -> hidden();\n"
                    b"  record Local(int value) { Local { hidden(); } }\n"
                    b"  visible();\n"
                    b"} }\n"
                ),
                ("method_declaration",),
            ),
            (
                Language.TYPESCRIPT,
                (
                    b"function run() {\n"
                    b"  function* nested() { hidden(); }\n"
                    b"  const generated = function* () { hidden(); };\n"
                    b"  visible();\n"
                    b"}\n"
                ),
                ("function_declaration",),
            ),
            (
                Language.KOTLIN,
                (b"fun run() {\n  val nested = { hidden() }\n  visible()\n}\n"),
                ("function_declaration",),
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"func run() {\n"
                    b"  nested := func() { hidden() }\n"
                    b"  visible()\n"
                    b"}\n"
                ),
                ("function_declaration",),
            ),
            (
                Language.RUST,
                (b"fn run() {\n  let nested = || hidden();\n  visible();\n}\n"),
                ("function_item",),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run() {\n"
                    b"  void nested() { hidden(); }\n"
                    b"  System.Action action = delegate { hidden(); };\n"
                    b"  visible();\n"
                    b"} }\n"
                ),
                ("method_declaration",),
            ),
            (
                Language.CPP,
                (
                    b"void run() {\n"
                    b"  auto nested = []() { hidden(); };\n"
                    b"  visible();\n"
                    b"}\n"
                ),
                ("function_definition",),
            ),
            (
                Language.LUA,
                (
                    b"function run()\n"
                    b"  local nested = function() hidden() end\n"
                    b"  visible()\n"
                    b"end\n"
                ),
                ("function_declaration",),
            ),
        )
        for language, raw, callable_kinds in cases:
            with self.subTest(language=language):
                _, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                calls = {
                    event.text for event in events if event.kind is BodyEventKind.CALL
                }

                self.assertIn("visible", calls)
                self.assertNotIn("hidden", calls)

    def test_callable_metadata_covers_installed_grammar_shapes(self) -> None:
        expected = {
            Language.JAVA: {
                "compact_constructor_declaration",
                "constructor_declaration",
                "lambda_expression",
                "method_declaration",
            },
            Language.TYPESCRIPT: {
                "arrow_function",
                "function_declaration",
                "function_expression",
                "generator_function",
                "generator_function_declaration",
                "method_definition",
            },
            Language.KOTLIN: {
                "anonymous_function",
                "function_declaration",
                "getter",
                "lambda_literal",
                "secondary_constructor",
                "setter",
            },
            Language.GO: {
                "func_literal",
                "function_declaration",
                "method_declaration",
            },
            Language.RUST: {"closure_expression", "function_item"},
            Language.CSHARP: {
                "accessor_declaration",
                "anonymous_method_expression",
                "constructor_declaration",
                "conversion_operator_declaration",
                "destructor_declaration",
                "indexer_declaration",
                "lambda_expression",
                "local_function_statement",
                "method_declaration",
                "operator_declaration",
            },
            Language.C: {"function_definition"},
            Language.CPP: {"function_definition", "lambda_expression"},
            Language.LUA: {"function_declaration", "function_definition"},
            Language.HTML: set(),
        }
        metadata = getattr(
            treesitter_runtime,
            "_CALLABLE_KINDS_BY_LANGUAGE",
            {},
        )

        for language, required in expected.items():
            with self.subTest(language=language):
                self.assertTrue(required.issubset(metadata.get(language, frozenset())))

    def test_tree_sitter_binding_shape_matrix_uses_only_binder_roots(self) -> None:
        cases = (
            (
                Language.JAVA,
                (
                    b"class Probe { void run(Object value, String... rest) {\n"
                    b"  if (value instanceof Thing found) visible(found);\n"
                    b"  if (value instanceof Point(int first_component, "
                    b"int second_component)) visible(first_component, second_component);\n"
                    b"  try { visible(); } catch (Exception error) { visible(error); }\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                (
                    (BodyEventKind.PARAM, 1, "value", 1),
                    (BodyEventKind.PARAM, 1, "rest", 1),
                    (BodyEventKind.LOCAL, 2, "found", 1),
                    (BodyEventKind.LOCAL, 3, "first_component", 1),
                    (BodyEventKind.LOCAL, 3, "second_component", 1),
                    (BodyEventKind.LOCAL, 4, "error", 1),
                ),
                (
                    (2, "value", 1),
                    (2, "found", 2),
                    (3, "value", 1),
                    (4, "error", 2),
                ),
            ),
            (
                Language.TYPESCRIPT,
                (
                    b"function run({left: renamed, short, fallback = init}: Input, "
                    b"...rest: Input[]) {\n"
                    b"  const [first, second] = source;\n"
                    b"  try {} catch ({message}) {\n"
                    b"    visible(renamed, short, first, second, message, source);\n"
                    b"  }\n"
                    b"}\n"
                ),
                ("function_declaration",),
                (
                    (BodyEventKind.PARAM, 1, "renamed", 1),
                    (BodyEventKind.PARAM, 1, "short", 1),
                    (BodyEventKind.PARAM, 1, "fallback", 1),
                    (BodyEventKind.PARAM, 1, "rest", 1),
                    (BodyEventKind.LOCAL, 2, "first", 1),
                    (BodyEventKind.LOCAL, 2, "second", 1),
                    (BodyEventKind.LOCAL, 3, "message", 1),
                ),
                ((1, "left", 1), (1, "init", 1), (2, "source", 1)),
            ),
            (
                Language.TYPESCRIPT,
                b"const run = bare => { visible(bare); };\n",
                ("arrow_function",),
                ((BodyEventKind.PARAM, 1, "bare", 1),),
                (),
            ),
            (
                Language.C,
                (
                    b"void run(int bound, int values[bound], int extent) {\n"
                    b"  int local[extent];\n"
                    b"  visible(values, local, bound, extent);\n"
                    b"}\n"
                ),
                ("function_definition",),
                (
                    (BodyEventKind.PARAM, 1, "bound", 1),
                    (BodyEventKind.PARAM, 1, "values", 1),
                    (BodyEventKind.PARAM, 1, "extent", 1),
                    (BodyEventKind.LOCAL, 2, "local", 1),
                ),
                ((1, "bound", 2), (2, "extent", 1)),
            ),
            (
                Language.C,
                (
                    b"void run(void (*callback)(int callback_signature)) {\n"
                    b"  int (*factory)(int signature_name) = source;\n"
                    b"  visible(callback, factory);\n"
                    b"}\n"
                ),
                ("function_definition",),
                (
                    (BodyEventKind.PARAM, 1, "callback", 1),
                    (BodyEventKind.LOCAL, 2, "factory", 1),
                ),
                (
                    (1, "callback_signature", 1),
                    (2, "signature_name", 1),
                    (2, "source", 1),
                ),
            ),
            (
                Language.CPP,
                (
                    b"void run(int bound, int values[bound], int extent) {\n"
                    b"  int local[extent];\n"
                    b"  try { visible(); } catch (const Error& error) { visible(error); }\n"
                    b"}\n"
                ),
                ("function_definition",),
                (
                    (BodyEventKind.PARAM, 1, "bound", 1),
                    (BodyEventKind.PARAM, 1, "values", 1),
                    (BodyEventKind.PARAM, 1, "extent", 1),
                    (BodyEventKind.LOCAL, 2, "local", 1),
                    (BodyEventKind.LOCAL, 3, "error", 1),
                ),
                (
                    (1, "bound", 2),
                    (2, "extent", 1),
                    (3, "error", 2),
                ),
            ),
            (
                Language.CPP,
                (
                    b"void run(void (*callback)(int callback_signature)) {\n"
                    b"  int (*factory)(int signature_name) = source;\n"
                    b"  visible(callback, factory);\n"
                    b"}\n"
                ),
                ("function_definition",),
                (
                    (BodyEventKind.PARAM, 1, "callback", 1),
                    (BodyEventKind.LOCAL, 2, "factory", 1),
                ),
                (
                    (1, "callback_signature", 1),
                    (2, "signature_name", 1),
                    (2, "source", 1),
                ),
            ),
            (
                Language.CPP,
                (
                    b"void run(int value = fallback) {\n"
                    b"  auto [first, second] = pair();\n"
                    b"  visible(value, first, second);\n"
                    b"}\n"
                ),
                ("function_definition",),
                (
                    (BodyEventKind.PARAM, 1, "value", 1),
                    (BodyEventKind.LOCAL, 2, "first", 1),
                    (BodyEventKind.LOCAL, 2, "second", 1),
                ),
                ((1, "fallback", 1), (2, "pair", 1)),
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"func run(items []int) {\n"
                    b"  var first, second = source(), source()\n"
                    b"  third, fourth := pair()\n"
                    b"  for key, value := range items {\n"
                    b"    visible(first, second, third, fourth, key, value)\n"
                    b"  }\n"
                    b"}\n"
                ),
                ("function_declaration",),
                (
                    (BodyEventKind.PARAM, 2, "items", 1),
                    (BodyEventKind.LOCAL, 3, "first", 1),
                    (BodyEventKind.LOCAL, 3, "second", 1),
                    (BodyEventKind.LOCAL, 4, "third", 1),
                    (BodyEventKind.LOCAL, 4, "fourth", 1),
                    (BodyEventKind.LOCAL, 5, "key", 1),
                    (BodyEventKind.LOCAL, 5, "value", 1),
                ),
                ((5, "items", 1),),
            ),
            (
                Language.RUST,
                (
                    b"fn run(items: Vec<Option<i32>>) {\n"
                    b"  let (first, second) = pair();\n"
                    b"  for (index, value) in items.iter() { visible(index, value); }\n"
                    b"  if let Some(found) = maybe() { visible(found); }\n"
                    b"  match maybe() { Some(matched) => visible(matched), _ => {} }\n"
                    b"}\n"
                ),
                ("function_item",),
                (
                    (BodyEventKind.PARAM, 1, "items", 1),
                    (BodyEventKind.LOCAL, 2, "first", 1),
                    (BodyEventKind.LOCAL, 2, "second", 1),
                    (BodyEventKind.LOCAL, 3, "index", 1),
                    (BodyEventKind.LOCAL, 3, "value", 1),
                    (BodyEventKind.LOCAL, 4, "found", 1),
                    (BodyEventKind.LOCAL, 5, "matched", 1),
                ),
                ((2, "pair", 1),),
            ),
            (
                Language.RUST,
                (
                    b"struct Point { first: i32, second: i32 }\n"
                    b"fn run(point: Point) {\n"
                    b"  let Point { first, second: renamed } = point;\n"
                    b"  visible(first, renamed);\n"
                    b"}\n"
                ),
                ("function_item",),
                (
                    (BodyEventKind.PARAM, 2, "point", 1),
                    (BodyEventKind.LOCAL, 3, "first", 1),
                    (BodyEventKind.LOCAL, 3, "renamed", 1),
                ),
                ((3, "second", 1), (3, "point", 1)),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run() {\n"
                    b"  try { visible(); } catch (System.Exception error) { visible(error); }\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                ((BodyEventKind.LOCAL, 2, "error", 1),),
                ((2, "error", 2),),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run(object items) {\n"
                    b"  foreach (var item in items) { visible(item); }\n"
                    b"  if (source(out var found)) visible(found);\n"
                    b"  (int first, int second) = pair();\n"
                    b"  visible(first, second);\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                (
                    (BodyEventKind.PARAM, 1, "items", 1),
                    (BodyEventKind.LOCAL, 2, "item", 1),
                    (BodyEventKind.LOCAL, 3, "found", 1),
                    (BodyEventKind.LOCAL, 4, "first", 1),
                    (BodyEventKind.LOCAL, 4, "second", 1),
                ),
                (
                    (2, "items", 1),
                    (3, "source", 1),
                    (4, "pair", 1),
                ),
            ),
            (
                Language.KOTLIN,
                (
                    b"fun run() {\n"
                    b"  try { visible() } catch (error: Exception) { visible(error) }\n"
                    b"}\n"
                ),
                ("function_declaration",),
                ((BodyEventKind.LOCAL, 2, "error", 1),),
                ((2, "error", 2),),
            ),
            (
                Language.KOTLIN,
                (
                    b"fun run() {\n"
                    b"  val (first, second) = pair()\n"
                    b"  visible(first, second)\n"
                    b"}\n"
                ),
                ("function_declaration",),
                (
                    (BodyEventKind.LOCAL, 2, "first", 1),
                    (BodyEventKind.LOCAL, 2, "second", 1),
                ),
                ((2, "pair", 1),),
            ),
            (
                Language.KOTLIN,
                (
                    b"val action = { first, second ->\n"
                    b"  visible(first)\n"
                    b"  visible(second)\n"
                    b"}\n"
                ),
                ("lambda_literal",),
                (
                    (BodyEventKind.PARAM, 1, "first", 1),
                    (BodyEventKind.PARAM, 1, "second", 1),
                ),
                (),
            ),
            (
                Language.KOTLIN,
                (
                    b"class Probe { var item: Int = 0\n"
                    b"  set(next) {\n"
                    b"    visible(next)\n"
                    b"    field = next\n"
                    b"  }\n"
                    b"}\n"
                ),
                ("setter",),
                ((BodyEventKind.PARAM, 2, "next", 1),),
                ((4, "next", 1),),
            ),
            (
                Language.LUA,
                (
                    b"function run(items)\n"
                    b"  for index = start, finish do visible(index) end\n"
                    b"  for key, value in iterate(items) do visible(key, value) end\n"
                    b"end\n"
                ),
                ("function_declaration",),
                (
                    (BodyEventKind.PARAM, 1, "items", 1),
                    (BodyEventKind.LOCAL, 2, "index", 1),
                    (BodyEventKind.LOCAL, 3, "key", 1),
                    (BodyEventKind.LOCAL, 3, "value", 1),
                ),
                (
                    (2, "start", 1),
                    (2, "finish", 1),
                    (3, "iterate", 1),
                    (3, "items", 1),
                ),
            ),
        )
        for language, raw, callable_kinds, bindings, names in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                event_pairs = {(event.kind, event.span) for event in events}

                for kind, line, name, occurrence in bindings:
                    span = token_span(
                        snapshot,
                        line,
                        name,
                        occurrence=occurrence,
                    )
                    self.assertIn((kind, span), event_pairs)
                    self.assertNotIn((BodyEventKind.NAME, span), event_pairs)
                for line, name, occurrence in names:
                    span = token_span(
                        snapshot,
                        line,
                        name,
                        occurrence=occurrence,
                    )
                    self.assertIn((BodyEventKind.NAME, span), event_pairs)
                    self.assertNotIn((BodyEventKind.PARAM, span), event_pairs)
                    self.assertNotIn((BodyEventKind.LOCAL, span), event_pairs)
                if language is Language.RUST and b"Some(" in raw:
                    for line in (4, 5):
                        span = token_span(snapshot, line, "Some")
                        self.assertIn((BodyEventKind.TYPE, span), event_pairs)
                        self.assertNotIn((BodyEventKind.PARAM, span), event_pairs)
                        self.assertNotIn((BodyEventKind.LOCAL, span), event_pairs)

    def test_loop_binding_roles_distinguish_declarations_from_uses(self) -> None:
        cases = (
            (
                Language.TYPESCRIPT,
                (
                    b"function run(existing: Item, items: Item[]) {\n"
                    b"  for (existing of items) { visible(existing); }\n"
                    b"  for (const created of items) { visible(created); }\n"
                    b"}\n"
                ),
                ("function_declaration",),
                ((BodyEventKind.NAME, 2, "existing"),),
                ((BodyEventKind.LOCAL, 3, "created"),),
            ),
            (
                Language.CPP,
                (
                    b"void run(Items items) {\n"
                    b"  for (auto value : items) { visible(value); }\n"
                    b"}\n"
                ),
                ("function_definition",),
                ((BodyEventKind.NAME, 2, "items"),),
                ((BodyEventKind.LOCAL, 2, "value"),),
            ),
        )
        for language, raw, callable_kinds, uses, declarations in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                event_pairs = {(event.kind, event.span) for event in events}
                for kind, line, name in (*uses, *declarations):
                    span = token_span(snapshot, line, name)
                    self.assertIn((kind, span), event_pairs)
                    opposite = (
                        BodyEventKind.LOCAL
                        if kind is BodyEventKind.NAME
                        else BodyEventKind.NAME
                    )
                    self.assertNotIn((opposite, span), event_pairs)

    def test_tree_sitter_literal_shape_matrix_is_explicit_and_exact(self) -> None:
        cases = (
            (
                Language.JAVA,
                b"class Probe { void run() { consume('x', 2); } }\n",
                ("method_declaration",),
                (
                    ("character_literal", "<string>"),
                    ("decimal_integer_literal", "<number>"),
                ),
            ),
            (
                Language.TYPESCRIPT,
                b'function run() { consume("x", 2); }\n',
                ("function_declaration",),
                (("string", "<string>"), ("number", "<number>")),
            ),
            (
                Language.KOTLIN,
                b"fun run() { consume('x', 2.0) }\n",
                ("function_declaration",),
                (("character_literal", "<string>"), ("float_literal", "<number>")),
            ),
            (
                Language.GO,
                b"package probe\nfunc run() { consume('x', 2i) }\n",
                ("function_declaration",),
                (("rune_literal", "<string>"), ("imaginary_literal", "<number>")),
            ),
            (
                Language.RUST,
                b"fn run() { consume('x', 2); }\n",
                ("function_item",),
                (("char_literal", "<string>"), ("integer_literal", "<number>")),
            ),
            (
                Language.CSHARP,
                b'class Probe { void run() { consume(@"x", 2.0); } }\n',
                ("method_declaration",),
                (("verbatim_string_literal", "<string>"), ("real_literal", "<number>")),
            ),
            (
                Language.C,
                b"void run() { consume('x', 2); }\n",
                ("function_definition",),
                (("char_literal", "<string>"), ("number_literal", "<number>")),
            ),
            (
                Language.CPP,
                b'void run() { consume(R"(x)", 2); }\n',
                ("function_definition",),
                (("raw_string_literal", "<string>"), ("number_literal", "<number>")),
            ),
            (
                Language.LUA,
                b'function run() consume("x", 2) end\n',
                ("function_declaration",),
                (("string", "<string>"), ("number", "<number>")),
            ),
        )
        for language, raw, callable_kinds, expected in cases:
            with self.subTest(language=language):
                snapshot, tree, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                literal_events = tuple(
                    event for event in events if event.kind is BodyEventKind.LITERAL
                )
                expected_events = []
                for node_kind, text in expected:
                    node = ast_collect(tree.root_node, (node_kind,))[0]
                    expected_events.append((text, node_span(snapshot, node)))

                self.assertEqual(
                    tuple((event.text, event.span) for event in literal_events),
                    tuple(expected_events),
                )

    def test_kotlin_multiline_and_keyword_literals_are_normalized(self) -> None:
        raw = b'fun run() { consume("""text""", true, false, null) }\n'
        snapshot, tree, _, events = tree_body_fixture(
            self,
            Language.KOTLIN,
            raw,
            ("function_declaration",),
        )
        multiline = ast_collect(tree.root_node, ("multiline_string_literal",))[0]
        expected = {
            ("<string>", node_span(snapshot, multiline)),
            ("<bool>", token_span(snapshot, 1, "true")),
            ("<bool>", token_span(snapshot, 1, "false")),
            ("<null>", token_span(snapshot, 1, "null")),
        }

        self.assertEqual(
            {
                (event.text, event.span)
                for event in events
                if event.kind is BodyEventKind.LITERAL
            },
            expected,
        )

    def test_reference_facts_join_member_and_nested_type_events_exactly(self) -> None:
        python_raw = b"def run(obj):\n    return obj.member\n"
        python_source = source(
            Language.PYTHON,
            file="member.py",
            raw=python_raw,
        )
        python_callable = ast.parse(python_source.text).body[0]
        python_owner = symbol_id(
            python_source,
            (),
            SymbolKind.FUNCTION,
            "run",
            ("obj",),
        )
        python_member_span = token_span(python_source, 2, "member")
        python_events = ast_body_events(python_source, python_callable)
        python_ir = FileIR(
            python_source,
            references=(
                reference(
                    python_owner,
                    python_member_span,
                    "member",
                    "obj",
                    ReferenceKind.NAME,
                    context=ReferenceContext.CODE,
                    confidence=ReferenceConfidence.DEFINITE,
                ),
            ),
            bodies=(
                BodyIR(
                    python_owner,
                    ast_span(python_source, python_callable),
                    python_events,
                ),
            ),
        )

        assert_body_fact_events(self, python_ir)
        self.assertIn(
            (BodyEventKind.MEMBER, python_member_span),
            {(event.kind, event.span) for event in python_events},
        )
        self.assertEqual(len(python_events), len(set(python_events)))

        java_raw = (
            b"class Probe { void run(Foo<Bar> value, Thing obj) {\n"
            b"  consume(obj.member);\n"
            b"} }\n"
        )
        java_source, java_tree, java_callable, java_events = tree_body_fixture(
            self,
            Language.JAVA,
            java_raw,
            ("method_declaration",),
        )
        java_owner = symbol_id(
            java_source,
            ("Probe",),
            SymbolKind.METHOD,
            "run",
            ("Foo<Bar>", "Thing"),
        )
        type_nodes = {
            node.text.decode(): node
            for node in ast_collect(java_tree.root_node, ("type_identifier",))
            if node.text in {b"Foo", b"Bar"}
        }
        member_node = next(
            node
            for node in ast_collect(java_tree.root_node, ("identifier",))
            if node.text == b"member"
        )
        java_references = tuple(
            reference(
                java_owner,
                node_span(java_source, node),
                name,
                None,
                ReferenceKind.TYPE,
                context=ReferenceContext.TYPE,
                confidence=ReferenceConfidence.DEFINITE,
            )
            for name, node in sorted(type_nodes.items())
        ) + (
            reference(
                java_owner,
                node_span(java_source, member_node),
                "member",
                "obj",
                ReferenceKind.NAME,
                context=ReferenceContext.CODE,
                confidence=ReferenceConfidence.DEFINITE,
            ),
        )
        java_ir = FileIR(
            java_source,
            references=java_references,
            bodies=(
                BodyIR(
                    java_owner,
                    node_span(java_source, java_callable),
                    java_events,
                ),
            ),
        )

        assert_body_fact_events(self, java_ir)
        member_span = node_span(java_source, member_node)
        java_event_pairs = {(event.kind, event.span) for event in java_events}
        self.assertIn((BodyEventKind.MEMBER, member_span), java_event_pairs)
        self.assertEqual(len(java_events), len(set(java_events)))

    def test_tree_sitter_member_shape_matrix_emits_member_and_name(self) -> None:
        cases = (
            (
                Language.KOTLIN,
                b"fun run(obj: Thing) { consume(obj.member) }\n",
                ("function_declaration",),
                ((1, "member"),),
            ),
            (
                Language.LUA,
                b"function run(obj) consume(obj.member); obj:method() end\n",
                ("function_declaration",),
                ((1, "member"), (1, "method")),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run(Box<Item> obj) {\n"
                    b"  consume(obj.member);\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                ((2, "member"),),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run(Box<Item> obj) {\n"
                    b"  obj.Method<Item>();\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                ((2, "Method"),),
            ),
            (
                Language.CPP,
                (b"void run(Box<Item> obj) {\n  consume(obj.member);\n}\n"),
                ("function_definition",),
                ((2, "member"),),
            ),
            (
                Language.CPP,
                (b"void run(Box<Item> obj) {\n  obj.template method<Item>();\n}\n"),
                ("function_definition",),
                ((2, "method"),),
            ),
        )
        for language, raw, callable_kinds, members in cases:
            with self.subTest(language=language):
                snapshot, _, callable_node, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                owner = symbol_id(
                    snapshot,
                    (),
                    SymbolKind.FUNCTION,
                    "run",
                )
                member_spans = tuple(
                    (name, token_span(snapshot, line, name)) for line, name in members
                )
                file_ir = FileIR(
                    snapshot,
                    references=tuple(
                        reference(
                            owner,
                            span,
                            name,
                            "obj",
                            ReferenceKind.NAME,
                            context=ReferenceContext.CODE,
                            confidence=ReferenceConfidence.DEFINITE,
                        )
                        for name, span in member_spans
                    ),
                    bodies=(
                        BodyIR(
                            owner,
                            node_span(snapshot, callable_node),
                            events,
                        ),
                    ),
                )

                assert_body_fact_events(self, file_ir)
                event_pairs = {(event.kind, event.span) for event in events}
                for _, span in member_spans:
                    self.assertIn((BodyEventKind.MEMBER, span), event_pairs)
                    self.assertIn((BodyEventKind.NAME, span), event_pairs)
                self.assertEqual(len(events), len(set(events)))

    def test_qualified_call_members_do_not_reclassify_unqualified_calls(self) -> None:
        cases = (
            (
                Language.JAVA,
                b"class Probe { void run(Obj obj) { obj.method(); plain(); } }\n",
                ("method_declaration",),
                "method",
                "plain",
            ),
            (
                Language.CPP,
                b"void run() { Type::method(); plain(); }\n",
                ("function_definition",),
                "method",
                "plain",
            ),
            (
                Language.CPP,
                b"void run() { Type::method<Item>(); plain<Item>(); }\n",
                ("function_definition",),
                "method",
                "plain",
            ),
            (
                Language.RUST,
                b"fn run() { Type::method(); plain(); }\n",
                ("function_item",),
                "method",
                "plain",
            ),
            (
                Language.RUST,
                b"fn run() { Type::method::<Item>(); plain::<Item>(); }\n",
                ("function_item",),
                "method",
                "plain",
            ),
        )
        for language, raw, callable_kinds, member, plain in cases:
            with self.subTest(language=language):
                snapshot, _, callable_node, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                owner = symbol_id(snapshot, (), SymbolKind.FUNCTION, "run")
                member_span = token_span(snapshot, 1, member)
                plain_span = token_span(snapshot, 1, plain)
                file_ir = FileIR(
                    snapshot,
                    references=(
                        reference(
                            owner,
                            member_span,
                            member,
                            "qualifier",
                            ReferenceKind.NAME,
                            context=ReferenceContext.CODE,
                            confidence=ReferenceConfidence.DEFINITE,
                        ),
                    ),
                    bodies=(
                        BodyIR(
                            owner,
                            node_span(snapshot, callable_node),
                            events,
                        ),
                    ),
                )

                assert_body_fact_events(self, file_ir)
                event_pairs = {(event.kind, event.span) for event in events}
                self.assertIn((BodyEventKind.MEMBER, member_span), event_pairs)
                self.assertIn((BodyEventKind.NAME, member_span), event_pairs)
                self.assertIn((BodyEventKind.NAME, plain_span), event_pairs)
                self.assertNotIn((BodyEventKind.MEMBER, plain_span), event_pairs)

    def test_tree_sitter_type_context_matrix_emits_exact_leaf_spans(self) -> None:
        cases = (
            (
                Language.CSHARP,
                b"class Probe { void run(Box<Item> value) { visible(value); } }\n",
                ("method_declaration",),
                ((1, "Box"), (1, "Item")),
                (),
            ),
            (
                Language.CSHARP,
                b"class Probe { void run() { Generic<Item>(); } }\n",
                ("method_declaration",),
                ((1, "Item"),),
                ((1, "Generic"),),
            ),
            (
                Language.CPP,
                b"void run(Box<Item> value) { visible(value); }\n",
                ("function_definition",),
                ((1, "Box"), (1, "Item")),
                (),
            ),
            (
                Language.KOTLIN,
                b"fun run(value: Map<Key, Value>) { visible(value) }\n",
                ("function_declaration",),
                ((1, "Map"), (1, "Key"), (1, "Value")),
                (),
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"func run(values [extent]Element) { visible(values) }\n"
                ),
                ("function_declaration",),
                ((2, "Element"),),
                ((2, "extent"),),
            ),
        )
        for language, raw, callable_kinds, type_names, value_names in cases:
            with self.subTest(language=language):
                snapshot, _, callable_node, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                owner = symbol_id(
                    snapshot,
                    (),
                    SymbolKind.FUNCTION,
                    "run",
                )
                references = tuple(
                    reference(
                        owner,
                        token_span(snapshot, line, name),
                        name,
                        None,
                        ReferenceKind.TYPE,
                        context=ReferenceContext.TYPE,
                        confidence=ReferenceConfidence.DEFINITE,
                    )
                    for line, name in type_names
                ) + tuple(
                    reference(
                        owner,
                        token_span(snapshot, line, name),
                        name,
                        None,
                        ReferenceKind.NAME,
                        context=ReferenceContext.CODE,
                        confidence=ReferenceConfidence.DEFINITE,
                    )
                    for line, name in value_names
                )
                file_ir = FileIR(
                    snapshot,
                    references=references,
                    bodies=(
                        BodyIR(
                            owner,
                            node_span(snapshot, callable_node),
                            events,
                        ),
                    ),
                )

                assert_body_fact_events(self, file_ir)
                self.assertEqual(len(events), len(set(events)))
                if b"<" in raw:
                    self.assertFalse(
                        any(
                            event.kind is BodyEventKind.OPERATOR
                            and event.text in {"<", ">"}
                            for event in events
                        )
                    )

    def test_const_generic_identifiers_remain_name_facts(self) -> None:
        cases = (
            (
                Language.CPP,
                b"void run(std::array<int, N> values) { visible(values); }\n",
                ("function_definition",),
            ),
            (
                Language.RUST,
                b"fn run(values: Array<i32, N>) { visible(values); }\n",
                ("function_item",),
            ),
        )
        for language, raw, callable_kinds in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                span = token_span(snapshot, 1, "N")

                self.assertIn(
                    (BodyEventKind.NAME, span),
                    {(event.kind, event.span) for event in events},
                )

    def test_nested_type_declarations_are_owner_scoped(self) -> None:
        cases = (
            (
                Language.JAVA,
                (
                    b"class Probe { void run() {\n"
                    b"  class Local { int field = hidden(); }\n"
                    b"  visible();\n"
                    b"} }\n"
                ),
                ("method_declaration",),
            ),
            (
                Language.TYPESCRIPT,
                (
                    b"function run() {\n"
                    b"  class Local { field = hidden(); }\n"
                    b"  visible();\n"
                    b"}\n"
                ),
                ("function_declaration",),
            ),
        )
        for language, raw, callable_kinds in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                calls = {
                    event.text for event in events if event.kind is BodyEventKind.CALL
                }

                self.assertEqual(calls, {"visible"})
                self.assertNotIn(
                    token_span(snapshot, 2, "Local"),
                    {event.span for event in events},
                )

    def test_go_type_and_rust_async_regions_do_not_leak_into_owner(self) -> None:
        cases = (
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"func run() {\n"
                    b"  type Local struct { Field Hidden }\n"
                    b"  visible()\n"
                    b"}\n"
                ),
                ("function_declaration",),
                ((3, "Local"), (3, "Field"), (3, "Hidden")),
            ),
            (
                Language.RUST,
                (b"fn run() {\n  let future = async { hidden(); };\n  visible();\n}\n"),
                ("function_item",),
                (),
            ),
            (
                Language.RUST,
                (
                    b"fn run() {\n"
                    b"  const LOCAL: i32 = hidden();\n"
                    b"  static STATIC: i32 = hidden();\n"
                    b"  mod nested { pub fn inner() { hidden(); } }\n"
                    b"  visible();\n"
                    b"}\n"
                ),
                ("function_item",),
                ((2, "LOCAL"), (3, "STATIC"), (4, "nested")),
            ),
        )
        for language, raw, callable_kinds, excluded_names in cases:
            with self.subTest(language=language):
                snapshot, _, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                calls = {
                    event.text for event in events if event.kind is BodyEventKind.CALL
                }

                self.assertIn("visible", calls)
                self.assertNotIn("hidden", calls)
                event_spans = {event.span for event in events}
                for line, name in excluded_names:
                    self.assertNotIn(token_span(snapshot, line, name), event_spans)

    def test_constructor_initializer_roots_precede_owned_bodies(self) -> None:
        cases = (
            (
                Language.CPP,
                (
                    b"class Probe { int field; public:\n"
                    b"  Probe(): field(initialize([]() { hidden(); return 1; })) "
                    b"{ visible(); }\n"
                    b"};\n"
                ),
                ("function_definition",),
                "call_expression",
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe : Base {\n"
                    b"  Probe() : base(initialize(() => hidden())) { visible(); }\n"
                    b"}\n"
                ),
                ("constructor_declaration",),
                "invocation_expression",
            ),
            (
                Language.KOTLIN,
                (
                    b"class Probe(val value: Int) {\n"
                    b"  constructor(): this(initialize()) { visible() }\n"
                    b"}\n"
                ),
                ("secondary_constructor",),
                "call_expression",
            ),
        )
        for language, raw, callable_kinds, call_kind in cases:
            with self.subTest(language=language):
                snapshot, tree, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                calls = tuple(
                    event for event in events if event.kind is BodyEventKind.CALL
                )

                self.assertEqual(
                    [event.text for event in calls], ["initialize", "visible"]
                )
                self.assertNotIn("hidden", {event.text for event in events})
                nodes = {
                    node.text.split(b"(", 1)[0].decode(): node
                    for node in ast_collect(tree.root_node, (call_kind,))
                    if node.text.startswith((b"initialize(", b"visible("))
                }
                self.assertEqual(
                    [event.span for event in calls],
                    [
                        node_span(snapshot, nodes["initialize"]),
                        node_span(snapshot, nodes["visible"]),
                    ],
                )

    def test_tree_sitter_construction_kinds_use_exact_expression_spans(self) -> None:
        cases = (
            (
                Language.JAVA,
                b"class Probe {\n  Probe() { this(1); }\n  Probe(int value) {}\n}\n",
                ("constructor_declaration",),
                "explicit_constructor_invocation",
            ),
            (
                Language.JAVA,
                b"class Probe { void run() { int[] values = new int[2]; } }\n",
                ("method_declaration",),
                "array_creation_expression",
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"type Item struct { Value int }\n"
                    b"func run() {\n"
                    b"  local := Item{Value: 1}\n"
                    b"}\n"
                ),
                ("function_declaration",),
                "composite_literal",
            ),
            (
                Language.KOTLIN,
                (
                    b"class Widget\n"
                    b"fun run(value: Widget?) {\n"
                    b"  val made = Widget()\n"
                    b"  target(value)\n"
                    b"}\n"
                ),
                ("function_declaration",),
                "call_expression",
            ),
            (
                Language.CSHARP,
                b"class Probe { Probe make() { return new(); } }\n",
                ("method_declaration",),
                "implicit_object_creation_expression",
            ),
            (
                Language.CSHARP,
                b"class Probe { void run() { int[] values = new int[2]; } }\n",
                ("method_declaration",),
                "array_creation_expression",
            ),
            (
                Language.CSHARP,
                b"class Probe { void run() { var values = new[] { 1, 2 }; } }\n",
                ("method_declaration",),
                "implicit_array_creation_expression",
            ),
            (
                Language.CSHARP,
                b"class Probe { void run() { var value = new { Item = 1 }; } }\n",
                ("method_declaration",),
                "anonymous_object_creation_expression",
            ),
            (
                Language.C,
                b"void run() { (struct Item) { 1 }; }\n",
                ("function_definition",),
                "compound_literal_expression",
            ),
            (
                Language.CPP,
                b"void run() { (Item) { 1 }; }\n",
                ("function_definition",),
                "compound_literal_expression",
            ),
            (
                Language.LUA,
                b"function run() local value = { item = 1 } end\n",
                ("function_declaration",),
                "table_constructor",
            ),
            (
                Language.KOTLIN,
                (b"class Probe(val value: Int) {\n  constructor(): this(1) {}\n}\n"),
                ("secondary_constructor",),
                "constructor_delegation_call",
            ),
        )
        for language, raw, callable_kinds, construct_kind in cases:
            with self.subTest(language=language):
                snapshot, tree, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                candidates = ast_collect(tree.root_node, (construct_kind,))
                if language is Language.KOTLIN and construct_kind == "call_expression":
                    candidates = [
                        node for node in candidates if node.text == b"Widget()"
                    ]
                self.assertEqual(len(candidates), 1)
                expected = node_span(snapshot, candidates[0])
                construction_spans = {
                    event.span
                    for event in events
                    if event.kind is BodyEventKind.CONSTRUCT
                }

                self.assertEqual(construction_spans, {expected})

    def test_java_control_keywords_and_catch_binding_use_token_spans(self) -> None:
        raw = (
            b"class Probe { void run(int limit) {\n"
            b"  for (int i = 0; i < limit; i++) {\n"
            b"    try { target(); } catch (Exception error) { recover(error); }\n"
            b"  }\n"
            b"} }\n"
        )
        snapshot, tree, _, events = tree_body_fixture(
            self,
            Language.JAVA,
            raw,
            ("method_declaration",),
        )
        for_node = ast_collect(tree.root_node, ("for",))[0]
        catch_node = ast_collect(tree.root_node, ("catch",))[0]
        catch_parameter = next(
            node
            for node in ast_collect(tree.root_node, ("identifier",))
            if node.text == b"error" and node.start_point.row == 2
        )
        event_pairs = {(event.kind, event.span) for event in events}

        self.assertIn(
            (BodyEventKind.KEYWORD, node_span(snapshot, for_node)),
            event_pairs,
        )
        self.assertIn(
            (BodyEventKind.KEYWORD, node_span(snapshot, catch_node)),
            event_pairs,
        )
        self.assertIn(
            (BodyEventKind.LOCAL, node_span(snapshot, catch_parameter)),
            event_pairs,
        )
        self.assertNotIn(
            (BodyEventKind.NAME, node_span(snapshot, catch_parameter)),
            event_pairs,
        )
        validate_body_events(events)

    def test_structured_control_shape_matrix_is_balanced_and_normalized(self) -> None:
        cases = (
            (
                Language.CPP,
                (
                    b"void run(Items items) {\n"
                    b"  for (auto item : items) { visible(item); }\n"
                    b"}\n"
                ),
                ("function_definition",),
                ("loop",),
                ("loop",),
            ),
            (
                Language.CSHARP,
                (
                    b"class Probe { void run(dynamic items, object gate) {\n"
                    b"  foreach (var item in items) { visible(item); }\n"
                    b"  lock (gate) { visible(gate); }\n"
                    b"  using (var resource = open()) { visible(resource); }\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                ("loop", "with", "with"),
                ("loop", "with", "with"),
            ),
            (
                Language.JAVA,
                (
                    b"class Probe { void run(Resource resource, boolean flag) {\n"
                    b"  try (Resource item = open()) { visible(item); }\n"
                    b"  int chosen = flag ? one() : two();\n"
                    b"} }\n"
                ),
                ("method_declaration",),
                ("try", "if"),
                ("try", "if"),
            ),
            (
                Language.TYPESCRIPT,
                (
                    b"function run(flag: boolean) {\n"
                    b"  const chosen = flag ? one() : two();\n"
                    b"  do { visible(); } while (flag);\n"
                    b"}\n"
                ),
                ("function_declaration",),
                ("if", "loop"),
                ("if", "loop"),
            ),
            (
                Language.GO,
                (
                    b"package probe\n"
                    b"func run(value int) {\n"
                    b"  switch value { case 1: visible() }\n"
                    b"}\n"
                ),
                ("function_declaration",),
                ("match",),
                ("match",),
            ),
            (
                Language.KOTLIN,
                (
                    b"fun run(flag: Boolean) {\n"
                    b"  do { visible() } while (flag)\n"
                    b"  try { visible() } finally { visible() }\n"
                    b"}\n"
                ),
                ("function_declaration",),
                ("loop", "try", "finally"),
                ("loop", "finally", "try"),
            ),
            (
                Language.LUA,
                (
                    b"function run(done)\n"
                    b"  do visible() end\n"
                    b"  repeat visible() until done\n"
                    b"end\n"
                ),
                ("function_declaration",),
                ("loop",),
                ("loop",),
            ),
        )
        for language, raw, callable_kinds, expected_enters, expected_exits in cases:
            with self.subTest(language=language):
                snapshot, tree, _, events = tree_body_fixture(
                    self,
                    language,
                    raw,
                    callable_kinds,
                )
                enters = tuple(
                    event.text
                    for event in events
                    if event.kind is BodyEventKind.CONTROL_ENTER
                )
                exits = tuple(
                    event.text
                    for event in events
                    if event.kind is BodyEventKind.CONTROL_EXIT
                )

                self.assertEqual(enters, expected_enters)
                self.assertEqual(exits, expected_exits)
                if language is Language.LUA:
                    do_statement = ast_collect(tree.root_node, ("do_statement",))[0]
                    self.assertNotIn(
                        node_span(snapshot, do_statement),
                        {
                            event.span
                            for event in events
                            if event.kind
                            in {
                                BodyEventKind.CONTROL_ENTER,
                                BodyEventKind.CONTROL_EXIT,
                            }
                        },
                    )
                validate_body_events(events)

        metadata = getattr(treesitter_runtime, "_CONTROL_KINDS_BY_LANGUAGE", {})
        required = {
            Language.JAVA: {
                "expression_switch_statement": "match",
                "ternary_expression": "if",
                "try_with_resources_statement": "try",
            },
            Language.KOTLIN: {
                "do_while_statement": "loop",
                "finally_block": "finally",
            },
            Language.TYPESCRIPT: {"ternary_expression": "if"},
            Language.GO: {"expression_switch_statement": "match"},
            Language.CSHARP: {
                "foreach_statement": "loop",
                "lock_statement": "with",
                "using_statement": "with",
            },
            Language.CPP: {"for_range_loop": "loop"},
            Language.LUA: {"repeat_statement": "loop"},
        }
        for language, expected in required.items():
            with self.subTest(metadata=language):
                self.assertTrue(expected.items() <= metadata.get(language, {}).items())
        self.assertNotIn("do_statement", metadata.get(Language.LUA, {}))


if __name__ == "__main__":
    unittest.main()
