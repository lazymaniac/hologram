import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import Symbol, _tree_lines, build_digest, extract_file, render_simple  # noqa: E402

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


@needs_java
class SimpleDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build_digest(JAVAMINI)

    def test_signatures_present(self):
        self.assertIn("evaluate(OrderId,List<ItemId>):Quote", self.out)
        self.assertIn("of(String):⟨X⟩", self.out)

    def test_calls_follow_signature_inline(self):
        lines = self.out.splitlines()
        ev = next(ln for ln in lines if "evaluate(OrderId" in ln)
        self.assertIn("> ", ev)
        self.assertIn("UnknownItemException", ev)

    def test_no_docs_no_sections(self):
        self.assertNotIn("Rule-tree", self.out)
        self.assertNotIn("## MODULES", self.out)
        self.assertNotIn("## API", self.out)

    def test_packages_compressed_group_labels(self):
        self.assertIn("ids", self.out)
        self.assertNotIn("src/ids", self.out)

    def test_calls_inline_no_calls_word(self):
        body = "\n".join(self.out.splitlines()[3:])   # skip title/legend/query lines
        self.assertNotIn("calls ", body)


@needs_java
class SameShapeGroupingTest(unittest.TestCase):
    def test_identical_types_grouped_with_hole_notation(self):
        out = build_digest(JAVAMINI)
        self.assertIn("ItemId,OrderId,UserId(R: String)", out)
        self.assertEqual(out.count("of(String):⟨X⟩"), 1)
        self.assertNotIn("of(String):UserId", out)


class RenderUnitTest(unittest.TestCase):
    def test_platform_and_ubiquitous_calls_filtered(self):
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
            out = render_simple(Path(tmp), syms, [], "regen")
        chains = " ".join(ln.split(" > ", 1)[1] for ln in out.splitlines() if " > " in ln)
        self.assertNotIn("requireNonNull", chains)  # not project-defined -> platform noise
        self.assertNotIn("helperCommon", chains)    # project-defined but ubiquitous -> dropped
        self.assertIn("helper3", chains)            # project-defined and rare -> kept

    def test_tree_shares_prefixes_once(self):
        types_by_dir = {"com/x/a": ["A(C)"], "com/x/b": ["B(C)"]}
        lines = _tree_lines(types_by_dir)
        joined = "\n".join(lines)
        self.assertEqual(joined.count("com/x"), 1)
        self.assertNotIn("com/x/a", joined)

    def test_hot_types_marked(self):
        syms = [Symbol(name="Widget", kind="class", file="a/w.py", line=1, visibility="pub"),
                Symbol(name="Rare", kind="class", file="b/r.py", line=1, visibility="pub")]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [], "regen",
                                scores={"Widget": 12.0, "Rare": 2.0})
        widget_line = next(ln for ln in out.splitlines() if "Widget" in ln)
        rare_line = next(ln for ln in out.splitlines() if "Rare(" in ln)
        self.assertIn("×12", widget_line)
        self.assertNotIn("×", rare_line)


class EnumValuesTest(unittest.TestCase):
    @needs_java
    def test_java_enum_constants_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/OrderStatus.java", JAVAMINI)
        e = next(s for s in syms if s.kind == "enum")
        self.assertEqual(e.params, ["NEW", "PAID", "SHIPPED"])

    @needs_java
    def test_enum_values_rendered(self):
        out = build_digest(JAVAMINI)
        self.assertIn("OrderStatus(E: NEW,PAID,SHIPPED)", out)

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
        self.assertIn("quoteFor(OrderId):Quote", after)


@needs_java
class QualifiedCallTest(unittest.TestCase):
    def test_receiver_kept_for_qualified_calls(self):
        syms = extract_file(JAVAMINI / "src/App.java", JAVAMINI)
        main = next(s for s in syms if s.name == "main")
        self.assertIn("engine.evaluate", main.calls)

    def test_render_resolves_receiver_to_declared_type(self):
        out = build_digest(JAVAMINI)
        main_line = next(ln for ln in out.splitlines() if "main(String[])" in ln)
        self.assertIn("PricingEngine.evaluate", main_line)   # engine -> its declared type
        self.assertNotIn("engine.evaluate", main_line)
        self.assertNotIn("List.of", main_line)               # platform type literal dropped
        self.assertNotIn("getenv", main_line)


