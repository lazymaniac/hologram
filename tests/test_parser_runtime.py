import ast
import hashlib
import inspect
import subprocess
import sys
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
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
            b"def run(first=alpha(), second=beta(), *, "
            b"third=gamma(), fourth: Kind=delta()):\n"
            b"    body()\n"
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
                (BodyEventKind.PARAM, "third"),
                (BodyEventKind.CALL, "gamma"),
                (BodyEventKind.PARAM, "fourth"),
                (BodyEventKind.TYPE, "Kind"),
                (BodyEventKind.CALL, "delta"),
                (BodyEventKind.CALL, "body"),
            ),
        )
        for name in ("first", "second", "third", "fourth"):
            event = next(
                item
                for item in events
                if item.kind is BodyEventKind.PARAM and item.text == name
            )
            self.assertEqual(event.span, token_span(snapshot, 1, name))

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
            expected_kind = (
                BodyEventKind.PARAM if line == 1 else BodyEventKind.LOCAL
            )
            span = token_span(snapshot, line, name)
            self.assertIn((expected_kind, span), event_pairs)
            self.assertNotIn((BodyEventKind.NAME, span), event_pairs)

    def test_tree_sitter_construction_kinds_use_exact_expression_spans(self) -> None:
        cases = (
            (
                Language.JAVA,
                b"class Probe {\n  Probe() { this(1); }\n  Probe(int value) {}\n}\n",
                ("constructor_declaration",),
                "explicit_constructor_invocation",
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
                if language is Language.KOTLIN:
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


if __name__ == "__main__":
    unittest.main()
