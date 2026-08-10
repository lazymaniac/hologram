import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram
from hologram import extract_file
from hologram.model import SourceFile, SourceRole, SymbolKind, Visibility
from hologram.parsers.api import extract_file as extract_canonical_file
from hologram.scan import detect_language

POLY = Path(__file__).resolve().parent / "fixtures" / "polyglot"


def _needs(lang):
    return unittest.skipUnless(hologram.has_parser(lang),
                               f"tree-sitter grammar for {lang} not installed")


def _canonical_file(path: Path, root: Path):
    raw = path.read_bytes()
    language = detect_language(path)
    if language is None:
        raise AssertionError(f"unknown fixture language: {path}")
    return extract_canonical_file(
        SourceFile(
            path,
            path.relative_to(root).as_posix(),
            language,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
    )


@_needs("go")
class GoExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _canonical_file(POLY / "sample.go", POLY)
        cls.syms = cls.result.symbols

    def test_struct_with_fields_and_interface(self):
        store = next(s for s in self.syms if s.name == "Store")
        self.assertEqual(store.kind, SymbolKind.CLASS)
        self.assertEqual(store.params, ("map[string]Item", "int"))
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, SymbolKind.INTERFACE)
        quote = next(s for s in self.syms if s.name == "Quote")
        self.assertEqual(quote.container, "Pricer")
        self.assertEqual(quote.returns, "(int,error)")

    def test_method_receiver_and_visibility(self):
        get = next(s for s in self.syms if s.name == "Get")
        self.assertEqual(get.container, "Store")
        self.assertEqual(get.visibility, Visibility.PUBLIC)
        lookup = next(s for s in self.syms if s.name == "lookup")
        self.assertEqual(lookup.visibility, Visibility.PRIVATE)

    def test_receiver_binding_resolves_calls(self):
        get = next(s for s in self.syms if s.name == "Get")
        self.assertIn(
            ("s", "lookup"),
            {
                (call.receiver, call.name)
                for call in self.result.calls
                if call.caller == get.id
            },
        )
        self.assertIn(
            ("s", "Store"),
            {(binding.name, binding.type_name) for binding in get.bindings},
        )


@_needs("rust")
class RustExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _canonical_file(POLY / "sample.rs", POLY)
        cls.syms = cls.result.symbols

    def test_struct_enum_trait(self):
        rat = next(s for s in self.syms if s.name == "Rational")
        self.assertEqual(rat.kind, SymbolKind.CLASS)
        self.assertEqual(rat.params, ("i64", "i64"))
        force = next(s for s in self.syms if s.name == "Force")
        self.assertEqual(force.params, ("Asserted", "Entailed", "Supported"))
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, SymbolKind.INTERFACE)

    def test_trait_impl_becomes_super(self):
        rat = next(s for s in self.syms if s.name == "Rational")
        self.assertIn("Pricer", rat.supers)

    def test_impl_methods_and_visibility(self):
        of = next(s for s in self.syms if s.name == "of" and s.container == "Rational")
        self.assertEqual(of.visibility, Visibility.PUBLIC)
        self.assertEqual(of.returns, "Rational")
        self.assertIn(
            "Rational",
            {call.name for call in self.result.calls if call.caller == of.id},
        )
        reduce = next(s for s in self.syms if s.name == "reduce")
        self.assertEqual(reduce.visibility, Visibility.PRIVATE)


@_needs("csharp")
class CSharpExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _canonical_file(POLY / "Sample.cs", POLY)
        cls.syms = cls.result.symbols

    def test_record_enum_interface_class(self):
        rec = next(s for s in self.syms if s.name == "OrderId")
        self.assertEqual(rec.kind, SymbolKind.RECORD)
        self.assertEqual(rec.params, ("string",))
        status = next(s for s in self.syms if s.name == "Status")
        self.assertEqual(status.params, ("New", "Paid"))
        eng = next(s for s in self.syms if s.name == "PricingEngine")
        self.assertEqual(eng.supers, ("IPricer",))

    def test_methods_ctor_visibility_calls(self):
        ev = next(s for s in self.syms
                  if s.name == "Evaluate" and s.container == "PricingEngine")
        self.assertEqual(ev.visibility, Visibility.PUBLIC)
        calls = {call.name for call in self.result.calls if call.caller == ev.id}
        self.assertIn("Compute", calls)
        self.assertIn("Quote", calls)
        comp = next(s for s in self.syms if s.name == "Compute")
        self.assertEqual(comp.visibility, Visibility.PRIVATE)
        ctor = next(s for s in self.syms if s.kind is SymbolKind.CONSTRUCTOR)
        self.assertEqual(ctor.params, ("Dictionary<string,long>",))


