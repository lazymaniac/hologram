import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import build_digest, estimate_tokens, extract_file  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"
PYMINI = FIXTURES / "pymini"


class CallExtractionTest(unittest.TestCase):
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


class SimpleDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build_digest(JAVAMINI, budget=8000)

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

    def test_no_budget_enforcement_in_simple_mode(self):
        tight = build_digest(JAVAMINI, budget=260)
        self.assertIn("PricingEngine", tight)
        self.assertIn("> ", tight)

    def test_full_mode_still_available(self):
        full = build_digest(JAVAMINI, budget=8000, mode="full")
        self.assertIn("## MODULES", full)


if __name__ == "__main__":
    unittest.main()


class SameShapeGroupingTest(unittest.TestCase):
    def test_identical_types_grouped_with_hole_notation(self):
        out = build_digest(JAVAMINI, budget=8000)
        self.assertIn("ItemId,OrderId,UserId(R: String)", out)
        self.assertEqual(out.count("of(String):⟨X⟩"), 1)
        self.assertNotIn("of(String):UserId", out)


class TreeAndCallFormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build_digest(JAVAMINI, budget=8000)

    def test_calls_inline_after_signature_no_calls_word(self):
        body = "\n".join(self.out.splitlines()[2:])
        self.assertNotIn("calls ", body)
        lines = self.out.splitlines()
        ev = next(ln for ln in lines if "evaluate(OrderId" in ln)
        self.assertIn("> ", ev)
        self.assertIn("UnknownItemException", ev)

    def test_platform_and_ubiquitous_calls_filtered(self):
        from digest import Symbol, render_simple
        import tempfile
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
        self.assertNotIn("requireNonNull", out)   # not project-defined -> platform noise
        self.assertNotIn("helperCommon", out)     # project-defined but ubiquitous -> dropped
        self.assertIn("helper3", out)             # project-defined and rare -> kept

    def test_tree_shares_prefixes_once(self):
        from digest import _tree_lines
        types_by_dir = {"com/x/a": ["A(C)"], "com/x/b": ["B(C)"]}
        lines = _tree_lines(types_by_dir)
        joined = "\n".join(lines)
        self.assertEqual(joined.count("com/x"), 1)
        self.assertNotIn("com/x/a", joined)


class EnumValuesTest(unittest.TestCase):
    def test_java_enum_constants_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/OrderStatus.java", JAVAMINI)
        e = next(s for s in syms if s.kind == "enum")
        self.assertEqual(e.params, ["NEW", "PAID", "SHIPPED"])

    def test_enum_values_rendered(self):
        out = build_digest(JAVAMINI, budget=8000)
        self.assertIn("OrderStatus(E: NEW,PAID,SHIPPED)", out)

    def test_python_enum_values_extracted(self):
        import tempfile
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
        out = build_digest(JAVAMINI, budget=8000)
        idx = out.index("PricePort(I)")
        after = out[idx:idx + 200]
        self.assertIn("quoteFor(OrderId):Quote", after)


class QualifiedCallTest(unittest.TestCase):
    def test_receiver_kept_for_qualified_calls(self):
        syms = extract_file(JAVAMINI / "src/App.java", JAVAMINI)
        main = next(s for s in syms if s.name == "main")
        self.assertIn("engine.evaluate", main.calls)

    def test_render_shows_qualified_project_call(self):
        out = build_digest(JAVAMINI, budget=8000)
        main_line = next(ln for ln in out.splitlines() if "main(String[])" in ln)
        self.assertIn("engine.evaluate", main_line)


class CtorComponentsTest(unittest.TestCase):
    def test_class_constructor_params_shown_as_components(self):
        out = build_digest(JAVAMINI, budget=8000)
        self.assertIn("PricingEngine(C: Map<ItemId,Long>)", out)


class ReconstructablePathTest(unittest.TestCase):
    def test_tree_labels_keep_real_path_segments(self):
        out = build_digest(JAVAMINI, budget=8000)
        lines = out.splitlines()
        self.assertIn("src", lines)
        self.assertTrue(any(ln.strip() == "engine" for ln in lines))


class LanguageFilterTest(unittest.TestCase):
    def test_only_requested_language_included(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mixed"
            shutil.copytree(JAVAMINI, root)
            (root / "script.py").write_text("def py_helper() -> int:\n    return 1\n")
            all_langs = build_digest(root, budget=8000)
            self.assertIn("py_helper", all_langs)
            java_only = build_digest(root, budget=8000, langs={"java"})
            self.assertNotIn("py_helper", java_only)
            self.assertIn("PricingEngine", java_only)

    def test_cli_lang_flag(self):
        import tempfile
        from digest import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "d.md"
            code = run_cli(["build", "--root", str(JAVAMINI), "--out", str(out),
                            "--lang", "java", "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("PricingEngine", out.read_text())


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
        out = build_digest(JAVAMINI, budget=8000)
        self.assertIn("PricingEngine(C: Map<ItemId,Long>) : PricePort", out)
        self.assertIn("DeltaOp(I sealed: AddOp|RemoveOp)", out)
        self.assertIn("(R: String) : DeltaOp", out)


class LegendTest(unittest.TestCase):
    def test_legend_line_present(self):
        out = build_digest(JAVAMINI, budget=8000)
        second = out.splitlines()[1]
        self.assertIn("legend", second)
        self.assertIn("⟨X⟩", second)
        self.assertIn("> calls", second)


class ThrowsTest(unittest.TestCase):
    def test_java_throws_clause_extracted(self):
        syms = extract_file(JAVAMINI / "src/engine/PricingEngine.java", JAVAMINI)
        ev = next(s for s in syms if s.name == "evaluate")
        self.assertEqual(ev.raises, ["UnknownItemException"])

    def test_python_raise_types_extracted(self):
        syms = extract_file(PYMINI / "models.py", PYMINI)
        check = next(s for s in syms if s.name == "check" and s.container == "UserId")
        self.assertEqual(check.raises, ["ValueError"])

    def test_throws_rendered_on_signature(self):
        out = build_digest(JAVAMINI, budget=8000)
        self.assertIn("evaluate(OrderId,List<ItemId>):Quote !UnknownItemException", out)


class FanInMarkerTest(unittest.TestCase):
    def test_hot_types_marked(self):
        from digest import Symbol, render_simple
        import tempfile
        syms = [Symbol(name="Widget", kind="class", file="a/w.py", line=1, visibility="pub"),
                Symbol(name="Rare", kind="class", file="b/r.py", line=1, visibility="pub")]
        with tempfile.TemporaryDirectory() as tmp:
            out = render_simple(Path(tmp), syms, [], "regen",
                                scores={"Widget": 12.0, "Rare": 2.0})
        widget_line = next(ln for ln in out.splitlines() if "Widget" in ln)
        rare_line = next(ln for ln in out.splitlines() if "Rare(" in ln)
        self.assertIn("×12", widget_line)
        self.assertNotIn("×", rare_line)

    def test_private-corpus_style_marker_via_build(self):
        out = build_digest(JAVAMINI, budget=8000)
        self.assertNotIn("×0", out)


class TightFormatTest(unittest.TestCase):
    def test_ascii_return_sep_and_tight_commas(self):
        out = build_digest(JAVAMINI, budget=8000)
        self.assertIn("evaluate(OrderId,List<ItemId>):Quote", out)
        self.assertIn("OrderStatus(E: NEW,PAID,SHIPPED)", out)
        self.assertIn("ItemId,OrderId,UserId(R: String)", out)
        self.assertIn("(I sealed: AddOp|RemoveOp)", out)
        self.assertNotIn("→", out)
