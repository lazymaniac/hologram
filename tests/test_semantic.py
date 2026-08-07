import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import (  # noqa: E402
    apply_packs,
    capability_index,
    extract_file,
    load_packs,
    scan_files,
    type_lineage,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"
PYMINI = FIXTURES / "pymini"


def _symbols(root):
    syms = []
    for f in scan_files(root):
        syms.extend(extract_file(f, root))
    return syms


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.lineage = type_lineage(_symbols(JAVAMINI))

    def test_producers(self):
        quote = self.lineage["Quote"]
        self.assertIn("PricingEngine.evaluate", quote.producers)

    def test_consumers(self):
        order = self.lineage["OrderId"]
        self.assertIn("PricingEngine.evaluate", order.consumers)

    def test_holders_record_components(self):
        order = self.lineage["OrderId"]
        self.assertIn("Quote", order.holders)

    def test_only_project_types(self):
        self.assertNotIn("String", self.lineage)
        self.assertNotIn("long", self.lineage)


class CapabilityTest(unittest.TestCase):
    def test_factory_capability(self):
        caps = capability_index(_symbols(JAVAMINI))
        self.assertIn(("String", "UserId"), caps)
        self.assertIn("UserId.of", caps[("String", "UserId")])

    def test_multi_param_capability(self):
        caps = capability_index(_symbols(JAVAMINI))
        key = ("OrderId, List<ItemId>", "Quote")
        self.assertIn(key, caps)
        self.assertIn("PricingEngine.evaluate", caps[key])


class PackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs = load_packs()
        cls.java_matches = apply_packs(cls.packs, scan_files(JAVAMINI), JAVAMINI)
        cls.py_matches = apply_packs(cls.packs, scan_files(PYMINI), PYMINI)

    def _concept(self, matches, concept):
        return [m for m in matches if m.concept == concept]

    def test_java_test_spec_display_names(self):
        texts = {m.value for m in self._concept(self.java_matches, "test_spec")}
        self.assertIn("orders over ten items get ten percent off", texts)
        self.assertIn("unknown item is rejected", texts)

    def test_python_test_spec_from_names(self):
        texts = {m.value for m in self._concept(self.py_matches, "test_spec")}
        self.assertIn("bulk orders get discount", texts)

    def test_java_invariant_guards(self):
        values = {m.value for m in self._concept(self.java_matches, "invariant")}
        self.assertTrue(any("requireNonNull" in v for v in values))
        self.assertTrue(any("blank id" in v for v in values))

    def test_java_entry_point_main(self):
        entries = self._concept(self.java_matches, "entry_point")
        self.assertTrue(any(m.file.endswith("App.java") for m in entries))

    def test_java_config_key(self):
        cfgs = {m.value for m in self._concept(self.java_matches, "config")}
        self.assertIn("SHOP_CURRENCY", cfgs)

    def test_java_throws_index(self):
        throws = {m.value for m in self._concept(self.java_matches, "error")}
        self.assertIn("UnknownItemException", throws)
        self.assertIn("IllegalArgumentException", throws)


if __name__ == "__main__":
    unittest.main()
