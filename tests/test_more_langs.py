import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import build_digest, extract_file  # noqa: E402

POLY = Path(__file__).resolve().parent / "fixtures" / "polyglot"


def _needs(lang):
    return unittest.skipUnless(hologram.has_parser(lang),
                               f"tree-sitter grammar for {lang} not installed")


@_needs("go")
class GoExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.go", POLY)

    def test_consts(self):
        consts = {s.name: (s.signature, s.visibility)
                  for s in self.syms if s.kind == "const"}
        self.assertEqual(consts["MaxItems"], ("MaxItems=10", "pub"))
        self.assertEqual(consts["Topic"], ('Topic="items.changed"', "pub"))
        self.assertEqual(consts["internal"][1], "priv")
        self.assertEqual(consts["First"][0], "First")  # iota: name only

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

    def test_attributes_and_consts(self):
        find = next(s for s in self.syms if s.name == "find_point")
        self.assertEqual(find.decorators, ['get("/points/{id}")'])
        const = next(s for s in self.syms if s.kind == "const")
        self.assertEqual(const.signature, "MAX_POINTS=10")
        out = build_digest(POLY)
        self.assertIn("@GET/points/{id}", out)
        self.assertNotIn("find_point(id):Point ×0", out)

    def test_struct_enum_trait(self):
        point = next(s for s in self.syms if s.name == "Point")
        self.assertEqual(point.kind, "class")
        self.assertEqual(point.params, ["i64", "i64"])
        self.assertEqual(point.fields, ["x", "y"])
        axis = next(s for s in self.syms if s.name == "Axis")
        self.assertEqual(axis.params, ["Horizontal", "Vertical", "Depth"])
        locatable = next(s for s in self.syms if s.name == "Locatable")
        self.assertEqual(locatable.kind, "interface")
        self.assertEqual(locatable.supers, ["Clone"])  # trait X: Y supertrait bound

    def test_trait_impl_becomes_super(self):
        point = next(s for s in self.syms if s.name == "Point")
        self.assertIn("Locatable", point.supers)

    def test_impl_methods_and_visibility(self):
        new = next(s for s in self.syms if s.name == "new" and s.container == "Point")
        self.assertEqual(new.visibility, "pub")
        self.assertEqual(new.param_names, ["x", "y"])
        self.assertEqual(new.returns, "Point")
        self.assertIn("Point", new.calls)          # struct literal = construction
        translate = next(s for s in self.syms if s.name == "translate")
        self.assertEqual(translate.visibility, "priv")


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

    def test_attributes_routes_and_consts(self):
        ctrl = next(s for s in self.syms if s.name == "OrdersController")
        self.assertEqual(ctrl.decorators, ["ApiController", 'Route("api/orders")'])
        find = next(s for s in self.syms if s.name == "Find")
        self.assertEqual(find.decorators, ['HttpGet("{id}")'])
        const = next(s for s in self.syms if s.kind == "const")
        self.assertEqual(const.signature, "MaxItems=10")
        self.assertNotIn("MaxItems", ctrl.fields)


@_needs("c")
class CExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.c", POLY)

    def test_typedef_struct_and_enum(self):
        point = next(s for s in self.syms if s.name == "Point")
        self.assertEqual(point.kind, "class")
        self.assertEqual(point.params, ["int", "int"])
        self.assertEqual(point.fields, ["x", "y"])
        axis = next(s for s in self.syms if s.name == "Axis")
        self.assertEqual(axis.params, ["HORIZONTAL", "VERTICAL"])

    def test_static_fn_private_prototype_public(self):
        total = next(s for s in self.syms if s.name == "component_sum")
        self.assertEqual(total.visibility, "priv")
        self.assertEqual(total.params, ["Point*"])
        self.assertEqual(total.param_names, ["point"])
        add = next(s for s in self.syms if s.name == "point_add")
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

    def test_private_header_declaration_controls_out_of_line_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "secret.hpp").write_text(
                "class Secret {\nprivate:\n  void hidden();\n"
                "public:\n  void run();\n};\n")
            (root / "secret.cpp").write_text(
                '#include "secret.hpp"\n'
                "void Secret::hidden() { run(); }\n"
                "void Secret::run() {}\n")
            output = build_digest(root)

        self.assertIn("- hidden", output)
        self.assertNotIn("hidden()", output)


