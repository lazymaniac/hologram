import ast
import hashlib
import inspect
import subprocess
import sys
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from hologram.model import (
    BodyEventKind,
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


class _FakeLanguage:
    def __init__(self, capsule: object) -> None:
        self.capsule = capsule


class _FakeParser:
    def __init__(self, language: object) -> None:
        self.language = language


class ParserRuntimeTest(unittest.TestCase):
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

    def test_importing_package_does_not_import_optional_grammars_or_extractors(
        self,
    ) -> None:
        code = """
import sys
import hologram
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
extractors = sorted(
    name for name in sys.modules
    if name.startswith('hologram.parsers.')
    and name.rsplit('.', 1)[-1] not in {'api', 'common', 'treesitter'}
)
if loaded or extractors:
    raise SystemExit(f'eager imports: {loaded!r} {extractors!r}')
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


if __name__ == "__main__":
    unittest.main()
