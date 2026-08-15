import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hologram import Symbol, run_cli  # noqa: E402
from hologram.review import (Finding, Snapshot,  # noqa: E402
                             render_report, report_data,
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
        self.assertEqual(found[0].kind, "fn")
        self.assertEqual(found[0].path, "billing/money.py")

    def test_id_is_stable_when_only_rendered_pointer_changes(self):
        old = _snap([_fn("normalize_amount", file="util/text.py")])
        new = _snap([_fn("normalize_amount", file="util/text.py"),
                     _fn("normalise_amounts", file="billing/money.py")])
        described = review_snapshots(
            old, new, checks=frozenset({"dup"}))[0]
        mapped = review_snapshots(
            old, new, "normalize_amount(value)",
            checks=frozenset({"dup"}))[0]

        self.assertNotEqual(described.detail, mapped.detail)
        self.assertEqual(described.id, mapped.id)
        self.assertRegex(described.id, r"^hr1-[0-9a-f]{20}$")

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

    def test_dead_id_survives_a_signature_only_attempt(self):
        old = _snap([_fn("existing")])
        before = _snap(
            [_fn("existing"), _fn("brand_new_helper", params=["value"])],
            usage={"existing": 5, "brand_new_helper": 1})
        after = _snap(
            [_fn("existing"), _fn("brand_new_helper", params=["item"])],
            usage={"existing": 5, "brand_new_helper": 1})

        first = review_snapshots(old, before, checks=frozenset({"dead"}))[0]
        second = review_snapshots(old, after, checks=frozenset({"dead"}))[0]
        self.assertNotEqual(first.detail, second.detail)
        self.assertEqual(first.id, second.id)

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

    def test_dead_suppressed_when_a_test_file_mentions_the_name(self):
        # framework-indirect tests (HTTP drivers, reflection) leave no static
        # call edge but do mention the symbol — that's exercised, not dead
        old = _snap([_fn("existing")])
        newcomer = _fn("brand_new_helper")
        new = _snap([_fn("existing"), newcomer],
                    file_tokens={"tests/test_x.py": {"brand_new_helper"}},
                    usage={"existing": 5, "brand_new_helper": 1})
        self.assertEqual(
            review_snapshots(old, new, checks=frozenset({"dead"})), [])

    def test_api_summary(self):
        old = _snap([_fn("kept"), _fn("gone")])
        new = _snap([_fn("kept"), _fn("added_fn")])
        found = review_snapshots(old, new, checks=frozenset({"api"}))
        self.assertEqual(len(found), 1)
        self.assertIn("+1 (added_fn)", found[0].detail)
        self.assertIn("−1 (gone)", found[0].detail)

        # A different surface delta gets a different identity.  The benchmark
        # records the old finding as resolved and the replacement as new-final
        # instead of falsely claiming that an unrelated drift persisted.
        partial = review_snapshots(
            old, _snap([_fn("kept"), _fn("gone"), _fn("other_addition")]),
            checks=frozenset({"api"}))
        self.assertNotEqual(found[0].detail, partial[0].detail)
        self.assertNotEqual(found[0].id, partial[0].id)

        alpha = review_snapshots(
            _snap([]), _snap([_fn("Alpha")]),
            checks=frozenset({"api"}))
        beta = review_snapshots(
            _snap([]), _snap([_fn("Beta")]),
            checks=frozenset({"api"}))
        self.assertNotEqual(alpha[0].id, beta[0].id)

        return_change = review_snapshots(
            _snap([_fn("fetch", params=["key"], returns="Old")]),
            _snap([_fn("fetch", params=["key"], returns="New")]),
            checks=frozenset({"api"}))
        self.assertIn("fetch: (key):Old→(key):New",
                      return_change[0].detail)

    def test_api_detects_public_type_shape_and_kind_changes(self):
        old_type = Symbol(
            name="Envelope", kind="class", file="model.py", line=1,
            visibility="pub", lang="python", fields=["key"],
            supers=["BaseEnvelope"], permits=["TextEnvelope"],
            decorators=["Scheduled"])
        new_type = Symbol(
            name="Envelope", kind="record", file="model.py", line=20,
            visibility="pub", lang="python", fields=["key", "payload"],
            supers=["VersionedEnvelope"], permits=["BinaryEnvelope"],
            decorators=["ApiController"])

        found = review_snapshots(
            _snap([old_type]), _snap([new_type]),
            checks=frozenset({"api"}))

        self.assertEqual(len(found), 1)
        self.assertIn("Envelope: class→record", found[0].detail)
        self.assertIn("fields [key]→[key,payload]", found[0].detail)
        self.assertIn("supers [BaseEnvelope]→[VersionedEnvelope]",
                      found[0].detail)
        self.assertIn("permits [TextEnvelope]→[BinaryEnvelope]",
                      found[0].detail)
        self.assertIn("decorators [@Scheduled]→[@ApiController]",
                      found[0].detail)

        repeated = review_snapshots(
            _snap([old_type]), _snap([new_type]),
            checks=frozenset({"api"}))[0]
        another_change = review_snapshots(
            _snap([old_type]),
            _snap([Symbol(
                name="Envelope", kind="record", file="model.py", line=20,
                visibility="pub", lang="python", fields=["key", "body"],
                supers=["VersionedEnvelope"], permits=["BinaryEnvelope"],
                decorators=["ApiController"])]),
            checks=frozenset({"api"}))[0]
        self.assertEqual(found[0].id, repeated.id)
        self.assertNotEqual(found[0].id, another_change.id)

    def test_api_detects_mapped_routes_raises_ctors_and_constants(self):
        old_route = _fn(
            "load", params=["str"], param_names=["key"], returns="Result",
            decorators=['router.get("/items")'], raises=["MissingException"])
        new_route = _fn(
            "load", params=["str"], param_names=["item_key"], returns="Result",
            decorators=['router.post("/items")'], raises=["InvalidException"])
        route = review_snapshots(
            _snap([old_route]), _snap([new_route]),
            checks=frozenset({"api"}))[0]
        self.assertIn("args [key]→[item_key]", route.detail)
        self.assertIn("decorators [@GET/items]→[@POST/items]", route.detail)
        self.assertIn("raises [Missing]→[Invalid]", route.detail)

        old_ctor = _fn("Envelope", kind="ctor", container="Envelope",
                       params=["str"], returns="Envelope")
        new_ctor = _fn("Envelope", kind="ctor", container="Envelope",
                       params=["bytes"], returns="Envelope")
        ctor = review_snapshots(
            _snap([old_ctor]), _snap([new_ctor]),
            checks=frozenset({"api"}))[0]
        self.assertIn("Envelope: (str)→(bytes)", ctor.detail)

        old_const = Symbol(
            name="MAX_BATCH", kind="const", file="settings.py", line=1,
            signature="MAX_BATCH=8", visibility="pub", lang="python")
        new_const = Symbol(
            name="MAX_BATCH", kind="const", file="settings.py", line=1,
            signature="MAX_BATCH=16", visibility="pub", lang="python")
        const = review_snapshots(
            _snap([old_const]), _snap([new_const]),
            checks=frozenset({"api"}))[0]
        self.assertIn("value MAX_BATCH=8→MAX_BATCH=16", const.detail)

    def test_unmapped_decorator_does_not_create_api_drift(self):
        old = _snap([_fn("load", decorators=["ImplementationNote"])])
        new = _snap([_fn("load", decorators=["OtherInternalNote"])])
        self.assertEqual(
            review_snapshots(old, new, checks=frozenset({"api"})), [])

    def test_api_preserves_every_same_name_overload(self):
        old_int = _fn("convert", params=["int"])
        old_text = _fn("convert", params=["str"])
        new_flag = _fn("convert", params=["bool"])
        unchanged_text = _fn("convert", params=["str"])

        found = review_snapshots(
            _snap([old_int, old_text]), _snap([new_flag, unchanged_text]),
            checks=frozenset({"api"}))

        self.assertEqual(len(found), 1)
        self.assertIn("convert: variants", found[0].detail)
        self.assertIn("fn (int)", found[0].detail)
        self.assertIn("fn (bool)", found[0].detail)
        # Declaration order is not API drift and must not perturb the ID.
        self.assertEqual(
            review_snapshots(
                _snap([old_int, old_text]), _snap([old_text, old_int]),
                checks=frozenset({"api"})), [])

    def test_makefile_under_tests_is_reviewed_as_production_api(self):
        owner = Symbol(name="Makefile", kind="class", file="tests/Makefile",
                       line=1, visibility="pub", lang="make")
        old_target = Symbol(name="old", kind="method", file="tests/Makefile",
                            line=2, container="Makefile", visibility="pub",
                            lang="make")
        new_target = Symbol(name="new", kind="method", file="tests/Makefile",
                            line=2, container="Makefile", visibility="pub",
                            lang="make")

        found = review_snapshots(
            _snap([owner, old_target]), _snap([owner, new_target]),
            checks=frozenset({"api"}))

        self.assertEqual(len(found), 1)
        self.assertIn("+1 (new)", found[0].detail)
        self.assertIn("−1 (old)", found[0].detail)


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

    def test_human_report_format_does_not_expose_structured_metadata(self):
        finding = Finding("dead", "new_helper", "dead: original message",
                          kind="fn", path="src/helper.py")
        self.assertEqual(
            render_report([finding], "HEAD~1"),
            "hologram review vs HEAD~1: 1 finding(s)\n"
            "- dead: original message\n")

    def test_structured_report_is_sorted_and_json_serializable(self):
        dup = Finding("dup", "similar_name", "dup: detail", kind="fn",
                      path="src/z.py")
        dead = Finding("dead", "unused_name", "dead: detail", kind="fn",
                       path="src/a.py")

        forward = report_data([dup, dead], "HEAD")
        reverse = report_data([dead, dup], "HEAD")

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["schema_version"], 1)
        self.assertEqual(forward["count"], 2)
        self.assertEqual(
            [item["check"] for item in forward["findings"]],
            ["dead", "dup"])
        self.assertEqual(
            set(forward["findings"][0]),
            {"id", "check", "kind", "subject", "path", "detail"})
        json.dumps(forward, sort_keys=True)

    def test_finding_id_ignores_wording_and_normalizes_paths(self):
        first = Finding("dead", "helper", "old wording", kind="fn",
                        path=r"src\helper.py")
        second = Finding("dead", "helper", "new wording", kind="fn",
                         path="src/helper.py")
        elsewhere = Finding("dead", "helper", "new wording", kind="fn",
                            path="other/helper.py")

        self.assertEqual(first.path, "src/helper.py")
        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.id, elsewhere.id)

    def test_wording_edits_preserve_baseline_versus_final_identity(self):
        # The identity contract the benchmark's final-state measurement relies
        # on: a re-worded finding is the same finding, a removed one is gone.
        seen = Finding("dead", "seen", "baseline seen", kind="fn",
                       path="src/seen.py")
        resolved = Finding("orphan", "resolved", "baseline resolved",
                           kind="test-reference", path="tests/test_old.py")
        still_seen = Finding("dead", "seen", "later wording", kind="fn",
                             path="src/seen.py")

        baseline = {finding.id for finding in (seen, resolved)}
        final = {finding.id for finding in (still_seen,)}

        self.assertEqual(baseline & final, {seen.id})
        self.assertEqual(baseline - final, {resolved.id})

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

    def test_cli_review_json_is_machine_readable_even_when_clean(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            (repo / "util.py").write_text("def normalize(v):\n    return v\n")
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-qm", "base"]):
                subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_cli(["review", "HEAD", "--root", str(repo),
                                "--json", "--quiet-if-clean"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["revision"], "HEAD")
            self.assertEqual(payload["count"], 0)
            self.assertEqual(payload["findings"], [])

    def test_review_survives_git_hook_environment(self):
        # git exports GIT_DIR / GIT_INDEX_FILE while running hooks; review
        # spawns git against other directories and must scrub them
        import contextlib
        import io
        import os
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
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
            saved = {k: os.environ.get(k) for k in ("GIT_DIR",
                                                    "GIT_INDEX_FILE")}
            os.environ["GIT_DIR"] = ".git"
            os.environ["GIT_INDEX_FILE"] = ".git/index.lock"  # pre-commit shape
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = run_cli(["review", "HEAD", "--root", str(repo)])
                self.assertEqual(code, 0)
                self.assertIn("normalize_amount", out.getvalue())
                self.assertEqual(os.environ["GIT_DIR"], ".git")
                self.assertEqual(os.environ["GIT_INDEX_FILE"],
                                 ".git/index.lock")
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

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
