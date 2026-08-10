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
    Language,
    SourceFile,
    SourceRole,
    SymbolKind,
)
from hologram.parsers.api import extract_file
from hologram.parsers.common import validate_body_events
from tests.parser_assertions import assert_body_fact_events

CSHARP_SOURCE = b"""\
using Pricing = Shop.Engine.PricingEngine;
using Shop.Ids;
namespace Shop.App;

[Service]
public record OrderId(string Value);
public enum Status { New, Paid }
public interface IPrice { Quote Run(OrderId id); }
public class Outer : Base, IPrice {
    private const int Limit = 3;
    public string Name { get; init; }
    public class Inner {
        public Quote Work(OrderId id) => new Quote(id);
    }
    public Outer(string name) { Name = name; }
    [Override]
    public override Quote Run(OrderId id) {
        var x = new Quote(id);
        if (id.Valid()) return this.Help(x);
        return x;
    }
    private Quote Help(Quote x) => x;
    public static void Main(string[] args) { Boot(args); }
}
"""


KOTLIN_SOURCE = b"""\
package shop.app
import shop.engine.PricingEngine as Engine
import shop.ids.*

const val LIMIT: Int = 3
data class OrderId(val value: String)
enum class Status { NEW, PAID }
interface Price { fun run(id: OrderId): Quote }
open class Base
@Service
class Outer(val name: String) : Base(), Price {
    val title: String = name
    companion object {
        const val RATE: Int = 2
    }
    inner class Inner {
        fun work(id: OrderId): Quote = Quote(id)
    }
    constructor() : this("x")
    override fun run(id: OrderId): Quote {
        val x = Engine().get(id)
        if (id.valid()) return help(x)
        return x
    }
    private fun help(x: Quote): Quote = x
}

fun main(args: Array<String>) { boot(args) }
"""


def snapshot(raw: bytes, language: Language, file: str) -> SourceFile:
    return SourceFile(
        Path("/missing") / file,
        file,
        language,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def symbol(result, name: str, kind: SymbolKind | None = None):
    return next(
        item
        for item in result.symbols
        if item.name == name and (kind is None or item.kind is kind)
    )


@unittest.skipUnless(hologram.has_parser("csharp"), "tree-sitter-c-sharp not installed")
class CSharpParserTest(unittest.TestCase):
    def test_namespace_alias_imports_types_members_and_snapshot_are_canonical(
        self,
    ) -> None:
        source = snapshot(CSHARP_SOURCE, Language.CSHARP, "src/Outer.cs")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("disk reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("disk reread")),
        ):
            result = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(result.module, "Shop.App")
        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [
                ("Shop.Engine", "PricingEngine", "Pricing", False),
                ("Shop.Ids", None, None, True),
            ],
        )
        outer = symbol(result, "Outer", SymbolKind.CLASS)
        self.assertEqual(outer.supers, ("Base", "IPrice"))
        self.assertEqual(
            symbol(result, "Inner", SymbolKind.CLASS).id.container_path, ("Outer",)
        )
        self.assertEqual(
            symbol(result, "Work", SymbolKind.METHOD).id.container_path,
            ("Outer", "Inner"),
        )
        self.assertEqual(symbol(result, "Limit", SymbolKind.CONSTANT).returns, "int")
        self.assertEqual(symbol(result, "Name", SymbolKind.PROPERTY).returns, "string")
        record = symbol(result, "OrderId", SymbolKind.RECORD)
        self.assertEqual(record.params, ("string",))
        self.assertEqual(record.components, ("Value",))
        self.assertIn("Service", record.annotations)

    def test_constructors_calls_bindings_modifiers_bodies_and_ids_are_complete(
        self,
    ) -> None:
        result = extract_file(snapshot(CSHARP_SOURCE, Language.CSHARP, "src/Outer.cs"))
        constructor = symbol(result, "Outer", SymbolKind.CONSTRUCTOR)
        run = symbol(result, "Run", SymbolKind.METHOD)
        main = symbol(result, "Main", SymbolKind.METHOD)

        self.assertEqual(constructor.params, ("string",))
        self.assertEqual(constructor.returns, "Outer")
        self.assertIn(Binding("name", "string"), constructor.bindings)
        self.assertTrue(
            {Binding("id", "OrderId"), Binding("x", "Quote")}.issubset(
                set(run.bindings)
            )
        )
        self.assertIn("override", run.modifiers)
        self.assertIn("Override", run.annotations)
        self.assertIn("static", main.modifiers)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
                if call.caller == run.id
            ],
            [
                (None, "Quote", CallKind.CONSTRUCT, 1),
                ("id", "Valid", CallKind.CALL, 0),
                ("this", "Help", CallKind.CALL, 1),
            ],
        )
        work = symbol(result, "Work", SymbolKind.METHOD)
        self.assertEqual(
            next(
                body for body in result.bodies if body.owner == work.id
            ).span.start_line,
            13,
        )
        body = next(item for item in result.bodies if item.owner == run.id)
        validate_body_events(body.events)
        self.assertTrue(
            {
                BodyEventKind.PARAM,
                BodyEventKind.LOCAL,
                BodyEventKind.CALL,
                BodyEventKind.CONSTRUCT,
                BodyEventKind.CONTROL_ENTER,
                BodyEventKind.CONTROL_EXIT,
            }.issubset({event.kind for event in body.events})
        )
        assert_body_fact_events(self, result)

        shifted = extract_file(
            snapshot(b"\n" + CSHARP_SOURCE, Language.CSHARP, "src/Outer.cs")
        )
        self.assertEqual(
            {item.id for item in result.symbols}, {item.id for item in shifted.symbols}
        )

    def test_legacy_projection_filters_new_members_and_strips_this(self) -> None:
        projected = hologram.extract_file(
            Path("/repo/src/Outer.cs"),
            Path("/repo"),
            text=CSHARP_SOURCE.decode(),
        )
        names = {item.name for item in projected}
        run = next(
            item
            for item in projected
            if item.name == "Run" and item.container == "Outer"
        )

        self.assertTrue({"Outer", "Inner", "Work"}.issubset(names))
        self.assertTrue({"Value", "Name", "Limit"}.isdisjoint(names))
        self.assertEqual(run.calls, ["Quote", "id.Valid", "Help"])
        self.assertEqual(
            run.bindings,
            {"Limit": "int", "id": "OrderId", "x": "Quote"},
        )