@_needs("c")
class CExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.c", POLY)

    def test_typedef_struct_and_enum(self):
        rat = next(s for s in self.syms if s.name == "Rational")
        self.assertEqual(rat.kind, "class")
        self.assertEqual(rat.params, ["int", "int"])
        force = next(s for s in self.syms if s.name == "Force")
        self.assertEqual(force.params, ["ASSERTED", "ENTAILED"])

    def test_static_fn_private_prototype_public(self):
        red = next(s for s in self.syms if s.name == "reduce")
        self.assertEqual(red.visibility, "priv")
        self.assertEqual(red.params, ["Rational*"])
        add = next(s for s in self.syms if s.name == "rational_add")
        self.assertEqual(add.visibility, "pub")
        self.assertEqual(add.returns, "int")


@_needs("cpp")
class CppExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.cpp", POLY)

    def test_class_access_sections(self):
        ev = next(s for s in self.syms if s.name == "evaluate")
        self.assertEqual(ev.visibility, "pub")
        self.assertEqual(ev.container, "Engine")
        comp = next(s for s in self.syms if s.name == "compute")
        self.assertEqual(comp.visibility, "priv")

    def test_out_of_line_definition_merges_calls(self):
        ev = next(s for s in self.syms if s.name == "evaluate")
        self.assertIn("compute", ev.calls)
        # compute declared in-class (priv), defined out-of-line: one symbol only
        self.assertEqual(len([s for s in self.syms if s.name == "compute"]), 1)

    def test_ctor(self):
        ctor = next(s for s in self.syms if s.kind == "ctor")
        self.assertEqual(ctor.name, "Engine")


@_needs("lua")
class LuaExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.lua", POLY)

    def test_module_functions_and_methods(self):
        quote = next(s for s in self.syms if s.name == "quote")
        self.assertEqual(quote.container, "M")
        self.assertEqual(quote.visibility, "pub")
        self.assertIn("helper", quote.calls)
        reset = next(s for s in self.syms if s.name == "reset")
        self.assertEqual(reset.container, "M")

    def test_local_function_private(self):
        helper = next(s for s in self.syms if s.name == "helper")
        self.assertEqual(helper.visibility, "priv")
        self.assertEqual(helper.params, ["x"])


@_needs("html")
class HtmlExtractTest(unittest.TestCase):
    def test_ids_and_custom_elements(self):
        syms = extract_file(POLY / "page.html", POLY)
        names = {s.name for s in syms}
        self.assertIn("#app", names)
        self.assertIn("#main-nav", names)
        self.assertIn("nav-menu", names)
        self.assertIn("price-card", names)
        self.assertTrue(all(s.visibility == "priv" for s in syms))


