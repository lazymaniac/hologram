from __future__ import annotations

import ast
import hashlib
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import hologram.parsers.python as python_parser
from hologram.model import (
    Binding,
    BodyEvent,
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
    SourceSpan,
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
            [
                ("order", "OrderId"),
                ("\0hologram-arity", "1:1"),
                ("client", "Client"),
                ("note", "Label"),
            ],
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

    def test_callable_bindings_retain_python_accepted_arity_range(self) -> None:
        result = extract_file(
            snapshot(
                b"""\
def optional(a: int, b: str = "x", *, c: bool = True):
    return a

def variadic(a, *rest, **kwargs):
    return a
""",
                file="arity.py",
                language=Language.PYTHON,
            )
        )

        optional = next(item for item in result.symbols if item.name == "optional")
        variadic = next(item for item in result.symbols if item.name == "variadic")
        self.assertIn(Binding("\0hologram-arity", "1:3"), optional.bindings)
        self.assertIn(Binding("\0hologram-arity", "1:*"), variadic.bindings)

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

    def test_pymini_calls_preserve_canonical_source_order(self) -> None:
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

    def test_annotation_context_references_are_explicitly_possible(self) -> None:
        result = extract_file(
            snapshot(
                b"""\
@entrypoint
@framework.decorator
def decorated(value: "Forward") -> "Result":
    return value
""",
                file="annotations.py",
                language=Language.PYTHON,
            )
        )

        annotations = [
            reference
            for reference in result.references
            if reference.context is ReferenceContext.ANNOTATION
        ]
        self.assertEqual(
            [reference.name for reference in annotations],
            ["entrypoint", "framework", "decorator", "Forward", "Result"],
        )
        self.assertTrue(
            all(
                reference.confidence is ReferenceConfidence.POSSIBLE
                for reference in annotations
            )
        )
        assert_body_fact_events(self, result)

    def test_literal_values_and_annotated_metadata_are_not_type_references(
        self,
    ) -> None:
        result = extract_file(
            snapshot(
                b"""\
from typing import Annotated, Literal

def parse(
    status: Literal["ready", "Phantom"],
    user: Annotated["User", "Metadata", marker, pkg.Meta],
    nested: dict[str, list["Real"]],
) -> Annotated[list["Result"], "ReturnMetadata"]:
    local: Annotated[list["Kept"], "LocalMetadata", local_marker] = user
    choice: Literal["Ignored"] = status
    return local
""",
                file="type-strings.py",
                language=Language.PYTHON,
            )
        )

        possible_annotations = [
            (reference.name, reference.kind)
            for reference in result.references
            if reference.context is ReferenceContext.ANNOTATION
            and reference.confidence is ReferenceConfidence.POSSIBLE
        ]
        self.assertEqual(
            possible_annotations,
            [
                ("User", ReferenceKind.TYPE),
                ("marker", ReferenceKind.NAME),
                ("pkg", ReferenceKind.NAME),
                ("Meta", ReferenceKind.NAME),
                ("Real", ReferenceKind.TYPE),
                ("Result", ReferenceKind.TYPE),
                ("Kept", ReferenceKind.TYPE),
                ("local_marker", ReferenceKind.NAME),
            ],
        )
        self.assertTrue(
            {
                "Ignored",
                "LocalMetadata",
                "Metadata",
                "Phantom",
                "ReturnMetadata",
                "ready",
            }.isdisjoint(reference.name for reference in result.references)
        )
        parse_symbol = next(
            symbol for symbol in result.symbols if symbol.name == "parse"
        )
        body = next(body for body in result.bodies if body.owner == parse_symbol.id)
        event_facts = {(event.kind, event.text) for event in body.events}
        self.assertIn((BodyEventKind.TYPE, "Kept"), event_facts)
        self.assertIn((BodyEventKind.NAME, "local_marker"), event_facts)
        self.assertNotIn((BodyEventKind.TYPE, "LocalMetadata"), event_facts)
        assert_body_fact_events(self, result)

    def test_attribute_reference_event_lookup_is_preindexed_once(self) -> None:
        raw = (
            b"def inspect(obj):\n"
            b"    return (obj.first, obj.second, obj.third, obj.fourth)\n"
        )
        source = snapshot(raw, file="attributes.py", language=Language.PYTHON)
        callable_node = ast.parse(source.text).body[0]
        self.assertIsInstance(callable_node, ast.FunctionDef)
        events = python_parser.ast_body_events(source, callable_node)

        class CountingEvents:
            def __init__(self, values: tuple[BodyEvent, ...]) -> None:
                self.values = values
                self.iterations = 0

            def __iter__(self) -> Iterator[BodyEvent]:
                self.iterations += 1
                return iter(self.values)

        counting = CountingEvents(events)
        owner = python_parser.symbol_id(
            source,
            (),
            SymbolKind.FUNCTION,
            "inspect",
        )
        visitor = python_parser._OwnedFactVisitor(
            source,
            owner,
            cast(tuple[BodyEvent, ...], counting),
        )
        for statement in callable_node.body:
            visitor.visit(statement)

        self.assertEqual(counting.iterations, 1)
        self.assertEqual(
            [reference.name for reference in visitor.references],
            [
                "obj",
                "first",
                "obj",
                "second",
                "obj",
                "third",
                "obj",
                "fourth",
            ],
        )
        event_pairs = {(event.kind, event.span) for event in events}
        for reference in visitor.references:
            self.assertIn((BodyEventKind.NAME, reference.span), event_pairs)

    def test_typing_import_aliases_preserve_annotation_argument_roles(self) -> None:
        result = extract_file(
            snapshot(
                b"""\
from typing import Annotated as A, Literal as L

def parse(
    status: L["ready", "Phantom"],
    user: A[list["User"], "Metadata", marker],
    nested: dict[str, list["Real"]],
) -> A[list["Result"], "ReturnMetadata"]:
    local: A[list["Kept"], "LocalMetadata", local_marker] = user
    choice: L["Ignored"] = status
    nested_choice: A[L["InnerIgnored"], "NestedMetadata"] = status
    return local
""",
                file="aliased-type-strings.py",
                language=Language.PYTHON,
            )
        )

        possible_annotations = [
            (reference.name, reference.kind)
            for reference in result.references
            if reference.context is ReferenceContext.ANNOTATION
            and reference.confidence is ReferenceConfidence.POSSIBLE
        ]
        self.assertEqual(
            possible_annotations,
            [
                ("User", ReferenceKind.TYPE),
                ("marker", ReferenceKind.NAME),
                ("Real", ReferenceKind.TYPE),
                ("Result", ReferenceKind.TYPE),
                ("Kept", ReferenceKind.TYPE),
                ("local_marker", ReferenceKind.NAME),
            ],
        )
        suppressed = {
            "Ignored",
            "InnerIgnored",
            "LocalMetadata",
            "Metadata",
            "NestedMetadata",
            "Phantom",
            "ReturnMetadata",
            "ready",
        }
        self.assertTrue(
            suppressed.isdisjoint(reference.name for reference in result.references)
        )
        parse_symbol = next(
            symbol for symbol in result.symbols if symbol.name == "parse"
        )
        body = next(body for body in result.bodies if body.owner == parse_symbol.id)
        event_facts = {(event.kind, event.text) for event in body.events}
        self.assertIn((BodyEventKind.TYPE, "Kept"), event_facts)
        self.assertIn((BodyEventKind.NAME, "local_marker"), event_facts)
        for suppressed_name in suppressed:
            self.assertNotIn((BodyEventKind.TYPE, suppressed_name), event_facts)
            self.assertNotIn((BodyEventKind.NAME, suppressed_name), event_facts)
        assert_body_fact_events(self, result)


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
        self.assertEqual(
            [call.name for call in result.calls],
            ["printf", "real.target"],
        )
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

    def test_helm_action_shapes_reject_missing_or_trailing_arguments(self) -> None:
        malformed = (
            b'{{define "x"}}{{if}}{{end}}{{end}}',
            b'{{define "x" trailing}}{{end}}',
            b'{{define "x"}}{{end trailing}}',
            b'{{define ""}}{{end}}',
            b'{{define "x"}}{{break trailing}}{{end}}',
            b'{{define "x"}}{{continue true}}{{end}}',
            b'{{define "x"}}{{break}}{{end}}',
            b'{{define "x"}}{{continue}}{{end}}',
        )
        for index, raw in enumerate(malformed):
            with self.subTest(index=index):
                source = snapshot(
                    raw,
                    file=f"chart/templates/action-shape-{index}.tpl",
                    language=Language.HELM,
                )
                result = extract_file(source)
                project = extract_project(Path("/repo"), (source,))
                codes = [diagnostic.code for diagnostic in result.diagnostics]
                self.assertIn("helm-syntax-error", codes)
                self.assertNotIn("extractor-crash", codes)
                self.assertFalse(project.complete)

        valid = extract_file(
            snapshot(
                b"""\
{{define "valid"}}
{{if required "message" .Values.enabled | default true}}{{end}}
{{range .Values.items}}{{end}}
{{with .Values.context}}{{end}}
{{block "nested" .}}{{end}}
{{end}}
""",
                file="chart/templates/valid-shapes.tpl",
                language=Language.HELM,
            )
        )
        self.assertEqual(valid.diagnostics, ())

    def test_helm_assignment_and_generic_commands_emit_joined_body_facts(
        self,
    ) -> None:
        raw = b"""\
{{define "commands"}}
{{ $name := printf "%s" .Values.name }}{{ required "msg" $name }}
{{end}}
"""
        result = extract_file(
            snapshot(
                raw,
                file="chart/templates/commands.tpl",
                language=Language.HELM,
            )
        )

        definition = result.symbols[0]
        line = raw.splitlines()[1]
        printf_start = line.index(b"printf")
        printf_end = line.index(b".Values.name") + len(b".Values.name")
        required_start = line.index(b"required")
        required_end = line.rindex(b"$name") + len(b"$name")
        expected_calls = [
            (
                "printf",
                SourceSpan(
                    result.source.file,
                    2,
                    printf_start,
                    2,
                    printf_end,
                ),
            ),
            (
                "required",
                SourceSpan(
                    result.source.file,
                    2,
                    required_start,
                    2,
                    required_end,
                ),
            ),
        ]
        self.assertEqual(
            [(call.name, call.span) for call in result.calls],
            expected_calls,
        )
        self.assertTrue(all(call.caller == definition.id for call in result.calls))
        body = result.bodies[0]
        local_span = SourceSpan(
            result.source.file,
            2,
            line.index(b"$name"),
            2,
            line.index(b"$name") + len(b"$name"),
        )
        self.assertIn(
            (BodyEventKind.LOCAL, "$name", local_span),
            {(event.kind, event.text, event.span) for event in body.events},
        )
        event_pairs = {(event.kind, event.span) for event in body.events}
        for _, call_span in expected_calls:
            self.assertIn((BodyEventKind.CALL, call_span), event_pairs)
        assert_body_fact_events(self, result)

    def test_helm_root_values_are_valid_pipeline_operands(self) -> None:
        raw = b"""\
{{define "rooted"}}
{{ include "target" $ }}
{{ printf "%s" $.Values.name }}
{{end}}
"""
        result = extract_file(
            snapshot(
                raw,
                file="chart/templates/rooted.tpl",
                language=Language.HELM,
            )
        )

        self.assertEqual(result.diagnostics, ())
        definition = result.symbols[0]
        include_line, printf_line = raw.splitlines()[1:3]
        include_start = include_line.index(b"include")
        root_start = include_line.index(b"$")
        printf_start = printf_line.index(b"printf")
        field_start = printf_line.index(b"$.Values.name")
        expected_calls = [
            (
                "target",
                SourceSpan(
                    result.source.file,
                    2,
                    include_start,
                    2,
                    root_start + 1,
                ),
            ),
            (
                "printf",
                SourceSpan(
                    result.source.file,
                    3,
                    printf_start,
                    3,
                    field_start + len(b"$.Values.name"),
                ),
            ),
        ]
        self.assertEqual(
            [(call.name, call.span) for call in result.calls],
            expected_calls,
        )
        self.assertTrue(all(call.caller == definition.id for call in result.calls))
        body_facts = {
            (event.kind, event.text, event.span) for event in result.bodies[0].events
        }
        self.assertIn(
            (
                BodyEventKind.NAME,
                "$",
                SourceSpan(
                    result.source.file,
                    2,
                    root_start,
                    2,
                    root_start + 1,
                ),
            ),
            body_facts,
        )
        self.assertIn(
            (
                BodyEventKind.NAME,
                "$.Values.name",
                SourceSpan(
                    result.source.file,
                    3,
                    field_start,
                    3,
                    field_start + len(b"$.Values.name"),
                ),
            ),
            body_facts,
        )
        assert_body_fact_events(self, result)

    def test_helm_nested_commands_and_loop_controls_have_exact_roles(self) -> None:
        raw = b"""\
{{define "nested"}}
{{ printf "%s" (required "msg" .Values.name) }}
{{ range .Values.items }}
{{ break }}
{{ continue }}
{{ end }}
{{end}}
"""
        result = extract_file(
            snapshot(
                raw,
                file="chart/templates/nested-commands.tpl",
                language=Language.HELM,
            )
        )

        self.assertEqual(result.diagnostics, ())
        command_line = raw.splitlines()[1]
        command_end = command_line.index(b".Values.name") + len(b".Values.name")
        expected_calls = [
            (
                "printf",
                SourceSpan(
                    result.source.file,
                    2,
                    command_line.index(b"printf"),
                    2,
                    command_end,
                ),
            ),
            (
                "required",
                SourceSpan(
                    result.source.file,
                    2,
                    command_line.index(b"required"),
                    2,
                    command_end,
                ),
            ),
        ]
        self.assertEqual(
            [(call.name, call.span) for call in result.calls],
            expected_calls,
        )
        event_facts = {
            (event.kind, event.text, event.span) for event in result.bodies[0].events
        }
        for call_name, call_span in expected_calls:
            self.assertIn((BodyEventKind.CALL, call_name, call_span), event_facts)
        for line_number, keyword in ((4, "break"), (5, "continue")):
            self.assertIn(
                (
                    BodyEventKind.KEYWORD,
                    keyword,
                    SourceSpan(
                        result.source.file,
                        line_number,
                        3,
                        line_number,
                        3 + len(keyword),
                    ),
                ),
                event_facts,
            )
        self.assertTrue(
            {"break", "continue"}.isdisjoint(call.name for call in result.calls)
        )
        assert_body_fact_events(self, result)

    def test_helm_quoted_names_decode_go_escapes_and_reject_invalid_ones(
        self,
    ) -> None:
        valid = (
            (b'{{define "\\u0066oo"}}{{end}}', "foo"),
            (b'{{define "\\xC3\\xA9"}}{{end}}', "é"),
            (b'{{define "\\303\\251"}}{{end}}', "é"),
            (b'{{define "caf\\xC3\\251"}}{{end}}', "café"),
            (b'{{define "\\U000000e9"}}{{end}}', "é"),
        )
        for index, (raw, expected_name) in enumerate(valid):
            with self.subTest(valid=index):
                result = extract_file(
                    snapshot(
                        raw,
                        file=f"chart/templates/escaped-{index}.tpl",
                        language=Language.HELM,
                    )
                )
                self.assertEqual(result.diagnostics, ())
                self.assertEqual(
                    [symbol.name for symbol in result.symbols],
                    [expected_name],
                )

        valid_rune = extract_file(
            snapshot(
                b"{{define \"rune\"}}{{printf '\\u00e9'}}{{end}}",
                file="chart/templates/valid-rune.tpl",
                language=Language.HELM,
            )
        )
        self.assertEqual(valid_rune.diagnostics, ())

        invalid = (
            b'{{define "\\q"}}{{end}}',
            b'{{define "\\u12"}}{{end}}',
            b'{{define "\\xFF"}}{{end}}',
            b"{{define \"x\"}}{{printf '\\xC3\\xA9'}}{{end}}",
        )
        for index, raw in enumerate(invalid):
            with self.subTest(index=index):
                source = snapshot(
                    raw,
                    file=f"chart/templates/invalid-escape-{index}.tpl",
                    language=Language.HELM,
                )
                result = extract_file(source)
                project = extract_project(Path("/repo"), (source,))
                codes = [diagnostic.code for diagnostic in result.diagnostics]
                self.assertIn("helm-syntax-error", codes)
                self.assertNotIn("extractor-crash", codes)
                self.assertFalse(project.complete)

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


if __name__ == "__main__":
    unittest.main()
