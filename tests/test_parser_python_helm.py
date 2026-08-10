from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hologram import legacy
from hologram.model import (
    BodyEventKind,
    CallKind,
    DiagnosticSeverity,
    FileIR,
    Language,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    SourceFile,
    SourceRole,
    SymbolKind,
    Visibility,
)
from hologram.parsers.api import extract_file, extract_project
from hologram.parsers.common import validate_body_events
from tests.parser_assertions import assert_body_fact_events

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PYMINI = FIXTURES / "pymini"
POLYGLOT = FIXTURES / "polyglot"

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

PYTHON_DECLARATIONS = b"""\
from enum import Enum

class Color(Enum):
    RED = 1
    GREEN = 2

class Record:
    value: str

    @property
    def title(self) -> str:
        return self.value

async def outer(order: OrderId) -> None:
    client = Client()
    note: Label
    class Inner:
        def method(self) -> None:
            helper()
    async def nested(item: ItemId) -> None:
        raise PricingError()
    await nested(order)
"""

PYTHON_BODY = b"""\
def analyze(items: list[str]) -> int:
    total = 0
    for item in items:
        if item != "z":
            total += len(item.strip())
    note = "\xc5\xbc"; target()
    return total
"""

HELM_ACTIONS = b"""\
{{- define "pricing.outer" -}}
{{- if .Values.enabled }}
{{- range .Values.items }}
{{ . | include "pricing.inner" }}
{{- end }}
{{ template "pricing.fallback" . }}
{{- end }}
{{- end }}
"""


