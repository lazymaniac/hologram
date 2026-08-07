import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import (  # noqa: E402
    build_digest,
    estimate_tokens,
    harvest_header,
    parse_git_numstat,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


class TokenEstimateTest(unittest.TestCase):
    def test_four_chars_per_token(self):
        self.assertEqual(estimate_tokens("a" * 400), 100)


class HeaderTest(unittest.TestCase):
    def test_purpose_from_readme(self):
        header = harvest_header(JAVAMINI)
        self.assertIn("Tiny order management demo", header.purpose)

    def test_stack_from_manifest(self):
        header = harvest_header(JAVAMINI)
        self.assertIn("maven", header.stack)


class NumstatParseTest(unittest.TestCase):
    def test_churn_counted_per_file(self):
        out = (
            "abc1\tfix pricing rounding\n"
            "10\t2\tsrc/engine/PricingEngine.java\n"
            "def2\tadd item ids\n"
            "5\t0\tsrc/ids/ItemId.java\n"
            "3\t1\tsrc/engine/PricingEngine.java\n"
        )
        churn, subjects = parse_git_numstat(out)
        self.assertEqual(churn["src/engine/PricingEngine.java"], 2)
        self.assertEqual(churn["src/ids/ItemId.java"], 1)
        self.assertEqual(subjects[0], "fix pricing rounding")


class BuildDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build_digest(JAVAMINI, budget=8000, mode="full")

    def test_all_sections_present(self):
        for section in ("PURPOSE:", "## MODULES", "## API", "## ARCHETYPES", "## TYPE LINEAGE",
                        "## CAPABILITIES", "## INVARIANTS", "## BEHAVIORS", "## INDEXES"):
            self.assertIn(section, self.out)

    def test_archetype_groups_the_three_ids(self):
        self.assertRegex(self.out, r"3×")
        for name in ("UserId", "OrderId", "ItemId"):
            self.assertIn(name, self.out)

    def test_behavior_text_appears(self):
        self.assertIn("orders over ten items get ten percent off", self.out)

    def test_budget_respected(self):
        self.assertLessEqual(estimate_tokens(self.out), 8000)

    def test_small_budget_respected_and_valid(self):
        small = build_digest(JAVAMINI, budget=500, mode="full")
        self.assertLessEqual(estimate_tokens(small), 500)
        self.assertIn("PURPOSE:", small)


if __name__ == "__main__":
    unittest.main()


class PurposeTrimTest(unittest.TestCase):
    def test_long_purpose_trimmed_at_word_boundary(self):
        import tempfile
        from digest import harvest_header
        with tempfile.TemporaryDirectory() as tmp:
            words = " ".join(f"word{i}" for i in range(200))
            (Path(tmp) / "README.md").write_text(f"# T\n\n{words}\n")
            purpose = harvest_header(Path(tmp)).purpose
            self.assertLessEqual(len(purpose), 400)
            self.assertTrue(purpose.endswith("…"))
            self.assertRegex(purpose, r"word\d+ …$")
