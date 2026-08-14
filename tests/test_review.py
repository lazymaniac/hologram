import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hologram import Symbol, run_cli  # noqa: E402
from hologram.review import (Snapshot, render_report,  # noqa: E402
                             review_snapshots)


def _snap(symbols, file_tokens=None, usage=None):
    return Snapshot(symbols=symbols, file_tokens=file_tokens or {},
                    usage_tokens=Counter(usage or {}))


def _fn(name, file="app/mod.py", calls=(), container=None, vis="pub",
        kind="fn", **kw):
    return Symbol(name=name, kind=kind, file=file, line=1,
                  signature=f"{name}()", visibility=vis, container=container,
                  lang="python", calls=list(calls), **kw)


class DupCheckTest(unittest.TestCase):
    def test_name_similar_non_calling_addition_flagged(self):
        old = _snap([_fn("normalize_amount", file="util/text.py")])
        new = _snap([_fn("normalize_amount", file="util/text.py"),
                     _fn("normalise_amounts", file="billing/money.py")])
        found = review_snapshots(old, new, checks=frozenset({"dup"}))
        self.assertEqual(len(found), 1)
        self.assertIn("normalize_amount", found[0].detail)

    def test_delegation_is_not_duplicate(self):
        old = _snap([_fn("normalize_amount", file="util/text.py")])
        new = _snap([_fn("normalize_amount", file="util/text.py"),
                     _fn("normalise_amounts", file="billing/money.py",
                         calls=["normalize_amount"])])
        self.assertEqual(
            review_snapshots(old, new, checks=frozenset({"dup"})), [])

    def test_short_and_stoplisted_names_skipped(self):
        old = _snap([_fn("get", file="a.py"), _fn("abc", file="a.py")])
        new = _snap([_fn("get", file="a.py"), _fn("abc", file="a.py"),
                     _fn("get", file="b.py"), _fn("abd", file="b.py")])
        self.assertEqual(
            review_snapshots(old, new, checks=frozenset({"dup"})), [])


class RecoverCheckTest(unittest.TestCase):
    def _tri(self, new_test_calls):
        prod = _fn("settle", file="pay/svc.py")
        old_test = _fn("test_a", file="tests/test_pay.py", kind="method",
                       container="SettleTest", calls=["settle"])
        old = _snap([prod, old_test])
        new_syms = [prod, old_test,
                    _fn("test_b", file="tests/test_pay.py", kind="method",
                        container="OtherTest", calls=new_test_calls)]
        return old, _snap(new_syms)

    def test_recovering_different_class_flagged(self):
        old, new = self._tri(["settle"])
        found = review_snapshots(old, new, checks=frozenset({"recover"}))
        self.assertEqual(len(found), 1)
        self.assertIn("SettleTest", found[0].detail)

    def test_same_class_growth_not_flagged(self):
        prod = _fn("settle", file="pay/svc.py")
        old = _snap([prod, _fn("test_a", file="tests/test_pay.py",
                               kind="method", container="SettleTest",
                               calls=["settle"])])
        new = _snap([prod,
                     _fn("test_a", file="tests/test_pay.py", kind="method",
                         container="SettleTest", calls=["settle"]),
                     _fn("test_c", file="tests/test_pay.py", kind="method",
                         container="SettleTest", calls=["settle"])])
        self.assertEqual(
            review_snapshots(old, new, checks=frozenset({"recover"})), [])


