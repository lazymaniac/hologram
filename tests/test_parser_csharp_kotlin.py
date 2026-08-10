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
    Language,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    SourceFile,
    SourceRole,
    SourceSpan,
    SymbolKind,
)
from hologram.parsers.api import extract_file, extract_project
from hologram.parsers.common import validate_body_events
from hologram.resolve import ResolutionStatus, resolve_project
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

    def test_all_namespaces_accessors_and_anonymous_facts_have_exact_owners(
        self,
    ) -> None:
        raw = b"""\
namespace Alpha {
    public class Same {
        public int Value {
            get { return read(); }
            set { write(value); }
        }
        public int Computed => compute();
        public Same() { initialize(); }
        public void Run(OrderId input) {
            System.Func<OrderId, Quote> callback = (OrderId value) => {
                var made = new Quote(value);
                hidden(value, made);
                return made;
            };
            callback(input);
        }
    }
    namespace Nested { public class Deep {} }
}
namespace Beta { public class Same {} }
"""
        result = extract_file(snapshot(raw, Language.CSHARP, "src/Gaps.cs"))

        sole = extract_file(
            snapshot(
                b"namespace Only.Core { public class Kept {} }\n",
                Language.CSHARP,
                "src/Only.cs",
            )
        )
        self.assertEqual(sole.module, "Only.Core")
        self.assertEqual(
            symbol(sole, "Kept", SymbolKind.CLASS).id.container_path,
            (),
        )

        self.assertIsNone(result.module)
        self.assertEqual(
            {
                item.name
                for item in result.symbols
                if item.kind is SymbolKind.MODULE
            },
            {"Alpha", "Alpha.Nested", "Beta"},
        )
        same_types = [
            item
            for item in result.symbols
            if item.name == "Same" and item.kind is SymbolKind.CLASS
        ]
        self.assertEqual(
            [item.id.container_path for item in same_types],
            [("Alpha",), ("Beta",)],
        )
        self.assertEqual(len({item.id for item in same_types}), 2)
        self.assertEqual(
            symbol(result, "Deep", SymbolKind.CLASS).id.container_path,
            ("Alpha", "Nested"),
        )

        accessors = [
            item
            for item in result.symbols
            if item.kind is SymbolKind.METHOD
            and item.id.container_path == ("Alpha", "Same", "Value")
        ]
        self.assertEqual([item.name for item in accessors], ["get", "set"])
        self.assertEqual(
            [
                [call.name for call in result.calls if call.caller == item.id]
                for item in accessors
            ],
            [["read"], ["write"]],
        )
        computed = symbol(result, "Computed", SymbolKind.PROPERTY)
        self.assertEqual(
            [call.name for call in result.calls if call.caller == computed.id],
            ["compute"],
        )
        self.assertTrue(any(body.owner == computed.id for body in result.bodies))

        constructor = next(
            item
            for item in result.symbols
            if item.kind is SymbolKind.CONSTRUCTOR
        )
        self.assertGreater(constructor.body_lines, 0)
        self.assertEqual(
            [call.name for call in result.calls if call.caller == constructor.id],
            ["initialize"],
        )
        run = symbol(result, "Run", SymbolKind.METHOD)
        self.assertTrue(
            {
                Binding("input", "OrderId"),
                Binding("callback", "Func"),
                Binding("value", "OrderId"),
                Binding("made", "Quote"),
            }.issubset(set(run.bindings))
        )
        self.assertEqual(
            [call.name for call in result.calls if call.caller == run.id],
            ["Quote", "hidden", "callback"],
        )
        body = next(item for item in result.bodies if item.owner == run.id)
        self.assertIn(
            (BodyEventKind.PARAM, "value"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertIn(
            (BodyEventKind.LOCAL, "made"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertFalse(
            {"callback", "value", "made"} & {item.name for item in result.symbols}
        )
        self.assertEqual(
            len(result.bodies), len({item.owner for item in result.bodies})
        )
        assert_body_fact_events(self, result)

        shifted = extract_file(
            snapshot(b"\n" + raw, Language.CSHARP, "src/Gaps.cs")
        )
        self.assertEqual(
            {item.id for item in result.symbols},
            {item.id for item in shifted.symbols},
        )

        projected = hologram.extract_file(
            Path("/repo/src/Gaps.cs"),
            Path("/repo"),
            text=raw.decode(),
        )
        projected_constructor = next(
            item for item in projected if item.kind == "ctor"
        )
        self.assertEqual(projected_constructor.size, 0)
        self.assertFalse(any(item.name in {"get", "set"} for item in projected))

    def test_utf8_exact_end_spans_and_reference_provenance(self) -> None:
        line = "    public Quote Run(OrderId é) => é.Valid();".encode()
        raw = b"namespace Utf;\npublic class C {\n" + line + b"\n}\n"
        result = extract_file(snapshot(raw, Language.CSHARP, "src/Utf.cs"))
        run = symbol(result, "Run", SymbolKind.METHOD)
        call = next(item for item in result.calls if item.name == "Valid")
        call_text = "é.Valid()".encode()
        call_start = line.index(call_text)

        self.assertEqual(run.span, SourceSpan("src/Utf.cs", 3, 4, 3, len(line)))
        self.assertEqual(
            call.span,
            SourceSpan(
                "src/Utf.cs",
                3,
                call_start,
                3,
                call_start + len(call_text),
            ),
        )
        order_id = next(
            item
            for item in result.references
            if item.owner == run.id and item.name == "OrderId"
        )
        self.assertEqual(order_id.kind, ReferenceKind.TYPE)
        self.assertEqual(order_id.context, ReferenceContext.TYPE)
        self.assertEqual(order_id.confidence, ReferenceConfidence.DEFINITE)
        type_start = line.index(b"OrderId")
        self.assertEqual(
            order_id.span,
            SourceSpan(
                "src/Utf.cs",
                3,
                type_start,
                3,
                type_start + len(b"OrderId"),
            ),
        )

    def test_reference_context_confidence_and_token_spans_are_exact(self) -> None:
        raw = b"[Mark]\nclass Box : Base { Result Run(Input input) => use(input); }\n"
        result = extract_file(snapshot(raw, Language.CSHARP, "src/Refs.cs"))

        self.assertEqual(
            [
                (
                    item.owner.name,
                    item.name,
                    item.kind,
                    item.context,
                    item.confidence,
                    item.span,
                )
                for item in result.references
            ],
            [
                (
                    "Box",
                    "Mark",
                    ReferenceKind.TYPE,
                    ReferenceContext.ANNOTATION,
                    ReferenceConfidence.POSSIBLE,
                    SourceSpan("src/Refs.cs", 1, 1, 1, 5),
                ),
                (
                    "Box",
                    "Base",
                    ReferenceKind.TYPE,
                    ReferenceContext.TYPE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.cs", 2, 12, 2, 16),
                ),
                (
                    "Run",
                    "Result",
                    ReferenceKind.TYPE,
                    ReferenceContext.TYPE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.cs", 2, 19, 2, 25),
                ),
                (
                    "Run",
                    "Input",
                    ReferenceKind.TYPE,
                    ReferenceContext.TYPE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.cs", 2, 30, 2, 35),
                ),
                (
                    "Run",
                    "use",
                    ReferenceKind.NAME,
                    ReferenceContext.CODE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.cs", 2, 46, 2, 49),
                ),
                (
                    "Run",
                    "input",
                    ReferenceKind.NAME,
                    ReferenceContext.CODE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.cs", 2, 50, 2, 55),
                ),
            ],
        )
        run = symbol(result, "Run", SymbolKind.METHOD)
        self.assertEqual(
            next(call.span for call in result.calls if call.caller == run.id),
            SourceSpan("src/Refs.cs", 2, 46, 2, 56),
        )
        self.assertEqual(
            next(body.span for body in result.bodies if body.owner == run.id),
            SourceSpan("src/Refs.cs", 2, 43, 2, 56),
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
                ("shop.engine", "PricingEngine", "Engine", False),
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

    def test_aliased_top_level_import_keeps_package_and_imported_name(self) -> None:
        library = snapshot(
            b"package p\nfun fetch(): Int = 1\n",
            Language.KOTLIN,
            "src/p/Lib.kt",
        )
        app = snapshot(
            b"package q\nimport p.fetch as load\nfun run(): Int = load()\n",
            Language.KOTLIN,
            "src/q/App.kt",
        )
        project = extract_project(Path("/repo"), (library, app))
        result = next(file for file in project.files if file.source.file == app.file)

        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [("p", "fetch", "load", False)],
        )
        call = next(
            item
            for item in resolve_project(project).calls
            if item.fact.name == "load"
        )
        self.assertEqual(call.status, ResolutionStatus.RESOLVED)
        self.assertEqual(call.target.name if call.target else None, "fetch")

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

    def test_type_alias_enum_extensions_and_anonymous_facts_are_canonical(
        self,
    ) -> None:
        raw = b"""\
package gaps
typealias Alias = String
enum class State { @Deprecated("old") READY, DONE }
fun String.tag(): Quote = make(this)
fun Int.tag(): Quote = make(this)
fun outer(input: OrderId) {
    class Local(val value: Int)
    val callback = { value: OrderId ->
        val made = Widget(value)
        hidden(value, made)
    }
    val plain = { other -> another(other) }
    callback(input)
    plain(input)
}
"""
        result = extract_file(snapshot(raw, Language.KOTLIN, "src/Gaps.kt"))

        alias = symbol(result, "Alias", SymbolKind.TYPE)
        self.assertEqual(alias.params, ("String",))
        target = next(
            item
            for item in result.references
            if item.owner == alias.id and item.name == "String"
        )
        self.assertEqual(target.kind, ReferenceKind.TYPE)
        self.assertEqual(target.context, ReferenceContext.TYPE)
        self.assertEqual(target.confidence, ReferenceConfidence.DEFINITE)

        state = symbol(result, "State", SymbolKind.ENUM)
        self.assertEqual(state.components, ("READY", "DONE"))
        self.assertEqual(
            {
                item.name
                for item in result.symbols
                if item.kind is SymbolKind.CONSTANT
                and item.id.container_path == ("State",)
            },
            {"READY", "DONE"},
        )
        ready = symbol(result, "READY", SymbolKind.CONSTANT)
        self.assertIn("Deprecated", ready.annotations)

        extensions = [
            item
            for item in result.symbols
            if item.name == "tag" and item.kind is SymbolKind.FUNCTION
        ]
        self.assertEqual([item.params for item in extensions], [("String",), ("Int",)])
        self.assertEqual(len({item.id for item in extensions}), 2)
        for extension, receiver in zip(extensions, ("String", "Int"), strict=True):
            self.assertIn(Binding("this", receiver), extension.bindings)
            receiver_reference = next(
                item
                for item in result.references
                if item.owner == extension.id and item.name == receiver
            )
            self.assertEqual(receiver_reference.context, ReferenceContext.TYPE)
            self.assertEqual(
                receiver_reference.confidence,
                ReferenceConfidence.DEFINITE,
            )

        outer = symbol(result, "outer", SymbolKind.FUNCTION)
        self.assertEqual(
            symbol(result, "Local", SymbolKind.CLASS).id.container_path,
            ("outer(OrderId)",),
        )
        self.assertTrue(
            {
                Binding("input", "OrderId"),
                Binding("callback", "?"),
                Binding("plain", "?"),
                Binding("value", "OrderId"),
                Binding("other", "?"),
                Binding("made", "Widget"),
            }.issubset(set(outer.bindings))
        )
        self.assertEqual(
            [call.name for call in result.calls if call.caller == outer.id],
            ["Widget", "hidden", "another", "callback", "plain"],
        )
        body = next(item for item in result.bodies if item.owner == outer.id)
        self.assertIn(
            (BodyEventKind.PARAM, "value"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertIn(
            (BodyEventKind.PARAM, "other"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertIn(
            (BodyEventKind.LOCAL, "made"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertFalse(
            {"callback", "plain", "other", "made"}
            & {item.name for item in result.symbols}
        )
        self.assertEqual(
            symbol(result, "value", SymbolKind.PROPERTY).id.container_path,
            ("outer(OrderId)", "Local"),
        )
        self.assertEqual(
            len(result.bodies), len({item.owner for item in result.bodies})
        )
        assert_body_fact_events(self, result)

        projected = hologram.extract_file(
            Path("/repo/src/Gaps.kt"),
            Path("/repo"),
            text=raw.decode(),
        )
        projected_extensions = [item for item in projected if item.name == "tag"]
        self.assertEqual([item.params for item in projected_extensions], [[], []])
        self.assertEqual([item.container for item in projected_extensions], [None, None])

    def test_utf8_exact_end_spans_and_reference_provenance(self) -> None:
        line = "fun café(é: OrderId): Quote = é.valid()".encode()
        result = extract_file(
            snapshot(line + b"\n", Language.KOTLIN, "src/Utf.kt")
        )
        run = symbol(result, "café", SymbolKind.FUNCTION)
        call = next(item for item in result.calls if item.name == "valid")
        call_text = "é.valid()".encode()
        call_start = line.index(call_text)

        self.assertEqual(run.span, SourceSpan("src/Utf.kt", 1, 0, 1, len(line)))
        self.assertEqual(
            call.span,
            SourceSpan(
                "src/Utf.kt",
                1,
                call_start,
                1,
                call_start + len(call_text),
            ),
        )
        order_id = next(
            item
            for item in result.references
            if item.owner == run.id and item.name == "OrderId"
        )
        self.assertEqual(order_id.kind, ReferenceKind.TYPE)
        self.assertEqual(order_id.context, ReferenceContext.TYPE)
        self.assertEqual(order_id.confidence, ReferenceConfidence.DEFINITE)
        type_start = line.index(b"OrderId")
        self.assertEqual(
            order_id.span,
            SourceSpan(
                "src/Utf.kt",
                1,
                type_start,
                1,
                type_start + len(b"OrderId"),
            ),
        )

    def test_reference_context_confidence_and_token_spans_are_exact(self) -> None:
        raw = b"@Mark\nclass Box : Base() { fun run(input: Input): Result = use(input) }\n"
        result = extract_file(snapshot(raw, Language.KOTLIN, "src/Refs.kt"))

        self.assertEqual(
            [
                (
                    item.owner.name,
                    item.name,
                    item.kind,
                    item.context,
                    item.confidence,
                    item.span,
                )
                for item in result.references
            ],
            [
                (
                    "Box",
                    "Mark",
                    ReferenceKind.TYPE,
                    ReferenceContext.ANNOTATION,
                    ReferenceConfidence.POSSIBLE,
                    SourceSpan("src/Refs.kt", 1, 1, 1, 5),
                ),
                (
                    "Box",
                    "Base",
                    ReferenceKind.TYPE,
                    ReferenceContext.TYPE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.kt", 2, 12, 2, 16),
                ),
                (
                    "run",
                    "Input",
                    ReferenceKind.TYPE,
                    ReferenceContext.TYPE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.kt", 2, 36, 2, 41),
                ),
                (
                    "run",
                    "Result",
                    ReferenceKind.TYPE,
                    ReferenceContext.TYPE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.kt", 2, 44, 2, 50),
                ),
                (
                    "run",
                    "use",
                    ReferenceKind.NAME,
                    ReferenceContext.CODE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.kt", 2, 53, 2, 56),
                ),
                (
                    "run",
                    "input",
                    ReferenceKind.NAME,
                    ReferenceContext.CODE,
                    ReferenceConfidence.DEFINITE,
                    SourceSpan("src/Refs.kt", 2, 57, 2, 62),
                ),
            ],
        )
        run = symbol(result, "run", SymbolKind.METHOD)
        self.assertEqual(
            next(call.span for call in result.calls if call.caller == run.id),
            SourceSpan("src/Refs.kt", 2, 53, 2, 63),
        )
        self.assertEqual(
            next(body.span for body in result.bodies if body.owner == run.id),
            SourceSpan("src/Refs.kt", 2, 51, 2, 63),
        )


class CSharpKotlinSyntaxDiagnosticTest(unittest.TestCase):
    def test_syntax_errors_retain_partial_facts_and_make_projects_incomplete(
        self,
    ) -> None:
        cases = (
            (
                Language.CSHARP,
                "Broken.cs",
                (
                    b"public class Kept { void Ok(){ alive(); } }\n"
                    b"public class Broken {\n"
                ),
                SourceSpan("Broken.cs", 2, 0, 2, 21),
                {"Kept", "Ok"},
                "Ok",
                SourceSpan("Broken.cs", 1, 29, 1, 41),
            ),
            (
                Language.KOTLIN,
                "Broken.kt",
                b"class Kept { fun ok(){ alive(); val x: = 1 } }\nclass Good\n",
                SourceSpan("Broken.kt", 1, 37, 1, 38),
                {"Kept", "ok", "Good"},
                "ok",
                SourceSpan("Broken.kt", 1, 21, 1, 44),
            ),
        )
        for language, file, raw, error_span, names, callable_name, body_span in cases:
            if not hologram.has_parser(language.value):
                continue
            with self.subTest(language=language):
                source = snapshot(raw, language, file)
                result = extract_file(source)
                project = extract_project(Path("/repo"), (source,))
                self.assertIs(result.source, source)
                self.assertTrue(names.issubset({item.name for item in result.symbols}))
                self.assertEqual(
                    [item.code for item in result.diagnostics],
                    ["tree-sitter-syntax-error"],
                )
                self.assertEqual(
                    result.diagnostics[0].severity,
                    DiagnosticSeverity.ERROR,
                )
                self.assertEqual(result.diagnostics[0].span, error_span)
                callable_symbol = symbol(result, callable_name)
                self.assertEqual(
                    [call.name for call in result.calls if call.caller == callable_symbol.id],
                    ["alive"],
                )
                self.assertEqual(
                    next(
                        body.span
                        for body in result.bodies
                        if body.owner == callable_symbol.id
                    ),
                    body_span,
                )
                assert_body_fact_events(self, result)
                self.assertFalse(project.complete)


if __name__ == "__main__":
    unittest.main()
