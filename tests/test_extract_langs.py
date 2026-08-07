import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import cluster_skeletons, extract_file  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PYMINI = FIXTURES / "pymini"
TSMINI = FIXTURES / "tsmini"


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

    def test_three_dataclasses_cluster(self):
        syms = [s for s in extract_file(PYMINI / "models.py", PYMINI) if s.kind == "class"]
        archetypes, outliers = cluster_skeletons(syms)
        self.assertEqual(len(archetypes), 1)
        self.assertEqual(len(archetypes[0].members), 3)
        self.assertEqual(outliers, [])


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


if __name__ == "__main__":
    unittest.main()
