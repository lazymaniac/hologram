from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

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
    Visibility,
)
from hologram.parsers.api import DEFAULT_REGISTRY, extract_file, extract_project
from hologram.parsers.common import validate_body_events
from tests.parser_assertions import assert_body_fact_events

GO_SOURCE = b"""\
package app
import (
    p "shop/pricing"
    . "shop/ids"
    _ "shop/sidefx"
    "shop/plain"
)

const Limit = 3

type Embedded struct{}
type Store struct {
    Item OrderId
    Embedded
}
type Pricer interface {
    Quote(id OrderId) Quote
}

func Run(id OrderId) Quote {
    client := p.New()
    made := Store{Item: id}
    if id.Valid() {
        return client.Get(id)
    }
    _ = made
    return Quote{}
}

func (s *Store) Get(id OrderId) Quote {
    return s.lookup(id)
}

func (s *Store) lookup(id OrderId) Quote { return Quote{} }
"""


RUST_SOURCE = b"""\
#![allow(dead_code)]
use crate::pricing::Client as PricingClient;
use crate::ids::{self, OrderId, UserId as Uid, *};

pub const LIMIT: u32 = 3;

pub struct Rational { pub num: i64, den: i64 }
pub enum Force { Asserted, Entailed, Supported }
pub trait Pricer: Base {
    const RATE: u32;
    fn quote(&self, id: OrderId) -> Quote;
}

impl Pricer for Rational {
    const RATE: u32 = 1;
    fn quote(&self, id: OrderId) -> Quote {
        PricingClient::new().get(id)
    }
}

impl Rational {
    pub fn of(num: i64, den: i64) -> Rational {
        let value = Rational { num, den };
        value
    }
}

pub fn run(id: OrderId) -> Quote {
    if true { PricingClient::new().get(id) } else { fallback(id) }
}
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


@unittest.skipUnless(
    DEFAULT_REGISTRY.has_parser(Language.GO), "tree-sitter-go not installed"
)
class GoParserTest(unittest.TestCase):
    def test_package_imports_declarations_and_snapshot_are_canonical(self) -> None:
        source = snapshot(GO_SOURCE, Language.GO, "src/app.go")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("disk reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("disk reread")),
        ):
            result = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(result.module, "app")
        module = symbol(result, "app", SymbolKind.MODULE)
        self.assertEqual(module.id.container_path, ())
        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [
                ("shop/pricing", None, "p", False),
                ("shop/ids", None, ".", True),
                ("shop/sidefx", None, "_", False),
                ("shop/plain", None, None, False),
            ],
        )
        store = symbol(result, "Store", SymbolKind.CLASS)
        self.assertEqual(store.supers, ("Embedded",))
        self.assertEqual(store.components, ("Item",))
        self.assertEqual(store.id.container_path, ())
        self.assertEqual(
            symbol(result, "Item", SymbolKind.FIELD).id.container_path, ("Store",)
        )
        self.assertEqual(
            symbol(result, "Limit", SymbolKind.CONSTANT).visibility, Visibility.PUBLIC
        )
        quote = symbol(result, "Quote", SymbolKind.METHOD)
        self.assertEqual(quote.id.container_path, ("Pricer",))
        self.assertEqual(quote.params, ("OrderId",))
        self.assertEqual(quote.returns, "Quote")

        embedded = extract_file(
            snapshot(
                b"package app\n"
                b"type Base interface { Value() int }\n"
                b"type Derived interface { Base; Other() int }\n",
                Language.GO,
                "src/interfaces.go",
            )
        )
        derived = symbol(embedded, "Derived", SymbolKind.INTERFACE)
        self.assertEqual(derived.supers, ("Base",))
        self.assertTrue(
            any(
                reference.owner == derived.id
                and reference.kind is ReferenceKind.TYPE
                and reference.name == "Base"
                for reference in embedded.references
            )
        )

    def test_bindings_calls_references_bodies_and_stable_ids_are_complete(self) -> None:
        result = extract_file(snapshot(GO_SOURCE, Language.GO, "src/app.go"))
        run = symbol(result, "Run", SymbolKind.FUNCTION)
        get = symbol(result, "Get", SymbolKind.METHOD)

        self.assertTrue(
            {
                Binding("id", "OrderId"),
                Binding("client", "?"),
                Binding("made", "Store"),
            }.issubset(set(run.bindings))
        )
        self.assertIn(Binding("s", "Store"), get.bindings)
        calls = [call for call in result.calls if call.caller == run.id]
        self.assertEqual(
            [(call.receiver, call.name, call.kind, call.arity) for call in calls],
            [
                ("p", "New", CallKind.CALL, 0),
                (None, "Store", CallKind.CONSTRUCT, 1),
                ("id", "Valid", CallKind.CALL, 0),
                ("client", "Get", CallKind.CALL, 1),
                (None, "Quote", CallKind.CONSTRUCT, 0),
            ],
        )
        self.assertTrue(
            any(
                ref.owner == run.id
                and ref.name == "OrderId"
                and ref.kind is ReferenceKind.TYPE
                for ref in result.references
            )
        )
        body = next(item for item in result.bodies if item.owner == run.id)
        self.assertEqual(body.span.start_line, 20)
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
                BodyEventKind.KEYWORD,
                BodyEventKind.OPERATOR,
                BodyEventKind.CONTROL_ENTER,
                BodyEventKind.CONTROL_EXIT,
            }.issubset(kinds)
        )
        validate_body_events(body.events)
        assert_body_fact_events(self, result)

        shifted = extract_file(snapshot(b"\n" + GO_SOURCE, Language.GO, "src/app.go"))
        self.assertEqual(
            {item.id for item in result.symbols}, {item.id for item in shifted.symbols}
        )

    def test_direct_struct_fields_multi_value_bindings_and_anonymous_callable_facts(
        self,
    ) -> None:
        raw = b"""\
