import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import extract_file  # noqa: E402

POLY = Path(__file__).resolve().parent / "fixtures" / "polyglot"


def _needs(lang):
    return unittest.skipUnless(hologram.has_parser(lang),
                               f"tree-sitter grammar for {lang} not installed")


@_needs("go")
class GoExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.go", POLY)

    def test_struct_with_fields_and_interface(self):
        store = next(s for s in self.syms if s.name == "Store")
        self.assertEqual(store.kind, "class")
        self.assertEqual(store.params, ["map[string]Item", "int"])
        self.assertEqual(store.fields, ["items", "ttl"])
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, "interface")
        quote = next(s for s in self.syms if s.name == "Quote")
        self.assertEqual(quote.container, "Pricer")
        self.assertEqual(quote.returns, "(int,error)")

    def test_method_receiver_and_visibility(self):
        get = next(s for s in self.syms if s.name == "Get")
        self.assertEqual(get.container, "Store")
        self.assertEqual(get.param_names, ["id"])
        self.assertEqual(get.visibility, "pub")
        lookup = next(s for s in self.syms if s.name == "lookup")
        self.assertEqual(lookup.visibility, "priv")

    def test_receiver_binding_resolves_calls(self):
        get = next(s for s in self.syms if s.name == "Get")
        self.assertIn("s.lookup", get.calls)
        self.assertEqual(get.bindings.get("s"), "Store")


@_needs("rust")
class RustExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.rs", POLY)

    def test_struct_enum_trait(self):
        rat = next(s for s in self.syms if s.name == "Rational")
        self.assertEqual(rat.kind, "class")
        self.assertEqual(rat.params, ["i64", "i64"])
        self.assertEqual(rat.fields, ["num", "den"])
        force = next(s for s in self.syms if s.name == "Force")
        self.assertEqual(force.params, ["Asserted", "Entailed", "Supported"])
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, "interface")

    def test_trait_impl_becomes_super(self):
        rat = next(s for s in self.syms if s.name == "Rational")
        self.assertIn("Pricer", rat.supers)

    def test_impl_methods_and_visibility(self):
        of = next(s for s in self.syms if s.name == "of" and s.container == "Rational")
        self.assertEqual(of.visibility, "pub")
        self.assertEqual(of.param_names, ["num", "den"])
        self.assertEqual(of.returns, "Rational")
        self.assertIn("Rational", of.calls)          # struct literal = construction
        reduce = next(s for s in self.syms if s.name == "reduce")
        self.assertEqual(reduce.visibility, "priv")


@_needs("csharp")
class CSharpExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "Sample.cs", POLY)

    def test_record_enum_interface_class(self):
        rec = next(s for s in self.syms if s.name == "OrderId")
        self.assertEqual(rec.kind, "record")
        self.assertEqual(rec.params, ["string"])
        self.assertEqual(rec.fields, ["Value"])
        status = next(s for s in self.syms if s.name == "Status")
        self.assertEqual(status.params, ["New", "Paid"])
        eng = next(s for s in self.syms if s.name == "PricingEngine")
        self.assertEqual(eng.supers, ["IPricer"])
        self.assertEqual(eng.fields, ["prices"])

    def test_methods_ctor_visibility_calls(self):
        ev = next(s for s in self.syms
                  if s.name == "Evaluate" and s.container == "PricingEngine")
        self.assertEqual(ev.visibility, "pub")
        self.assertEqual(ev.param_names, ["id"])
        self.assertIn("Compute", ev.calls)
        self.assertIn("Quote", ev.calls)
        comp = next(s for s in self.syms if s.name == "Compute")
        self.assertEqual(comp.visibility, "priv")
        ctor = next(s for s in self.syms if s.kind == "ctor")
        self.assertEqual(ctor.params, ["Dictionary<string,long>"])

    def test_throw_statements_become_raises(self):
        ev = next(s for s in self.syms
                  if s.name == "Evaluate" and s.container == "PricingEngine")
        self.assertEqual(ev.raises, ["UnknownOrderException"])