@_needs("bash")
class BashExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.sh", POLY)

    def test_script_root_node_and_both_definition_forms(self):
        script = next(s for s in self.syms if s.name == "sample.sh")
        self.assertEqual(script.kind, "class")
        self.assertEqual(script.signature, "script sample.sh")
        fns = {s.name for s in self.syms if s.kind == "method"}
        self.assertEqual(fns, {"_log", "build_image", "deploy"})
        deploy = next(s for s in self.syms if s.name == "deploy")
        self.assertEqual(deploy.container, "sample.sh")
        self.assertEqual(deploy.signature, "deploy()")

    def test_variables_with_values_secret_redacted(self):
        consts = {s.name: s.signature for s in self.syms if s.kind == "const"}
        self.assertEqual(consts["MAX_RETRIES"], "MAX_RETRIES=3")
        self.assertEqual(consts["REGISTRY"], 'REGISTRY="docker.example.io"')
        self.assertEqual(consts["DEPLOY_TOKEN"], "DEPLOY_TOKEN")  # redacted
        self.assertEqual(consts["STAMP"], "STAMP")  # command substitution

    def test_underscore_prefix_private(self):
        log = next(s for s in self.syms if s.name == "_log")
        self.assertEqual(log.visibility, "priv")
        self.assertTrue(all(
            s.visibility == "pub" for s in self.syms
            if s.kind == "method" and s.name != "_log"))

    def test_call_chains(self):
        deploy = next(s for s in self.syms if s.name == "deploy")
        self.assertIn("_log", deploy.calls)
        self.assertIn("build_image", deploy.calls)
        self.assertIn("docker", deploy.calls)
        build = next(s for s in self.syms if s.name == "build_image")
        self.assertIn("_log", build.calls)

    def test_sizes(self):
        self.assertTrue(all(s.size > 0 for s in self.syms
                            if s.kind == "method"))


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

    def test_public_module_methods_survive_end_to_end_rendering(self):
        output = build_digest(POLY, langs={"lua"})
        self.assertIn("M: quote(id)", output)
        self.assertIn("reset()", output)


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

    def test_annotations_and_consts(self):
        ctrl = next(s for s in self.syms if s.name == "OrderController")
        self.assertEqual(ctrl.decorators,
                         ["RestController", 'RequestMapping("/api/orders")'])
        find = next(s for s in self.syms if s.name == "find")
        self.assertEqual(find.decorators, ['GetMapping("/{id}")'])
        consts = {s.name: s.signature for s in self.syms if s.kind == "const"}
        self.assertEqual(consts, {"MAX_RETRIES": "MAX_RETRIES=3",
                                  "PAGE_SIZE": "PAGE_SIZE=25"})

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


WEBMINI = Path(__file__).resolve().parent / "fixtures" / "webmini"


@_needs("tsx")
class ReactComponentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(WEBMINI / "Component.tsx", WEBMINI)

    def test_jsx_usage_becomes_calls_intrinsics_dropped(self):
        ul = next(s for s in self.syms if s.name == "UserList")
        self.assertIn("UserCard", ul.calls)
        self.assertIn("Badge", ul.calls)
        self.assertNotIn("section", ul.calls)

    def test_memo_wrapped_default_export_recovered(self):
        page = next(s for s in self.syms if s.name == "Page")
        self.assertEqual(page.decorators, ["memo"])
        self.assertIn("UserList", page.calls)

    def test_fc_type_argument_replaces_untyped_props(self):
        card = next(s for s in self.syms if s.name == "UserCard")
        self.assertEqual(card.params, ["Props"])

    def test_component_render_tree_in_digest(self):
        out = build_digest(WEBMINI)
        page_line = next(ln for ln in out.splitlines() if "Page()" in ln)
        self.assertIn("@memo", page_line)
        self.assertIn("UserList", page_line.split(">", 1)[1])


