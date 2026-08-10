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

    def test_package_root_no_longer_exposes_private_java_extractor(self) -> None:
        self.assertNotIn("_extract_java", hologram.__dict__)
        with self.assertRaises(AttributeError):
            hologram.__getattr__("_extract_java")


if __name__ == "__main__":
    unittest.main()
