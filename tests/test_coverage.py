import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import build_digest  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


class ModuleCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build_digest(JAVAMINI, budget=8000, mode="full")

    def test_modules_section_first_and_complete(self):
        self.assertIn("## MODULES", self.out)
        modules = self.out.split("## MODULES")[1].split("##")[0]
        self.assertIn("ids", modules)
        self.assertIn("engine", modules)

    def test_package_info_sentence_used(self):
        self.assertIn("Typed identifiers for shop entities", self.out)

    def test_every_public_type_named_somewhere(self):
        for name in ("UserId", "OrderId", "ItemId", "PricingEngine", "Quote",
                     "UnknownItemException", "App"):
            self.assertIn(name, self.out)

    def test_api_section_covers_every_package(self):
        self.assertIn("## API", self.out)
        api = self.out.split("## API")[1].split("\n##")[0]
        self.assertIn("PricingEngine", api)
        self.assertIn("UserId", api)

    def test_modules_survive_small_budget(self):
        small = build_digest(JAVAMINI, budget=1500, mode="full")
        self.assertIn("## MODULES", small)
        self.assertIn("Typed identifiers", small)


if __name__ == "__main__":
    unittest.main()


class GitTrackedScanTest(unittest.TestCase):
    def test_ignored_files_excluded_in_git_repo(self):
        import shutil
        import subprocess
        import tempfile
        from digest import scan_files
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src").mkdir(parents=True)
            (repo / "data").mkdir()
            (repo / "src" / "A.java").write_text("public class A {}\n")
            (repo / "data" / "Vendored.java").write_text("public class Vendored {}\n")
            (repo / ".gitignore").write_text("data/\n")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qm", "init"], cwd=repo, check=True)
            rels = {str(p.relative_to(repo)) for p in scan_files(repo)}
            self.assertEqual(rels, {"src/A.java"})


class TestNoiseSuppressionTest(unittest.TestCase):
    def test_test_classes_absent_from_api_inventory(self):
        out = build_digest(JAVAMINI, budget=8000, mode="full")
        api = out.split("## API")[1].split("\n##")[0]
        self.assertNotIn("PricingEngineTest", api)

    def test_module_lines_dedupe_by_short_name(self):
        from digest import _module_lines, _short_dirs
        docs = {
            "src/main/java/com/x/ids": "Typed ids.",
            "src/test/java/com/x/ids": "· IdsTest",
        }
        short = _short_dirs(sorted(docs))
        lines = _module_lines(docs, short)
        matching = [ln for ln in lines if ln.startswith("com/x/ids/") or ln.startswith("ids/")]
        self.assertEqual(len(matching), 1)
        self.assertIn("Typed ids", matching[0])


class InventoryCompressionTest(unittest.TestCase):
    def test_repeated_names_deduped_and_wrapped(self):
        from digest import Symbol, _api_lines
        symbols = [Symbol(name="main", kind="fn", file=f"tools/s{i}.py", line=1,
                          visibility="pub") for i in range(50)]
        symbols += [Symbol(name=f"helper{i}", kind="fn", file="tools/lib.py", line=i,
                           visibility="pub") for i in range(40)]
        lines = _api_lines(symbols, {}, {"tools": "tools"})
        inventory = [ln for ln in lines if not ln.startswith("  ")]
        self.assertTrue(any("main(F)×50" in ln for ln in inventory))
        self.assertTrue(all(len(ln) <= 170 for ln in inventory))
