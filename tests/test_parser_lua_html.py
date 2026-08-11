from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from hologram.model import (
    Binding,
    BodyEventKind,
    CallKind,
    Language,
    ReferenceConfidence,
    ReferenceKind,
    SourceFile,
    SourceRole,
    SymbolKind,
    Visibility,
)
from hologram.parsers.api import DEFAULT_REGISTRY, extract_file
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


LUA_STATIC_BRACKET_SOURCE = b"""\
local dep = require [==[pkg.deep]==]
local M = {}
M['LIMIT'] = 7
M["run"] = function(name)
    local runtime = require(name)
    dep['call'](name)
    return runtime(name)
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


@unittest.skipUnless(
    DEFAULT_REGISTRY.has_parser(Language.LUA), "tree-sitter-lua not installed"
)
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
        self.assertIs(helper.visibility, Visibility.PRIVATE)
        self.assertIn(Binding("x", "?"), helper.bindings)
        self.assertEqual(run.id.container_path, ("M",))
        self.assertEqual(run.params, ("?",))
        self.assertIn(Binding("id", "?"), run.bindings)
        self.assertEqual(nested.id.container_path, ("M", "run"))
        self.assertIs(nested.visibility, Visibility.PRIVATE)
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

    def test_local_function_nested_in_assigned_callable_is_private(self) -> None:
        raw = b"""\
local Filter = create_filter({
post_filter = function()
  local function server_started(value)
    return value
  end
  return server_started(true)
end
})
return Filter
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/filter.lua"))

        nested = symbol(result, "server_started", SymbolKind.FUNCTION)
        self.assertIs(nested.visibility, Visibility.PRIVATE)

    def test_anonymous_callback_calls_do_not_roll_into_named_owner(self) -> None:
        raw = b"""\
local M = {}
local function nested() end
function M.run()
  direct()
  schedule(function()
    nested()
  end)
end
return M
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/callback_calls.lua"))
        run = symbol(result, "run", SymbolKind.METHOD)

        self.assertEqual(
            [
                (item.receiver, item.name)
                for item in result.calls
                if item.caller == run.id
            ],
            [(None, "direct"), (None, "schedule")],
        )
        body = next(item for item in result.bodies if item.owner == run.id)
        self.assertNotIn(
            "nested",
            [item.text for item in body.events if item.kind is BodyEventKind.CALL],
        )
        self.assertTrue(
            any(
                item.owner is None
                and item.name == "nested"
                and item.confidence is ReferenceConfidence.POSSIBLE
                for item in result.references
            )
        )

    def test_table_callback_slot_keeps_possible_reachability(self) -> None:
        raw = b"""\
