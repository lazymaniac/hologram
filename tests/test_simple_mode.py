import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import (  # noqa: E402
    Symbol,
    _factored_name_tokens,
    _tree_lines,
    build_digest,
    extract_file,
    render_simple,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"
PYMINI = FIXTURES / "pymini"

needs_java = unittest.skipUnless(hologram.has_parser("java"),
                                 "tree-sitter-java not installed")


class CallExtractionTest(unittest.TestCase):
    @needs_java
    def test_java_method_calls_recorded(self):
        syms = extract_file(JAVAMINI / "src/engine/PricingEngine.java", JAVAMINI)
        ev = next(s for s in syms if s.name == "evaluate")
        self.assertIn("UnknownItemException", ev.calls)
        self.assertIn("Quote", ev.calls)
        self.assertNotIn("if", ev.calls)
        self.assertNotIn("for", ev.calls)

    def test_python_function_calls_recorded(self):
        syms = extract_file(PYMINI / "app.py", PYMINI)
        fn = next(s for s in syms if s.name == "price_order")
        self.assertIn("item.check", fn.calls)

    def test_call_extraction_has_no_display_cap(self):
        source = "def run():\n" + "".join(f"    helper{i}()\n" for i in range(13))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "app.py"
            path.write_text(source)
            run = next(s for s in extract_file(path, root) if s.name == "run")
        self.assertEqual(run.calls, [f"helper{i}" for i in range(13)])


@needs_java
class SimpleDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build_digest(JAVAMINI)

    def test_signatures_present(self):
        self.assertIn("evaluate(order,items):Quote", self.out)
        self.assertIn("of(raw):Self", self.out)

    def test_calls_follow_signature_inline(self):
        lines = self.out.splitlines()
        ev = next(ln for ln in lines if "evaluate(order,items)" in ln)
        self.assertIn("> ", ev)
        self.assertIn("UnknownItemException", ev)

    def test_ordinary_and_informative_constructors_kept(self):
        # A class field list does not prove that public construction exists.
        self.assertIn("PricingEngine(basePrices)", self.out)
        self.assertIn("UnknownItemException(item)", self.out)

    def test_no_docs_no_sections(self):
        self.assertNotIn("Calculates order totals", self.out)
        self.assertNotIn("## MODULES", self.out)
        self.assertNotIn("## API", self.out)

    def test_packages_compressed_group_labels(self):
        self.assertIn("ids", self.out)
        self.assertNotIn("src/ids", self.out)

    def test_calls_inline_no_calls_word(self):
        body = "\n".join(self.out.splitlines()[2:])
        self.assertNotIn("calls ", body)


@needs_java
class SameShapeGroupingTest(unittest.TestCase):
    def test_identical_types_grouped_with_hole_notation(self):
        out = build_digest(JAVAMINI)
        self.assertIn("ItemId,OrderId,UserId(R{value})", out)
        self.assertEqual(out.count("of(raw):Self"), 1)
        self.assertNotIn("of(raw):UserId", out)


class RenderUnitTest(unittest.TestCase):
    def test_platform_calls_filtered_but_frequent_project_calls_kept(self):
        syms = []
        for i in range(30):
            syms.append(Symbol(name=f"fn{i}", kind="fn", file="a/mod.py", line=i,
                               signature=f"fn{i}()", visibility="pub",
                               calls=["requireNonNull", "helperCommon", f"helper{i}"]))
            syms.append(Symbol(name=f"helper{i}", kind="fn", file="a/lib.py", line=i,
                               signature=f"helper{i}()", visibility="priv"))
        syms.append(Symbol(name="helperCommon", kind="fn", file="a/lib.py", line=99,
                           signature="helperCommon()", visibility="priv"))
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        chains = " ".join(ln.split(" > ", 1)[1] for ln in out.splitlines()
                          if " > " in ln and not ln.startswith("·"))
        self.assertNotIn("requireNonNull", chains)  # not project-defined -> platform noise
        self.assertIn("helperCommon", chains)       # frequent project call remains architectural
        self.assertIn("helper3", chains)            # project-defined and rare -> kept

    def test_tree_shares_prefixes_once(self):
        types_by_dir = {"com/x/a": ["A(C)"], "com/x/b": ["B(C)"]}
        lines = _tree_lines(types_by_dir)
        joined = "\n".join(lines)
        self.assertEqual(joined.count("com/x"), 1)
        self.assertNotIn("com/x/a", joined)

    def test_named_fields_replace_types(self):
        syms = [Symbol(name="Widget", kind="class", file="a/w.py", line=1,
                       params=["str", "int"], fields=["name", "size"],
                       visibility="pub")]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("Widget(C{name,size})", out)
        self.assertNotIn("C{str,int}", out)


class EnumValuesTest(unittest.TestCase):
    @needs_java
    def test_java_enum_constants_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/OrderStatus.java", JAVAMINI)
        e = next(s for s in syms if s.kind == "enum")
        self.assertEqual(e.params, ["NEW", "PAID", "SHIPPED"])

    @needs_java
    def test_enum_values_rendered(self):
        out = build_digest(JAVAMINI)
        self.assertIn("OrderStatus(E{NEW,PAID,SHIPPED})", out)

    def test_python_enum_values_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "status.py"
            p.write_text(
                "from enum import Enum\n\n"
                "class Color(Enum):\n    RED = 1\n    GREEN = 2\n"
            )
            syms = extract_file(p, Path(tmp))
        e = next(s for s in syms if s.name == "Color")
        self.assertEqual(e.kind, "enum")
        self.assertEqual(e.params, ["RED", "GREEN"])


@needs_java
class InterfaceMethodTest(unittest.TestCase):
    def test_bodyless_interface_methods_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/PricePort.java", JAVAMINI)
        methods = {s.name: s for s in syms if s.kind == "method"}
        self.assertIn("quoteFor", methods)
        self.assertEqual(methods["quoteFor"].returns, "Quote")
        self.assertEqual(methods["quoteFor"].container, "PricePort")
        self.assertIn("supports", methods)
        self.assertEqual(methods["supports"].returns, "boolean")

    def test_primitive_return_body_method_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/OrderStatus.java", JAVAMINI)
        methods = {s.name for s in syms if s.kind == "method"}
        self.assertIn("isTerminal", methods)

    def test_interface_methods_rendered(self):
        out = build_digest(JAVAMINI)
        idx = out.index("PricePort(I)")
        after = out[idx:idx + 200]
        self.assertIn("quoteFor(order):Quote", after)


@needs_java
class QualifiedCallTest(unittest.TestCase):
    def test_receiver_kept_for_qualified_calls(self):
        syms = extract_file(JAVAMINI / "src/App.java", JAVAMINI)
        main = next(s for s in syms if s.name == "main")
        self.assertIn("engine.evaluate", main.calls)

    def test_render_resolves_receiver_to_declared_type(self):
        out = build_digest(JAVAMINI)
        main_line = next(ln for ln in out.splitlines() if "main(args)" in ln)
        self.assertIn("evaluate", main_line)   # engine -> unique project method
        self.assertNotIn("engine.evaluate", main_line)
        self.assertNotIn("List.of", main_line)               # platform type literal dropped
        self.assertNotIn("getenv", main_line)


@needs_java
class FieldNamesTest(unittest.TestCase):
    def test_declared_field_names_shown(self):
        out = build_digest(JAVAMINI)
        self.assertIn("PricingEngine(C{basePrices})", out)
        self.assertNotIn("C{Map<ItemId,Long>}", out)


@needs_java
class ReconstructablePathTest(unittest.TestCase):
    def test_tree_labels_keep_real_path_segments(self):
        out = build_digest(JAVAMINI)
        lines = out.splitlines()
        self.assertIn("src", lines)
        self.assertTrue(any(ln.strip() == "engine" for ln in lines))


@needs_java
class LanguageFilterTest(unittest.TestCase):
    def test_only_requested_language_included(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mixed"
            shutil.copytree(JAVAMINI, root)
            (root / "script.py").write_text("def py_helper() -> int:\n    return 1\n")
            all_langs = build_digest(root)
            self.assertIn("py_helper", all_langs)
            java_only = build_digest(root, langs={"java"})
            self.assertNotIn("py_helper", java_only)
            self.assertIn("PricingEngine", java_only)

    def test_cli_lang_typo_errors_instead_of_empty_map(self):
        from hologram import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["build", "--root", tmp, "--lang", "pyton", "--quiet"])
        self.assertIn("unknown language", str(ctx.exception))
        self.assertIn("python", str(ctx.exception))  # known list is shown

    def test_cli_lang_flag(self):
        import shutil

        from hologram import embedded_digest, run_cli
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            code = run_cli(["build", "--root", str(proj),
                            "--lang", "java", "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("PricingEngine", embedded_digest(proj / "CLAUDE.md"))


@needs_java
class RelationsTest(unittest.TestCase):
    def test_implements_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/PricingEngine.java", JAVAMINI)
        t = next(s for s in syms if s.name == "PricingEngine")
        self.assertEqual(t.supers, ["PricePort"])

    def test_sealed_permits_extracted(self):
        syms = extract_file(JAVAMINI / "src/transport/Vehicle.java", JAVAMINI)
        t = next(s for s in syms if s.name == "Vehicle")
        self.assertEqual(t.permits, ["Bicycle", "Scooter"])

    def test_generic_supers_not_split_on_type_args(self):
        from hologram.symbols import _heritage
        supers, _ = _heritage(
            "extends React.Component<WidgetProps, WidgetState> implements OnInit")
        self.assertEqual(supers, ["Component", "OnInit"])
        supers, _ = _heritage("extends AbstractMap<K, V> implements Map<K, V>")
        self.assertEqual(supers, ["AbstractMap", "Map"])

    def test_relations_rendered(self):
        out = build_digest(JAVAMINI)
        # interface relations live on the interface line, not per implementor
        self.assertIn("PricePort(I) ←PricingEngine", out)
        self.assertNotIn(": PricePort", out)
        self.assertIn("Vehicle(I) sealed:Bicycle|Scooter", out)
        self.assertNotIn(": Vehicle", out)  # sealed permits already say it
        # non-interface supers keep the : T suffix
        self.assertIn("UnknownItemException(C) : RuntimeException", out)


class InterfaceImplementorsTest(unittest.TestCase):
    def test_inversion_moves_relation_to_interface(self):
        syms = [
            Symbol(name="Port", kind="interface", file="core/p.java", line=1,
                   visibility="pub", lang="java"),
            Symbol(name="AImpl", kind="class", file="core/a.java", line=1,
                   visibility="pub", lang="java", supers=["Port"]),
            Symbol(name="BImpl", kind="class", file="core/b.java", line=1,
                   visibility="pub", lang="java", supers=["Port", "Base"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("Port(I) ←AImpl|BImpl", out)
        self.assertNotIn(": Port", out)
        self.assertIn("BImpl(C) : Base", out)   # external super survives
        self.assertIn("←A|B=implementors", out.splitlines()[1])

    def test_many_implementors_summarized_to_count(self):
        syms = [Symbol(name="Port", kind="interface", file="p.java", line=1,
                       visibility="pub", lang="java")]
        syms += [Symbol(name=f"Impl{i}", kind="class", file=f"i{i}.java", line=1,
                        visibility="pub", lang="java", supers=["Port"])
                 for i in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("Port(I) ←8 impls", out)
        self.assertNotIn("Impl0|", out)


@needs_java
class LegendTest(unittest.TestCase):
    def test_legend_line_present(self):
        out = build_digest(JAVAMINI)
        second = out.splitlines()[1]
        self.assertIn("C/R/I{fields}", second)
        self.assertIn("f(args):Ret > project calls", second)
        self.assertIn("?=tests", second)

    def test_legend_covers_emitted_notation_and_nothing_else(self):
        digest = build_digest(JAVAMINI)
        second = digest.splitlines()[1]
        body = "\n".join(digest.splitlines()[2:])
        # notation → legend clause; clause present exactly when notation occurs
        for notation, clause in ((" : ", ":T=supers"), ("sealed:", "sealed:A|B"),
                                 ("»", "»=re-exports"), ("Self", "Self=own type"),
                                 ("✓", "✓=tested"),
                                 ("×0", "×0=no static use"), (" ~", "~N=lines")):
            if notation in body:
                self.assertIn(clause, second)
            else:
                self.assertNotIn(clause, second)

    def test_legend_prunes_unused_clauses_on_small_corpus(self):
        digest = build_digest(PYMINI)
        second = digest.splitlines()[1]
        body = "\n".join(digest.splitlines()[2:])
        self.assertNotIn("sealed:", body)
        self.assertNotIn("sealed:A|B", second)
        self.assertNotIn("»", second)
        self.assertIn("C/R/I{fields}", second)

    def test_no_query_or_regeneration_prose(self):
        out = build_digest(JAVAMINI)
        self.assertNotIn("query this file", out)
        self.assertNotIn("regen:", out)
        self.assertNotIn("grep", out)


class ConstExtractTest(unittest.TestCase):
    def test_python_module_constants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text(
                "MAX_RETRIES = 3\n"
                "BASE_URL = 'https://api.example.com/v1/orders'\n"
                "TIERS = {'gold': 0.2}\n"
                "_HIDDEN = 1\n"
                "app = object()\n"
                "LOGGER = make()\n")
            out = build_digest(root)
        self.assertIn("= config.py: MAX_RETRIES=3,BASE_URL,TIERS", out)
        self.assertIn("= consts", out.splitlines()[1])
        self.assertNotIn("_HIDDEN", out)
        self.assertNotIn("LOGGER", out)      # call RHS: not a literal
        self.assertNotIn("example.com", out)  # >24 chars renders name-only

    @needs_java
    def test_java_static_final_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "C.java").write_text(
                "public class C {\n"
                "  public static final int MAX_PAGE = 100;\n"
                "  static final String TOPIC = \"orders.created\";\n"
                "  private int state = 1;\n"
                "}\n")
            out = build_digest(root)
        self.assertIn('MAX_PAGE=100,TOPIC="orders.created"', out)
        self.assertNotIn("C{MAX_PAGE", out)  # not restated as fields


class SecretRedactionTest(unittest.TestCase):
    """Secret-shaped constant values never reach the map — the map is copied
    into context files that get committed."""

    def test_secret_named_constants_render_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text(
                "API_KEY = 'abcd1234'\n"
                "AUTH_TOKEN = 'tok'\n"
                "DB_PASSWORD = 'hunter2'\n"
                "SECRET_SALT = 'x'\n"
                "SESSION_COOKIE = 'sid'\n"
                "MAX_RETRIES = 3\n")
            out = build_digest(root)
        self.assertIn("API_KEY", out)          # the name stays informative
        self.assertNotIn("abcd1234", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("tok", out.replace("AUTH_TOKEN", ""))
        self.assertNotIn("sid", out.replace("SESSION_COOKIE", ""))
        self.assertIn("MAX_RETRIES=3", out)    # innocent values still inline

    def test_secret_shaped_values_redacted_regardless_of_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text(
                "STRIPE = 'sk-live-abc'\n"
                "GH = 'ghp_zzzzz'\n"
                "JWT = 'eyJhbGciOi'\n"
                "AWS = 'AKIAIOSFODNN7'\n")
            out = build_digest(root)
        for leaked in ("sk-live", "ghp_", "eyJ", "AKIA"):
            self.assertNotIn(leaked, out)
        self.assertIn("STRIPE", out)

    def test_helper_is_the_single_shared_gate(self):
        from hologram.symbols import const_signature
        self.assertEqual(const_signature("TIMEOUT", "30"), "TIMEOUT=30")
        self.assertEqual(const_signature("PRIVATE_KEY_PATH", "'k.pem'"),
                         "PRIVATE_KEY_PATH")
        self.assertEqual(const_signature("URL", "'" + "x" * 30 + "'"), "URL")
        self.assertEqual(const_signature("TIERS", None), "TIERS")


class RouteRenderTest(unittest.TestCase):
    def _render(self, *syms):
        with tempfile.TemporaryDirectory() as tmp:
            return render_simple(Path(tmp), list(syms), [])

    def test_spring_route_and_class_prefix(self):
        out = self._render(
            Symbol(name="UserController", kind="class", file="api/u.java", line=1,
                   visibility="pub", lang="java",
                   decorators=["RestController", 'RequestMapping("/api/v1")']),
            Symbol(name="find", kind="method", file="api/u.java", line=2,
                   container="UserController", signature="find(long):User",
                   returns="User", visibility="pub", lang="java",
                   decorators=['GetMapping("/users/{id}")']))
        self.assertIn("UserController(C) @/api/v1", out)
        self.assertIn("find():User @GET/users/{id}", out)
        self.assertIn("@=route/annotation", out)

    def test_jaxrs_verb_and_path_pair(self):
        out = self._render(
            Symbol(name="list", kind="fn", file="api/o.java", line=1,
                   signature="list()", visibility="pub", lang="java",
                   decorators=["GET", 'Path("/orders")']))
        self.assertIn("list() @GET/orders", out)

    def test_flask_verb_from_methods_kwarg(self):
        out = self._render(
            Symbol(name="create", kind="fn", file="app.py", line=1,
                   signature="create()", visibility="pub", lang="python",
                   decorators=["app.route('/orders',methods=['POST'])"]))
        self.assertIn("create() @POST/orders", out)

    def test_markers_render_bare_and_noise_dropped(self):
        out = self._render(
            Symbol(name="settle", kind="fn", file="pay.java", line=1,
                   signature="settle()", visibility="pub", lang="java",
                   decorators=["Transactional", "Override",
                               "SuppressWarnings(\"unchecked\")"]))
        self.assertIn("settle() @Transactional", out)
        self.assertNotIn("Override", out)
        self.assertNotIn("SuppressWarnings", out)

    def test_angular_component_selector(self):
        out = self._render(
            Symbol(name="UserComponent", kind="class", file="u.ts", line=1,
                   visibility="pub", lang="typescript",
                   decorators=["Component({ selector: 'app-user' })"]))
        self.assertIn("UserComponent(C) @app-user", out)

    def test_no_decorators_no_legend_clause(self):
        out = self._render(
            Symbol(name="plain", kind="fn", file="a.py", line=1,
                   signature="plain()", visibility="pub", lang="python"))
        self.assertNotIn("@=route/annotation", out)

    def test_symfony_route_with_methods_array(self):
        out = self._render(
            Symbol(name="list", kind="method", file="C.php", line=2,
                   container="OrderController", signature="list()",
                   visibility="pub", lang="php",
                   decorators=["Route('/orders', methods: ['GET'])"]),
            Symbol(name="OrderController", kind="class", file="C.php", line=1,
                   visibility="pub", lang="php"))
        self.assertIn("list() @GET/orders", out)

    def test_aspnet_class_prefix_and_verb_attribute(self):
        out = self._render(
            Symbol(name="OrdersController", kind="class", file="O.cs", line=1,
                   visibility="pub", lang="csharp",
                   decorators=["ApiController", 'Route("api/orders")']),
            Symbol(name="Find", kind="method", file="O.cs", line=2,
                   container="OrdersController", signature="Find(long):Order",
                   returns="Order", visibility="pub", lang="csharp",
                   decorators=['HttpGet("{id}")']))
        self.assertIn("OrdersController(C) @ApiController @/api/orders", out)
        self.assertIn("Find():Order @GET/{id}", out)

    def test_rust_bare_verb_allowed_python_still_suppressed(self):
        out = self._render(
            Symbol(name="list_orders", kind="fn", file="m.rs", line=1,
                   signature="list_orders()", visibility="pub", lang="rust",
                   decorators=['get("/orders")']))
        self.assertIn("list_orders() @GET/orders", out)
        out = self._render(
            Symbol(name="not_a_route", kind="fn", file="a.py", line=1,
                   signature="not_a_route()", visibility="pub", lang="python",
                   decorators=['get("/orders")']))
        self.assertNotIn("@GET", out)


class ThrowsTest(unittest.TestCase):
    @needs_java
    def test_java_throws_clause_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/PricingEngine.java", JAVAMINI)
        ev = next(s for s in syms if s.name == "evaluate")
        self.assertEqual(ev.raises, ["UnknownItemException"])

    def test_python_raise_types_extracted(self):
        syms = extract_file(PYMINI / "models.py", PYMINI)
        check = next(s for s in syms if s.name == "check" and s.container == "UserId")
        self.assertEqual(check.raises, ["ValueError"])

    @needs_java
    def test_throws_rendered_on_signature_exception_suffix_dropped(self):
        out = build_digest(JAVAMINI)
        self.assertIn("evaluate(order,items):Quote !UnknownItem", out)
        self.assertNotIn("!UnknownItemException", out)


class TransitiveReductionTest(unittest.TestCase):
    def _render(self, calls_a):
        syms = [
            Symbol(name="a", kind="fn", file="m/x.py", line=1, signature="a()",
                   visibility="pub", calls=calls_a),
            Symbol(name="b", kind="fn", file="m/x.py", line=2, signature="b()",
                   visibility="pub", calls=["c"]),
            Symbol(name="c", kind="fn", file="m/x.py", line=3, signature="c()",
                   visibility="pub"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            return render_simple(Path(tmp), syms, [])

    def test_implied_edge_dropped(self):
        out = self._render(["b", "c"])          # a>b,c but b>c: c implied
        a_line = next(ln for ln in out.splitlines() if ln.strip().startswith("a()"))
        self.assertEqual(a_line.strip(), "a() > b")

    def test_direct_only_edge_kept(self):
        out = self._render(["c"])
        a_line = next(ln for ln in out.splitlines() if ln.strip().startswith("a()"))
        self.assertEqual(a_line.strip(), "a() > c")

    def test_cycle_members_both_kept(self):
        syms = [
            Symbol(name="x", kind="fn", file="m/x.py", line=1, signature="x()",
                   visibility="pub", calls=["y", "z"]),
            Symbol(name="y", kind="fn", file="m/x.py", line=2, signature="y()",
                   visibility="pub", calls=["x"]),
            Symbol(name="z", kind="fn", file="m/x.py", line=3, signature="z()",
                   visibility="pub", calls=["x"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        x_line = next(ln for ln in out.splitlines() if ln.strip().startswith("x()"))
        # y and z are in x's cycle-SCC-adjacent set; neither implies the other
        # via a path that avoids x, so both survive
        self.assertIn("y", x_line)
        self.assertIn("z", x_line)


class VoidOmissionTest(unittest.TestCase):
    def test_python_none_return_omitted(self):
        syms = extract_file(PYMINI / "app.py", PYMINI)
        main = next(s for s in syms if s.name == "main")
        self.assertEqual(main.signature, "main()")
        self.assertEqual(main.returns, "None")   # data kept, rendering omits

    @needs_java
    def test_java_void_omitted_in_signature(self):
        syms = extract_file(JAVAMINI / "src/App.java", JAVAMINI)
        main = next(s for s in syms if s.name == "main")
        self.assertEqual(main.signature, "main(String[])")
        self.assertEqual(main.returns, "void")


class GroupExtrasTest(unittest.TestCase):
    def test_shared_methods_once_extras_per_member(self):
        syms = [
            Symbol(name="AId", kind="record", file="m/a.py", line=1, params=["String"],
                   fields=["value"],
                   visibility="pub"),
            Symbol(name="BId", kind="record", file="m/b.py", line=1, params=["String"],
                   fields=["value"],
                   visibility="pub"),
            Symbol(name="of", kind="method", file="m/a.py", line=2, container="AId",
                   params=["String"], param_names=["raw"], returns="AId",
                   signature="of(String):AId", visibility="pub"),
            Symbol(name="of", kind="method", file="m/b.py", line=2, container="BId",
                   params=["String"], param_names=["raw"], returns="BId",
                   signature="of(String):BId", visibility="pub"),
            Symbol(name="extra", kind="method", file="m/b.py", line=3, container="BId",
                   signature="extra():int", returns="int", visibility="pub"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("AId,BId(R{value})", out)          # grouped despite extra method
        self.assertEqual(out.count("of(raw):Self"), 1)  # shared shown once
        self.assertIn("BId: extra():int", out)            # divergence kept -> no coverage loss


class PrivateMembersTest(unittest.TestCase):
    def _syms(self):
        return [
            Symbol(name="Cache", kind="class", file="m/cache.py", line=1,
                   visibility="pub"),
            Symbol(name="get", kind="method", file="m/cache.py", line=2,
                   container="Cache", signature="get(str):str", visibility="pub"),
            Symbol(name="_evict", kind="method", file="m/cache.py", line=3,
                   container="Cache", signature="_evict(int):int", visibility="priv"),
            Symbol(name="_rebalance", kind="method", file="m/cache.py", line=4,
                   container="Cache", signature="_rebalance()", visibility="priv"),
            Symbol(name="_helper", kind="fn", file="m/util.py", line=1,
                   signature="_helper():int", visibility="priv"),
        ]

    def test_private_names_packed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), self._syms(), [])
        self.assertIn("- _evict,_rebalance", out)      # class privates, names only
        self.assertIn("- util.py: _helper", out)       # module-level privates per file
        self.assertNotIn("_evict(int)", out)           # no signatures by default

    def test_prefix_factoring_is_lossless_and_profitable(self):
        packed = ",".join(_factored_name_tokens([
            "_extract_java", "_extract_python", "_extract_typescript", "_helper",
        ]))
        self.assertEqual(packed, "_extract_{java,python,typescript},_helper")

    def test_suffix_factoring_camel_and_separator_boundaries(self):
        packed = ",".join(_factored_name_tokens([
            "TaskLoaderTest", "WorkspaceTest", "ReportTest", "Runner",
        ]))
        self.assertEqual(packed, "{TaskLoader,Workspace,Report}Test,Runner")
        packed = ",".join(_factored_name_tokens([
            "load_spec", "save_spec", "drop_spec",
        ]))
        self.assertEqual(packed, "{load,save,drop}_spec")

    def test_prefix_and_suffix_groups_claim_disjoint_names(self):
        # every name reconstructable exactly once; ×0-marked names never
        # join a suffix group (marker must stay outermost)
        packed = ",".join(_factored_name_tokens([
            "_run_a", "_run_b", "_run_c", "AlphaTest", "BetaTest",
            "GammaTest", "EpsilonTest×0",
        ]))
        self.assertEqual(
            packed,
            "_run_{a,b,c},{Alpha,Beta,Gamma}Test,EpsilonTest×0")


class CompactMapContractTest(unittest.TestCase):
    def test_cross_language_test_path_patterns(self):
        for path in ("service_test.go", "service.test.ts", "service.spec.ts",
                     "src/__tests__/service.ts", "src/FooTests.java", "src/FooIT.java"):
            with self.subTest(path=path):
                self.assertTrue(hologram._is_test_path(path))

    def test_overload_collision_adds_types_only_where_needed(self):
        syms = [
            Symbol(name="find", kind="fn", file="api.py", line=1,
                   params=["str"], param_names=["id"], returns="Item",
                   visibility="pub"),
            Symbol(name="find", kind="fn", file="api.py", line=2,
                   params=["int"], param_names=["id"], returns="Item",
                   visibility="pub"),
            Symbol(name="save", kind="fn", file="api.py", line=3,
                   params=["Item"], param_names=["item"], visibility="pub"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("find(id:str):Item", out)
        self.assertIn("find(id:int):Item", out)
        self.assertIn("save(item)", out)
        self.assertNotIn("save(item:Item)", out)

    def test_public_calls_can_target_private_but_external_calls_drop(self):
        syms = [
            Symbol(name="run", kind="fn", file="svc.py", line=1,
                   visibility="pub", calls=["_step", "json.dumps"]),
            Symbol(name="_step", kind="fn", file="svc.py", line=2,
                   visibility="priv"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("run() > _step", out)
        self.assertIn("- svc.py: _step", out)
        self.assertNotIn("json.dumps", out)
        self.assertNotIn("_step()", out)

    def test_test_index_lists_every_file_and_class_but_no_test_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "tests" / "test_service.py"
            go_file = root / "service_test.go"
            test_file.parent.mkdir()
            test_file.write_text("")
            go_file.write_text("")
            syms = [
                Symbol(name="ServiceTest", kind="class", file="tests/test_service.py",
                       line=1, visibility="priv", lang="python"),
                Symbol(name="test_run", kind="fn", file="tests/test_service.py",
                       line=2, visibility="pub", lang="python"),
            ]
            out = render_simple(root, syms, [test_file, go_file])
        self.assertIn("test_service.py{ServiceTest}", out)
        self.assertIn("service_test.go", out)
        self.assertNotIn("test_run", out)

    def test_duplicate_top_level_names_get_file_qualification(self):
        syms = [
            Symbol(name="run", kind="fn", file="a.py", line=1, visibility="pub"),
            Symbol(name="run", kind="fn", file="b.py", line=1, visibility="pub"),
            Symbol(name="Thing", kind="class", file="a.py", line=2,
                   fields=["left"], visibility="pub"),
            Symbol(name="Thing", kind="class", file="b.py", line=2,
                   fields=["right"], visibility="pub"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [])
        self.assertIn("a.py:run()", out)
        self.assertIn("b.py:run()", out)
        self.assertIn("a.py:Thing(C{left})", out)
        self.assertIn("b.py:Thing(C{right})", out)


@needs_java
class TightFormatTest(unittest.TestCase):
    def test_ascii_return_sep_and_tight_commas(self):
        out = build_digest(JAVAMINI)
        self.assertIn("evaluate(order,items):Quote", out)
        self.assertIn("OrderStatus(E{NEW,PAID,SHIPPED})", out)
        self.assertIn("ItemId,OrderId,UserId(R{value})", out)
        self.assertIn("Vehicle(I) sealed:Bicycle|Scooter", out)
        ev = next(ln for ln in out.splitlines() if "evaluate(order" in ln)
        self.assertNotIn("→", ev)   # ascii `:Ret`, not the pretty arrow

    def test_zero_usage_marker(self):
        out = build_digest(JAVAMINI)
        app = next(ln for ln in out.splitlines() if "App(C)" in ln)
        main = next(ln for ln in out.splitlines() if "main(args)" in ln)
        pricing = next(ln for ln in out.splitlines() if "PricingEngine(C{" in ln)
        self.assertIn("×0", app)
        self.assertIn("×0", main)
        self.assertNotIn("×0", pricing)


class ZeroUsageMarkerTest(unittest.TestCase):
    def test_framework_entry_points_not_marked_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "api.py").write_text(
                '@app.route("/orders")\n'
                "def orders():\n    pass\n\n"
                "def plain_dead():\n    pass\n")
            out = build_digest(root)
        self.assertIn("plain_dead()×0", out.replace(" ×0", "×0"))
        self.assertNotIn("orders()×0", out.replace(" ×0", "×0"))
        self.assertIn("@GET/orders", out)

    def test_rust_route_handler_not_marked_dead(self):
        from hologram.gather import _framework_invoked
        rust = Symbol(name="list_orders", kind="fn", file="m.rs", line=1,
                      visibility="pub", lang="rust",
                      decorators=['get("/orders")'])
        py = Symbol(name="get_helper", kind="fn", file="a.py", line=1,
                    visibility="pub", lang="python",
                    decorators=['get("/orders")'])
        self.assertTrue(_framework_invoked(rust))
        self.assertFalse(_framework_invoked(py))

    def test_marks_only_unreferenced_functions_and_classes(self):
        calls = "\n".join(f"    missing{i}()" for i in range(12))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text(
                "class Used:\n    pass\n\n"
                "class Unused:\n    pass\n\n"
                "class Service:\n"
                "    def __init__(self):\n        pass\n\n"
                "def used():\n    pass\n\n"
                "def unused():\n    pass\n\n"
                "def _used():\n    pass\n\n"
                "def _unused():\n    pass\n\n"
                "def caller():\n"
                f"{calls}\n"
                "    used()\n"
                "    Used()\n"
                "    _used()\n\n"
                "# Unused, unused, and _unused are intentionally left here\n"
                "note = 'Unused unused _unused'\n"
            )
            out = build_digest(root)
        lines = out.splitlines()
        used_class = next(line for line in lines if "Used(C)" in line)
        unused_class = next(line for line in lines if "Unused(C)" in line)
        used = next(line for line in lines if line.strip().startswith("used()"))
        unused = next(line for line in out.splitlines()
                      if line.strip().startswith("unused()"))
        caller = next(line for line in lines if line.strip().startswith("caller()"))
        private_line = next(line for line in lines if "app.py:" in line)
        self.assertNotIn("×0", used_class)
        self.assertIn("×0", unused_class)
        self.assertNotIn("×0", used)
        self.assertIn("×0", unused)
        self.assertIn("×0", caller)
        self.assertNotIn("_used×0", private_line)
        self.assertIn("_unused×0", private_line)
        self.assertNotIn("__init__×0", out)

    def test_apostrophes_in_comments_do_not_hide_static_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text(
                "# caller's result\n"
                "def used():\n    pass\n\n"
                "def caller():\n    used()\n"
                "# worker's result\n"
            )
            out = build_digest(root)

        used = next(line for line in out.splitlines()
                    if line.strip().startswith("used()"))
        self.assertNotIn("×0", used)

    @unittest.skipUnless(hologram.has_parser("html"),
                         "tree-sitter-html not installed")
    def test_html_selectors_are_not_code_usage_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<main id="driver"></main>\n')
            out = build_digest(root)

        self.assertIn("#driver", out)
        self.assertNotIn("#driver×0", out)

    def test_legend_describes_static_usage_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mod.py").write_text("def _orphan():\n    pass\n")
            out = build_digest(root)

        self.assertIn("_orphan×0", out)
        self.assertIn("×0=no static use", out)


class PrecomputedRenderTest(unittest.TestCase):
    def test_precomputed_inputs_render_identically(self):
        from hologram.render import (_helper_class_ids, _resolved_project_calls,
                                     _total_loc)
        syms = [Symbol(name="Engine", kind="class", file="a/Engine.java",
                       line=1, visibility="pub", lang="java",
                       fields=["basePrices"]),
                Symbol(name="run", kind="method", file="a/Engine.java", line=2,
                       container="Engine", visibility="pub", lang="java",
                       signature="run()", calls=["helper"]),
                Symbol(name="helper", kind="fn", file="a/util.java", line=1,
                       visibility="pub", lang="java", signature="helper()")]
        with tempfile.TemporaryDirectory() as tmp:
            plain = render_simple(Path(tmp), syms, [])
            pre = render_simple(
                Path(tmp), syms, [], loc=_total_loc([]),
                resolved=_resolved_project_calls(syms),
                helpers=_helper_class_ids(syms, None))
        self.assertEqual(plain, pre)


class TestIndexDietTest(unittest.TestCase):
    def _sym(self, name, kind, file, container=None, calls=(), line=1):
        return Symbol(name=name, kind=kind, file=file, line=line,
                      visibility="pub", lang="java", container=container,
                      signature=f"{name}()", calls=list(calls))

    def _render(self, syms, files):
        with tempfile.TemporaryDirectory() as tmp:
            return render_simple(Path(tmp), syms,
                                 [Path(tmp) / f for f in files])

    def test_single_class_file_folds_and_keeps_edge(self):
        syms = [self._sym("loadTheme", "fn", "src/Theme.java"),
                self._sym("ThemeTest", "class", "test/ThemeTest.java"),
                self._sym("t1", "method", "test/ThemeTest.java",
                          container="ThemeTest", calls=["loadTheme"])]
        out = self._render(syms, ["src/Theme.java", "test/ThemeTest.java"])
        self.assertIn("? tests ·.java", out)
        self.assertIn("ThemeTest>loadTheme", out)
        self.assertNotIn("ThemeTest{", out)
        self.assertNotIn(".java{", out)

    def test_multi_class_file_keeps_braces(self):
        syms = [self._sym("PricingTest", "class", "test/PricingTest.java"),
                self._sym("Bulk", "class", "test/PricingTest.java", line=9)]
        out = self._render(syms, ["test/PricingTest.java"])
        self.assertIn("PricingTest{PricingTest,Bulk}", out)

    def test_stem_mismatch_keeps_braces(self):
        syms = [self._sym("ServiceTest", "class", "test/AllTests.java")]
        out = self._render(syms, ["test/AllTests.java"])
        self.assertIn("AllTests{ServiceTest}", out)

    def test_mixed_extensions_keep_everything(self):
        syms = [self._sym("ATest", "class", "test/ATest.java"),
                Symbol(name="test_b", kind="fn", file="test/test_b.py", line=1,
                       visibility="pub", lang="python", signature="test_b()")]
        out = self._render(syms, ["test/ATest.java", "test/test_b.py"])
        self.assertIn("? tests\n", out)
        self.assertIn("ATest.java", out)
        self.assertIn("test_b.py", out)

    def test_determinism(self):
        syms = [self._sym("PricingTest", "class", "test/PricingTest.java")]
        self.assertEqual(self._render(syms, ["test/PricingTest.java"]),
                         self._render(syms, ["test/PricingTest.java"]))


def _ctor_fixture(ctor_kw=None, fields=("basePrices",), args=("basePrices",)):
    cls = Symbol(name="Engine", kind="class", file="a/Engine.java", line=1,
                 visibility="pub", lang="java", fields=list(fields))
    ctor = Symbol(name="Engine", kind="ctor", file="a/Engine.java", line=2,
                  container="Engine", visibility="pub", lang="java",
                  signature=f"Engine({','.join(args)})", params=list(args),
                  param_names=list(args), **(ctor_kw or {}))
    return [cls, ctor]


class CtorSuppressionTest(unittest.TestCase):
    def _render(self, syms):
        with tempfile.TemporaryDirectory() as tmp:
            return render_simple(Path(tmp), syms, [])

    def test_bare_field_restating_ctor_kept_for_ordinary_class(self):
        out = self._render(_ctor_fixture())
        self.assertIn("Engine(C{basePrices})", out)
        # Fields do not prove that an ordinary class is publicly constructible.
        self.assertIn("\n  Engine(basePrices)", out)

    def test_ctor_with_extra_fact_kept(self):
        out = self._render(_ctor_fixture(ctor_kw={"raises": ["BadPrice"]}))
        self.assertIn("Engine(basePrices) !BadPrice", out)

    def test_ctor_with_different_args_kept(self):
        out = self._render(_ctor_fixture(args=("prices", "clock")))
        self.assertIn("Engine(prices,clock)", out)

    def test_no_arg_ctor_of_componentless_class_kept(self):
        cls = Symbol(name="App", kind="class", file="a/App.java", line=1,
                     visibility="pub", lang="java")
        ctor = Symbol(name="App", kind="ctor", file="a/App.java", line=2,
                      container="App", visibility="pub", lang="java",
                      signature="App()")
        out = self._render([cls, ctor])
        self.assertIn("App()\n", out)

    def test_grouped_same_shape_ctor_suppressed(self):
        syms = []
        for n in ("ItemId", "OrderId"):
            syms.append(Symbol(name=n, kind="record", file="ids/Ids.java",
                               line=1, visibility="pub", lang="java",
                               fields=["value"]))
            syms.append(Symbol(name=n, kind="ctor", file="ids/Ids.java", line=2,
                               container=n, visibility="pub", lang="java",
                               signature=f"{n}(value)", params=["value"],
                               param_names=["value"]))
        out = self._render(syms)
        self.assertIn("ItemId,OrderId(R{value})", out)
        self.assertNotIn("Self(value)", out)


class DunderPrivateTest(unittest.TestCase):
    def _render(self, syms):
        with tempfile.TemporaryDirectory() as tmp:
            return render_simple(Path(tmp), syms, [])

    def test_dunder_methods_out_helper_stays(self):
        syms = [Symbol(name="Box", kind="class", file="a/box.py", line=1,
                       visibility="pub", lang="python")]
        for n in ("__init__", "__repr__", "_pack"):
            syms.append(Symbol(name=n, kind="method", file="a/box.py", line=2,
                               container="Box", visibility="priv",
                               lang="python", signature=f"{n}(self)"))
        out = self._render(syms)
        self.assertIn("- _pack", out)
        self.assertNotIn("__init__", out)
        self.assertNotIn("__repr__", out)

    def test_class_with_only_init_loses_its_line(self):
        syms = [Symbol(name="Box", kind="class", file="a/box.py", line=1,
                       visibility="pub", lang="python"),
                Symbol(name="__init__", kind="method", file="a/box.py", line=2,
                       container="Box", visibility="priv", lang="python",
                       signature="__init__(self)")]
        out = self._render(syms)
        self.assertNotIn("- ", "\n".join(
            l for l in out.splitlines() if l.strip().startswith("- ")))

    def test_module_level_dunder_function_stays(self):
        syms = [Symbol(name="__getattr__", kind="fn", file="a/mod.py", line=1,
                       visibility="priv", lang="python",
                       signature="__getattr__(name)")]
        out = self._render(syms)
        self.assertIn("__getattr__", out)


if __name__ == "__main__":
    unittest.main()
