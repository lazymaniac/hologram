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
    ReferenceKind,
    SourceFile,
    SourceRole,
    SymbolKind,
    Visibility,
)
from hologram.parsers.api import extract_file
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


@unittest.skipUnless(hologram.has_parser("go"), "tree-sitter-go not installed")
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

    def test_legacy_projection_drops_new_composite_calls_and_unknown_locals(
        self,
    ) -> None:
        projected = hologram.extract_file(
            Path("/repo/src/app.go"),
            Path("/repo"),
            text=GO_SOURCE.decode(),
        )
        run = next(item for item in projected if item.name == "Run")

        self.assertEqual(run.calls, ["p.New", "id.Valid", "client.Get"])
        self.assertEqual(run.bindings, {"id": "OrderId", "made": "Store"})
        self.assertNotIn("Store", run.calls)
        self.assertNotIn("Quote", run.calls)


@unittest.skipUnless(hologram.has_parser("rust"), "tree-sitter-rust not installed")
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
            if item.name == "quote" and item.id.container_path == ("Rational",)
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

    def test_legacy_projection_restores_self_and_complex_call_shapes(self) -> None:
        projected = hologram.extract_file(
            Path("/repo/src/lib.rs"),
            Path("/repo"),
            text=RUST_SOURCE.decode(),
        )
        trait_quote = next(
            item
            for item in projected
            if item.name == "quote" and item.container == "Pricer"
        )
        impl_quote = next(
            item
            for item in projected
            if item.name == "quote" and item.container == "Rational"
        )
        of = next(item for item in projected if item.name == "of")

        self.assertEqual(trait_quote.bindings, {"id": "OrderId"})
        self.assertEqual(
            impl_quote.bindings,
            {"id": "OrderId", "self": "Rational"},
        )
        self.assertEqual(impl_quote.calls, ["get", "PricingClient.new"])
        self.assertEqual(
            of.bindings,
            {
                "num": "i64",
                "den": "i64",
                "value": "Rational",
                "self": "Rational",
            },
        )
        self.assertEqual(of.calls, ["Rational"])


if __name__ == "__main__":
    unittest.main()