local function prepare(value) return value end
return {
  pre_filter = prepare,
  run = function(value) return prepare(value) end,
}
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/filter.lua"))

        possible = [
            item
            for item in result.references
            if item.owner is None
            and item.name == "prepare"
            and item.confidence is ReferenceConfidence.POSSIBLE
        ]
        self.assertEqual(len(possible), 2)

    def test_static_brackets_long_requires_and_dynamic_requires_are_exact(self) -> None:
        source = snapshot(
            LUA_STATIC_BRACKET_SOURCE,
            Language.LUA,
            "src/static_brackets.lua",
        )
        result = extract_file(source)

        self.assertEqual(
            [(item.module, item.name, item.alias) for item in result.imports],
            [("pkg.deep", None, "dep")],
        )
        limit = symbol(result, "LIMIT", SymbolKind.CONSTANT)
        self.assertEqual(limit.id.container_path, ("M",))
        self.assertEqual(
            source.raw.splitlines()[limit.span.start_line - 1][
                limit.span.start_column : limit.span.end_column
            ],
            b"LIMIT",
        )

        run = symbol(result, "run", SymbolKind.METHOD)
        self.assertEqual(run.id.container_path, ("M",))
        self.assertEqual(run.params, ("?",))
        self.assertEqual(
            [
                (call.receiver, call.name, call.arity)
                for call in result.calls
                if call.caller == run.id
            ],
            [
                (None, "require", 1),
                ("dep", "call", 1),
                (None, "runtime", 1),
            ],
        )
        dynamic_require = next(
            call
            for call in result.calls
            if call.caller == run.id and call.name == "require"
        )
        run_body = next(item for item in result.bodies if item.owner == run.id)
        self.assertIn(
            (BodyEventKind.CALL, dynamic_require.span, "require"),
            {(event.kind, event.span, event.text) for event in run_body.events},
        )
        bracket_reference = next(
            reference
            for reference in result.references
            if reference.owner == run.id
            and reference.qualifier == "dep"
            and reference.name == "call"
        )
        self.assertEqual(
            source.raw.splitlines()[bracket_reference.span.start_line - 1][
                bracket_reference.span.start_column : bracket_reference.span.end_column
            ],
            b"call",
        )
        self.assertIn(
            (BodyEventKind.MEMBER, bracket_reference.span, "call"),
            {(event.kind, event.span, event.text) for event in run_body.events},
        )
        self.assertIn(
            (BodyEventKind.NAME, bracket_reference.span, "call"),
            {(event.kind, event.span, event.text) for event in run_body.events},
        )
        assert_body_fact_events(self, result)

    def test_returned_table_callback_local_is_not_a_module(self) -> None:
        raw = b"""\
return {
  setup = function()
    local M = {}
    function M.run()
      return true
    end
    return M
  end,
}
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/callback.lua"))

        self.assertIsNone(result.module)
        self.assertFalse(
            [item for item in result.symbols if item.kind is SymbolKind.MODULE]
        )

    def test_top_level_module_wins_over_function_local_shadow(self) -> None:
        raw = b"""\
local M = {}
local function configure()
  local M = {}
  function M.run()
    return "shadow"
  end
end
function M.top()
  return true
end
return M
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/shadow.lua"))

        modules = [item for item in result.symbols if item.kind is SymbolKind.MODULE]
        self.assertEqual(result.module, "M")
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0].span.start_line, 1)

    def test_repeated_constants_keep_only_the_initial_declaration(self) -> None:
        raw = b"""\
TOP = 1
TOP = "changed"
local M = {}
M.LIMIT = 3
M.LIMIT = {}
return M
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/constants.lua"))

        top = [
            item
            for item in result.symbols
            if item.name == "TOP" and item.kind is SymbolKind.CONSTANT
        ]
        limit = [
            item
            for item in result.symbols
            if item.name == "LIMIT" and item.kind is SymbolKind.CONSTANT
        ]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].span.start_line, 1)
        self.assertEqual(top[0].returns, "number")
        self.assertEqual(len(limit), 1)
        self.assertEqual(limit[0].span.start_line, 4)
        self.assertEqual(limit[0].returns, "number")

    def test_chunk_tables_are_modules_and_only_literals_are_constants(self) -> None:
        raw = b"""\
local imported = require("external")
local CONFIG = { enabled = true }
local label = "ready"
local count = 2
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/config.lua"))

        self.assertEqual(
            [item.name for item in result.symbols if item.kind is SymbolKind.MODULE],
            ["CONFIG"],
        )
        self.assertEqual(
            {
                item.name: item.returns
                for item in result.symbols
                if item.kind is SymbolKind.CONSTANT
            },
            {"label": "string", "count": "number"},
        )
        self.assertNotIn("imported", {item.name for item in result.symbols})

    def test_nested_explicit_members_include_their_lexical_owner(self) -> None:
        raw = b"""\
local M = {}
local function install_one()
  M.run = function()
    return "one"
  end
end
local function install_two()
  M.run = function()
    return "two"
  end
end
return M
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/installers.lua"))

        runs = [
            item
            for item in result.symbols
            if item.name == "run" and item.kind is SymbolKind.METHOD
        ]
        self.assertEqual(
            [item.id.container_path for item in runs],
            [("install_one", "M"), ("install_two", "M")],
        )
        self.assertEqual(len({item.id for item in runs}), 2)

    def test_returned_table_field_paths_disambiguate_nested_members(self) -> None:
        raw = b"""\