@needs_java
class CtorComponentsTest(unittest.TestCase):
    def test_class_constructor_params_shown_as_components(self):
        out = build_digest(JAVAMINI)
        self.assertIn("PricingEngine(C: Map<ItemId,Long>)", out)


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
        from hologram import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "d.md"
            code = run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                            "--lang", "java", "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("PricingEngine", out.read_text())


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
        self.assertIn("PricingEngine(C: Map<ItemId,Long>) : PricePort", out)
        self.assertIn("DeltaOp(I sealed: AddOp|RemoveOp)", out)
        self.assertIn("(R: String) : DeltaOp", out)


@needs_java
class LegendTest(unittest.TestCase):
    def test_legend_line_present(self):
        out = build_digest(JAVAMINI)
        second = out.splitlines()[1]
        self.assertIn("legend", second)
        self.assertIn("⟨X⟩", second)
        self.assertIn("> calls", second)

    def test_query_recipes_line_present(self):
        out = build_digest(JAVAMINI)
        third = out.splitlines()[2]
        self.assertIn("query this file", third)
        self.assertIn("who calls X", third)
        self.assertIn("grep", third)


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
        self.assertIn("evaluate(OrderId,List<ItemId>):Quote !UnknownItem", out)
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
            return render_simple(Path(tmp), syms, [], "regen")

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
            out = render_simple(Path(tmp), syms, [], "regen")
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
                   visibility="pub"),
            Symbol(name="BId", kind="record", file="m/b.py", line=1, params=["String"],
                   visibility="pub"),
            Symbol(name="of", kind="method", file="m/a.py", line=2, container="AId",
                   signature="of(String):AId", visibility="pub"),
            Symbol(name="of", kind="method", file="m/b.py", line=2, container="BId",
                   signature="of(String):BId", visibility="pub"),
            Symbol(name="extra", kind="method", file="m/b.py", line=3, container="BId",
                   signature="extra():int", visibility="pub"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [], "regen")
        self.assertIn("AId,BId(R: String)", out)          # grouped despite extra method
        self.assertEqual(out.count("of(String):⟨X⟩"), 1)  # shared shown once
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
            out = render_simple(Path(tmp), self._syms(), [], "regen")
        self.assertIn("- _evict,_rebalance", out)      # class privates, names only
        self.assertIn("- util.py: _helper", out)       # module-level privates per file
        self.assertNotIn("_evict(int)", out)           # no signatures by default

    def test_private_signatures_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), self._syms(), [], "regen",
                                private_sigs=True)
        self.assertIn("-_evict(int):int", out)
        self.assertIn("-_helper():int", out)
        self.assertNotIn("- util:", out)               # names-only lines replaced

    def test_cli_private_flag(self):
        import shutil
        from hologram import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "svc.py").write_text(
                "class Svc:\n"
                "    def run(self) -> int:\n        return self._step()\n"
                "    def _step(self) -> int:\n        return 1\n"
            )
            out = Path(tmp) / "d.md"
            run_cli(["build", "--root", str(root), "--out", str(out), "--quiet"])
            self.assertIn("- _step", out.read_text())
            run_cli(["build", "--root", str(root), "--out", str(out),
                     "--private", "--quiet"])
            self.assertIn("-_step():int", out.read_text())


@needs_java
class TightFormatTest(unittest.TestCase):
    def test_ascii_return_sep_and_tight_commas(self):
        out = build_digest(JAVAMINI)
        self.assertIn("evaluate(OrderId,List<ItemId>):Quote", out)
        self.assertIn("OrderStatus(E: NEW,PAID,SHIPPED)", out)
        self.assertIn("ItemId,OrderId,UserId(R: String)", out)
        self.assertIn("(I sealed: AddOp|RemoveOp)", out)
        ev = next(ln for ln in out.splitlines() if "evaluate(OrderId" in ln)
        self.assertNotIn("→", ev)   # ascii `:Ret`, not the pretty arrow

    def test_no_fanin_zero_marker(self):
        out = build_digest(JAVAMINI)
        self.assertNotIn("×0", out)


if __name__ == "__main__":
    unittest.main()