package gaps
type A struct{}
type B struct{}
var moduleA, moduleB = A{}, B{}
type Outer struct {
    Inner struct { X int }
    Top string
}
func Run(input int) {
    type Local struct { Value int }
    var a, b = A{}, B{}
    callback := func(value int) {
        made := A{}
        hidden(value, made)
    }
    callback(input)
    _, _ = a, b
}
"""
        result = extract_file(snapshot(raw, Language.GO, "src/gaps.go"))
        outer = symbol(result, "Outer", SymbolKind.CLASS)
        run = symbol(result, "Run", SymbolKind.FUNCTION)

        module_a = symbol(result, "moduleA", SymbolKind.FIELD)
        module_b = symbol(result, "moduleB", SymbolKind.FIELD)
        self.assertEqual((module_a.returns, module_b.returns), ("A", "B"))
        self.assertEqual(
            [call.name for call in result.calls if call.caller == module_a.id],
            ["A"],
        )
        self.assertEqual(
            [call.name for call in result.calls if call.caller == module_b.id],
            ["B"],
        )

        self.assertEqual(outer.components, ("Inner", "Top"))
        self.assertEqual(
            {
                item.name
                for item in result.symbols
                if item.kind is SymbolKind.FIELD
                and item.id.container_path == ("Outer",)
            },
            {"Inner", "Top"},
        )
        self.assertEqual(
            symbol(result, "Local", SymbolKind.CLASS).id.container_path,
            ("Run(int)",),
        )
        self.assertTrue(
            {
                Binding("input", "int"),
                Binding("a", "A"),
                Binding("b", "B"),
                Binding("callback", "?"),
                Binding("value", "int"),
                Binding("made", "A"),
            }.issubset(set(run.bindings))
        )
        self.assertEqual(
            [call.name for call in result.calls if call.caller == run.id],
            ["A", "B", "A", "hidden", "callback"],
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
            {"callback", "made", "value"} & {item.name for item in result.symbols}
        )
        self.assertEqual(
            len(result.bodies), len({item.owner for item in result.bodies})
        )
        assert_body_fact_events(self, result)

    def test_utf8_exact_spans_reference_provenance_and_uncapped_calls(self) -> None:
        line = "func Run(é OrderId) Quote { return é.Valid() }".encode()
        raw = b"package utf\n" + line + b"\n"
        source = snapshot(raw, Language.GO, "src/utf.go")
        result = extract_file(source)
        run = symbol(result, "Run", SymbolKind.FUNCTION)
        call = next(item for item in result.calls if item.name == "Valid")
        call_text = "é.Valid()".encode()
        call_start = line.index(call_text)

        self.assertEqual(
            run.span,
            SourceSpan("src/utf.go", 2, 0, 2, len(line)),
        )
        self.assertEqual(
            call.span,
            SourceSpan(
                "src/utf.go",
                2,
                call_start,
                2,
                call_start + len(call_text),
            ),
        )
        order_id = next(
            item
            for item in result.references
            if item.owner == run.id and item.name == "OrderId"
        )
        self.assertEqual(order_id.context, ReferenceContext.TYPE)
        self.assertEqual(order_id.confidence, ReferenceConfidence.DEFINITE)
        type_start = line.index(b"OrderId")
        self.assertEqual(
            order_id.span,
            SourceSpan(
                "src/utf.go",
                2,
                type_start,
                2,
                type_start + len(b"OrderId"),
            ),
        )

        statements = b"\n".join(
            f"    call{index}({index})".encode() for index in range(14)
        )
        calls_result = extract_file(
            snapshot(
                b"package calls\nfunc Run() {\n" + statements + b"\n}\n",
                Language.GO,
                "src/calls.go",
            )
        )
        calls_run = symbol(calls_result, "Run", SymbolKind.FUNCTION)
        self.assertEqual(
            [call.name for call in calls_result.calls if call.caller == calls_run.id],
            [f"call{index}" for index in range(14)],
        )


@unittest.skipUnless(
    DEFAULT_REGISTRY.has_parser(Language.RUST), "tree-sitter-rust not installed"
)
class RustParserTest(unittest.TestCase):
    def test_use_trees_types_trait_impl_and_immutable_supers(self) -> None:
        result = extract_file(snapshot(RUST_SOURCE, Language.RUST, "src/lib.rs"))

        self.assertEqual(result.module, "src/lib")
        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [
                ("crate::pricing", "Client", "PricingClient", False),
                ("crate", "ids", None, False),
                ("crate::ids", "OrderId", None, False),
                ("crate::ids", "UserId", "Uid", False),
                ("crate::ids", None, None, True),
            ],
        )
        rational = symbol(result, "Rational", SymbolKind.CLASS)
        self.assertEqual(rational.supers, ("Pricer",))
        self.assertTrue(
            {"Pricer", "Rational"}.issubset(
                {
                    reference.name
                    for reference in result.references
                    if reference.owner == rational.id
                    and reference.kind is ReferenceKind.TYPE
                }
            )
        )
        self.assertEqual(rational.components, ("num", "den"))
        self.assertEqual(
            symbol(result, "num", SymbolKind.FIELD).id.container_path, ("Rational",)
        )
        self.assertEqual(
            symbol(result, "LIMIT", SymbolKind.CONSTANT).id.container_path, ()
        )
        self.assertEqual(
            symbol(result, "RATE", SymbolKind.CONSTANT).id.container_path, ("Pricer",)
        )

    def test_self_bindings_calls_references_bodies_and_stable_ids_are_complete(
        self,
    ) -> None:
        result = extract_file(snapshot(RUST_SOURCE, Language.RUST, "src/lib.rs"))
        quote = next(
            item
            for item in result.symbols
            if item.name == "quote"
            and item.id.container_path == ("Rational", "impl Pricer")
        )
        run = symbol(result, "run", SymbolKind.FUNCTION)
        self.assertIn(Binding("self", "Rational"), quote.bindings)
        self.assertIn(Binding("id", "OrderId"), quote.bindings)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
                if call.caller == quote.id
            ],
            [
                ("PricingClient::new()", "get", CallKind.CALL, 1),
                ("PricingClient", "new", CallKind.CALL, 0),
            ],
        )
        body = next(item for item in result.bodies if item.owner == run.id)
        validate_body_events(body.events)
        self.assertIn(
            BodyEventKind.CONTROL_ENTER, {event.kind for event in body.events}
        )
        self.assertTrue(
            any(
                ref.owner == run.id and ref.name == "OrderId"
                for ref in result.references
            )
        )
        assert_body_fact_events(self, result)

        shifted = extract_file(
            snapshot(b"\n" + RUST_SOURCE, Language.RUST, "src/lib.rs")
        )
        self.assertEqual(
            {item.id for item in result.symbols}, {item.id for item in shifted.symbols}
        )

    def test_use_wildcard_direct_trait_bounds_and_trait_impl_owners_are_exact(
        self,
    ) -> None:
        raw = b"""\