@_needs("typescript")
class AngularExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(WEBMINI / "app.component.ts", WEBMINI)

    def test_component_selector_and_injectable(self):
        comp = next(s for s in self.syms if s.name == "UserListComponent")
        self.assertTrue(any(d.startswith("Component(") for d in comp.decorators))
        svc = next(s for s in self.syms if s.name == "UserService")
        self.assertEqual(svc.decorators, ["Injectable()"])

    def test_di_and_output_bindings(self):
        ng = next(s for s in self.syms if s.name == "ngOnInit")
        self.assertEqual(ng.bindings["svc"], "UserService")
        self.assertEqual(ng.bindings["cfg"], "Config")
        self.assertEqual(ng.bindings["picked"], "EventEmitter")

    def test_route_config_extracted(self):
        syms = extract_file(WEBMINI / "app.routes.ts", WEBMINI)
        routes = next(s for s in syms if s.kind == "const")
        self.assertEqual(
            routes.signature,
            "routes=/users→UserListComponent,/orders/:id→OrderComponent")

    def test_digest_shows_selector_and_routes(self):
        out = build_digest(WEBMINI)
        self.assertIn("@app-user-list", out)
        self.assertIn("/users→UserListComponent", out)
        self.assertNotIn("ngOnInit() ×0", out)  # lifecycle hook is not dead code

    def test_inline_template_elements_become_component_edges(self):
        app = next(s for s in self.syms if s.name == "AppComponent")
        self.assertIn("app-user-list", app.calls)
        out = build_digest(WEBMINI)
        self.assertIn("AppComponent(C{all}) @app-root > UserListComponent", out)

    def test_templateurl_external_html_joins(self):
        out = build_digest(WEBMINI)
        self.assertIn("ShellComponent(C) @app-shell > UserListComponent", out)

    def test_duplicate_selector_produces_no_edge(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.ts").write_text(
                "@Component({ selector: 'app-x', template: '<i></i>' })\n"
                "export class XComponent {}\n"
                "@Component({ selector: 'app-x', template: '<i></i>' })\n"
                "export class YComponent {}\n"
                "@Component({ selector: 'app-z', template: '<app-x/>' })\n"
                "export class ZComponent {}\n")
            out = build_digest(root)
        self.assertNotIn("> XComponent", out)
        self.assertNotIn("> YComponent", out)


