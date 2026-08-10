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

C_SOURCE = """\
#include \"engine.h\"
#include <stdint.h>

typedef struct Rational {
    int numerator;
    int denominator;
} Rational;

typedef union Scalar {
    int integer;
    double decimal;
} Scalar;

typedef enum Force { ASSERTED, ENTAILED } Force;
const int LIMIT = 3;
int state;

static int reduce(Rational *r);
int rational_add(Rational left, Rational right);

static int reduce(Rational *r) {
    int value = r->numerator + 1;
    if (value > 0) {
        return gcd(value, r->denominator);
    }
    return value;
}

int rational_add(Rational left, Rational right) {
    Rational made = (Rational){left.numerator + right.numerator, 0};
    return reduce(&made);
}

/* ż */ int utf8_global;
""".encode()


CPP_SOURCE = b"""\
#include \"engine.h\"
#include <vector>

namespace shop {
struct Base {};
class Engine : public Base {
public:
    explicit Engine(int n);
    [[nodiscard]] int compute(int id) const;
    int compute(double id) const;
    struct Inner {
        int value;
        int ping();
    };
    static constexpr int Limit = 3;
protected:
    int guard;
private:
    int secret;
};

int Engine::compute(int id) const {
    if (id > 0) return helper(id);
    return id;
}

int Engine::compute(double id) const { return helper((int)id); }
Engine::Engine(int n) : secret(n) {}
int Engine::Inner::ping() { return helper(value); }

class Box {
public:
    int compute(int id) { return id; }
};
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


def symbols(result, name: str, kind: SymbolKind | None = None):
    return [
        item
        for item in result.symbols
        if item.name == name and (kind is None or item.kind is kind)
    ]


@unittest.skipUnless(hologram.has_parser("c"), "tree-sitter-c not installed")
class CParserTest(unittest.TestCase):
    def test_includes_typedefs_fields_constants_and_snapshot_are_canonical(
        self,
    ) -> None:
        source = snapshot(C_SOURCE, Language.C, "src/rational.c")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("disk reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("disk reread")),
        ):
            result = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(
            [(item.module, item.name) for item in result.imports],
            [("engine.h", None), ("stdint.h", None)],
        )
        rational = symbols(result, "Rational", SymbolKind.CLASS)[0]
        scalar = symbols(result, "Scalar", SymbolKind.CLASS)[0]
        force = symbols(result, "Force", SymbolKind.ENUM)[0]
        self.assertEqual(rational.params, ("int", "int"))
        self.assertEqual(rational.components, ("numerator", "denominator"))
        self.assertEqual(scalar.params, ("int", "double"))
        self.assertEqual(force.params, ("ASSERTED", "ENTAILED"))
        self.assertEqual(
            symbols(result, "numerator", SymbolKind.FIELD)[0].id.container_path,
            ("Rational",),
        )
        self.assertEqual(
            symbols(result, "LIMIT", SymbolKind.CONSTANT)[0].returns,
            "int",
        )
        self.assertEqual(symbols(result, "state", SymbolKind.FIELD)[0].returns, "int")

        utf8 = symbols(result, "utf8_global", SymbolKind.FIELD)[0]
        self.assertEqual(utf8.span.start_column, len("/* ż */ int ".encode()))
        self.assertEqual(utf8.span.end_column, len("/* ż */ int utf8_global".encode()))

    def test_prototypes_definitions_calls_bindings_and_bodies_merge_immutably(
        self,
    ) -> None:
        result = extract_file(snapshot(C_SOURCE, Language.C, "src/rational.c"))
        reduce = symbols(result, "reduce", SymbolKind.FUNCTION)
        add = symbols(result, "rational_add", SymbolKind.FUNCTION)

        self.assertEqual(len(reduce), 1)
        self.assertEqual(len(add), 1)
        self.assertEqual(reduce[0].id.signature_key, "(Rational*)")
        self.assertEqual(reduce[0].span.start_line, 21)
        self.assertEqual(reduce[0].visibility, Visibility.PRIVATE)
        self.assertIn(Binding("r", "Rational"), reduce[0].bindings)
        self.assertIn(Binding("value", "int"), reduce[0].bindings)
        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
                if call.caller == reduce[0].id
            ],
            [(None, "gcd", CallKind.CALL, 2)],
        )
        self.assertEqual(add[0].id.signature_key, "(Rational,Rational)")
        self.assertEqual(add[0].span.start_line, 29)
        self.assertEqual(
            [call.name for call in result.calls if call.caller == add[0].id],
            ["Rational", "reduce"],
        )

        body = next(item for item in result.bodies if item.owner == reduce[0].id)
        validate_body_events(body.events)
        self.assertTrue(
            {
                BodyEventKind.PARAM,
                BodyEventKind.LOCAL,
                BodyEventKind.NAME,
                BodyEventKind.TYPE,
                BodyEventKind.CALL,
                BodyEventKind.MEMBER,
                BodyEventKind.LITERAL,
                BodyEventKind.OPERATOR,
                BodyEventKind.KEYWORD,
                BodyEventKind.CONTROL_ENTER,
                BodyEventKind.CONTROL_EXIT,
            }.issubset({event.kind for event in body.events})
        )
        self.assertTrue(
            any(
                ref.owner == reduce[0].id
                and ref.name == "Rational"
                and ref.kind is ReferenceKind.TYPE
                for ref in result.references
            )
        )
        assert_body_fact_events(self, result)

        shifted = extract_file(snapshot(b"\n" + C_SOURCE, Language.C, "src/rational.c"))
        self.assertEqual(
            {item.id for item in result.symbols},
            {item.id for item in shifted.symbols},
        )


@unittest.skipUnless(hologram.has_parser("cpp"), "tree-sitter-cpp not installed")
class CppParserTest(unittest.TestCase):
    def test_namespace_nested_types_fields_bases_access_and_attributes(self) -> None:
        result = extract_file(snapshot(CPP_SOURCE, Language.CPP, "src/engine.cpp"))

        self.assertEqual(result.module, "shop")
        namespace = symbols(result, "shop", SymbolKind.MODULE)[0]
        self.assertEqual(namespace.id.container_path, ())
        engine = symbols(result, "Engine", SymbolKind.CLASS)[0]
        self.assertEqual(engine.id.container_path, ("shop",))
        self.assertEqual(engine.supers, ("Base",))
        inner = symbols(result, "Inner", SymbolKind.CLASS)[0]
        self.assertEqual(inner.id.container_path, ("shop", "Engine"))
        self.assertEqual(
            symbols(result, "value", SymbolKind.FIELD)[0].id.container_path,
            ("shop", "Engine", "Inner"),
        )
        self.assertEqual(
            symbols(result, "Limit", SymbolKind.CONSTANT)[0].visibility,
            Visibility.PUBLIC,
        )
        self.assertIn(
            "static", symbols(result, "Limit", SymbolKind.CONSTANT)[0].modifiers
        )
        self.assertEqual(
            symbols(result, "guard", SymbolKind.FIELD)[0].visibility,
            Visibility.PROTECTED,
        )
        self.assertEqual(
            symbols(result, "secret", SymbolKind.FIELD)[0].visibility,
            Visibility.PRIVATE,
        )

    def test_overloads_out_of_line_definitions_and_owners_are_exact(self) -> None:
        result = extract_file(snapshot(CPP_SOURCE, Language.CPP, "src/engine.cpp"))
        engine_computes = [
            item
            for item in symbols(result, "compute", SymbolKind.METHOD)
            if item.id.container_path == ("shop", "Engine")
        ]
        by_key = {item.id.signature_key: item for item in engine_computes}
        compute = by_key["(int)"]
        out_of_line_compute = next(
            item
            for item in result.symbols
            if item.id == compute.id and item.span.start_line == 22
        )
        self.assertEqual(compute.id.signature_key, "(int)")
        self.assertEqual(compute.id, out_of_line_compute.id)
        self.assertEqual(
            len([item for item in result.symbols if item.id == compute.id]), 1
        )
        self.assertEqual(set(by_key), {"(int)", "(double)"})
        self.assertEqual(compute.visibility, Visibility.PUBLIC)
        self.assertIn("const", compute.modifiers)
        self.assertIn("nodiscard", compute.annotations)
        self.assertEqual(
            [call.name for call in result.calls if call.caller == compute.id],
            ["helper"],
        )
        self.assertTrue(any(body.owner == compute.id for body in result.bodies))

        constructor = symbols(result, "Engine", SymbolKind.CONSTRUCTOR)[0]
        self.assertEqual(constructor.id.container_path, ("shop", "Engine"))
        self.assertEqual(constructor.params, ("int",))
        self.assertEqual(constructor.returns, "Engine")
        self.assertIn("explicit", constructor.modifiers)
        ping = symbols(result, "ping", SymbolKind.METHOD)[0]
        self.assertEqual(ping.id.container_path, ("shop", "Engine", "Inner"))
        box_compute = next(
            item
            for item in symbols(result, "compute", SymbolKind.METHOD)
            if item.id.container_path == ("shop", "Box")
        )
        self.assertNotEqual(box_compute.id, compute.id)
        assert_body_fact_events(self, result)

        shifted = extract_file(
            snapshot(b"\n\n" + CPP_SOURCE, Language.CPP, "src/engine.cpp")
        )
        self.assertEqual(
            {item.id for item in result.symbols},
            {item.id for item in shifted.symbols},
        )


if __name__ == "__main__":
    unittest.main()
