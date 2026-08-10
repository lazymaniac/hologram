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
    FileIR,
    ImportRef,
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
from hologram.parsers.api import DEFAULT_REGISTRY, extract_file
from hologram.parsers.common import validate_body_events
from hologram.scan import detect_language
from tests.parser_assertions import assert_body_fact_events

ALIAS_SOURCE = b'''\
import Client, { Quote as PriceQuote } from "./api";
import * as ids from "./ids";
export { OrderId as PublicOrderId } from "./ids";
export const load = (id: ids.OrderId): PriceQuote => Client.fetch?.(id);
export const onReady = (): void => {};
register({ handler: "onReady" });
const note = "onReady";
'''

DECLARATION_SOURCE = b'''\
@sealed
export abstract class Outer extends Base implements API {
  private readonly count: number = 1;
  static label = "x";
  constructor(public readonly client: Client) {}

  @memo
  protected async run<T>(input?: T): Promise<T> {
    class Nested { value: number = 1; }
    function inner(x: T): T { return x; }
    const arrow = (n: number): Box => new Box(n);
    if (input) {
      for (const item of [input]) {
        try { return await this.client.go(item); }
        catch (error) { throw error; }
        finally { this.cleanup(); }
      }
    }
    return inner(input!);
  }

  private fieldArrow = (x: string): void => console.log(x);
}

export interface API extends Parent {
  read(id: string): Promise<Item>;
  readonly value?: number;
}

export type Pair = [string, number];
export enum State { Ready, Done = 2 }
export const TOP = 1;
let mutable = 2;

export const api = {
  get(path: string): string { return fetchIt(path); },
  post: (body: string): string => body,
};
'''

OWNERSHIP_SOURCE = b'''\
queue(() => boot());

export function outer(items: Item[]): void {
  items.map((item) => helper(item));
  const named = (value: Item): Result => convert(value);
  function inner(value: Item): Result { return finish(value); }
  named(items[0]);
  inner(items[0]);
}
'''

HIDDEN_SHAPES_SOURCE = b'''\
declare function ambient(x: Input): Output;
function overloaded(x: string): void;
function overloaded(x: number): void;
function overloaded(x: unknown): void {}

abstract class AbstractApi {
  abstract read(x: Input): Output;
  get value(): Output { return current; }
  set value(next: Output) { publish(next); }
}

export const asyncArrow = async (input: Input): Promise<Output> => load(input);
export function construct(): Map<string, Output> {
  return new Map<string, Output>();
}
'''

EDGE_SHAPES_SOURCE = b'''\
class Outer {
  method(): void { class Inner extends Base {} }
}

enum Commented { A, /* middle */ B, // tail
  C }

class PrivateApi {
  #field: number;
  #method(): void {}
}

const locallyExported = 1;
export { locallyExported };
const { visible, hidden } = source;
export { visible };

namespace NestedApi {
  export function run(): void {}
  export class Client {}
  class Internal {}
}

class Initialized {
  value = init();
  static { boot(); }
  @decorated()
  method(): void {}
}
'''

BODY_SOURCE = '''\
export function flow(items: Item[]): Result {
  const total: number = 0;
  for (const item of items) {
    if (item.ok) {
      try {
        const widget = new Widget("za\u017c\u00f3\u0142\u0107");
        service.handle(widget);
      } catch (error) {
        throw error;
      } finally {
        total + 1;
      }
    }
  }
  return finish(total);
}
'''.encode()