class HelmExtractTest(unittest.TestCase):
    def test_chart_values_defines(self):
        chart = extract_file(POLY / "chart/Chart.yaml", POLY)
        self.assertEqual(chart[0].name, "pricing-service")
        self.assertEqual(chart[0].kind, "class")
        values = extract_file(POLY / "chart/values.yaml", POLY)
        names = {s.name for s in values}
        self.assertEqual(names, {"replicaCount", "image", "resources"})
        tpl = extract_file(POLY / "chart/templates/_helpers.tpl", POLY)
        self.assertEqual({s.name for s in tpl},
                         {"pricing.fullname", "pricing.labels"})
        self.assertTrue(all(s.visibility == "pub" for s in tpl))

    def test_plain_yaml_outside_chart_ignored(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ci.yaml"
            p.write_text("stages:\n  - build\nname: pipeline\n")
            self.assertEqual(extract_file(p, Path(tmp)), [])


@_needs("kotlin")
class KotlinExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _canonical_file(POLY / "Sample.kt", POLY)
        cls.syms = cls.result.symbols

    def test_data_class_enum_interface(self):
        oid = next(s for s in self.syms if s.name == "OrderId")
        self.assertEqual(oid.kind, SymbolKind.RECORD)
        self.assertEqual(oid.params, ("String",))
        status = next(s for s in self.syms if s.name == "Status")
        self.assertEqual(status.params, ("NEW", "PAID"))
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, SymbolKind.INTERFACE)

    def test_class_supers_methods_visibility(self):
        eng = next(s for s in self.syms if s.name == "PricingEngine")
        self.assertEqual(eng.supers, ("Pricer",))
        self.assertEqual(eng.params, ("Map<String,Long>",))
        quote = next(s for s in self.syms if s.name == "quote"
                     and s.container == "PricingEngine")
        self.assertEqual(quote.visibility, Visibility.PUBLIC)
        self.assertEqual(quote.returns, "Long")
        self.assertIn(
            "compute",
            {call.name for call in self.result.calls if call.caller == quote.id},
        )
        comp = next(s for s in self.syms if s.name == "compute")
        self.assertEqual(comp.visibility, Visibility.PRIVATE)

    def test_top_level_fn(self):
        fn = next(s for s in self.syms if s.name == "normalize")
        self.assertEqual(fn.kind, SymbolKind.FUNCTION)
        self.assertEqual(fn.returns, "List<Long>")


@_needs("typescript")
class TsGapsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _canonical_file(POLY / "barrel.ts", POLY)
        cls.syms = cls.result.symbols

    def test_type_aliases(self):
        uid = next(s for s in self.syms if s.name == "UserId")
        self.assertEqual(uid.kind, SymbolKind.TYPE)
        self.assertEqual(uid.params, ("string",))
        self.assertEqual(uid.visibility, Visibility.PUBLIC)

    def test_object_literal_api(self):
        api = next(s for s in self.syms if s.name == "api")
        self.assertEqual(api.kind, SymbolKind.CLASS)
        get = next(s for s in self.syms if s.name == "get")
        self.assertEqual(get.container, "api")
        self.assertIn(
            "fetchIt",
            {call.name for call in self.result.calls if call.caller == get.id},
        )
        post = next(s for s in self.syms if s.name == "post")
        self.assertEqual(post.container, "api")
        self.assertEqual(post.returns, "string")

    def test_reexports(self):
        reex = {s.name for s in self.syms if s.kind is SymbolKind.REEXPORT}
        self.assertEqual(reex, {"OrderId", "PriceQuote"})


@_needs("tsx")
class TsxExtractTest(unittest.TestCase):
    def test_jsx_component_arrow_extracted(self):
        syms = _canonical_file(POLY / "Button.tsx", POLY).symbols
        btn = next(
            s
            for s in syms
            if s.name == "Button" and s.kind is SymbolKind.FUNCTION
        )
        self.assertEqual(btn.kind, SymbolKind.FUNCTION)
        self.assertEqual(btn.visibility, Visibility.PUBLIC)
        self.assertIn("track", {s.name for s in syms})


@_needs("vue")
class SfcExtractTest(unittest.TestCase):
    def test_component_symbol_and_script_contents(self):
        syms = _canonical_file(POLY / "Panel.vue", POLY).symbols
        comp = next(s for s in syms if s.name == "Panel")
        self.assertEqual(comp.kind, SymbolKind.CLASS)
        use = next(s for s in syms if s.name == "usePanel")
        self.assertEqual(use.kind, SymbolKind.FUNCTION)
        self.assertEqual(use.visibility, Visibility.PUBLIC)
        self.assertEqual(use.returns, "string")
        self.assertGreater(use.span.start_line, 1)   # offset into the SFC preserved


if __name__ == "__main__":
    unittest.main()
