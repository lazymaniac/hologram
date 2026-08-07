import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import extract_file  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PYMINI = FIXTURES / "pymini"
TSMINI = FIXTURES / "tsmini"

needs_ts = unittest.skipUnless(hologram.has_parser("typescript"),
                               "tree-sitter-typescript not installed")


class PythonExtractTest(unittest.TestCase):
    def test_classes_and_methods(self):
        syms = extract_file(PYMINI / "models.py", PYMINI)
        classes = {s.name for s in syms if s.kind == "class"}
        self.assertEqual(classes, {"UserId", "OrderId", "ItemId"})
        methods = [s for s in syms if s.kind == "method" and s.container == "UserId"]
        self.assertEqual([m.name for m in methods], ["check"])

    def test_function_signature_from_annotations(self):
        syms = extract_file(PYMINI / "app.py", PYMINI)
        fns = {s.name: s for s in syms if s.kind == "fn"}
        self.assertIn("price_order", fns)
        self.assertEqual(fns["price_order"].params, ["OrderId", "list[ItemId]"])
        self.assertEqual(fns["price_order"].returns, "int")


@needs_ts
class TypeScriptExtractTest(unittest.TestCase):
    def test_interface_class_function(self):
        syms = extract_file(TSMINI / "api.ts", TSMINI)
        by_kind = {}
        for s in syms:
            by_kind.setdefault(s.kind, set()).add(s.name)
        self.assertIn("Quote", by_kind.get("interface", set()))
        self.assertIn("PricingClient", by_kind.get("class", set()))
        self.assertIn("formatCents", by_kind.get("fn", set()))

    def test_method_and_returns(self):
        syms = extract_file(TSMINI / "api.ts", TSMINI)
        m = next(s for s in syms if s.name == "fetchQuote")
        self.assertEqual(m.kind, "method")
        self.assertEqual(m.container, "PricingClient")
        self.assertEqual(m.returns, "Promise<Quote>")

    def test_exported_symbols_public(self):
        syms = extract_file(TSMINI / "api.ts", TSMINI)
        fn = next(s for s in syms if s.name == "formatCents")
        self.assertEqual(fn.visibility, "pub")


@needs_ts
class ArrowFunctionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(TSMINI / "arrows.ts", TSMINI)

    def test_exported_arrow_is_public_fn(self):
        fn = next(s for s in self.syms if s.name == "fetchUser")
        self.assertEqual(fn.kind, "fn")
        self.assertEqual(fn.visibility, "pub")
        self.assertEqual(fn.params, ["string"])
        self.assertEqual(fn.returns, "Promise<string>")
        self.assertIn("lookup", fn.calls)

    def test_unexported_arrow_is_private(self):
        fn = next(s for s in self.syms if s.name == "lookup")
        self.assertEqual(fn.visibility, "priv")
        self.assertEqual(fn.returns, "string")

    def test_const_non_function_not_extracted(self):
        self.assertNotIn("cache", {s.name for s in self.syms})

    def test_class_field_arrow_is_method(self):
        m = next(s for s in self.syms if s.name == "onEvent")
        self.assertEqual(m.kind, "method")
        self.assertEqual(m.container, "EventHub")
        self.assertEqual(m.visibility, "pub")
        self.assertIn("dispatch", m.calls)

    def test_nested_closures_not_top_level(self):
        # the arrow body of fetchUser contains no declarators, but guard anyway:
        # every extracted fn must be fetchUser/lookup or an EventHub member
        names = {s.name for s in self.syms if s.kind == "fn"}
        self.assertEqual(names, {"fetchUser", "lookup"})


if __name__ == "__main__":
    unittest.main()