@_needs("c")
class CExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.c", POLY)

    def test_typedef_struct_and_enum(self):
        rat = next(s for s in self.syms if s.name == "Rational")
        self.assertEqual(rat.kind, "class")
        self.assertEqual(rat.params, ["int", "int"])
        self.assertEqual(rat.fields, ["num", "den"])
        force = next(s for s in self.syms if s.name == "Force")
        self.assertEqual(force.params, ["ASSERTED", "ENTAILED"])

    def test_static_fn_private_prototype_public(self):
        red = next(s for s in self.syms if s.name == "reduce")
        self.assertEqual(red.visibility, "priv")
        self.assertEqual(red.params, ["Rational*"])
        self.assertEqual(red.param_names, ["r"])
        add = next(s for s in self.syms if s.name == "rational_add")
        self.assertEqual(add.visibility, "pub")
        self.assertEqual(add.returns, "int")


@_needs("cpp")
class CppExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.cpp", POLY)

    def test_class_access_sections(self):
        engine = next(s for s in self.syms if s.name == "Engine" and s.kind == "class")
        self.assertEqual(engine.fields, ["prices"])
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

    def test_throw_statements_become_raises(self):
        ev = next(s for s in self.syms if s.name == "evaluate")
        self.assertEqual(ev.raises, ["BadInput"])


@_needs("bash")
class BashExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.sh", POLY)

    def test_both_definition_forms(self):
        names = {s.name for s in self.syms}
        self.assertEqual(names, {"_log", "build_image", "deploy"})
        deploy = next(s for s in self.syms if s.name == "deploy")
        self.assertEqual(deploy.kind, "fn")
        self.assertEqual(deploy.signature, "deploy()")

    def test_underscore_prefix_private(self):
        log = next(s for s in self.syms if s.name == "_log")
        self.assertEqual(log.visibility, "priv")
        self.assertTrue(all(
            s.visibility == "pub" for s in self.syms if s.name != "_log"))

    def test_call_chains(self):
        deploy = next(s for s in self.syms if s.name == "deploy")
        self.assertIn("_log", deploy.calls)
        self.assertIn("build_image", deploy.calls)
        self.assertIn("docker", deploy.calls)
        build = next(s for s in self.syms if s.name == "build_image")
        self.assertIn("_log", build.calls)

    def test_sizes(self):
        self.assertTrue(all(s.size > 0 for s in self.syms))


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
        self.assertEqual(helper.param_names, ["x"])


@_needs("css")
class CssExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "theme.css", POLY)

    def test_selectors(self):
        names = {s.name for s in self.syms}
        self.assertIn(".card", names)
        self.assertIn(".card-title", names)
        self.assertIn(".nav-item", names)
        self.assertIn("#app", names)
        self.assertIn(".sidebar", names)  # inside @media

    def test_pseudo_classes_not_selectors(self):
        names = {s.name for s in self.syms}
        self.assertNotIn(".hover", names)
        self.assertNotIn(".root", names)

    def test_custom_properties_and_keyframes(self):
        names = {s.name for s in self.syms}
        self.assertIn("--brand-color", names)
        self.assertIn("--gap", names)
        self.assertIn("@spin", names)
        self.assertTrue(all(s.visibility == "priv" for s in self.syms))

    def test_dedup(self):
        self.assertEqual(len([s for s in self.syms if s.name == ".card"]), 1)


