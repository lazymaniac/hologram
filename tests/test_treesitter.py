import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import tree_sitter  # noqa: F401
    HAS_TS = True
except ImportError:
    HAS_TS = False

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


@unittest.skipUnless(HAS_TS, "tree-sitter not installed (regex fallback covered elsewhere)")
class TreeSitterJavaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from digest import _extract_java_treesitter
        cls.extract = staticmethod(_extract_java_treesitter)

    def _syms(self, rel):
        text = (JAVAMINI / rel).read_text()
        return self.extract(text, rel)

    def test_dispatch_prefers_treesitter(self):
        import digest
        self.assertTrue(digest.USING_TREESITTER)

    def test_types_methods_params_returns(self):
        syms = self._syms("src/engine/PricingEngine.java")
        t = next(s for s in syms if s.kind == "class")
        self.assertEqual(t.name, "PricingEngine")
        self.assertEqual(t.supers, ["PricePort"])
        ev = next(s for s in syms if s.name == "evaluate")
        self.assertEqual(ev.params, ["OrderId", "List<ItemId>"])
        self.assertEqual(ev.returns, "Quote")
        self.assertEqual(ev.raises, ["UnknownItemException"])
        self.assertEqual(ev.container, "PricingEngine")

    def test_record_components_and_supers(self):
        syms = self._syms("src/delta/AddOp.java")
        t = next(s for s in syms if s.kind == "record")
        self.assertEqual(t.params, ["String"])
        self.assertEqual(t.supers, ["DeltaOp"])

    def test_sealed_permits(self):
        syms = self._syms("src/delta/DeltaOp.java")
        t = next(s for s in syms if s.kind == "interface")
        self.assertEqual(t.permits, ["AddOp", "RemoveOp"])

    def test_interface_bodyless_methods(self):
        syms = self._syms("src/engine/PricePort.java")
        methods = {s.name: s for s in syms if s.kind == "method"}
        self.assertEqual(methods["quoteFor"].returns, "Quote")
        self.assertEqual(methods["supports"].returns, "boolean")

    def test_enum_constants(self):
        syms = self._syms("src/engine/OrderStatus.java")
        e = next(s for s in syms if s.kind == "enum")
        self.assertEqual(e.params, ["NEW", "PAID", "SHIPPED"])
        self.assertIn("isTerminal", {s.name for s in syms})

    def test_calls_with_receivers(self):
        syms = self._syms("src/App.java")
        main = next(s for s in syms if s.name == "main")
        self.assertIn("engine.evaluate", main.calls)
        self.assertIn("OrderId.of", main.calls)
        self.assertIn("PricingEngine", main.calls)

    def test_ctor_extracted(self):
        syms = self._syms("src/engine/PricingEngine.java")
        ctor = next(s for s in syms if s.kind == "ctor")
        self.assertEqual(ctor.params, ["Map<ItemId,Long>"])

    def test_skeletons_group_identical_ids(self):
        from digest import cluster_skeletons
        types = []
        for rel in ("src/ids/UserId.java", "src/ids/OrderId.java", "src/ids/ItemId.java"):
            types.extend(s for s in self._syms(rel) if s.kind == "record")
        archetypes, outliers = cluster_skeletons(types)
        self.assertEqual(len(archetypes), 1)
        self.assertEqual(outliers, [])


if __name__ == "__main__":
    unittest.main()