return {
  methods = {
    slash_commands = {
      fetch = {
        setup = function(self)
          self.handlers.set_body = function()
            return "slash"
          end
        end,
      },
    },
    tools = {
      fetch_webpage = {
        setup = function(self)
          self.handlers.set_body = function()
            return "tool"
          end
        end,
      },
    },
  },
}
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/adapter.lua"))

        setups = [item for item in result.symbols if item.name == "setup"]
        self.assertEqual(
            [item.id.container_path for item in setups],
            [
                ("methods", "slash_commands", "fetch"),
                ("methods", "tools", "fetch_webpage"),
            ],
        )
        set_bodies = [item for item in result.symbols if item.name == "set_body"]
        self.assertEqual(
            [item.id.container_path for item in set_bodies],
            [
                (
                    "methods",
                    "slash_commands",
                    "fetch",
                    "setup",
                    "self",
                    "handlers",
                ),
                (
                    "methods",
                    "tools",
                    "fetch_webpage",
                    "setup",
                    "self",
                    "handlers",
                ),
            ],
        )
        self.assertEqual(len({item.id for item in set_bodies}), 2)

    def test_repeated_exact_callable_uses_the_last_runtime_definition(self) -> None:
        raw = b"""\
local T = {}
T['x'] = function()
  return "old"
end
T['x'] = function()
  return "new"
end
return T
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/redefined.lua"))

        callables = [item for item in result.symbols if item.name == "x"]
        self.assertEqual(len(callables), 1)
        self.assertEqual(callables[0].span.start_line, 5)
        owned_bodies = [item for item in result.bodies if item.owner == callables[0].id]
        self.assertEqual(len(owned_bodies), 1)
        self.assertEqual(owned_bodies[0].span.start_line, 6)

    def test_replaced_callable_prunes_nested_symbols_from_its_old_body(self) -> None:
        raw = b"""\
local T = {}
T.x = function()
  local function stale()
    return "old"
  end
  return stale()
end
T.x = function()
  local function current()
    return "new"
  end
  return current()
end
return T
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/replaced_body.lua"))

        self.assertNotIn("stale", {item.name for item in result.symbols})
        current = symbol(result, "current", SymbolKind.FUNCTION)
        self.assertEqual(current.id.container_path, ("T", "x"))
        self.assertEqual(current.span.start_line, 9)
        self.assertEqual(
            [item.name for item in result.calls if item.caller.name == "x"],
            ["current"],
        )
        self.assertNotIn("stale", {item.owner.name for item in result.bodies})

    def test_direct_returned_table_functions_are_methods(self) -> None:
        raw = b"""\
return {
  setup = function(value)
    return value
  end,
}
"""
        result = extract_file(snapshot(raw, Language.LUA, "src/scenario.lua"))

        setup = symbol(result, "setup", SymbolKind.METHOD)
        self.assertEqual(setup.id.container_path, ())

@unittest.skipUnless(
    DEFAULT_REGISTRY.has_parser(Language.HTML), "tree-sitter-html not installed"
)
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

    def test_id_attribute_name_is_ascii_case_insensitive(self) -> None:
        source = snapshot(
            b'<case-panel ID="Root" Id=\'Second\' iD=bare id="lower"></case-panel>\n',
            Language.HTML,
            "web/case.html",
        )
        result = extract_file(source)

        self.assertEqual(
            [item.name for item in result.symbols],
            ["case-panel", "#Root", "#Second", "#bare", "#lower"],
        )
        for name in ("Root", "Second", "bare", "lower"):
            item = symbol(result, f"#{name}", SymbolKind.FUNCTION)
            self.assertEqual(
                source.raw.splitlines()[item.span.start_line - 1][
                    item.span.start_column : item.span.end_column
                ].decode(),
                name,
            )


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