class DeadOrphanApiTest(unittest.TestCase):
    def test_dead_on_arrival_flagged(self):
        old = _snap([_fn("existing")])
        newcomer = _fn("brand_new_helper")
        new = _snap([_fn("existing"), newcomer],
                    usage={"existing": 5, "brand_new_helper": 1})
        found = review_snapshots(old, new, checks=frozenset({"dead"}))
        self.assertEqual([f.subject for f in found], ["brand_new_helper"])

    def test_orphaned_test_reference_flagged(self):
        prod = _fn("legacy_path", file="core/old.py")
        cls = _fn("LegacyTest", file="tests/test_old.py", kind="class")
        test = _fn("test_l", file="tests/test_old.py", kind="method",
                   container="LegacyTest", calls=["legacy_path"])
        old = _snap([prod, cls, test])
        new = _snap([cls, test],  # production symbol deleted, test untouched
                    file_tokens={"tests/test_old.py": {"legacy_path"}})
        found = review_snapshots(old, new, checks=frozenset({"orphan"}))
        self.assertEqual(len(found), 1)
        self.assertIn("legacy_path", found[0].detail)

    def test_orphan_suppressed_when_test_updated(self):
        prod = _fn("legacy_path", file="core/old.py")
        cls = _fn("LegacyTest", file="tests/test_old.py", kind="class")
        test = _fn("test_l", file="tests/test_old.py", kind="method",
                   container="LegacyTest", calls=["legacy_path"])
        old = _snap([prod, cls, test])
        new = _snap([cls, test],
                    file_tokens={"tests/test_old.py": {"new_path"}})
        self.assertEqual(
            review_snapshots(old, new, checks=frozenset({"orphan"})), [])

    def test_api_summary(self):
        old = _snap([_fn("kept"), _fn("gone")])
        new = _snap([_fn("kept"), _fn("added_fn")])
        found = review_snapshots(old, new, checks=frozenset({"api"}))
        self.assertEqual(len(found), 1)
        self.assertIn("+1 (added_fn)", found[0].detail)
        self.assertIn("−1 (gone)", found[0].detail)


class PlaceCheckTest(unittest.TestCase):
    def test_strong_affinity_advises_move(self):
        billing = [_fn(f"bill{i}", file="billing/core.py") for i in range(3)]
        newcomer = _fn("late_fee_calc", file="util/misc.py",
                       calls=["bill0", "bill1", "bill2"])
        old = _snap(billing)
        new = _snap(billing + [newcomer])
        found = review_snapshots(old, new, checks=frozenset({"place"}))
        self.assertEqual(len(found), 1)
        self.assertIn("billing", found[0].detail)

    def test_split_mass_stays_silent(self):
        a = [_fn("aa1", file="a/m.py"), _fn("bb1", file="b/m.py")]
        newcomer = _fn("mixed_thing", file="c/m.py", calls=["aa1", "bb1"])
        self.assertEqual(
            review_snapshots(_snap(a), _snap(a + [newcomer]),
                             checks=frozenset({"place"})), [])


class ReportAndCliTest(unittest.TestCase):
    def test_empty_report_is_empty_string(self):
        self.assertEqual(render_report([], "HEAD"), "")

    def test_cli_review_end_to_end(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            (repo / "util.py").write_text(
                "def normalize_amount(v):\n    return int(v)\n")
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-qm", "base"]):
                subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
            (repo / "money.py").write_text(
                "def normalise_amounts(vs):\n    return [int(v) for v in vs]\n")
            subprocess.run(["git", "add", "-A"], cwd=repo,
                           capture_output=True)  # untracked files are unscanned
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_cli(["review", "HEAD", "--root", str(repo)])
            self.assertEqual(code, 0)
            self.assertIn("hologram review vs HEAD", out.getvalue())
            self.assertIn("normalize_amount", out.getvalue())

    def test_history_only_change_never_alters_digest(self):
        from hologram import build_digest
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            (repo / "a.py").write_text("def f():\n    pass\n")
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-qm", "one"]):
                subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
            before = build_digest(repo)
            subprocess.run(["git", "-c", "user.email=t@t", "-c",
                            "user.name=t", "commit", "-qm", "empty",
                            "--allow-empty"], cwd=repo, check=True,
                           capture_output=True)
            self.assertEqual(build_digest(repo), before)


if __name__ == "__main__":
    unittest.main()
