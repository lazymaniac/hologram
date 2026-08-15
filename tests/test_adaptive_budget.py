import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from hologram.gather import _gather, _zero_usage_names
from hologram.render import (
    BudgetBundle,
    _helper_class_ids,
    _resolved_project_calls,
    _total_loc,
    build_digest_with_stats,
    estimate_tokens,
    render_simple,
    summarize_budget,
)
from hologram.symbols import Symbol


def _level_tokens(root: Path, detail: int, budget: int) -> int:
    files, symbols, file_tokens, usage_tokens, state = _gather(root, None)
    digest = render_simple(
        root, symbols, files, state=state,
        zero_usage=_zero_usage_names(symbols, usage_tokens),
        file_tokens=file_tokens, detail=detail, budget=budget,
        loc=_total_loc(files), resolved=_resolved_project_calls(symbols),
        helpers=_helper_class_ids(symbols, file_tokens),
    )
    return estimate_tokens(digest)


def _budget_above_level(root: Path, detail: int, slack: int) -> int:
    """Choose a stable budget despite the budget stamp's digit width."""
    budget = 1_000
    for _ in range(4):
        budget = _level_tokens(root, detail, budget) + slack
    return budget


def _write_twin_clients(root: Path, *, route: bool = True) -> None:
    """Synthetic ownership stress case; no external project lineage."""
    for file, prefix in (("one.py", "alpha"), ("two.py", "beta")):
        lines = ["class Client:"]
        if route and file == "one.py":
            lines.extend([
                '    @app.get("/health")',
                "    def health(self):",
                '        return "ok"',
                "",
            ])
        for index in range(20):
            lines.extend([
                f"    def {prefix}_operation_with_long_name_{index}"
                "(self, value: str) -> str:",
                "        return value",
                "",
            ])
        (root / file).write_text("\n".join(lines))


class AdaptiveBudgetGoldenTest(unittest.TestCase):
    def test_boundary_packing_is_deterministic_complete_and_owner_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_twin_clients(root)

            first, first_stats = build_digest_with_stats(root, budget=116)
            second, second_stats = build_digest_with_stats(root, budget=116)

        self.assertEqual(first, second)
        self.assertEqual(first_stats, second_stats)
        self.assertEqual(first_stats.effective_detail, "L5-adaptive:1/40")
        self.assertEqual(first_stats.selected_tokens, estimate_tokens(first))
        self.assertEqual(first_stats.selected_tokens, 115)
        self.assertEqual(first_stats.skeleton_tokens, 116)
        self.assertLessEqual(first_stats.selected_tokens, 116)
        self.assertGreater(first_stats.utilization, 0.99)
        self.assertTrue(first_stats.fits)

        # Golden body: one whole method fact fits. No signature is shortened,
        # no external route is sacrificed, and equal owner names stay split.
        self.assertEqual(first.splitlines()[1:], [
            "· C/R/I{fields} · f(args):Ret · ×0=no static use "
            "· @=route/annotation",
            "‥ budget dropped: test coverage edges, test-helper signatures, "
            "private names, untested call chains, unreferenced types' methods "
            "(partial) — the map no longer carries these facts; NEVER guess "
            "them, read the source file first",
            "one.py:Client(C)",
            " health() @GET/health",
            "two.py:Client(C) ×0",
            " beta_operation_with_long_name_0(value):str ×0",
        ])
        self.assertIn("· budget 116 A5", first.splitlines()[0])
        self.assertNotIn("alpha_operation_with_long_name_", first)
        operation_lines = [line.strip() for line in first.splitlines()
                           if "operation_with_long_name" in line]
        self.assertEqual(operation_lines,
                         ["beta_operation_with_long_name_0(value):str ×0"])

        retained = first_stats.retained_bundles
        dropped = first_stats.dropped_bundles
        self.assertEqual(len(retained), 1)
        self.assertIn("two.py|python|Client|method|beta_", retained[0])
        self.assertTrue(any("one.py|python|Client|method|alpha_" in item
                            for item in dropped))
        self.assertEqual(first_stats.retained_categories,
                         (("cold-methods", 1),))
        self.assertEqual(first_stats.dropped_categories,
                         (("cold-methods", 39),))

    def test_selected_private_inventory_keeps_whole_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source: list[str] = []
            for index in range(60):
                source.extend([
                    f"def _long_private_helper_name_{index}():",
                    "    return 1",
                    "",
                ])
            source.extend([
                "def run():",
                "    return _long_private_helper_name_0()",
            ])
            (root / "app.py").write_text("\n".join(source))
            digest, stats = build_digest_with_stats(root, budget=100)

        self.assertEqual(stats.effective_detail, "L3-adaptive:1/60")
        self.assertLessEqual(estimate_tokens(digest), 100)
        self.assertIn("- app.py: _long_private_helper_name_0\n", digest)
        self.assertNotIn("_long_private_helper_name_1,", digest)
        self.assertEqual(stats.retained_categories,
                         (("private-names", 1),
                          ("untested-call-chains", 1)))
        self.assertEqual(stats.dropped_categories,
                         (("private-names", 59),))

    def test_every_budget_that_holds_the_skeleton_is_a_hard_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_twin_clients(root)
            results = [build_digest_with_stats(root, budget=budget)
                       for budget in (116, 120, 140, 200, 400)]

        for digest, stats in results:
            self.assertLessEqual(stats.skeleton_tokens, stats.requested_budget)
            self.assertLessEqual(estimate_tokens(digest), stats.requested_budget)
            self.assertTrue(stats.fits)


