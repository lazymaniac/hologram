import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


@unittest.skipUnless(hologram.has_parser("java"), "tree-sitter-java not installed")
class TreeSitterJavaTest(unittest.TestCase):
    def _syms(self, rel):
        text = (JAVAMINI / rel).read_text()
        return hologram._extract_java(text, rel)

    def test_types_methods_params_returns(self):
        syms = self._syms("src/engine/PricingEngine.java")
        t = next(s for s in syms if s.kind == "class")
        self.assertEqual(t.name, "PricingEngine")
        self.assertEqual(t.supers, ["PricePort"])
        self.assertEqual(t.fields, ["basePrices"])
        ev = next(s for s in syms if s.name == "evaluate")
        self.assertEqual(ev.params, ["OrderId", "List<ItemId>"])
        self.assertEqual(ev.param_names, ["order", "items"])
        self.assertEqual(ev.returns, "Quote")
        self.assertEqual(ev.raises, ["UnknownItemException"])
        self.assertEqual(ev.container, "PricingEngine")

    def test_record_components_and_supers(self):
        syms = self._syms("src/transport/Bicycle.java")
        t = next(s for s in syms if s.kind == "record")
        self.assertEqual(t.params, ["String"])
        self.assertEqual(t.fields, ["serial"])
        self.assertEqual(t.supers, ["Vehicle"])

    def test_sealed_permits(self):
        syms = self._syms("src/transport/Vehicle.java")
        t = next(s for s in syms if s.kind == "interface")
        self.assertEqual(t.permits, ["Bicycle", "Scooter"])

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
        self.assertEqual(ctor.param_names, ["basePrices"])


@unittest.skipUnless(hologram.has_parser("java"), "tree-sitter-java not installed")
class MissingParserErrorTest(unittest.TestCase):
    def test_extract_file_errors_without_parser(self):
        saved = hologram._PARSERS["java"]
        hologram._PARSERS["java"] = None
        try:
            with self.assertRaises(SystemExit):
                hologram.extract_file(JAVAMINI / "src/App.java", JAVAMINI)
        finally:
            hologram._PARSERS["java"] = saved


if __name__ == "__main__":
    unittest.main()