use foo::*;
pub trait Parent: Outer<Inner> + crate::Other {}
pub trait A { fn f(&self); }
pub trait B { fn f(&self); }
pub struct S;
impl A for S { fn f(&self) { a_call(); } }
impl B for S { fn f(&self) { b_call(); } }
"""
        result = extract_file(snapshot(raw, Language.RUST, "src/traits.rs"))

        self.assertEqual(
            [
                (item.module, item.name, item.alias, item.wildcard)
                for item in result.imports
            ],
            [("foo", None, None, True)],
        )
        parent = symbol(result, "Parent", SymbolKind.INTERFACE)
        self.assertEqual(parent.supers, ("Outer", "Other"))
        self.assertTrue(
            {"Outer", "Inner", "Other"}.issubset(
                {
                    item.name
                    for item in result.references
                    if item.owner == parent.id and item.kind is ReferenceKind.TYPE
                }
            )
        )
        struct = symbol(result, "S", SymbolKind.CLASS)
        self.assertEqual(struct.supers, ("A", "B"))
        implementations = [
            item
            for item in result.symbols
            if item.name == "f" and item.kind is SymbolKind.METHOD and item.body_lines
        ]
        self.assertEqual(
            [item.id.container_path for item in implementations],
            [("S", "impl A"), ("S", "impl B")],
        )
        self.assertEqual(len({item.id for item in implementations}), 2)
        self.assertEqual(
            [
                [call.name for call in result.calls if call.caller == item.id]
                for item in implementations
            ],
            [["a_call"], ["b_call"]],
        )
        self.assertEqual(
            len(result.bodies), len({item.owner for item in result.bodies})
        )

    def test_closure_facts_nested_type_utf8_spans_and_provenance(self) -> None:
        raw = b"""\