@_needs("typescript")
class TsLossRecoveryTest(unittest.TestCase):
    """Data the grammar always had that the extractor used to drop."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "w.ts").write_text(
            "export interface Repo {\n"
            "  find(id: string): Promise<User>;\n"
            "  save(u: User): void;\n"
            "}\n"
            "export class Widget {\n"
            "  @Output() picked = new EventEmitter<User>();\n"
            "  @Input() users: User[] = [];\n"
            "  constructor(@Inject(TOKEN) private cfg: Config,\n"
            "              readonly svc: UserService) {}\n"
            "  ngOnInit(): void { this.svc.load(); }\n"
            "}\n")
        cls.syms = extract_file(root / "w.ts", root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_interface_methods_extracted(self):
        find = next(s for s in self.syms
                    if s.name == "find" and s.container == "Repo")
        self.assertEqual(find.kind, "method")
        self.assertEqual(find.returns, "Promise<User>")
        self.assertEqual(find.param_names, ["id"])

    def test_untyped_field_bound_through_new_expression(self):
        widget = next(s for s in self.syms if s.name == "Widget")
        self.assertIn("picked", widget.fields)
        ng = next(s for s in self.syms if s.name == "ngOnInit")
        self.assertEqual(ng.bindings["picked"], "EventEmitter")

    def test_decorated_and_readonly_ctor_params_parse(self):
        ctor = next(s for s in self.syms if s.kind == "ctor")
        self.assertEqual(ctor.params, ["Config", "UserService"])
        self.assertEqual(ctor.param_names, ["cfg", "svc"])
        ng = next(s for s in self.syms if s.name == "ngOnInit")
        self.assertEqual(ng.bindings["cfg"], "Config")
        self.assertEqual(ng.bindings["svc"], "UserService")


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


@_needs("ruby")
class RubyExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.rb", POLY)

    def test_attr_and_ivar_fields(self):
        eng = next(s for s in self.syms if s.name == "PricingEngine")
        self.assertEqual(eng.fields, ["prices"])

    def test_module_class_and_methods(self):
        eng = next(s for s in self.syms if s.name == "PricingEngine"
                   and s.kind == "class")
        self.assertEqual(eng.lang, "ruby")
        quote = next(s for s in self.syms if s.name == "quote")
        self.assertEqual(quote.container, "PricingEngine")
        self.assertEqual(quote.param_names, ["id"])
        self.assertIn("compute", quote.calls)

    def test_private_section_toggles_visibility(self):
        comp = next(s for s in self.syms if s.name == "compute")
        self.assertEqual(comp.visibility, "priv")
        quote = next(s for s in self.syms if s.name == "quote")
        self.assertEqual(quote.visibility, "pub")

    def test_initialize_becomes_ctor_top_level_def_becomes_fn(self):
        ctor = next(s for s in self.syms if s.kind == "ctor")
        self.assertEqual(ctor.name, "PricingEngine")
        self.assertEqual(ctor.param_names, ["prices"])
        fn = next(s for s in self.syms if s.name == "normalize")
        self.assertEqual(fn.kind, "fn")


@_needs("php")
class PhpExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.php", POLY)

    def test_attributes_and_class_consts(self):
        ctrl = next(s for s in self.syms if s.name == "OrderController")
        self.assertEqual(ctrl.decorators, ["AsController"])
        idx = next(s for s in self.syms if s.name == "index")
        self.assertEqual(idx.decorators, ["Route('/orders',methods: ['GET'])"])
        const = next(s for s in self.syms if s.kind == "const")
        self.assertEqual(const.signature, "MAX_ITEMS=10")
        out = build_digest(POLY)
        self.assertIn("index():array @GET/orders", out)

    def test_interface_class_supers_fields(self):
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, "interface")
        eng = next(s for s in self.syms if s.name == "PricingEngine"
                   and s.kind == "class")
        self.assertEqual(eng.supers, ["Pricer"])
        self.assertEqual(eng.fields, ["prices"])

    def test_methods_visibility_typed_params_returns(self):
        quote = next(s for s in self.syms if s.name == "quote"
                     and s.container == "PricingEngine")
        self.assertEqual(quote.visibility, "pub")
        self.assertEqual(quote.params, ["OrderId"])
        self.assertEqual(quote.param_names, ["id"])
        self.assertEqual(quote.returns, "int")
        comp = next(s for s in self.syms if s.name == "compute")
        self.assertEqual(comp.visibility, "priv")

    def test_ctor_bindings_calls_throws(self):
        ctor = next(s for s in self.syms if s.kind == "ctor")
        self.assertEqual(ctor.name, "PricingEngine")
        quote = next(s for s in self.syms if s.name == "quote"
                     and s.container == "PricingEngine")
        self.assertIn("this.compute", quote.calls)
        self.assertEqual(quote.bindings.get("this"), "PricingEngine")
        self.assertEqual(quote.raises, ["UnknownOrderException"])
        demo = next(s for s in self.syms if s.name == "demo")
        self.assertEqual(demo.bindings.get("engine"), "PricingEngine")
        self.assertIn("engine.quote", demo.calls)


@_needs("swift")
class SwiftExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.swift", POLY)

    def test_throw_types_become_raises(self):
        q = next(s for s in self.syms
                 if s.name == "quote" and s.container == "PricingEngine")
        self.assertEqual(q.raises, ["PricingError"])

    def test_protocol_class_struct_kinds_and_supers(self):
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, "interface")
        eng = next(s for s in self.syms if s.name == "PricingEngine"
                   and s.kind == "class")
        self.assertEqual(eng.supers, ["Pricer"])
        self.assertEqual(eng.fields, ["prices"])
        oid = next(s for s in self.syms if s.name == "OrderId")
        self.assertEqual(oid.kind, "record")  # struct
        self.assertEqual(oid.fields, ["value"])

    def test_methods_visibility_init_ctor(self):
        quote = next(s for s in self.syms if s.name == "quote"
                     and s.container == "PricingEngine")
        self.assertEqual(quote.visibility, "pub")
        self.assertEqual(quote.param_names, ["id"])
        self.assertEqual(quote.returns, "Int")
        self.assertIn("compute", quote.calls)
        comp = next(s for s in self.syms if s.name == "compute"
                    and s.container == "PricingEngine")
        self.assertEqual(comp.visibility, "priv")
        ctor = next(s for s in self.syms if s.kind == "ctor")
        self.assertEqual(ctor.name, "PricingEngine")

    def test_local_binding_resolves_receiver(self):
        demo = next(s for s in self.syms if s.name == "demo")
        self.assertEqual(demo.bindings.get("engine"), "PricingEngine")
        self.assertIn("engine.quote", demo.calls)


@_needs("scala")
class ScalaExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(POLY / "sample.scala", POLY)

    def test_throw_new_becomes_raises(self):
        q = next(s for s in self.syms
                 if s.name == "quote" and s.container == "PricingEngine")
        self.assertEqual(q.raises, ["UnknownOrderException"])

    def test_case_class_trait_object_kinds(self):
        oid = next(s for s in self.syms if s.name == "OrderId")
        self.assertEqual(oid.kind, "record")
        self.assertEqual(oid.fields, ["value"])
        pricer = next(s for s in self.syms if s.name == "Pricer")
        self.assertEqual(pricer.kind, "interface")
        reg = next(s for s in self.syms if s.name == "Registry")
        self.assertEqual(reg.kind, "class")  # object singleton

    def test_class_supers_methods_visibility(self):
        eng = next(s for s in self.syms if s.name == "PricingEngine"
                   and s.kind == "class")
        self.assertEqual(eng.supers, ["Pricer"])
        self.assertEqual(eng.params, ["Map[String,Long]"])
        quote = next(s for s in self.syms if s.name == "quote"
                     and s.container == "PricingEngine")
        self.assertEqual(quote.returns, "Long")
        self.assertIn("compute", quote.calls)
        comp = next(s for s in self.syms if s.name == "compute"
                    and s.container == "PricingEngine")
        self.assertEqual(comp.visibility, "priv")

    def test_local_binding_resolves_receiver(self):
        demo = next(s for s in self.syms if s.name == "demo")
        self.assertEqual(demo.bindings.get("engine"), "PricingEngine")
        self.assertIn("engine.quote", demo.calls)


class MakefileExtractTest(unittest.TestCase):
    def test_rule_facts_are_exact_and_repeated_targets_merge(self):
        syms = extract_file(POLY / "Makefile", POLY)
        self.assertEqual(
            [(s.name, s.kind, s.line, s.signature, s.visibility, s.calls, s.size)
             for s in syms],
            [
                ("Makefile", "class", 1, "makefile Makefile", "pub", [], 0),
                ("deploy", "method", 15,
                 "deploy(ENV,MANIFEST,REGION,IMAGE,FLAGS,CHANNEL)", "pub",
                 ["build", "prepare", "audit"], 5),
                ("build", "method", 24, "build(CC)", "pub", [], 1),
                ("prepare", "method", 24, "prepare(CC)", "pub", [], 1),
                ("audit", "method", 24, "audit(CC)", "pub", [], 1),
                ("release", "method", 27, "release(ENV)", "pub",
                 ["deploy"], 1),
                ("test", "method", 31, "test(FLAGS)", "pub", [], 1),
                ("lint", "method", 31, "lint(FLAGS)", "pub", [], 1),
                ("_stage", "method", 34, "_stage(ENV)", "priv", [], 1),
            ])

    def test_digest_has_dependency_edge_and_no_external_target_dead_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text(
                "deploy: build\n\techo $(ENV)\n\nbuild:\n\t@:\n")
            lines = build_digest(root).splitlines()
            body = lines[2:-1]  # stable header/legend and volatile metadata footer
        self.assertIn(" LOC ", lines[-1])
        self.assertEqual(body, [
            "Makefile(C)",
            " deploy(ENV) > build",
            " build()",
        ])

    def test_shell_escaped_variable_is_not_a_make_parameter(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "show:\n\techo $${HOME} $(REAL)\n", "Makefile")
        show = next(symbol for symbol in symbols if symbol.name == "show")
        self.assertEqual(show.signature, "show(REAL)")

    def test_dollar_run_parity_controls_make_expansion(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "show:\n"
            "\techo $(ONE) $$(TWO) $$$(THREE) $$$$(FOUR) $$$$$(FIVE)\n",
            "Makefile")
        show = next(symbol for symbol in symbols if symbol.name == "show")
        self.assertEqual(show.signature, "show(ONE,THREE,FIVE)")

    def test_substitution_and_hyphenated_variable_references(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "TOOL-FLAGS := --verbose\n"
            "show:\n"
            "\techo $(IMAGE:latest=stable) ${FILES:.c=.o} $(TOOL-FLAGS)\n"
            "\techo $(shell pwd) $(wildcard *.c) $@ $(@D)\n"
            "\techo $(subst old,new,$(NESTED))\n",
            "Makefile")
        show = next(symbol for symbol in symbols if symbol.name == "show")
        self.assertEqual(
            show.signature, "show(IMAGE,FILES,TOOL-FLAGS,NESTED)")

    def test_hyphenated_override_assignments_pin_variables(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "override GLOBAL-FLAGS = fixed\n"
            "show: override TARGET-FLAGS = fixed\n"
            "show:\n"
            "\techo $(OPEN-FLAGS) $(GLOBAL-FLAGS) $(TARGET-FLAGS)\n",
            "Makefile")
        show = next(symbol for symbol in symbols if symbol.name == "show")
        self.assertEqual(show.signature, "show(OPEN-FLAGS)")

    def test_recipe_continuation_does_not_require_another_prefix(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "deploy:\n"
            "\techo $(FIRST) \\\n"
            "  $(SECOND) \\\n"
            "\t$(THIRD)\n",
            "Makefile")
        deploy = next(symbol for symbol in symbols if symbol.name == "deploy")
        self.assertEqual(deploy.signature, "deploy(FIRST,SECOND,THIRD)")
        self.assertEqual(deploy.size, 3)

    def test_custom_recipe_prefix_and_reset_are_honored(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            ".RECIPEPREFIX := >\n"
            "custom:\n"
            ">echo $(CUSTOM) \\\n"
            "  $(CONTINUED)\n"
            ".RECIPEPREFIX :=\n"
            "normal:\n"
            "\techo $(NORMAL)\n",
            "Makefile")
        methods = {symbol.name: symbol for symbol in symbols
                   if symbol.kind == "method"}
        self.assertEqual(methods["custom"].signature,
                         "custom(CUSTOM,CONTINUED)")
        self.assertEqual(methods["custom"].size, 2)
        self.assertEqual(methods["normal"].signature, "normal(NORMAL)")

    def test_hash_starts_a_make_comment_inside_prerequisite_word(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "deploy: bar#comment without leading whitespace\n"
            "\t@:\n"
            "bar:\n"
            "\t@:\n",
            "Makefile")
        deploy = next(symbol for symbol in symbols if symbol.name == "deploy")
        self.assertEqual(deploy.calls, ["bar"])

    def test_conditional_recipe_branches_remain_attached_to_target(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "deploy:\n"
            "ifeq ($(MODE),fast)\n"
            "\techo $(FAST)\n"
            "else\n"
            "ifdef SAFE_MODE\n"
            "\techo $(SAFE)\n"
            "else ifneq ($(MODE),disabled)\n"
            "\techo $(FALLBACK)\n"
            "endif\n"
            "endif\n"
            "next:\n"
            "\techo $(NEXT)\n",
            "Makefile")
        methods = {symbol.name: symbol for symbol in symbols
                   if symbol.kind == "method"}
        self.assertEqual(methods["deploy"].signature,
                         "deploy(FAST,SAFE,FALLBACK)")
        self.assertEqual(methods["deploy"].size, 3)
        self.assertEqual(methods["next"].signature, "next(NEXT)")

    def test_repeated_single_colon_uses_last_recipe_but_merges_prereqs(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "deploy: build\n"
            "\techo $(OLD)\n"
            "\techo $(OLDER)\n"
            "deploy: audit\n"
            "\techo $(NEW)\n"
            "deploy: verify\n",
            "Makefile")
        deploy = next(symbol for symbol in symbols if symbol.name == "deploy")
        self.assertEqual(deploy.signature, "deploy(NEW)")
        self.assertEqual(deploy.calls, ["build", "audit", "verify"])
        self.assertEqual(deploy.size, 1)

    def test_explicit_empty_recipe_overrides_earlier_recipe(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "deploy:\n"
            "\techo $(OLD)\n"
            "deploy: ;\n",
            "Makefile")
        deploy = next(symbol for symbol in symbols if symbol.name == "deploy")
        self.assertEqual(deploy.signature, "deploy()")
        self.assertEqual(deploy.size, 0)

    def test_repeated_double_colon_unions_independent_recipes(self):
        from hologram.extract.misc import _extract_make
        symbols = _extract_make(
            "deploy:: build\n"
            "\techo $(FIRST)\n"
            "deploy:: audit\n"
            "\techo $(SECOND)\n",
            "Makefile")
        deploy = next(symbol for symbol in symbols if symbol.name == "deploy")
        self.assertEqual(deploy.signature, "deploy(FIRST,SECOND)")
        self.assertEqual(deploy.calls, ["build", "audit"])
        self.assertEqual(deploy.size, 2)

    def test_named_makefile_detected_without_extension(self):
        from hologram import detect_language
        from pathlib import Path as P
        self.assertEqual(detect_language(P("x/Makefile")), "make")
        self.assertEqual(detect_language(P("x/GNUmakefile")), "make")
        self.assertEqual(detect_language(P("x/rules.mk")), "make")


if __name__ == "__main__":
    unittest.main()