def snapshot(
    raw: bytes,
    *,
    file: str,
    language: Language,
    path: Path | None = None,
) -> SourceFile:
    return SourceFile(
        path or Path("/repo") / file,
        file,
        language,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def fixture_snapshot(root: Path, relative: str, language: Language) -> SourceFile:
    path = root / relative
    return snapshot(path.read_bytes(), file=relative, language=language, path=path)


class PythonParserTest(unittest.TestCase):
    def assert_file_identity(self, file_ir: FileIR) -> None:
        for symbol in file_ir.symbols:
            self.assertEqual(symbol.id.file, file_ir.source.file)
            self.assertEqual(symbol.span.file, file_ir.source.file)

    def test_imports_calls_callback_and_source_snapshot_are_exact(self) -> None:
        source = snapshot(
            PYTHON_IMPORTS,
            file="shop/app.py",
            language=Language.PYTHON,
        )

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
            result = extract_file(source)
            repeated = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(result, repeated)
        self.assertEqual(result.module, "shop.app")
        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [
                ("shop.pricing", None, "pricing", False),
                ("shop.ids", "OrderId", "Oid", False),
            ],
        )
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
            ],
            [
                (None, "register", CallKind.CALL, 1),
                ("pricing", "Client", CallKind.CONSTRUCT, 0),
                ("client", "fetch", CallKind.CALL, 1),
            ],
        )
        quote = next(symbol for symbol in result.symbols if symbol.name == "quote")
        on_ready = next(
            symbol for symbol in result.symbols if symbol.name == "on_ready"
        )
        self.assertEqual(quote.id.signature_key, "(Oid)")
        self.assertEqual(on_ready.annotations, ('register("on_ready")',))
        self.assertEqual(len(result.bodies), 3)
        callbacks = [
            reference for reference in result.references if reference.name == "on_ready"
        ]
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].context, ReferenceContext.ANNOTATION)
        self.assertEqual(callbacks[0].confidence, ReferenceConfidence.POSSIBLE)
        self.assertFalse(
            any(
                reference.context is ReferenceContext.STRING
                for reference in result.references
            )
        )
        self.assert_file_identity(result)
        assert_body_fact_events(self, result)

    def test_module_identity_and_leading_line_change_spans_not_ids(self) -> None:
        package = extract_file(
            snapshot(
                b"def run(value: int) -> None:\n    pass\n",
                file="pkg/__init__.py",
                language=Language.PYTHON,
            )
        )
        original = extract_file(
            snapshot(
                b"def run(value: int) -> None:\n    pass\n",
                file="pkg/tool.py",
                language=Language.PYTHON,
            )
        )
        shifted = extract_file(
            snapshot(
                b"\ndef run(value: int) -> None:\n    pass\n",
                file="pkg/tool.py",
                language=Language.PYTHON,
            )
        )
        forward = extract_file(
            snapshot(
                b'def typed(value: "OrderId") -> None:\n    return None\n',
                file="pkg/forward.py",
                language=Language.PYTHON,
            )
        )

        self.assertEqual(package.module, "pkg")
        self.assertEqual(original.module, "pkg.tool")
        original_run = next(
            symbol for symbol in original.symbols if symbol.name == "run"
        )
        shifted_run = next(symbol for symbol in shifted.symbols if symbol.name == "run")
        typed = next(symbol for symbol in forward.symbols if symbol.name == "typed")
        self.assertEqual(original_run.id, shifted_run.id)
        self.assertEqual(original_run.span.start_line, 1)
        self.assertEqual(shifted_run.span.start_line, 2)
        self.assertEqual(typed.id.signature_key, "(OrderId)")
        forward_reference = next(
            reference for reference in forward.references if reference.name == "OrderId"
        )
        self.assertEqual(forward_reference.context, ReferenceContext.ANNOTATION)
        self.assertEqual(
            forward_reference.confidence,
            ReferenceConfidence.POSSIBLE,
        )
        assert_body_fact_events(self, original)
        assert_body_fact_events(self, shifted)
        assert_body_fact_events(self, forward)

    def test_nested_declarations_components_modifiers_bindings_and_raises(self) -> None:
        result = extract_file(
            snapshot(
                PYTHON_DECLARATIONS,
                file="declarations.py",
                language=Language.PYTHON,
            )
        )
        symbols = {(symbol.kind, symbol.name): symbol for symbol in result.symbols}
        color = symbols[(SymbolKind.ENUM, "Color")]
        record = symbols[(SymbolKind.CLASS, "Record")]
        outer = symbols[(SymbolKind.FUNCTION, "outer")]
        inner = symbols[(SymbolKind.CLASS, "Inner")]
        method = symbols[(SymbolKind.METHOD, "method")]
        nested = symbols[(SymbolKind.FUNCTION, "nested")]
        title = symbols[(SymbolKind.PROPERTY, "title")]

        self.assertEqual(color.params, ("RED", "GREEN"))
        self.assertEqual(color.components, ("RED", "GREEN"))
        self.assertEqual(record.params, ("str",))
        self.assertEqual(record.components, ("value",))
        self.assertEqual(title.annotations, ("property",))
        self.assertEqual(outer.modifiers, ("async",))
        self.assertEqual(inner.id.container_path, ("outer",))
        self.assertEqual(method.id.container_path, ("outer", "Inner"))
        self.assertEqual(nested.id.container_path, ("outer",))
        self.assertEqual(
            [(binding.name, binding.type_name) for binding in outer.bindings],
            [("order", "OrderId"), ("client", "Client"), ("note", "Label")],
        )
        self.assertEqual(nested.raises, ("PricingError",))
        self.assertNotIn(
            "order",
            {symbol.name for symbol in result.symbols},
        )
        self.assertNotIn("client", {symbol.name for symbol in result.symbols})
        self.assertEqual(
            [(symbol.name, symbol.span.start_line) for symbol in result.symbols],
            [
                ("declarations", 1),
                ("Color", 3),
                ("RED", 4),
                ("GREEN", 5),
                ("Record", 7),
                ("value", 8),
                ("title", 11),
                ("outer", 14),
                ("Inner", 17),
                ("method", 18),
                ("nested", 20),
            ],
        )
        self.assert_file_identity(result)
        assert_body_fact_events(self, result)

    def test_body_events_are_complete_balanced_and_use_utf8_byte_columns(self) -> None:
        result = extract_file(
            snapshot(PYTHON_BODY, file="body.py", language=Language.PYTHON)
        )
        analyze = next(symbol for symbol in result.symbols if symbol.name == "analyze")
        body = next(body for body in result.bodies if body.owner == analyze.id)
        validate_body_events(body.events)
        kinds = {event.kind for event in body.events}
        self.assertTrue(
            {
                BodyEventKind.PARAM,
                BodyEventKind.LOCAL,
                BodyEventKind.NAME,
                BodyEventKind.MEMBER,
                BodyEventKind.OPERATOR,
                BodyEventKind.LITERAL,
                BodyEventKind.KEYWORD,
                BodyEventKind.CALL,
            }.issubset(kinds)
        )
        controls = [
            (event.kind, event.text)
            for event in body.events
            if event.kind in {BodyEventKind.CONTROL_ENTER, BodyEventKind.CONTROL_EXIT}
        ]
        self.assertEqual(
            controls,
            [
                (BodyEventKind.CONTROL_ENTER, "loop"),
                (BodyEventKind.CONTROL_ENTER, "if"),
                (BodyEventKind.CONTROL_EXIT, "if"),
                (BodyEventKind.CONTROL_EXIT, "loop"),
            ],
        )
        target = next(call for call in result.calls if call.name == "target")
        raw_line = PYTHON_BODY.splitlines()[5]
        self.assertEqual(target.span.start_column, raw_line.index(b"target"))
        self.assertNotEqual(
            target.span.start_column,
            raw_line.decode("utf-8").index("target"),
        )
        assert_body_fact_events(self, result)

    def test_calls_are_source_ordered_and_never_capped(self) -> None:
        raw = b"def many():\n" + b"".join(
            f"    call_{index}()\n".encode() for index in range(15)
        )
        result = extract_file(snapshot(raw, file="many.py", language=Language.PYTHON))

        self.assertEqual(
            [call.name for call in result.calls],
            [f"call_{index}" for index in range(15)],
        )
        assert_body_fact_events(self, result)

    def test_syntax_error_is_structured_and_partial_invariants_still_hold(self) -> None:
        result = extract_file(
            snapshot(
                b"def broken(:\n    target()\n",
                file="broken.py",
                language=Language.PYTHON,
            )
        )

        self.assertEqual(result.symbols, ())
        self.assertEqual(result.diagnostics[0].code, "python-syntax-error")
        self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)
        assert_body_fact_events(self, result)

    def test_pymini_legacy_projection_preserves_the_frozen_v1_surface(self) -> None:
        canonical = extract_file(
            fixture_snapshot(
                PYMINI,
                "test_app.py",
                Language.PYTHON,
            )
        )
        canonical_bulk = next(
            symbol
            for symbol in canonical.symbols
            if symbol.name == "test_bulk_orders_get_discount"
        )
        self.assertEqual(
            [call.name for call in canonical.calls if call.caller == canonical_bulk.id],
            ["ItemId", "range", "price_order", "OrderId"],
        )

        projected = legacy.extract_file(PYMINI / "test_app.py", PYMINI)
        legacy_bulk = next(
            symbol
            for symbol in projected
            if symbol.name == "test_bulk_orders_get_discount"
        )
        self.assertEqual(
            legacy_bulk.calls,
            ["ItemId", "price_order", "range", "OrderId"],
        )

    def test_recognized_dynamic_strings_join_name_events_and_keep_literals(
        self,
    ) -> None:
        result = extract_file(
            snapshot(
                b"""\
def wire(service):
    register("registered")
    configure(callback="configured")
    getattr(service, "reflected")
""",
                file="callbacks.py",
                language=Language.PYTHON,
            )
        )

        dynamic = [
            reference
            for reference in result.references
            if reference.name in {"configured", "reflected", "registered"}
        ]
        self.assertEqual(
            [(reference.name, reference.context) for reference in dynamic],
            [
                ("registered", ReferenceContext.REFLECTION),
                ("configured", ReferenceContext.CONFIG),
                ("reflected", ReferenceContext.REFLECTION),
            ],
        )
        wire = next(symbol for symbol in result.symbols if symbol.name == "wire")
        body = next(body for body in result.bodies if body.owner == wire.id)
        for dynamic_reference in dynamic:
            self.assertEqual(dynamic_reference.kind, ReferenceKind.NAME)
            matching = [
                event.kind
                for event in body.events
                if event.span == dynamic_reference.span
            ]
            self.assertEqual(
                matching,
                [BodyEventKind.LITERAL, BodyEventKind.NAME],
            )
        assert_body_fact_events(self, result)

    def test_exception_handler_types_are_type_references_with_exact_events(
        self,
    ) -> None:
        result = extract_file(
            snapshot(
                b"""\
def handle():
    try:
        target()
    except ValueError:
        recover()
    except (pkg.Error, OtherError):
        fallback()
""",
                file="exceptions.py",
                language=Language.PYTHON,
            )
        )

        references = [
            reference
            for reference in result.references
            if reference.name in {"Error", "OtherError", "ValueError", "pkg"}
        ]
        self.assertEqual(
            [reference.name for reference in references],
            ["ValueError", "pkg", "Error", "OtherError"],
        )
        handle = next(symbol for symbol in result.symbols if symbol.name == "handle")
        body = next(body for body in result.bodies if body.owner == handle.id)
        body_events = {(event.kind, event.span) for event in body.events}
        for exception_reference in references:
            self.assertEqual(exception_reference.kind, ReferenceKind.TYPE)
            self.assertEqual(exception_reference.context, ReferenceContext.TYPE)
            self.assertIn(
                (BodyEventKind.TYPE, exception_reference.span),
                body_events,
            )
        assert_body_fact_events(self, result)

    def test_module_symbol_owns_direct_facts_and_module_lambda_body(self) -> None:
        result = extract_file(
            snapshot(
                b"""\
target()
service.start()
callback = lambda value: module_target(value)

def declared():
    nested_only()
""",
                file="pkg/app.py",
                language=Language.PYTHON,
            )
        )

        module = next(
            symbol for symbol in result.symbols if symbol.kind is SymbolKind.MODULE
        )
        self.assertEqual(module.name, "pkg.app")
        self.assertEqual(module.id.container_path, ())
        self.assertEqual(
            [call.name for call in result.calls if call.caller == module.id],
            ["target", "start", "module_target"],
        )
        self.assertFalse(
            any(
                call.name == "nested_only" and call.caller == module.id
                for call in result.calls
            )
        )
        module_body = next(body for body in result.bodies if body.owner == module.id)
        self.assertTrue(
            any(
                event.kind is BodyEventKind.PARAM and event.text == "value"
                for event in module_body.events
            )
        )
        assert_body_fact_events(self, result)

    def test_nested_lambdas_keep_owner_facts_events_and_no_symbols(self) -> None:
        result = extract_file(
            snapshot(
                b"""\
def outer(items, flag):
    nested = lambda value: (lambda: inner(value))
    mapped = list(map(lambda item: transform(item), items))
    chosen = (lambda: left()) if flag else (lambda: right())
    return nested, mapped, chosen
""",
                file="lambdas.py",
                language=Language.PYTHON,
            )
        )

        outer = next(symbol for symbol in result.symbols if symbol.name == "outer")
        self.assertEqual(
            [call.name for call in result.calls if call.caller == outer.id],
            ["inner", "list", "map", "transform", "left", "right"],
        )
        self.assertEqual(
            [symbol.name for symbol in result.symbols],
            ["lambdas", "outer"],
        )
        body = next(body for body in result.bodies if body.owner == outer.id)
        self.assertEqual(
            [event.text for event in body.events if event.kind is BodyEventKind.PARAM],
            ["items", "flag", "value", "item"],
        )
        self.assertEqual(
            [
                (event.kind, event.text)
                for event in body.events
                if event.kind
                in {BodyEventKind.CONTROL_ENTER, BodyEventKind.CONTROL_EXIT}
            ],
            [
                (BodyEventKind.CONTROL_ENTER, "if"),
                (BodyEventKind.CONTROL_EXIT, "if"),
            ],
        )
        self.assertEqual(
            len([call for call in result.calls if call.caller == outer.id]),
            6,
        )
        assert_body_fact_events(self, result)

    def test_legacy_projection_aggregates_nested_python_calls_in_ast_walk_order(
        self,
    ) -> None:
        raw = b"""\
def outer():
    def inner():
        nested_call()
    class Local:
        def method(self):
            class_call()
    callback = lambda: lambda_call()
    direct_call()
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "nested.py"
            path.write_bytes(raw)
            projected = legacy.extract_file(path, root)

        outer = next(symbol for symbol in projected if symbol.name == "outer")
        self.assertEqual(
            outer.calls,
            ["direct_call", "nested_call", "lambda_call", "class_call"],
        )


class HelmParserTest(unittest.TestCase):
    def test_chart_values_and_named_templates_preserve_existing_facts(self) -> None:
        chart_source = fixture_snapshot(
            POLYGLOT,
            "chart/Chart.yaml",
            Language.HELM,
        )
        values_source = fixture_snapshot(
            POLYGLOT,
            "chart/values.yaml",
            Language.HELM,
        )
        templates_source = fixture_snapshot(
            POLYGLOT,
            "chart/templates/_helpers.tpl",
            Language.HELM,
        )
        chart = extract_file(chart_source)
        values = extract_file(values_source)
        templates = extract_file(templates_source)

        self.assertEqual(
            [
                (
                    symbol.name,
                    symbol.kind,
                    symbol.visibility,
                    symbol.signature,
                    symbol.span.start_line,
                )
                for symbol in chart.symbols
            ],
            [
                (
                    "pricing-service",
                    SymbolKind.CLASS,
                    Visibility.PUBLIC,
                    "chart pricing-service",
                    2,
                )
            ],
        )
        self.assertEqual(
            [symbol.name for symbol in values.symbols],
            ["replicaCount", "image", "resources"],
        )
        self.assertEqual(
            [symbol.span.start_line for symbol in values.symbols],
            [1, 2, 4],
        )
        self.assertTrue(
            all(symbol.visibility is Visibility.PRIVATE for symbol in values.symbols)
        )
        self.assertEqual(
            [symbol.name for symbol in templates.symbols],
            ["pricing.fullname", "pricing.labels"],
        )
        self.assertEqual([symbol.body_lines for symbol in templates.symbols], [0, 0])
        self.assertEqual(len(templates.bodies), 2)
        for result, source in (
            (chart, chart_source),
            (values, values_source),
            (templates, templates_source),
        ):
            self.assertIs(result.source, source)
            for symbol in result.symbols:
                self.assertEqual(symbol.id.file, result.source.file)
                self.assertEqual(symbol.span.file, result.source.file)
            assert_body_fact_events(self, result)

    def test_template_actions_have_owned_calls_names_literals_and_controls(
        self,
    ) -> None:
        source = snapshot(
            HELM_ACTIONS,
            file="chart/templates/actions.tpl",
            language=Language.HELM,
        )
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
            result = extract_file(source)
            repeated = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(result, repeated)
        definition = result.symbols[0]
        self.assertEqual(definition.name, "pricing.outer")
        self.assertEqual(
            [
                (call.name, call.receiver, call.kind, call.arity)
                for call in result.calls
            ],
            [
                ("pricing.inner", None, CallKind.CALL, None),
                ("pricing.fallback", None, CallKind.CALL, None),
            ],
        )
        self.assertTrue(all(call.caller == definition.id for call in result.calls))
        body = result.bodies[0]
        validate_body_events(body.events)
        self.assertEqual(
            [
                (event.kind, event.text)
                for event in body.events
                if event.kind
                in {BodyEventKind.CONTROL_ENTER, BodyEventKind.CONTROL_EXIT}
            ],
            [
                (BodyEventKind.CONTROL_ENTER, "if"),
                (BodyEventKind.CONTROL_ENTER, "loop"),
                (BodyEventKind.CONTROL_EXIT, "loop"),
                (BodyEventKind.CONTROL_EXIT, "if"),
            ],
        )
        self.assertTrue(any(event.kind is BodyEventKind.NAME for event in body.events))
        self.assertTrue(
            any(event.kind is BodyEventKind.LITERAL for event in body.events)
        )
        self.assertEqual(
            [
                (event.kind, event.text)
                for event in body.events
                if event.kind
                in {
                    BodyEventKind.CALL,
                    BodyEventKind.LITERAL,
                    BodyEventKind.NAME,
                }
            ],
            [
                (BodyEventKind.NAME, ".Values.enabled"),
                (BodyEventKind.NAME, ".Values.items"),
                (BodyEventKind.NAME, "."),
                (BodyEventKind.CALL, "pricing.inner"),
                (BodyEventKind.LITERAL, "<string>"),
                (BodyEventKind.CALL, "pricing.fallback"),
                (BodyEventKind.LITERAL, "<string>"),
                (BodyEventKind.NAME, "."),
            ],
        )
        assert_body_fact_events(self, result)

    def test_chart_layout_gate_and_ordinary_yaml_are_complete(self) -> None:
        plain = snapshot(
            b"name: pipeline\nstages:\n  - build\n",
            file="ci.yaml",
            language=Language.HELM,
        )
        chart_yaml = snapshot(
            b"apiVersion: apps/v1\nkind: Deployment\n",
            file="chart/templates/deployment.yaml",
            language=Language.HELM,
        )

        plain_ir = extract_file(plain)
        chart_ir = extract_file(chart_yaml)
        project = extract_project(Path("/repo"), (chart_yaml, plain))

        self.assertEqual(plain_ir.symbols, ())
        self.assertEqual(chart_ir.symbols, ())
        self.assertEqual(plain_ir.diagnostics, ())
        self.assertEqual(chart_ir.diagnostics, ())
        self.assertTrue(project.complete)
        assert_body_fact_events(self, plain_ir)
        assert_body_fact_events(self, chart_ir)

    def test_template_ids_ignore_leading_blank_line_and_spans_use_bytes(self) -> None:
        raw = '{{- define "żółw" -}}\nż {{ .Values.name }}\n{{- end }}\n'.encode()
        original = extract_file(
            snapshot(
                raw,
                file="chart/templates/unicode.tpl",
                language=Language.HELM,
            )
        )
        shifted = extract_file(
            snapshot(
                b"\n" + raw,
                file="chart/templates/unicode.tpl",
                language=Language.HELM,
            )
        )

        self.assertEqual(original.symbols[0].id, shifted.symbols[0].id)
        self.assertEqual(original.symbols[0].span.start_line, 1)
        self.assertEqual(shifted.symbols[0].span.start_line, 2)
        name_event = next(
            event
            for event in original.bodies[0].events
            if event.kind is BodyEventKind.NAME
        )
        self.assertEqual(name_event.span.start_column, len("ż {{ ".encode()))
        assert_body_fact_events(self, original)
        assert_body_fact_events(self, shifted)

    def test_unclosed_template_is_partial_but_body_fact_invariant_holds(self) -> None:
        result = extract_file(
            snapshot(
                b'{{- define "broken" -}}\n{{ include "target" . }}\n',
                file="chart/templates/broken.tpl",
                language=Language.HELM,
            )
        )

        self.assertEqual(result.diagnostics[0].code, "helm-syntax-error")
        self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)
        self.assertEqual([call.name for call in result.calls], ["target"])
        assert_body_fact_events(self, result)

    def test_legacy_helm_projection_keeps_zero_body_lines(self) -> None:
        projected = legacy.extract_file(
            POLYGLOT / "chart/templates/_helpers.tpl",
            POLYGLOT,
        )

        self.assertEqual([symbol.size for symbol in projected], [0, 0])

    def test_action_scanner_ignores_comments_and_keeps_quoted_delimiters(
        self,
    ) -> None:
        result = extract_file(
            snapshot(
                b"""\
{{- define "scanner" -}}
{{/* include "fake.comment" . */}}
{{ printf "}}" `raw }}` '}' }}
{{ include "real.target" . }}
{{- end }}
""",
                file="chart/templates/scanner.tpl",
                language=Language.HELM,
            )
        )

        self.assertEqual(result.diagnostics, ())
        self.assertEqual([call.name for call in result.calls], ["real.target"])
        body = result.bodies[0]
        self.assertEqual(
            [
                event.text
                for event in body.events
                if event.kind is BodyEventKind.LITERAL
            ],
            ["<string>", "<string>", "<string>", "<string>"],
        )
        self.assertFalse(any("fake" in event.text for event in body.events))
        assert_body_fact_events(self, result)

    def test_unterminated_helm_actions_strings_and_comments_are_errors(self) -> None:
        malformed = (
            b'{{- define "broken" -}}\n{{ printf "unterminated }}\n{{- end }}\n',
            b"{{/* unterminated comment\n",
            b'{{ printf "ok"\n',
        )
        for index, raw in enumerate(malformed):
            with self.subTest(index=index):
                source = snapshot(
                    raw,
                    file=f"chart/templates/broken-{index}.tpl",
                    language=Language.HELM,
                )
                result = extract_file(source)
                project = extract_project(Path("/repo"), (source,))
                self.assertTrue(
                    any(
                        diagnostic.code == "helm-syntax-error"
                        and diagnostic.severity is DiagnosticSeverity.ERROR
                        for diagnostic in result.diagnostics
                    )
                )
                self.assertFalse(project.complete)
                assert_body_fact_events(self, result)

    def test_helm_structure_validation_rejects_invalid_actions(self) -> None:
        malformed = (
            b"{{ else }}\n",
            b"{{ end }}\n",
            b'{{ define "outer" }}{{ define "nested" }}{{ end }}{{ end }}\n',
            b"{{ if .Values.enabled }}\n",
        )
        for index, raw in enumerate(malformed):
            with self.subTest(index=index):
                result = extract_file(
                    snapshot(
                        raw,
                        file=f"chart/templates/structure-{index}.tpl",
                        language=Language.HELM,
                    )
                )
                self.assertTrue(
                    any(
                        diagnostic.code == "helm-syntax-error"
                        for diagnostic in result.diagnostics
                    )
                )
                assert_body_fact_events(self, result)

    def test_valid_helm_else_if_and_all_control_kinds_are_balanced(self) -> None:
        result = extract_file(
            snapshot(
                b"""\
{{ if .Values.first }}{{ else if .Values.second }}{{ end }}
{{ range .Values.items }}{{ else }}{{ end }}
{{ with .Values.context }}{{ end }}
{{ block "outside" . }}{{ end }}
{{ define "inside" }}
{{ if .Values.first }}{{ else if .Values.second }}{{ end }}
{{ range .Values.items }}{{ else }}{{ end }}
{{ with .Values.context }}{{ end }}
{{ block "nested" . }}{{ end }}
{{ end }}
""",
                file="chart/templates/controls.tpl",
                language=Language.HELM,
            )
        )

        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(result.bodies), 1)
        validate_body_events(result.bodies[0].events)
        self.assertEqual(
            [
                event.text
                for event in result.bodies[0].events
                if event.kind is BodyEventKind.CONTROL_ENTER
            ],
            ["if", "loop", "if"],
        )
        assert_body_fact_events(self, result)

    def test_values_yaml_preserves_unicode_word_keys_and_byte_spans(self) -> None:
        raw = "foó: 1\nplain: 2\n".encode()
        result = extract_file(
            snapshot(
                raw,
                file="chart/values.yaml",
                language=Language.HELM,
            )
        )

        self.assertEqual([symbol.name for symbol in result.symbols], ["foó", "plain"])
        self.assertEqual(result.symbols[0].span.start_column, 0)
        self.assertEqual(result.symbols[0].span.end_column, len("foó".encode()))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chart/values.yaml"
            path.parent.mkdir()
            path.write_bytes(raw)
            projected = legacy.extract_file(path, root)
        self.assertEqual([symbol.name for symbol in projected], ["foó", "plain"])


if __name__ == "__main__":
    unittest.main()
