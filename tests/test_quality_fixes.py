import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import (  # noqa: E402
    Match,
    Symbol,
    _behavior_lines,
    _fan_in_from_tokens,
    _invariant_lines,
    scan_files,
)


class ScanNoiseTest(unittest.TestCase):
    def _mk(self, tmp, rel):
        p = Path(tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("class A {}\n")

    def test_dot_dirs_and_fixture_dirs_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._mk(tmp, "src/Main.java")
            self._mk(tmp, ".worktrees/other/src/Main.java")
            self._mk(tmp, "tests/fixtures/Fake.java")
            self._mk(tmp, "src/test/resources/quality/Filler1.java")
            self._mk(tmp, "src/testdata/Sample.java")
            rels = {str(p.relative_to(tmp)) for p in scan_files(Path(tmp))}
            self.assertEqual(rels, {"src/Main.java"})


class FanInNormalizationTest(unittest.TestCase):
    def test_name_defined_in_many_files_scores_low(self):
        symbols = [
            Symbol(name="main", kind="fn", file=f"script{i}.py", line=1)
            for i in range(10)
        ] + [Symbol(name="Ledger", kind="class", file="ledger.py", line=1)]
        tokens = {f"script{i}.py": {"main", "Ledger"} for i in range(10)}
        tokens["ledger.py"] = {"Ledger"}
        scores = _fan_in_from_tokens(symbols, tokens)
        # main: 0 external refs per defining file (each ref file also defines it)
        # Ledger: referenced from 10 other files, defined once
        self.assertGreater(scores["Ledger"], scores["main"])
        self.assertGreaterEqual(scores["Ledger"], 10)


class InvariantGroupingTest(unittest.TestCase):
    def test_requirenonnull_grouped_per_type(self):
        matches = [
            Match("invariant", "requireNonNull(principalId)", "a/EvidenceViewContext.java", 3),
            Match("invariant", "requireNonNull(viewRevision)", "a/EvidenceViewContext.java", 4),
            Match("invariant", "requireNonNull(value)", "b/AggregateId.java", 2),
        ]
        lines = _invariant_lines(matches)
        joined = "\n".join(lines)
        self.assertIn("EvidenceViewContext: non-null principalId,viewRevision", joined)
        self.assertNotIn("requireNonNull(principalId)", joined)

    def test_multiline_values_collapse_whitespace(self):
        matches = [Match("invariant", "A,\n                B", "a/CanonicalId.java", 1)]
        lines = _invariant_lines(matches)
        self.assertIn("A, B", "\n".join(lines))


class BehaviorModuleLabelTest(unittest.TestCase):
    def test_module_label_is_last_two_dirs(self):
        matches = [
            Match("test_spec", "ledger refuses gaps",
                  "src/test/java/com/private-corpus/kernel/evidence/LedgerTest.java", 1),
        ]
        lines = _behavior_lines(matches)
        self.assertEqual(lines, ["kernel/evidence: ledger refuses gaps"])


if __name__ == "__main__":
    unittest.main()


class DocSentenceTest(unittest.TestCase):
    def test_trailing_brace_stripped(self):
        from digest import _first_doc_sentence
        text = "/** Parses sources into one {@link Model}. More text. */\npublic class X {"
        doc = _first_doc_sentence(text, text.index("public"))
        self.assertFalse(doc.rstrip().endswith("{"))
        self.assertIn("Parses sources", doc)


class ArchetypeMemberTruncationTest(unittest.TestCase):
    def test_member_list_capped(self):
        from digest import Archetype, _archetype_lines
        members = [Symbol(name=f"T{i}", kind="record", file=f"m/T{i}.java", line=1,
                          signature=f"record T{i}(String v)", skeleton_hash="h")
                   for i in range(100)]
        lines = _archetype_lines([Archetype("h", members)], members)
        member_line = lines[1]
        self.assertLess(len(member_line), 300)
        self.assertIn("+88 more", member_line)


class BehaviorWrappingTest(unittest.TestCase):
    def test_long_behavior_lists_wrap_into_short_lines(self):
        matches = [Match("test_spec", f"behavior number {i} does something specific", "m/x/T.java", i)
                   for i in range(40)]
        lines = _behavior_lines(matches)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(ln) <= 160 for ln in lines))