@_needs("html")
@_needs("typescript")
@_needs("css")
class HtmlNestedBlocksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "widget.html", POLY)

    def test_script_functions_extracted(self):
        render = next(s for s in self.syms if s.name == "renderWidget")
        self.assertEqual(render.lang, "typescript")
        self.assertIn("attach", render.calls)
        self.assertGreater(render.line, 1)  # offset into the html file

    def test_style_selectors_extracted(self):
        names = {s.name for s in self.syms}
        self.assertIn(".widget", names)
        self.assertIn("--accent", names)

    def test_ids_and_custom_elements_still_present(self):
        names = {s.name for s in self.syms}
        self.assertIn("#widget-root", names)
        self.assertIn("status-badge", names)
        # #widget-root appears as both html id and css id selector: one symbol
        self.assertEqual(len([s for s in self.syms
                              if s.name == "#widget-root"]), 1)


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
        cls.syms = extract_file(POLY / "Sample.kt", POLY)

    def test_data_class_enum_interface(self):
        oid = next(s for s in self.syms if s.name == "OrderId")
        self.assertEqual(oid.kind, "record")
        self.assertEqual(oid.params, ["String"])
        self.assertEqual(oid.fields, ["value"])
        status = next(s for s in self.syms if s.name == "Status")
        self.assertEqual(status.params, ["NEW", "PAID"])
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, "interface")

    def test_class_supers_methods_visibility(self):
        eng = next(s for s in self.syms if s.name == "PricingEngine")
        self.assertEqual(eng.supers, ["Pricer"])
        self.assertEqual(eng.params, ["Map<String,Long>"])
        self.assertEqual(eng.fields, ["prices"])
        quote = next(s for s in self.syms if s.name == "quote"
                     and s.container == "PricingEngine")
        self.assertEqual(quote.visibility, "pub")
        self.assertEqual(quote.returns, "Long")
        self.assertIn("compute", quote.calls)
        comp = next(s for s in self.syms if s.name == "compute")
        self.assertEqual(comp.visibility, "priv")

    def test_top_level_fn(self):
        fn = next(s for s in self.syms if s.name == "normalize")
        self.assertEqual(fn.kind, "fn")
        self.assertEqual(fn.returns, "List<Long>")

    def test_local_bindings_resolve_receivers(self):
        demo = next(s for s in self.syms if s.name == "demo")
        self.assertEqual(demo.bindings.get("engine"), "PricingEngine")
        self.assertEqual(demo.bindings.get("backup"), "Pricer")
        self.assertIn("engine.quote", demo.calls)

    def test_throws_annotation_and_throw_expressions_become_raises(self):
        comp = next(s for s in self.syms if s.name == "compute")
        self.assertEqual(comp.raises,
                         ["UnknownOrderException", "IllegalStateException"])


@_needs("typescript")
class TsGapsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "barrel.ts", POLY)

    def test_type_aliases(self):
        uid = next(s for s in self.syms if s.name == "UserId")
        self.assertEqual(uid.kind, "type")
        self.assertEqual(uid.params, ["string"])
        self.assertEqual(uid.visibility, "pub")

    def test_object_literal_api(self):
        api = next(s for s in self.syms if s.name == "api")
        self.assertEqual(api.kind, "class")
        get = next(s for s in self.syms if s.name == "get")
        self.assertEqual(get.container, "api")
        self.assertIn("fetchIt", get.calls)
        post = next(s for s in self.syms if s.name == "post")
        self.assertEqual(post.container, "api")
        self.assertEqual(post.returns, "string")

    def test_reexports(self):
        reex = {s.name for s in self.syms if s.kind == "reexport"}
        self.assertEqual(reex, {"OrderId", "PriceQuote"})


@_needs("tsx")
class TsxExtractTest(unittest.TestCase):
    def test_jsx_component_arrow_extracted(self):
        syms = extract_file(POLY / "Button.tsx", POLY)
        btn = next(s for s in syms if s.name == "Button")
        self.assertEqual(btn.kind, "fn")
        self.assertEqual(btn.visibility, "pub")
        self.assertIn("track", {s.name for s in syms})


@_needs("vue")
class SfcExtractTest(unittest.TestCase):
    def test_component_symbol_and_script_contents(self):
        syms = extract_file(POLY / "Panel.vue", POLY)
        comp = next(s for s in syms if s.name == "Panel")
        self.assertEqual(comp.kind, "class")
        use = next(s for s in syms if s.name == "usePanel")
        self.assertEqual(use.kind, "fn")
        self.assertEqual(use.visibility, "pub")
        self.assertEqual(use.returns, "string")
        self.assertGreater(use.line, 1)   # offset into the SFC preserved


if __name__ == "__main__":
    unittest.main()
