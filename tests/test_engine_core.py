import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import (  # noqa: E402
    Symbol,
    cluster_skeletons,
    detect_language,
    extract_file,
    fan_in_scores,
    scan_files,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


class ScanTest(unittest.TestCase):
    def test_finds_source_files(self):
        files = scan_files(JAVAMINI)
        names = {p.name for p in files}
        self.assertIn("UserId.java", names)
        self.assertIn("PricingEngine.java", names)

    def test_denylist_excludes_build_dirs(self):
        files = scan_files(JAVAMINI)
        names = {p.name for p in files}
        self.assertNotIn("Generated.java", names)

    def test_language_detection(self):
        self.assertEqual(detect_language(Path("A.java")), "java")
        self.assertEqual(detect_language(Path("a.py")), "python")
        self.assertEqual(detect_language(Path("a.ts")), "typescript")
        self.assertEqual(detect_language(Path("a.go")), "go")
        self.assertIsNone(detect_language(Path("a.unknownext")))


class JavaExtractTest(unittest.TestCase):
    def setUp(self):
        self.symbols = extract_file(JAVAMINI / "src/engine/PricingEngine.java", JAVAMINI)

    def test_finds_type_declaration(self):
        types = [s for s in self.symbols if s.kind == "class"]
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0].name, "PricingEngine")
        self.assertEqual(types[0].visibility, "pub")

    def test_finds_method_with_signature(self):
        methods = {s.name: s for s in self.symbols if s.kind == "method"}
        self.assertIn("evaluate", methods)
        ev = methods["evaluate"]
        self.assertEqual(ev.returns, "Quote")
        self.assertEqual(ev.params, ["OrderId", "List<ItemId>"])
        self.assertEqual(ev.container, "PricingEngine")

    def test_record_components_parsed(self):
        syms = extract_file(JAVAMINI / "src/ids/UserId.java", JAVAMINI)
        rec = next(s for s in syms if s.kind == "record")
        self.assertEqual(rec.name, "UserId")
        self.assertEqual(rec.params, ["String"])


class SkeletonTest(unittest.TestCase):
    def _type_skeleton(self, rel):
        syms = extract_file(JAVAMINI / rel, JAVAMINI)
        return next(s for s in syms if s.kind in ("record", "class")).skeleton_hash

    def test_same_shape_same_hash(self):
        self.assertEqual(
            self._type_skeleton("src/ids/UserId.java"),
            self._type_skeleton("src/ids/OrderId.java"),
        )

    def test_different_shape_different_hash(self):
        self.assertNotEqual(
            self._type_skeleton("src/ids/UserId.java"),
            self._type_skeleton("src/engine/PricingEngine.java"),
        )


class ClusterTest(unittest.TestCase):
    def test_three_same_shapes_form_archetype_rest_outliers(self):
        symbols = []
        for f in scan_files(JAVAMINI):
            symbols.extend(extract_file(f, JAVAMINI))
        top = [s for s in symbols if s.kind in ("class", "record", "interface", "enum")]
        archetypes, outliers = cluster_skeletons(top)
        self.assertEqual(len(archetypes), 1)
        members = {m.name for m in archetypes[0].members}
        self.assertEqual(members, {"UserId", "OrderId", "ItemId"})
        outlier_names = {s.name for s in outliers}
        self.assertIn("PricingEngine", outlier_names)

    def test_fewer_than_three_stay_outliers(self):
        a = Symbol(name="A", kind="class", file="a", line=1, skeleton_hash="h1")
        b = Symbol(name="B", kind="class", file="b", line=1, skeleton_hash="h1")
        archetypes, outliers = cluster_skeletons([a, b])
        self.assertEqual(archetypes, [])
        self.assertEqual({s.name for s in outliers}, {"A", "B"})


class FanInTest(unittest.TestCase):
    def test_cross_file_references_counted(self):
        files = scan_files(JAVAMINI)
        symbols = []
        for f in files:
            symbols.extend(extract_file(f, JAVAMINI))
        scores = fan_in_scores(symbols, files, JAVAMINI)
        # ItemId referenced from PricingEngine + UnknownItemException, OrderId from PricingEngine + Quote
        self.assertGreater(scores["ItemId"], 0)
        self.assertGreater(scores["OrderId"], 0)
        # Quote referenced only from PricingEngine; UnknownItemException only from PricingEngine
        self.assertGreaterEqual(scores["ItemId"], scores["Quote"])


if __name__ == "__main__":
    unittest.main()
