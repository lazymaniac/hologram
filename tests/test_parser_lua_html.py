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
)
from hologram.parsers.api import extract_file
from hologram.parsers.common import validate_body_events
from hologram.scan import detect_language
from tests.parser_assertions import assert_body_fact_events

LUA_SOURCE = b"""\
local pricing = require("shop.pricing")
local quote = require "shop.quote"
local M = {}
M.LIMIT = 3
local PRIVATE = 2

local function helper(x)
    local y = x + 1
    if y > 0 then
        return pricing.get(y)
    end
    return y
end

function M.run(id)
    local function nested(value)
        return quote.make(value)
    end
    return pricing.get(nested(id))
end

function M:reset(reason)
    self.state = reason
    return helper(reason)
end

return M
"""


HTML_SOURCE = b"""\
<!doctype html>
<price-card id="app">
  <nav-menu id='main-nav'></nav-menu>
  <price-card id="app"></price-card>
</price-card>
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


@unittest.skipUnless(hologram.has_parser("lua"), "tree-sitter-lua not installed")
class LuaParserTest(unittest.TestCase):
    def test_requires_module_constants_and_snapshot_are_canonical(self) -> None:
        source = snapshot(LUA_SOURCE, Language.LUA, "src/pricing.lua")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("disk reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("disk reread")),
        ):
            result = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(result.module, "M")
        self.assertEqual(
            [(item.module, item.name, item.alias) for item in result.imports],
            [
                ("shop.pricing", None, "pricing"),
                ("shop.quote", None, "quote"),
            ],
        )
        module = symbol(result, "M", SymbolKind.MODULE)
        self.assertEqual(module.id.container_path, ())
        limit = symbol(result, "LIMIT", SymbolKind.CONSTANT)
        self.assertEqual(limit.id.container_path, ("M",))
        self.assertEqual(limit.span.start_line, 4)
        self.assertEqual(limit.span.start_column, 2)
        self.assertEqual(limit.span.end_column, 7)
        private = symbol(result, "PRIVATE", SymbolKind.CONSTANT)
        self.assertEqual(private.id.container_path, ())

    def test_named_nested_functions_methods_calls_references_and_bodies(self) -> None:
        result = extract_file(snapshot(LUA_SOURCE, Language.LUA, "src/pricing.lua"))
        helper = symbol(result, "helper", SymbolKind.FUNCTION)
        run = symbol(result, "run", SymbolKind.METHOD)
        reset = symbol(result, "reset", SymbolKind.METHOD)
        nested = symbol(result, "nested", SymbolKind.FUNCTION)

        self.assertEqual(helper.params, ("?",))
        self.assertIn(Binding("x", "?"), helper.bindings)
        self.assertEqual(run.id.container_path, ("M",))
        self.assertEqual(run.params, ("?",))
        self.assertIn(Binding("id", "?"), run.bindings)
        self.assertEqual(nested.id.container_path, ("M", "run(?)"))
        self.assertIn(Binding("value", "?"), nested.bindings)
        self.assertEqual(reset.id.container_path, ("M",))
        self.assertIn(Binding("self", "M"), reset.bindings)
        self.assertIn(Binding("reason", "?"), reset.bindings)

        self.assertEqual(
            [
                (call.receiver, call.name, call.kind, call.arity)
                for call in result.calls
                if call.caller == run.id
            ],
            [
                ("pricing", "get", CallKind.CALL, 1),
                (None, "nested", CallKind.CALL, 1),
            ],
        )
        self.assertEqual(
            [
                (call.receiver, call.name)
                for call in result.calls
                if call.caller == nested.id
            ],
            [("quote", "make")],
        )
        self.assertTrue(
            any(
                ref.owner == run.id
                and ref.name == "get"
                and ref.qualifier == "pricing"
                and ref.kind is ReferenceKind.NAME
                for ref in result.references
            )
        )
        run_body = next(item for item in result.bodies if item.owner == run.id)
        helper_body = next(item for item in result.bodies if item.owner == helper.id)
        self.assertEqual(run_body.span.start_line, 16)
        validate_body_events(helper_body.events)
        self.assertTrue(
            {
                BodyEventKind.PARAM,
                BodyEventKind.LOCAL,
                BodyEventKind.NAME,
                BodyEventKind.CALL,
                BodyEventKind.MEMBER,
                BodyEventKind.LITERAL,
                BodyEventKind.OPERATOR,
                BodyEventKind.KEYWORD,
                BodyEventKind.CONTROL_ENTER,
                BodyEventKind.CONTROL_EXIT,
            }.issubset({event.kind for event in helper_body.events})
        )
        assert_body_fact_events(self, result)

        shifted = extract_file(
            snapshot(b"\n" + LUA_SOURCE, Language.LUA, "src/pricing.lua")
        )
        self.assertEqual(
            {item.id for item in result.symbols},
            {item.id for item in shifted.symbols},
        )


@unittest.skipUnless(hologram.has_parser("html"), "tree-sitter-html not installed")
class HtmlParserTest(unittest.TestCase):
    def test_ids_and_custom_elements_are_mixed_order_exact_and_bodyless(self) -> None:
        source = snapshot(HTML_SOURCE, Language.HTML, "web/page.html")
        result = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(
            [item.name for item in result.symbols],
            ["price-card", "#app", "nav-menu", "#main-nav"],
        )
        self.assertEqual(len(result.symbols), len({item.id for item in result.symbols}))
        self.assertEqual(result.bodies, ())
        self.assertEqual(result.calls, ())
        app = symbol(result, "#app", SymbolKind.FUNCTION)
        price = symbol(result, "price-card", SymbolKind.FUNCTION)
        self.assertEqual(
            source.raw.splitlines()[app.span.start_line - 1][
                app.span.start_column : app.span.end_column
            ],
            b"app",
        )
        self.assertEqual(
            source.raw.splitlines()[price.span.start_line - 1][
                price.span.start_column : price.span.end_column
            ],
            b"price-card",
        )
        self.assertEqual(app.id.container_path, ())
        self.assertEqual(price.id.container_path, ())


class ExtensionDispatchTest(unittest.TestCase):
    def test_header_and_helm_alias_extensions_dispatch(self) -> None:
        self.assertIs(detect_language(Path("include/api.h")), Language.C)
        self.assertIs(detect_language(Path("include/api.hpp")), Language.CPP)
        self.assertIs(detect_language(Path("chart/values.yml")), Language.HELM)
        self.assertIs(
            detect_language(Path("chart/templates/_helpers.tpl")), Language.HELM
        )

    def test_every_task_six_language_has_an_advertised_extractor(self) -> None:
        cases = (
            (Language.C, b"int value;\n", "include/value.h"),
            (Language.CPP, b"class Value {};\n", "include/value.hpp"),
            (Language.LUA, b"return {}\n", "src/value.lua"),
            (Language.HTML, b"<value-card></value-card>\n", "web/value.html"),
        )
        for language, raw, file in cases:
            with self.subTest(language=language):
                result = extract_file(snapshot(raw, language, file))
                self.assertNotIn(
                    "missing-extractor",
                    {diagnostic.code for diagnostic in result.diagnostics},
                )


if __name__ == "__main__":
    unittest.main()