def snapshot(
    raw: bytes,
    *,
    language: Language = Language.TYPESCRIPT,
    file: str = "src/example.ts",
    path: Path | None = None,
) -> SourceFile:
    return SourceFile(
        path or Path("/missing") / file,
        file,
        language,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def symbol(
    result: FileIR,
    name: str,
    kind: SymbolKind | None = None,
    container: tuple[str, ...] | None = None,
):
    found = next(
        (
            item
            for item in result.symbols
            if item.name == name
            and (kind is None or item.kind is kind)
            and (container is None or item.id.container_path == container)
        ),
        None,
    )
    if found is None:
        raise AssertionError(
            f"missing {kind or 'symbol'} {name!r} in container {container!r}"
        )
    return found


def token_span(
    source: SourceFile,
    token: bytes,
    *,
    occurrence: int = 1,
) -> SourceSpan:
    offset = -1
    for _ in range(occurrence):
        offset = source.raw.find(token, offset + 1)
        if offset < 0:
            raise AssertionError(f"missing token {token!r}")
    before = source.raw[:offset]
    line = before.count(b"\n") + 1
    line_start = before.rfind(b"\n") + 1
    column = offset - line_start
    end = offset + len(token)
    end_before = source.raw[:end]
    end_line = end_before.count(b"\n") + 1
    end_line_start = end_before.rfind(b"\n") + 1
    return SourceSpan(source.file, line, column, end_line, end - end_line_start)


def import_shape(item: ImportRef) -> tuple[str, str | None, str | None, bool, bool]:
    return item.module, item.name, item.alias, item.wildcard, item.reexport


@unittest.skipUnless(
    DEFAULT_REGISTRY.has_parser(Language.TYPESCRIPT),
    "tree-sitter-typescript not installed",
)
class TypeScriptParserTest(unittest.TestCase):
    def test_all_suffixes_dispatch_with_exact_language_ids(self) -> None:
        cases = {
            "plain.ts": Language.TYPESCRIPT,
            "plain.js": Language.JAVASCRIPT,
            "plain.mjs": Language.JAVASCRIPT,
            "plain.tsx": Language.TSX,
            "plain.jsx": Language.TSX,
        }
        for file, language in cases.items():
            with self.subTest(file=file):
                path = Path("/missing") / file
                self.assertIs(detect_language(path), language)
                raw = (
                    b"export const View = () => <div />;\n"
                    if language is Language.TSX
                    else b"export function value() { return 1; }\n"
                )
                source = snapshot(raw, language=language, file=file, path=path)
                result = extract_file(source)
                self.assertFalse(result.diagnostics)
                self.assertTrue(result.symbols)
                self.assertTrue(
                    all(item.id.language is language for item in result.symbols)
                )
                self.assertTrue(all(call.caller.language is language for call in result.calls))

    def test_snapshot_import_reexport_config_and_module_facts_are_exact(self) -> None:
        source = snapshot(ALIAS_SOURCE, file="src/aliases.ts")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("disk reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("disk reread")),
        ):
            result = extract_file(source)
            repeated = extract_file(source)

        self.assertIs(result.source, source)
        self.assertEqual(result, repeated)
        self.assertEqual(result.module, "src/aliases")
        module = symbol(result, "src/aliases", SymbolKind.MODULE)
        self.assertEqual(module.id.container_path, ())
        self.assertEqual(module.signature, "module src/aliases")
        self.assertIn(module.id, {body.owner for body in result.bodies})
        self.assertEqual(
            [import_shape(item) for item in result.imports],
            [
                ("./api", "default", "Client", False, False),
                ("./api", "Quote", "PriceQuote", False, False),
                ("./ids", None, "ids", True, False),
                ("./ids", "OrderId", "PublicOrderId", False, True),
            ],
        )
        public_id = symbol(result, "PublicOrderId", SymbolKind.REEXPORT)
        self.assertEqual(public_id.visibility, Visibility.PUBLIC)
        self.assertEqual(public_id.span, token_span(source, b"OrderId as PublicOrderId"))

        load = symbol(result, "load", SymbolKind.FUNCTION)
        self.assertEqual(load.params, ("ids.OrderId",))
        self.assertEqual(load.returns, "PriceQuote")
        fetch = next(call for call in result.calls if call.name == "fetch")
        self.assertEqual(fetch.caller, load.id)
        self.assertEqual(fetch.receiver, "Client")
        self.assertEqual(fetch.kind, CallKind.CALL)
        self.assertEqual(fetch.arity, 1)
        register = next(call for call in result.calls if call.name == "register")
        self.assertEqual(register.caller, module.id)

        type_refs = {
            (ref.name, ref.qualifier, ref.kind, ref.context)
            for ref in result.references
            if ref.kind is ReferenceKind.TYPE
        }
        self.assertIn(
            ("OrderId", "ids", ReferenceKind.TYPE, ReferenceContext.TYPE),
            type_refs,
        )
        self.assertIn(
            ("PriceQuote", None, ReferenceKind.TYPE, ReferenceContext.TYPE),
            type_refs,
        )
        callbacks = [
            ref
            for ref in result.references
            if ref.name == "onReady" and ref.context is ReferenceContext.CONFIG
        ]
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].owner, module.id)
        self.assertEqual(callbacks[0].kind, ReferenceKind.NAME)
        self.assertEqual(callbacks[0].confidence, ReferenceConfidence.POSSIBLE)
        self.assertEqual(callbacks[0].span, token_span(source, b'"onReady"'))
        self.assertFalse(
            any(
                ref.name == "onReady"
                and ref.context in {ReferenceContext.STRING, ReferenceContext.CODE}
                and ref.span == token_span(source, b'"onReady"', occurrence=2)
                for ref in result.references
            )
        )
        module_body = next(body for body in result.bodies if body.owner == module.id)
        self.assertIn(
            (BodyEventKind.NAME, callbacks[0].span),
            {(event.kind, event.span) for event in module_body.events},
        )
        self.assertIs(symbol(result, "note").kind, SymbolKind.CONSTANT)
        assert_body_fact_events(self, result)

    def test_declarations_containers_annotations_modifiers_and_bindings(self) -> None:
        result = extract_file(snapshot(DECLARATION_SOURCE, file="src/declarations.ts"))
        self.assertFalse(result.diagnostics)

        outer = symbol(result, "Outer", SymbolKind.CLASS)
        self.assertEqual(outer.supers, ("Base", "API"))
        self.assertEqual(outer.annotations, ("sealed",))
        self.assertEqual(outer.modifiers, ("export", "abstract"))
        self.assertEqual(outer.visibility, Visibility.PUBLIC)

        count = symbol(result, "count", SymbolKind.FIELD, ("Outer",))
        self.assertEqual(count.visibility, Visibility.PRIVATE)
        self.assertEqual(count.modifiers, ("readonly",))
        self.assertEqual(symbol(result, "label").id.container_path, ("Outer",))
        constructor = symbol(result, "Outer", SymbolKind.CONSTRUCTOR, ("Outer",))
        self.assertEqual(constructor.params, ("Client",))
        self.assertEqual(constructor.returns, "Outer")
        self.assertIn(Binding("client", "Client"), constructor.bindings)
        client = symbol(result, "client", SymbolKind.PROPERTY, ("Outer",))
        self.assertIn("readonly", client.modifiers)

        run = symbol(result, "run", SymbolKind.METHOD, ("Outer",))
        self.assertEqual(run.visibility, Visibility.PROTECTED)
        self.assertEqual(run.annotations, ("memo",))
        self.assertEqual(run.modifiers, ("async",))
        self.assertEqual(run.params, ("T",))
        self.assertEqual(run.returns, "Promise<T>")
        self.assertIn(Binding("input", "T"), run.bindings)
        self.assertIn(Binding("client", "Client"), run.bindings)

        self.assertEqual(
            symbol(result, "Nested", SymbolKind.CLASS).id.container_path,
            ("Outer", "run"),
        )
        self.assertEqual(
            symbol(result, "inner", SymbolKind.FUNCTION).id.container_path,
            ("Outer", "run"),
        )
        arrow = symbol(result, "arrow", SymbolKind.FUNCTION)
        self.assertEqual(arrow.id.container_path, ("Outer", "run"))
        self.assertEqual(arrow.params, ("number",))
        self.assertEqual(arrow.returns, "Box")
        field_arrow = symbol(result, "fieldArrow", SymbolKind.METHOD, ("Outer",))
        self.assertEqual(field_arrow.visibility, Visibility.PRIVATE)

        api = symbol(result, "API", SymbolKind.INTERFACE)
        self.assertEqual(api.supers, ("Parent",))
        read = symbol(result, "read", SymbolKind.METHOD, ("API",))
        self.assertEqual(read.params, ("string",))
        self.assertEqual(read.returns, "Promise<Item>")
        value = symbol(result, "value", SymbolKind.PROPERTY, ("API",))
        self.assertIn("readonly", value.modifiers)

        pair = symbol(result, "Pair", SymbolKind.TYPE)
        self.assertEqual(pair.params, ("[string,number]",))
        state = symbol(result, "State", SymbolKind.ENUM)
        self.assertEqual(state.params, ("Ready", "Done"))
        self.assertEqual(state.components, ("Ready", "Done"))
        self.assertEqual(
            {
                item.name
                for item in result.symbols
                if item.kind is SymbolKind.CONSTANT
                and item.id.container_path == ("State",)
            },
            {"Ready", "Done"},
        )
        self.assertIs(symbol(result, "TOP").kind, SymbolKind.CONSTANT)
        self.assertIs(symbol(result, "mutable").kind, SymbolKind.FIELD)

        object_api = symbol(result, "api", SymbolKind.CLASS)
        self.assertEqual(object_api.signature, "const api")
        self.assertEqual(
            symbol(result, "get", SymbolKind.METHOD).id.container_path,
            ("api",),
        )
        self.assertEqual(
            symbol(result, "post", SymbolKind.METHOD).id.container_path,
            ("api",),
        )
        self.assertTrue(all(item.id.language is Language.TYPESCRIPT for item in result.symbols))
        assert_body_fact_events(self, result)

    def test_calls_are_uncapped_optional_construct_exact_and_source_ordered(self) -> None:
        calls = b"\n".join(f"  fn{index}?.();".encode() for index in range(20))
        raw = b"export function many(): void {\n" + calls + b"\n  new Widget(1, 2);\n}\n"
        result = extract_file(snapshot(raw, file="src/many.ts"))
        many = symbol(result, "many", SymbolKind.FUNCTION)
        owned = [call for call in result.calls if call.caller == many.id]

        self.assertEqual([call.name for call in owned], [*(f"fn{i}" for i in range(20)), "Widget"])
        self.assertEqual(len(owned), 21)
        self.assertTrue(all(call.kind is CallKind.CALL for call in owned[:20]))
        self.assertEqual(owned[-1].kind, CallKind.CONSTRUCT)
        self.assertEqual(owned[-1].arity, 2)
        self.assertEqual(owned[-1].span, token_span(snapshot(raw, file="src/many.ts"), b"new Widget(1, 2)"))
        self.assertEqual(owned, sorted(owned, key=lambda call: call.span))
        assert_body_fact_events(self, result)

    def test_nested_named_and_anonymous_expressions_have_nearest_owner(self) -> None:
        result = extract_file(snapshot(OWNERSHIP_SOURCE, file="src/owners.ts"))
        module = symbol(result, "src/owners", SymbolKind.MODULE)
        outer = symbol(result, "outer", SymbolKind.FUNCTION)
        named = symbol(result, "named", SymbolKind.FUNCTION)
        inner = symbol(result, "inner", SymbolKind.FUNCTION)
        by_name = {call.name: call.caller for call in result.calls}

        self.assertEqual(by_name["queue"], module.id)
        self.assertEqual(by_name["boot"], module.id)
        self.assertEqual(by_name["map"], outer.id)
        self.assertEqual(by_name["helper"], outer.id)
        self.assertEqual(by_name["convert"], named.id)
        self.assertEqual(by_name["finish"], inner.id)
        self.assertEqual(by_name["named"], outer.id)
        self.assertEqual(by_name["inner"], outer.id)
        self.assertEqual(named.id.container_path, ("outer",))
        self.assertEqual(inner.id.container_path, ("outer",))
        assert_body_fact_events(self, result)

    def test_ambient_overloads_abstract_accessors_and_generic_constructs(self) -> None:
        result = extract_file(
            snapshot(HIDDEN_SHAPES_SOURCE, file="src/hidden-shapes.ts")
        )
        ambient = symbol(result, "ambient", SymbolKind.FUNCTION)
        self.assertEqual(ambient.params, ("Input",))
        overloads = [
            item
            for item in result.symbols
            if item.name == "overloaded" and item.kind is SymbolKind.FUNCTION
        ]
        self.assertEqual(
            {item.id.signature_key for item in overloads},
            {"(string)", "(number)", "(unknown)"},
        )
        abstract = symbol(result, "read", SymbolKind.METHOD, ("AbstractApi",))
        self.assertIn("abstract", abstract.modifiers)
        accessors = [
            item
            for item in result.symbols
            if item.name == "value" and item.kind is SymbolKind.METHOD
        ]
        self.assertEqual(len(accessors), 2)
        self.assertEqual(
            {modifier for item in accessors for modifier in item.modifiers},
            {"get", "set"},
        )
        async_arrow = symbol(result, "asyncArrow", SymbolKind.FUNCTION)
        self.assertIn("async", async_arrow.modifiers)
        construct = symbol(result, "construct", SymbolKind.FUNCTION)
        call = next(call for call in result.calls if call.caller == construct.id)
        self.assertEqual((call.name, call.kind, call.arity), ("Map", CallKind.CONSTRUCT, 0))
        assert_body_fact_events(self, result)

    def test_anonymous_default_exports_keep_calls_on_the_module_owner(self) -> None:
        raw = (
            b"export default function () { functionBoot(); }\n"
            b"export default class { run() { classBoot(); } }\n"
        )
        result = extract_file(snapshot(raw, file="src/defaults.ts"))
        module = symbol(result, "src/defaults", SymbolKind.MODULE)
        self.assertEqual(
            {call.name for call in result.calls if call.caller == module.id},
            {"functionBoot", "classBoot"},
        )
        self.assertFalse(
            any(
                item.name in {"default", "anonymous"}
                for item in result.symbols
            )
        )

    def test_edge_grammar_shapes_do_not_leak_flatten_or_drop_facts(self) -> None:
        result = extract_file(snapshot(EDGE_SHAPES_SOURCE, file="src/edges.ts"))
        outer = symbol(result, "Outer", SymbolKind.CLASS)
        inner = symbol(result, "Inner", SymbolKind.CLASS)
        self.assertEqual(outer.supers, ())
        self.assertEqual(inner.supers, ("Base",))
        self.assertEqual(inner.id.container_path, ("Outer", "method"))

        enum_constants = {
            item.name
            for item in result.symbols
            if item.kind is SymbolKind.CONSTANT
            and item.id.container_path == ("Commented",)
        }
        self.assertEqual(enum_constants, {"A", "B", "C"})
        self.assertEqual(
            symbol(result, "#field", SymbolKind.FIELD).visibility,
            Visibility.PRIVATE,
        )
        self.assertEqual(
            symbol(result, "#method", SymbolKind.METHOD).visibility,
            Visibility.PRIVATE,
        )
        self.assertEqual(
            symbol(result, "locallyExported", SymbolKind.CONSTANT).visibility,
            Visibility.PUBLIC,
        )
        self.assertEqual(
            symbol(result, "visible", SymbolKind.CONSTANT).visibility,
            Visibility.PUBLIC,
        )
        self.assertEqual(
            symbol(result, "hidden", SymbolKind.CONSTANT).visibility,
            Visibility.PRIVATE,
        )
        self.assertEqual(
            inner.visibility,
            Visibility.PRIVATE,
        )

        namespace = symbol(result, "NestedApi", SymbolKind.MODULE)
        self.assertEqual(namespace.id.container_path, ())
        self.assertEqual(
            symbol(result, "run", SymbolKind.FUNCTION).id.container_path,
            ("NestedApi",),
        )
        self.assertEqual(
            symbol(result, "Client", SymbolKind.CLASS).id.container_path,
            ("NestedApi",),
        )
        self.assertEqual(
            symbol(result, "Internal", SymbolKind.CLASS).visibility,
            Visibility.PRIVATE,
        )

        initialized = symbol(result, "Initialized", SymbolKind.CLASS)
        owned_calls = {
            call.name for call in result.calls if call.caller == initialized.id
        }
        self.assertEqual(owned_calls, {"init", "boot", "decorated"})
        self.assertEqual(len({item.id for item in result.symbols}), len(result.symbols))
        assert_body_fact_events(self, result)

    def test_sfc_mask_ignores_script_markup_inside_html_comments(self) -> None:
        raw = (
            b"<!-- <script>export function fake(): void { hidden(); }</script> -->\n"
            b"<script>export function real(): void { visible(); }</script>\n"
        )
        result = extract_file(
            snapshot(raw, language=Language.VUE, file="Commented.vue")
        )
        self.assertIn("real", {item.name for item in result.symbols})
        self.assertNotIn("fake", {item.name for item in result.symbols})
        self.assertEqual({call.name for call in result.calls}, {"visible"})

    def test_body_categories_controls_utf8_spans_and_stable_ids(self) -> None:
        source = snapshot(BODY_SOURCE, file="src/body.ts")
        original = extract_file(source)
        shifted = extract_file(snapshot(b"\n" + BODY_SOURCE, file="src/body.ts"))
        flow = symbol(original, "flow", SymbolKind.FUNCTION)
        shifted_flow = symbol(shifted, "flow", SymbolKind.FUNCTION)
        self.assertEqual(flow.id, shifted_flow.id)
        self.assertEqual(shifted_flow.span.start_line, flow.span.start_line + 1)

        body = next(item for item in original.bodies if item.owner == flow.id)
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
        self.assertEqual(
            [event.text for event in body.events if event.kind is BodyEventKind.CONTROL_ENTER],
            ["loop", "if", "try", "catch", "finally"],
        )
        self.assertEqual(
            len([event for event in body.events if event.kind is BodyEventKind.CONTROL_ENTER]),
            len([event for event in body.events if event.kind is BodyEventKind.CONTROL_EXIT]),
        )
        handle = next(call for call in original.calls if call.name == "handle")
        self.assertEqual(handle.span, token_span(source, b"service.handle(widget)"))
        assert_body_fact_events(self, original)

    def test_vue_and_svelte_masked_scripts_keep_original_byte_coordinates(self) -> None:
        raw = (
            "pr\u00e9<script>export const first = (): string => \"\u017c\";</script>tail\r\n"
            "<div>\u0142</div><script lang=\"ts\">export function second(x: number): number "
            "{ return x + 1; }</script>fin"
        ).encode()
        cases = ((Language.VUE, "Widget.vue"), (Language.SVELTE, "Widget.svelte"))
        for language, file in cases:
            with self.subTest(language=language):
                source = snapshot(raw, language=language, file=file)
                result = extract_file(source)
                self.assertFalse(result.diagnostics)
                self.assertEqual(result.module, "Widget")
                component = symbol(result, "Widget", SymbolKind.CLASS)
                first = symbol(result, "first", SymbolKind.FUNCTION)
                second = symbol(result, "second", SymbolKind.FUNCTION)
                self.assertEqual(component.signature, "component Widget")
                self.assertEqual(first.span.start_line, 1)
                self.assertEqual(first.span.start_column, token_span(source, b"first").start_column)
                self.assertEqual(second.span.start_line, 2)
                self.assertEqual(
                    second.span.start_column,
                    token_span(source, b"function second").start_column,
                )
                self.assertTrue(all(item.id.language is language for item in result.symbols))
                self.assertTrue(all(call.caller.language is language for call in result.calls))
                assert_body_fact_events(self, result)

    def test_component_is_emitted_without_a_script(self) -> None:
        for language, file in (
            (Language.VUE, "Only.vue"),
            (Language.SVELTE, "Only.svelte"),
        ):
            with self.subTest(language=language):
                result = extract_file(
                    snapshot(b"<template>\xc5\xbc</template>\r\n", language=language, file=file)
                )
                component = symbol(result, "Only", SymbolKind.CLASS)
                self.assertIs(component.id.language, language)
                self.assertEqual(component.span, SourceSpan(file, 1, 0, 2, 0))

    def test_syntax_errors_are_diagnostics_with_partial_facts(self) -> None:
        result = extract_file(
            snapshot(b"export class Broken { method( { return call(); }\n", file="Broken.ts")
        )
        self.assertIn("Broken", {item.name for item in result.symbols})
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0].code, "tree-sitter-syntax-error")
        self.assertEqual(result.diagnostics[0].severity, DiagnosticSeverity.ERROR)


if __name__ == "__main__":
    unittest.main()