class BudgetStatsContractTest(unittest.TestCase):
    def test_stats_are_json_native_and_account_for_every_bundle(self):
        bundles = {
            BudgetBundle(7, "public-methods", "a.py|A|run"),
            BudgetBundle(3, "private-names", "a.py|_helper"),
        }
        retained = {BudgetBundle(7, "public-methods", "a.py|A|run")}

        stats = summarize_budget(
            requested_budget=50,
            full_tokens=80,
            selected_tokens=45,
            skeleton_tokens=30,
            effective_detail="L3-adaptive:1/2",
            bundles=bundles,
            retained=retained,
        )
        payload = stats.as_dict()

        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(payload["policy_version"], "adaptive-bundles-v1")
        self.assertEqual(payload["utilization"], 0.9)
        self.assertEqual(payload["retained_categories"], {"public-methods": 1})
        self.assertEqual(payload["dropped_categories"], {"private-names": 1})
        self.assertEqual(payload["selection_trials"], 0)
        self.assertFalse(payload["search_truncated"])
        self.assertEqual(len(payload["retained_bundles"])
                         + len(payload["dropped_bundles"]), 2)

    def test_trial_cap_prefers_small_whole_facts_and_reports_search_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source: list[str] = []
            for index in range(140):
                source.extend([
                    f"def _oversized_private_fact_{index}_{'x' * 120}():",
                    "    return 1",
                    "",
                ])
            for index in range(20):
                source.extend([
                    f"def _z{index}():",
                    "    return 1",
                    "",
                ])
            source.extend(["def run():", "    return _z0()"])
            (root / "app.py").write_text("\n".join(source))
            digest, stats = build_digest_with_stats(root, budget=120)

        self.assertIn("_z0", digest)
        self.assertLessEqual(stats.selected_tokens, 120)
        self.assertGreaterEqual(stats.utilization, 0.9)
        self.assertLessEqual(stats.selection_trials, 128)
        self.assertIn(stats.stop_reason,
                      {"saturated", "trial-limit", "exhausted"})

    def test_resolved_payload_ranking_reaches_small_chain_before_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            components = [f"long_{index}_{'x' * 90}" for index in range(5)]
            for branch, caller_count in (("a", 128), ("b", 0)):
                directory = root / branch
                for component in components:
                    directory /= component
                directory.mkdir(parents=True)
                source = [
                    "class Client:",
                    "    def target(self):",
                    "        return 1",
                ]
                for index in range(caller_count):
                    source.extend([
                        f"    def a{index:03d}(self):",
                        "        return self.target()",
                    ])
                (directory / "module.py").write_text("\n".join(source) + "\n")
            (root / "zz.py").write_text(
                "def target_unique():\n"
                "    return 1\n\n"
                "def zzzzzz():\n"
                "    return target_unique()\n")

            budget = _budget_above_level(root, detail=4, slack=100)
            base_tokens = _level_tokens(root, detail=4, budget=budget)
            digest, stats = build_digest_with_stats(root, budget=budget)

        z_line = next(line for line in digest.splitlines()
                      if line.strip().startswith("zzzzzz()"))
        self.assertIn("> target_unique", z_line)
        self.assertGreater(stats.selected_tokens, base_tokens)
        self.assertLessEqual(stats.selected_tokens, budget)
        self.assertEqual(stats.selection_candidates, 129)
        self.assertTrue(stats.search_truncated)

    def test_trial_cap_does_not_starve_lower_cost_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = [f"VALUE_{index} = {index}" for index in range(20)]
            source.extend(["", "class C:"])
            for index in range(140):
                long_parameter = f"parameter_{'x' * 180}_{index}"
                source.extend([
                    f"    def m{index:03d}(self, {long_parameter}):",
                    "        return 1",
                ])
            source.extend([
                "",
                "def use(c: C):",
                "    return c.m000(1)",
            ])
            (root / "app.py").write_text("\n".join(source) + "\n")

            budget = _budget_above_level(root, detail=7, slack=40)
            base_tokens = _level_tokens(root, detail=7, budget=budget)
            digest, stats = build_digest_with_stats(root, budget=budget)

        retained = dict(stats.retained_categories)
        self.assertEqual(retained.get("const-values"), 20)
        self.assertNotIn("public-methods", retained)
        self.assertIn("0=0", digest)
        self.assertGreater(stats.selected_tokens, base_tokens)
        self.assertLessEqual(stats.selected_tokens, budget)
        self.assertEqual(stats.selection_candidates, 160)
        self.assertTrue(stats.search_truncated)

    def test_suppressed_record_constructor_is_not_counted_as_a_bundle(self):
        symbols = [
            Symbol(name="Point", kind="record", file="point.py", line=1,
                   fields=["x"], visibility="pub", lang="python"),
            Symbol(name="Point", kind="ctor", file="point.py", line=2,
                   container="Point", params=["x"], param_names=["x"],
                   visibility="pub", lang="python"),
        ]
        catalog: set[BudgetBundle] = set()
        retained: set[BudgetBundle] = set()
        with tempfile.TemporaryDirectory() as tmp:
            digest = render_simple(Path(tmp), symbols, [],
                                   budget_catalog=catalog,
                                   budget_retained=retained)

        self.assertIn("Point(R{x})", digest)
        self.assertNotIn(" Point(x)", digest)
        self.assertEqual(catalog, set())
        self.assertEqual(retained, set())

    def test_duplicate_ownerless_method_is_one_rendered_bundle(self):
        symbols = [
            Symbol(name="run", kind="method", file="module.lua", line=line,
                   container="M", visibility="pub", lang="lua")
            for line in (1, 2)
        ]
        catalog: set[BudgetBundle] = set()
        retained: set[BudgetBundle] = set()
        with tempfile.TemporaryDirectory() as tmp:
            digest = render_simple(Path(tmp), symbols, [],
                                   budget_catalog=catalog,
                                   budget_retained=retained)

        self.assertEqual(digest.count("run()"), 1)
        public = {bundle for bundle in catalog
                  if bundle.category == "public-methods"}
        self.assertEqual(len(public), 1)
        self.assertEqual(public, retained & public)

    def test_tiny_map_is_emitted_whole_when_no_candidate_can_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("def run():\n    return 1\n")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                digest, stats = build_digest_with_stats(root, budget=1)

        self.assertIn("run()", digest)
        self.assertTrue(digest.endswith("\n"))
        self.assertFalse(stats.fits)
        self.assertGreater(stats.selected_tokens, 1)
        self.assertTrue(stats.effective_detail.startswith("minimum-L"))
        self.assertIn("no complete map fits", stderr.getvalue())

    def test_zero_budget_means_unlimited_and_reports_no_utilization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("def run():\n    return 1\n")
            digest, stats = build_digest_with_stats(root, budget=0)

        self.assertEqual(stats.effective_detail, "full")
        self.assertEqual(stats.selected_tokens, estimate_tokens(digest))
        self.assertEqual(stats.requested_budget, 0)
        self.assertIsNone(stats.utilization)
        self.assertTrue(stats.fits)


if __name__ == "__main__":
    unittest.main()
