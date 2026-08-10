from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import hologram
from hologram.model import (
    Binding,
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
JAVAMINI = FIXTURES / "javamini"


INVALID_IMPORTS = b"""\
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
"""

VALID_IMPORTS = INVALID_IMPORTS.replace(
    b"import shop.ids.OrderId as InvalidJavaAlias;\n",
    b"",
)

FRAMEWORK_SOURCE = b"""\
package shop.framework;

public final class Config {
  @Bean
  public Handler onRefresh() { return new Handler(); }

  @Override
  protected Quote run() { return quote(); }

  @EventListener("onRefresh")
  void listen() {
    String unrelated = "onRefresh";
  }

  public static void main(String[] args) {
    boot(args);
  }
}
"""

NESTED_SOURCE = """\
package shop.nested;

public class Outer {
  static final String LABEL = "constant";
  private int count;

  class Inner {
    final Value field = new Value();

    void work(Input input) { String note = "ż"; target(input); }
  }
}
""".encode()

BODY_SOURCE = b"""\
package shop.body;
import java.util.List;

class Worker {
  Result flow(List<String> items) {
    int total = 0;
    for (String item : items) {
      if (item.length() > 0) {
        try {
          Widget widget = new Widget(item);
          service.handle(widget);
        } catch (RuntimeException error) {
          throw error;
        } finally {
          total += 1;
        }
      }
    }
    return finish(total);
  }
}
"""

COMPACT_RECORD = b"""\
package shop.records;

public record Ticket(String code, int count) implements Identified {
  public Ticket {
    validate(code);
  }
}
"""


def snapshot(
    raw: bytes,
    *,
    file: str = "src/App.java",
    path: Path | None = None,
) -> SourceFile:
    return SourceFile(
        path or Path("/repo") / file,
        file,
        Language.JAVA,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def fixture_snapshot(relative: str) -> SourceFile:
    path = JAVAMINI / relative
    return snapshot(path.read_bytes(), file=relative, path=path)


def symbol(result: FileIR, name: str, kind: SymbolKind | None = None):
    found = next(
        (
            item
            for item in result.symbols
            if item.name == name and (kind is None or item.kind is kind)
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"missing {kind or 'symbol'} {name!r}")
    return found


@unittest.skipUnless(hologram.has_parser("java"), "tree-sitter-java not installed")
class JavaParserTest(unittest.TestCase):
    def test_valid_package_imports_calls_bindings_and_snapshot_are_exact(self) -> None:
        source = snapshot(VALID_IMPORTS, path=Path("/missing/src/App.java"))

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
        module = symbol(result, "shop.app", SymbolKind.MODULE)
        self.assertEqual(module.id.container_path, ())
        self.assertEqual(module.signature, "module shop.app")
        import_tuples = {
            (item.module, item.name, item.alias, item.wildcard)
            for item in result.imports
        }
        self.assertIn(
            ("shop.engine", "PricingEngine", None, False),
            import_tuples,
        )
        self.assertIn(("shop.ids.OrderId", "of", None, False), import_tuples)
        self.assertIn(("java.util", None, None, True), import_tuples)
        self.assertEqual(
            [(call.receiver, call.name) for call in result.calls],
            [(None, "Handler"), ("OrderId", "of"), ("engine", "evaluate")],
        )
        self.assertEqual(
            [(call.kind, call.arity) for call in result.calls],
            [
                (CallKind.CONSTRUCT, 0),
                (CallKind.CALL, 1),
                (CallKind.CALL, 1),
            ],
        )
        run = symbol(result, "run", SymbolKind.METHOD)
        self.assertIn(Binding("engine", "PricingEngine"), run.bindings)
        self.assertIn(Binding("raw", "String"), run.bindings)
        self.assertIn(Binding("id", "OrderId"), run.bindings)
        self.assertFalse(result.diagnostics)
        assert_body_fact_events(self, result)

    def test_invalid_alias_is_diagnosed_but_valid_facts_survive(self) -> None:
        source = snapshot(INVALID_IMPORTS)

        result = extract_file(source)
        project = extract_project(Path("/repo"), (source,))

        self.assertEqual(result.module, "shop.app")
        self.assertIn("App", {item.name for item in result.symbols})
        self.assertEqual(
            [diagnostic.code for diagnostic in result.diagnostics],
            ["tree-sitter-syntax-error"],
        )
        self.assertEqual(
            result.diagnostics[0].severity,
            DiagnosticSeverity.ERROR,
        )
        self.assertFalse(project.complete)
        imports = {(item.module, item.name, item.wildcard) for item in result.imports}
        self.assertNotIn(("shop.ids", "OrderId", False), imports)
        self.assertIn(("shop.engine", "PricingEngine", False), imports)
        self.assertIn(("shop.ids.OrderId", "of", False), imports)
        self.assertIn(("java.util", None, True), imports)
        self.assertEqual(
            [(call.receiver, call.name) for call in result.calls],
            [(None, "Handler"), ("OrderId", "of"), ("engine", "evaluate")],
        )

    def test_fixture_declarations_preserve_v1_shape_and_add_fields(self) -> None:
        pricing = extract_file(fixture_snapshot("src/engine/PricingEngine.java"))
        pricing_type = symbol(pricing, "PricingEngine", SymbolKind.CLASS)
        evaluate = symbol(pricing, "evaluate", SymbolKind.METHOD)
        constructor = symbol(pricing, "PricingEngine", SymbolKind.CONSTRUCTOR)
        field = symbol(pricing, "basePrices", SymbolKind.FIELD)

        self.assertEqual(pricing.module, "shop.engine")
        self.assertEqual(pricing_type.supers, ("PricePort",))
        self.assertEqual(pricing_type.visibility, Visibility.PUBLIC)
        self.assertEqual(evaluate.params, ("OrderId", "List<ItemId>"))
        self.assertEqual(evaluate.returns, "Quote")
        self.assertEqual(evaluate.raises, ("UnknownItemException",))
        self.assertEqual(constructor.params, ("Map<ItemId,Long>",))
        self.assertEqual(constructor.returns, "PricingEngine")
        self.assertEqual(field.id.container_path, ("PricingEngine",))
        self.assertEqual(field.visibility, Visibility.PRIVATE)
        self.assertEqual(
            set(evaluate.bindings),
            {
                Binding("basePrices", "Map"),
                Binding("order", "OrderId"),
                Binding("items", "List"),
                Binding("total", "long"),
                Binding("item", "ItemId"),
                Binding("price", "Long"),
            },
        )

        interface = extract_file(fixture_snapshot("src/engine/PricePort.java"))
        price_port = symbol(interface, "PricePort", SymbolKind.INTERFACE)
        methods = {
            item.name: item
            for item in interface.symbols
            if item.kind is SymbolKind.METHOD
        }
        self.assertEqual(price_port.supers, ())
        self.assertEqual(methods["quoteFor"].returns, "Quote")
        self.assertEqual(methods["supports"].returns, "boolean")
        self.assertFalse(
            any(
                body.owner in {item.id for item in methods.values()}
                for body in interface.bodies
            )
        )

        record = extract_file(fixture_snapshot("src/delta/AddOp.java"))
        add_op = symbol(record, "AddOp", SymbolKind.RECORD)
        self.assertEqual(add_op.params, ("String",))
        self.assertEqual(add_op.components, ("nodeId",))
        self.assertEqual(add_op.supers, ("DeltaOp",))

        sealed = extract_file(fixture_snapshot("src/delta/DeltaOp.java"))
        delta_op = symbol(sealed, "DeltaOp", SymbolKind.INTERFACE)
        self.assertEqual(delta_op.permits, ("AddOp", "RemoveOp"))
        self.assertIn("sealed", delta_op.modifiers)

        enum = extract_file(fixture_snapshot("src/engine/OrderStatus.java"))
        order_status = symbol(enum, "OrderStatus", SymbolKind.ENUM)
        self.assertEqual(order_status.params, ("NEW", "PAID", "SHIPPED"))
        self.assertEqual(order_status.components, ("NEW", "PAID", "SHIPPED"))
        self.assertEqual(
            {item.name for item in enum.symbols if item.kind is SymbolKind.CONSTANT},
            {"NEW", "PAID", "SHIPPED"},
        )

    def test_interface_extends_and_compact_record_constructor(self) -> None:
        interface_source = snapshot(
            b"package p; interface Child extends Left, Right { void ping(); }",
            file="src/Child.java",
        )
        interface = extract_file(interface_source)
        child = symbol(interface, "Child", SymbolKind.INTERFACE)
        self.assertEqual(child.supers, ("Left", "Right"))

        record = extract_file(snapshot(COMPACT_RECORD, file="src/Ticket.java"))
        ticket = symbol(record, "Ticket", SymbolKind.RECORD)
        constructor = symbol(record, "Ticket", SymbolKind.CONSTRUCTOR)
        self.assertEqual(ticket.params, ("String", "int"))
        self.assertEqual(ticket.components, ("code", "count"))
        self.assertEqual(ticket.supers, ("Identified",))
        self.assertEqual(constructor.params, ("String", "int"))
        self.assertEqual(
            constructor.bindings,
            (Binding("code", "String"), Binding("count", "int")),
        )
        body = next(item for item in record.bodies if item.owner == constructor.id)
        self.assertEqual(
            [event.text for event in body.events if event.kind is BodyEventKind.PARAM],
            ["code", "count"],
        )
        self.assertIn(
            (None, "validate", CallKind.CALL, 1),
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in record.calls
            ],
        )
        assert_body_fact_events(self, record)

    def test_framework_annotations_modifiers_main_and_callback_reference(self) -> None:
        result = extract_file(
            snapshot(FRAMEWORK_SOURCE, file="src/framework/Config.java")
        )
        config = symbol(result, "Config", SymbolKind.CLASS)
        refresh = symbol(result, "onRefresh", SymbolKind.METHOD)
        run = symbol(result, "run", SymbolKind.METHOD)
        listen = symbol(result, "listen", SymbolKind.METHOD)
        main = symbol(result, "main", SymbolKind.METHOD)

        self.assertEqual(config.modifiers, ("public", "final"))
        self.assertEqual(refresh.annotations, ("Bean",))
        self.assertEqual(run.annotations, ("Override",))
        self.assertEqual(refresh.span.start_line, 5)
        self.assertEqual(run.span.start_line, 8)
        self.assertEqual(listen.span.start_line, 11)
        self.assertEqual(run.modifiers, ("protected",))
        self.assertEqual(listen.annotations, ('EventListener("onRefresh")',))
        self.assertEqual(main.modifiers, ("public", "static"))
        self.assertEqual(main.name, "main")
        self.assertEqual(main.params, ("String[]",))
        self.assertEqual(main.returns, "void")

        callbacks = [
            reference
            for reference in result.references
            if reference.name == "onRefresh"
            and reference.context is ReferenceContext.ANNOTATION
        ]
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].owner, listen.id)
        self.assertEqual(callbacks[0].kind, ReferenceKind.NAME)
        self.assertIsNone(callbacks[0].qualifier)
        self.assertEqual(callbacks[0].confidence, ReferenceConfidence.POSSIBLE)
        self.assertEqual(callbacks[0].span.start_line, 10)
        self.assertFalse(
            any(
                reference.name == "onRefresh"
                and reference.context is ReferenceContext.STRING
                for reference in result.references
            )
        )
        annotation_refs = [
            reference
            for reference in result.references
            if reference.context is ReferenceContext.ANNOTATION
        ]
        self.assertTrue(annotation_refs)
        self.assertTrue(
            all(
                reference.confidence is ReferenceConfidence.POSSIBLE
                for reference in annotation_refs
            )
        )
        assert_body_fact_events(self, result)

    def test_nested_fields_ids_spans_utf8_and_bindings(self) -> None:
        original = extract_file(snapshot(NESTED_SOURCE, file="src/nested/Outer.java"))
        shifted = extract_file(
            snapshot(b"\n" + NESTED_SOURCE, file="src/nested/Outer.java")
        )

        by_key = {(item.kind, item.name): item for item in original.symbols}
        self.assertTrue(
            {
                (SymbolKind.CONSTANT, "LABEL"),
                (SymbolKind.FIELD, "count"),
                (SymbolKind.CLASS, "Inner"),
                (SymbolKind.FIELD, "field"),
                (SymbolKind.METHOD, "work"),
            }.issubset(by_key)
        )
        label = by_key[(SymbolKind.CONSTANT, "LABEL")]
        count = by_key[(SymbolKind.FIELD, "count")]
        inner = by_key[(SymbolKind.CLASS, "Inner")]
        field = by_key[(SymbolKind.FIELD, "field")]
        work = by_key[(SymbolKind.METHOD, "work")]
        self.assertEqual(label.id.container_path, ("Outer",))
        self.assertEqual(count.id.container_path, ("Outer",))
        self.assertEqual(inner.id.container_path, ("Outer",))
        self.assertEqual(field.id.container_path, ("Outer", "Inner"))
        self.assertEqual(work.id.container_path, ("Outer", "Inner"))
        self.assertEqual(label.span.file, "src/nested/Outer.java")
        self.assertEqual(label.span.start_line, 4)

        self.assertEqual(
            [item.id for item in original.symbols],
            [item.id for item in shifted.symbols],
        )
        self.assertEqual(
            [item.span.start_line + 1 for item in original.symbols],
            [item.span.start_line for item in shifted.symbols],
        )
        original_calls = [
            call
            for call in original.calls
            if call.caller == work.id and call.name == "target"
        ]
        self.assertEqual(len(original_calls), 1)
        call = original_calls[0]
        line = NESTED_SOURCE.splitlines()[9]
        self.assertEqual(
            call.span.start_column,
            len(line[: line.index(b"target")]),
        )
        self.assertGreater(
            call.span.start_column,
            NESTED_SOURCE.decode().splitlines()[9].index("target"),
        )

        self.assertIn(Binding("input", "Input"), work.bindings)
        self.assertIn(Binding("note", "String"), work.bindings)
        symbol_names = {item.name for item in original.symbols}
        self.assertNotIn("input", symbol_names)
        self.assertNotIn("note", symbol_names)
        body = next(item for item in original.bodies if item.owner == work.id)
        self.assertIn(
            (BodyEventKind.PARAM, "input"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertIn(
            (BodyEventKind.LOCAL, "note"),
            {(event.kind, event.text) for event in body.events},
        )

    def test_body_events_are_complete_balanced_and_exactly_joined(self) -> None:
        result = extract_file(snapshot(BODY_SOURCE, file="src/body/Worker.java"))
        flow = symbol(result, "flow", SymbolKind.METHOD)
        body = next(item for item in result.bodies if item.owner == flow.id)
        kinds = {event.kind for event in body.events}
        self.assertTrue(
            {
                BodyEventKind.PARAM,
                BodyEventKind.LOCAL,
                BodyEventKind.NAME,
                BodyEventKind.TYPE,
                BodyEventKind.CALL,
                BodyEventKind.CONSTRUCT,
                BodyEventKind.MEMBER,
                BodyEventKind.LITERAL,
                BodyEventKind.OPERATOR,
                BodyEventKind.KEYWORD,
                BodyEventKind.CONTROL_ENTER,
                BodyEventKind.CONTROL_EXIT,
            }.issubset(kinds)
        )
        validate_body_events(body.events)
        controls = [
            event.text
            for event in body.events
            if event.kind is BodyEventKind.CONTROL_ENTER
        ]
        self.assertEqual(controls, ["loop", "if", "try", "catch", "finally"])
        self.assertEqual(
            sorted(
                event.text
                for event in body.events
                if event.kind is BodyEventKind.CONTROL_ENTER
            ),
            sorted(
                event.text
                for event in body.events
                if event.kind is BodyEventKind.CONTROL_EXIT
            ),
        )
        self.assertIn(Binding("widget", "Widget"), flow.bindings)
        self.assertIn(Binding("error", "RuntimeException"), flow.bindings)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
            ],
            [
                ("item", "length", CallKind.CALL, 0),
                (None, "Widget", CallKind.CONSTRUCT, 1),
                ("service", "handle", CallKind.CALL, 1),
                (None, "finish", CallKind.CALL, 1),
            ],
        )
        assert_body_fact_events(self, result)

    def test_calls_are_uncapped_and_preserve_arity_receiver_and_construction(
        self,
    ) -> None:
        statements = b"\n".join(
            f"    call{index}({index});".encode() for index in range(15)
        )
        raw = (
            b"""\
package shop.calls;
class Calls {
  void all(String raw) {
    Widget widget = new Widget();
    OrderId.of(raw);
    factory.make(raw, widget);
"""
            + statements
            + b"""
  }
}
"""
        )
        result = extract_file(snapshot(raw, file="src/calls/Calls.java"))
        all_method = symbol(result, "all", SymbolKind.METHOD)
        owned = [call for call in result.calls if call.caller == all_method.id]

        self.assertEqual(len(owned), 18)
        self.assertEqual(
            [(call.receiver, call.name, call.kind, call.arity) for call in owned[:3]],
            [
                (None, "Widget", CallKind.CONSTRUCT, 0),
                ("OrderId", "of", CallKind.CALL, 1),
                ("factory", "make", CallKind.CALL, 2),
            ],
        )
        self.assertEqual(
            [call.name for call in owned[3:]], [f"call{i}" for i in range(15)]
        )
        self.assertTrue(all(call.arity == 1 for call in owned[3:]))
        self.assertIn(Binding("widget", "Widget"), all_method.bindings)
        assert_body_fact_events(self, result)

    def test_commented_qualified_package_and_imports_are_structural(self) -> None:
        raw = b"""\
@PackageInfo
package shop /* package segment */ . app /* package segment */ . api;
import shop /* import segment */ . engine /* import segment */ . Engine;
import static shop /* import segment */ . Factory /* import segment */ . make;
import java /* import segment */ . util /* import segment */ . *;

class Probe {}
"""

        result = extract_file(snapshot(raw, file="src/api/package-info.java"))

        self.assertFalse(result.diagnostics)
        self.assertEqual(result.module, "shop.app.api")
        self.assertEqual(
            [(item.module, item.name, item.wildcard) for item in result.imports],
            [
                ("shop.engine", "Engine", False),
                ("shop.Factory", "make", False),
                ("java.util", None, True),
            ],
        )

    def test_type_use_and_body_annotations_are_only_possible_references(self) -> None:
        raw = b"""\
class Probe {
  List<@ElementAnno String> field;

  @ReturnAnno String run(@ParamAnno String input) {
    @LocalAnno String local = (@CastAnno String) input;
    return local;
  }
}
"""

        result = extract_file(snapshot(raw, file="src/Probe.java"))
        annotation_names = {
            "ElementAnno",
            "ReturnAnno",
            "ParamAnno",
            "LocalAnno",
            "CastAnno",
        }
        found = [item for item in result.references if item.name in annotation_names]

        self.assertEqual({item.name for item in found}, annotation_names)
        self.assertTrue(
            all(
                item.context is ReferenceContext.ANNOTATION
                and item.confidence is ReferenceConfidence.POSSIBLE
                and item.kind is ReferenceKind.TYPE
                for item in found
            )
        )
        self.assertEqual(len(found), len(annotation_names))
        annotation_spans = {item.span for item in found}
        self.assertFalse(
            any(
                item.span in annotation_spans
                and item.confidence is ReferenceConfidence.DEFINITE
                for item in result.references
            )
        )
        assert_body_fact_events(self, result)

    def test_member_shadowing_and_lambda_reachability_keep_exact_evidence(self) -> None:
        raw = b"""\
package app;
class Reachability {
  private static final int LIMIT = 1;
  private int value;
  private Reachability() {}
  private int helper() { return value; }
  void run(int value) {
    this.value = value;
    stream.map(item -> helper() + LIMIT);
  }
}
"""
        result = extract_file(snapshot(raw, file="src/Reachability.java"))
        run = symbol(result, "run", SymbolKind.METHOD)

        field_reference = next(
            item
            for item in result.references
            if item.owner == run.id
            and item.name == "value"
            and item.qualifier == "this"
        )
        self.assertIs(field_reference.confidence, ReferenceConfidence.DEFINITE)
        possible = {
            item.name
            for item in result.references
            if item.owner is None and item.confidence is ReferenceConfidence.POSSIBLE
        }
        self.assertTrue({"helper", "LIMIT"}.issubset(possible))

    def test_local_event_listener_callback_joins_a_literal_and_name_event(
        self,
    ) -> None:
        raw = b"""\
class Probe {
  void run(String value) {
    @EventListener("onRefresh") String selected = value;
    @Other("onRefresh") String ordinary = value;
  }
}
"""

        result = extract_file(snapshot(raw, file="src/Probe.java"))
        run = symbol(result, "run", SymbolKind.METHOD)
        callback = [
            item
            for item in result.references
            if item.name == "onRefresh" and item.context is ReferenceContext.ANNOTATION
        ]

        self.assertEqual(len(callback), 1)
        self.assertEqual(callback[0].owner, run.id)
        self.assertEqual(callback[0].kind, ReferenceKind.NAME)
        self.assertEqual(
            callback[0].confidence,
            ReferenceConfidence.POSSIBLE,
        )
        self.assertEqual(callback[0].span.start_line, 3)
        body = next(item for item in result.bodies if item.owner == run.id)
        same_span = [
            (event.kind, event.text)
            for event in body.events
            if event.span == callback[0].span
        ]
        self.assertIn((BodyEventKind.LITERAL, "<string>"), same_span)
        self.assertIn((BodyEventKind.NAME, "onRefresh"), same_span)
        assert_body_fact_events(self, result)

    def test_initializers_emit_calls_and_references_under_their_owner(self) -> None:
        raw = b"""\
class Probe {
  static final Factory FACTORY = Factory.create(Seed.VALUE);
  Widget field = new Widget(Source.value);

  static { Registry.install(FACTORY); }
  { helper(field); }
}
"""

        result = extract_file(snapshot(raw, file="src/Probe.java"))
        probe = symbol(result, "Probe", SymbolKind.CLASS)
        factory = symbol(result, "FACTORY", SymbolKind.CONSTANT)
        field = symbol(result, "field", SymbolKind.FIELD)

        self.assertIn(
            (factory.id, "Factory", "create", CallKind.CALL),
            {
                (call.caller, call.receiver, call.name, call.kind)
                for call in result.calls
            },
        )
        self.assertIn(
            (field.id, None, "Widget", CallKind.CONSTRUCT),
            {
                (call.caller, call.receiver, call.name, call.kind)
                for call in result.calls
            },
        )
        self.assertEqual(
            [
                (call.receiver, call.name)
                for call in result.calls
                if call.caller == probe.id
            ],
            [("Registry", "install"), (None, "helper")],
        )
        owned_reference_names = {
            owner: {
                reference.name
                for reference in result.references
                if reference.owner == owner
                and reference.confidence is ReferenceConfidence.DEFINITE
            }
            for owner in (factory.id, field.id, probe.id)
        }
        self.assertTrue({"Seed", "VALUE"}.issubset(owned_reference_names[factory.id]))
        self.assertTrue({"Source", "value"}.issubset(owned_reference_names[field.id]))
        self.assertTrue(
            {"Registry", "FACTORY", "field"}.issubset(owned_reference_names[probe.id])
        )

    def test_class_and_method_generic_bounds_emit_type_references(self) -> None:
        raw = b"""\
class Box<T extends Base & Marker> {
  <U extends Helper & Comparable<U>> U convert(U value) { return value; }
}
"""

        result = extract_file(snapshot(raw, file="src/Box.java"))
        box = symbol(result, "Box", SymbolKind.CLASS)
        convert = symbol(result, "convert", SymbolKind.METHOD)
        by_owner = {
            owner: {
                reference.name
                for reference in result.references
                if reference.owner == owner
                and reference.kind is ReferenceKind.TYPE
                and reference.confidence is ReferenceConfidence.DEFINITE
            }
            for owner in (box.id, convert.id)
        }

        self.assertTrue({"Base", "Marker"}.issubset(by_owner[box.id]))
        self.assertTrue({"Helper", "Comparable"}.issubset(by_owner[convert.id]))

    def test_local_types_under_overloads_have_signature_aware_containers(self) -> None:
        raw = b"""\
class Host {
  void run(String value) { class Local { void act() { first(); } } }
  void run(int value) { class Local { void act() { second(); } } }
}
"""

        result = extract_file(snapshot(raw, file="src/Host.java"))
        locals_ = [
            item
            for item in result.symbols
            if item.name == "Local" and item.kind is SymbolKind.CLASS
        ]
        actions = [
            item
            for item in result.symbols
            if item.name == "act" and item.kind is SymbolKind.METHOD
        ]

        self.assertEqual(
            {item.id.container_path for item in locals_},
            {("Host", "run(String)"), ("Host", "run(int)")},
        )
        self.assertEqual(
            {item.id.container_path for item in actions},
            {
                ("Host", "run(String)", "Local"),
                ("Host", "run(int)", "Local"),
            },
        )
        self.assertEqual(len({item.id for item in (*locals_, *actions)}), 4)

    def test_enum_constant_class_body_members_are_owned_by_the_constant(self) -> None:
        raw = b"""\
enum Mode {
  E { @Override void act() { service.run(); } },
  A { @Override void act() { helper(); } };
  abstract void act();
}
"""

        result = extract_file(snapshot(raw, file="src/Mode.java"))
        actions = [
            item
            for item in result.symbols
            if item.name == "act" and item.kind is SymbolKind.METHOD
        ]
        constant_actions = [
            item
            for item in actions
            if item.id.container_path in {("Mode", "E"), ("Mode", "A")}
        ]

        self.assertEqual(
            {item.id.container_path for item in constant_actions},
            {("Mode", "E"), ("Mode", "A")},
        )
        calls_by_owner = {
            action.id: [
                (call.receiver, call.name)
                for call in result.calls
                if call.caller == action.id
            ]
            for action in constant_actions
        }
        e = next(action for action in constant_actions if action.container == "E")
        a = next(action for action in constant_actions if action.container == "A")
        self.assertEqual(calls_by_owner[e.id], [("service", "run")])
        self.assertEqual(calls_by_owner[a.id], [(None, "helper")])
        assert_body_fact_events(self, result)

    def test_java_implicit_member_visibility_matches_the_language(self) -> None:
        raw = b"""\
interface Contract {
  class NestedClass {}
  interface NestedInterface {}
  enum NestedEnum { VALUE }
  record NestedRecord(int value) {}
}

enum Shade {
  LIGHT;
  Shade() {}
}
"""

        result = extract_file(snapshot(raw, file="src/Contract.java"))
        member_types = {
            item.name: item for item in result.symbols if item.name.startswith("Nested")
        }
        shade_constructor = symbol(result, "Shade", SymbolKind.CONSTRUCTOR)

        self.assertEqual(
            {item.visibility for item in member_types.values()},
            {Visibility.PUBLIC},
        )
        self.assertEqual(shade_constructor.visibility, Visibility.PRIVATE)

    def test_java_legacy_calls_keep_v1_projection_without_losing_canonical_facts(
        self,
    ) -> None:
        raw = b"""\
class Probe {
  void execute() {
    service.get().run();
    new p.Foo();
    new int[2];
  }
}
"""
        source = snapshot(raw, file="src/Probe.java")

        canonical = extract_file(source)
        execute = symbol(canonical, "execute", SymbolKind.METHOD)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind)
                for call in canonical.calls
                if call.caller == execute.id
            ],
            [
                ("service.get()", "run", CallKind.CALL),
                ("service", "get", CallKind.CALL),
                (None, "p.Foo", CallKind.CONSTRUCT),
                (None, "int", CallKind.CONSTRUCT),
            ],
        )

        legacy = hologram.extract_file(
            Path("/repo/src/Probe.java"),
            Path("/repo"),
            text=raw.decode(),
        )
        legacy_execute = next(item for item in legacy if item.name == "execute")
        self.assertEqual(legacy_execute.calls, ["run", "service.get", "p.Foo"])

    def test_java_legacy_calls_drop_this_and_super_receivers(self) -> None:
        raw = b"""\
class Probe extends Parent {
  void execute() {
    this.own();
    super.base();
  }
}
"""
        source = snapshot(raw, file="src/Probe.java")

        canonical = extract_file(source)
        execute = symbol(canonical, "execute", SymbolKind.METHOD)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind)
                for call in canonical.calls
                if call.caller == execute.id
            ],
            [
                ("this", "own", CallKind.CALL),
                ("super", "base", CallKind.CALL),
            ],
        )

        legacy = hologram.extract_file(
            Path("/repo/src/Probe.java"),
            Path("/repo"),
            text=raw.decode(),
        )
        legacy_execute = next(item for item in legacy if item.name == "execute")
        self.assertEqual(legacy_execute.calls, ["own", "base"])

    def test_package_root_no_longer_exposes_private_java_extractor(self) -> None:
        self.assertNotIn("_extract_java", hologram.__dict__)
        with self.assertRaises(AttributeError):
            hologram.__getattr__("_extract_java")


if __name__ == "__main__":
    unittest.main()
