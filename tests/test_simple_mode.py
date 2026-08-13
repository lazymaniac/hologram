import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import (  # noqa: E402
    Symbol,
    _factored_names,
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
        self.assertIn("of(raw):⟨X⟩", self.out)

    def test_calls_follow_signature_inline(self):
        lines = self.out.splitlines()
        ev = next(ln for ln in lines if "evaluate(order,items)" in ln)
        self.assertIn("> ", ev)
        self.assertIn("UnknownItemException", ev)

    def test_public_ctor_rendered_without_return_type(self):
        self.assertIn("PricingEngine(basePrices)", self.out)
        self.assertNotIn("PricingEngine(basePrices):PricingEngine", self.out)
        self.assertIn("UnknownItemException(item)", self.out)

    def test_no_docs_no_sections(self):
        self.assertNotIn("Rule-tree", self.out)
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
        self.assertEqual(out.count("of(raw):⟨X⟩"), 1)
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
        syms = extract_file(JAVAMINI / "src/delta/DeltaOp.java", JAVAMINI)
        t = next(s for s in syms if s.name == "DeltaOp")
        self.assertEqual(t.permits, ["AddOp", "RemoveOp"])

    def test_relations_rendered(self):
        out = build_digest(JAVAMINI)
        self.assertIn("PricingEngine(C{basePrices}) : PricePort", out)
        self.assertIn("DeltaOp(I) sealed:AddOp|RemoveOp", out)
        self.assertIn("(R{nodeId}) : DeltaOp", out)


@needs_java
class LegendTest(unittest.TestCase):
    def test_legend_line_present(self):
        out = build_digest(JAVAMINI)
        second = out.splitlines()[1]
        self.assertIn("C/R/I{fields}", second)
        self.assertIn("f(args):Ret > project calls", second)
        self.assertIn("?=tests", second)

    def test_no_query_or_regeneration_prose(self):
        out = build_digest(JAVAMINI)
        self.assertNotIn("query this file", out)
        self.assertNotIn("regen:", out)
        self.assertNotIn("grep", out)


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
        self.assertEqual(out.count("of(raw):⟨X⟩"), 1)  # shared shown once
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
        packed = _factored_names([
            "_extract_java", "_extract_python", "_extract_typescript", "_helper",
        ])
        self.assertEqual(packed, "_extract_{java,python,typescript},_helper")


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
        self.assertIn("(I) sealed:AddOp|RemoveOp", out)
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

    def test_html_selectors_are_not_code_usage_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<main id="driver"></main>\n')
            out = build_digest(root)

        self.assertIn("#driver", out)
        self.assertNotIn("#driver×0", out)

    def test_legend_describes_static_usage_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = build_digest(Path(tmp))

        self.assertIn("×0=no static use", out)


if __name__ == "__main__":
    unittest.main()