fn outer(input: OrderId) {
    struct Local { value: i32 }
    let callback = |value: OrderId| {
        let made = Widget::new(value);
        hidden(made);
    };
    callback(input);
}
"""
        result = extract_file(snapshot(raw, Language.RUST, "src/closure.rs"))
        outer = symbol(result, "outer", SymbolKind.FUNCTION)
        self.assertEqual(
            symbol(result, "Local", SymbolKind.CLASS).id.container_path,
            ("outer(OrderId)",),
        )
        self.assertTrue(
            {
                Binding("input", "OrderId"),
                Binding("callback", "?"),
                Binding("value", "OrderId"),
                Binding("made", "Widget"),
            }.issubset(set(outer.bindings))
        )
        self.assertEqual(
            [call.name for call in result.calls if call.caller == outer.id],
            ["new", "hidden", "callback"],
        )
        body = next(item for item in result.bodies if item.owner == outer.id)
        self.assertIn(
            (BodyEventKind.PARAM, "value"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertIn(
            (BodyEventKind.LOCAL, "made"),
            {(event.kind, event.text) for event in body.events},
        )
        self.assertFalse({"callback", "made"} & {item.name for item in result.symbols})
        self.assertEqual(
            symbol(result, "value", SymbolKind.FIELD).id.container_path,
            ("outer(OrderId)", "Local"),
        )
        assert_body_fact_events(self, result)

        line = "pub fn run(é: OrderId) -> Quote { é.valid() }".encode()
        source = snapshot(line + b"\n", Language.RUST, "src/utf.rs")
        utf_result = extract_file(source)
        run = symbol(utf_result, "run", SymbolKind.FUNCTION)
        call = next(item for item in utf_result.calls if item.name == "valid")
        call_text = "é.valid()".encode()
        call_start = line.index(call_text)
        self.assertEqual(run.span, SourceSpan("src/utf.rs", 1, 0, 1, len(line)))
        self.assertEqual(
            call.span,
            SourceSpan(
                "src/utf.rs",
                1,
                call_start,
                1,
                call_start + len(call_text),
            ),
        )
        order_id = next(
            item
            for item in utf_result.references
            if item.owner == run.id and item.name == "OrderId"
        )
        self.assertEqual(order_id.context, ReferenceContext.TYPE)
        self.assertEqual(order_id.confidence, ReferenceConfidence.DEFINITE)
        type_start = line.index(b"OrderId")
        self.assertEqual(
            order_id.span,
            SourceSpan(
                "src/utf.rs",
                1,
                type_start,
                1,
                type_start + len(b"OrderId"),
            ),
        )


class GoRustSyntaxDiagnosticTest(unittest.TestCase):
    def test_syntax_errors_retain_partial_facts_and_make_projects_incomplete(
        self,
    ) -> None:
        cases = (
            (
                Language.GO,
                "broken.go",
                (
                    b"package broken\ntype Kept struct{}\nfunc Ok(){ alive() }\n"
                    b"func Broken( {\n"
                ),
                SourceSpan("broken.go", 4, 0, 4, 14),
                {"broken", "Kept", "Ok"},
                "Ok",
                SourceSpan("broken.go", 3, 9, 3, 20),
            ),
            (
                Language.RUST,
                "broken.rs",
                b"pub struct Kept;\nfn ok(){ alive(); }\nfn broken( {\n",
                SourceSpan("broken.rs", 3, 0, 3, 12),
                {"broken", "Kept", "ok"},
                "ok",
                SourceSpan("broken.rs", 2, 7, 2, 19),
            ),
        )
        for language, file, raw, error_span, names, callable_name, body_span in cases:
            if not DEFAULT_REGISTRY.has_parser(language):
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
                    [
                        call.name
                        for call in result.calls
                        if call.caller == callable_symbol.id
                    ],
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