@unittest.skipUnless(hologram.has_parser("kotlin"), "tree-sitter-kotlin not installed")
class KotlinParserTest(unittest.TestCase):
    def test_package_imports_types_components_nested_members_and_constants(
        self,
    ) -> None:
        result = extract_file(snapshot(KOTLIN_SOURCE, Language.KOTLIN, "src/Outer.kt"))

        self.assertEqual(result.module, "shop.app")
        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [
                ("shop.engine.PricingEngine", None, "Engine", False),
                ("shop.ids", None, None, True),
            ],
        )
        outer = symbol(result, "Outer", SymbolKind.CLASS)
        self.assertEqual(outer.supers, ("Base", "Price"))
        self.assertIn("Service", outer.annotations)
        self.assertEqual(
            symbol(result, "Inner", SymbolKind.CLASS).id.container_path, ("Outer",)
        )
        self.assertEqual(
            symbol(result, "work", SymbolKind.METHOD).id.container_path,
            ("Outer", "Inner"),
        )
        record = symbol(result, "OrderId", SymbolKind.RECORD)
        self.assertEqual(record.params, ("String",))
        self.assertEqual(record.components, ("value",))
        self.assertEqual(
            symbol(result, "value", SymbolKind.PROPERTY).id.container_path, ("OrderId",)
        )
        self.assertEqual(
            symbol(result, "LIMIT", SymbolKind.CONSTANT).id.container_path, ()
        )
        self.assertEqual(
            symbol(result, "RATE", SymbolKind.CONSTANT).id.container_path,
            ("Outer", "Companion"),
        )

    def test_constructors_calls_bindings_modifiers_bodies_and_ids_are_complete(
        self,
    ) -> None:
        result = extract_file(snapshot(KOTLIN_SOURCE, Language.KOTLIN, "src/Outer.kt"))
        constructors = [
            item
            for item in result.symbols
            if item.kind is SymbolKind.CONSTRUCTOR
            and item.id.container_path == ("Outer",)
        ]
        self.assertEqual({item.params for item in constructors}, {("String",), ()})
        run = symbol(result, "run", SymbolKind.METHOD)
        main = symbol(result, "main", SymbolKind.FUNCTION)
        self.assertTrue(
            {Binding("id", "OrderId"), Binding("x", "Engine")}.issubset(
                set(run.bindings)
            )
        )
        self.assertIn("override", run.modifiers)
        self.assertIn("entrypoint", main.annotations)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
                if call.caller == run.id
            ],
            [
                (None, "Engine", CallKind.CONSTRUCT, 0),
                ("Engine()", "get", CallKind.CALL, 1),
                ("id", "valid", CallKind.CALL, 0),
                (None, "help", CallKind.CALL, 1),
            ],
        )
        help_symbol = symbol(result, "help", SymbolKind.METHOD)
        self.assertEqual(
            next(
                body for body in result.bodies if body.owner == help_symbol.id
            ).span.start_line,
            25,
        )
        body = next(item for item in result.bodies if item.owner == run.id)
        validate_body_events(body.events)
        self.assertTrue(
            {
                BodyEventKind.PARAM,
                BodyEventKind.LOCAL,
                BodyEventKind.CALL,
                BodyEventKind.CONSTRUCT,
                BodyEventKind.CONTROL_ENTER,
                BodyEventKind.CONTROL_EXIT,
            }.issubset({event.kind for event in body.events})
        )
        assert_body_fact_events(self, result)

        shifted = extract_file(
            snapshot(b"\n" + KOTLIN_SOURCE, Language.KOTLIN, "src/Outer.kt")
        )
        self.assertEqual(
            {item.id for item in result.symbols}, {item.id for item in shifted.symbols}
        )

    def test_legacy_projection_filters_constructors_and_collapses_receivers(
        self,
    ) -> None:
        projected = hologram.extract_file(
            Path("/repo/src/Outer.kt"),
            Path("/repo"),
            text=KOTLIN_SOURCE.decode(),
        )
        names = {item.name for item in projected}
        run = next(
            item
            for item in projected
            if item.name == "run" and item.container == "Outer"
        )

        self.assertFalse(any(item.kind == "ctor" for item in projected))
        self.assertNotIn("Companion", names)
        self.assertTrue({"Outer", "Inner", "work"}.issubset(names))
        self.assertEqual(run.calls, ["get", "Engine", "id.valid", "help"])
        self.assertEqual(run.bindings, {"name": "String", "id": "OrderId"})


if __name__ == "__main__":
    unittest.main()
