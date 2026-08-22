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


def _level_digest(root: Path, detail: int, budget: int) -> str:
    files, symbols, file_tokens, usage_tokens, state = _gather(root, None)
    return render_simple(
        root, symbols, files, state=state,
        zero_usage=_zero_usage_names(symbols, usage_tokens),
        file_tokens=file_tokens, detail=detail, budget=budget,
        loc=_total_loc(files), resolved=_resolved_project_calls(symbols),
        helpers=_helper_class_ids(symbols, file_tokens),
    )


def _level_tokens(root: Path, detail: int, budget: int) -> int:
    return estimate_tokens(_level_digest(root, detail, budget))


def _budget_above_level(root: Path, detail: int, slack: int) -> int:
    """Choose a stable budget despite the budget stamp's digit width."""
    budget = 1_000
    for _ in range(4):
        budget = _level_tokens(root, detail, budget) + slack
    return budget


def _first_budget_matching(root: Path, predicate):
    """Find a tight fitting budget without coupling tests to rendered bytes."""
    _, unlimited = build_digest_with_stats(root, budget=0)
    start = max(1, unlimited.skeleton_tokens)
    stop = max(start, unlimited.full_tokens) + 64
    for budget in range(start, stop + 1):
        # Searching crosses the no-complete-candidate boundary by design.
        with contextlib.redirect_stderr(io.StringIO()):
            digest, stats = build_digest_with_stats(root, budget=budget)
        if stats.fits and predicate(digest, stats):
            return digest, stats
    raise AssertionError("no fitting budget satisfied the semantic predicate")


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


class AdaptiveBudgetSemanticTest(unittest.TestCase):
    def test_boundary_packing_is_deterministic_complete_and_owner_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_twin_clients(root)

            first, first_stats = build_digest_with_stats(root, budget=116)
            second, second_stats = build_digest_with_stats(root, budget=116)

        self.assertEqual(first, second)
        self.assertEqual(first_stats, second_stats)
        self.assertIn("-adaptive:", first_stats.effective_detail)
        self.assertEqual(first_stats.selected_tokens, estimate_tokens(first))
        self.assertLessEqual(first_stats.selected_tokens, 116)
        self.assertTrue(first_stats.fits)

        # Admitted facts remain complete, external routes survive, and equal
        # owner names remain file-qualified. Equal-tier room spans both files.
        self.assertIn("health() @GET/health", first)
        self.assertIn("one.py\n Client(C)", first)
        self.assertIn("two.py\n Client(C)", first)
        operation_lines = [line.strip() for line in first.splitlines()
                           if "operation_with_long_name" in line]
        self.assertTrue(operation_lines)
        for line in operation_lines:
            self.assertRegex(
                line,
                r"^(alpha|beta)_operation_with_long_name_\d+\(value\):str ×0$")
        self.assertTrue(any(line.startswith("alpha_") for line in operation_lines))
        self.assertTrue(any(line.startswith("beta_") for line in operation_lines))

        retained = first_stats.retained_bundles
        dropped = first_stats.dropped_bundles
        self.assertTrue(retained)
        self.assertTrue(any("one.py|python|Client|method|alpha_" in item
                            for item in retained))
        self.assertTrue(any("two.py|python|Client|method|beta_" in item
                            for item in retained))
        self.assertTrue(any("one.py|python|Client|method|alpha_" in item
                            for item in dropped))
        self.assertEqual(dict(first_stats.retained_reasons),
                         {"public API": len(retained)})

    def test_tested_and_cross_file_paths_precede_local_private_leaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "pricing.py").write_text(
                "def price(order):\n    return order\n")
            (root / "checkout.py").write_text(
                "from pricing import price\n\n"
                "def checkout(order):\n    return price(order)\n")
            (root / "inventory.py").write_text(
                "def reserve(items):\n    return items\n")
            leaves = "\n".join(
                f"def _local_private_leaf_{index}_{'x' * 30}():\n"
                f"    return {index}\n"
                for index in range(8)
            )
            (root / "shipping.py").write_text(
                "from inventory import reserve\n\n"
                "def ship(items):\n    return reserve(items)\n\n" + leaves)
            (root / "tests" / "test_checkout.py").write_text(
                "from checkout import checkout\n\n"
                "def test_checkout():\n    return checkout(object())\n")

            # Ranking is what is under test, so both searches stay on the
            # semantic floor; the structure floor below it renders the same
            # facts without the markers these assertions read.
            semantic_digest, semantic_stats = _first_budget_matching(
                root,
                lambda _digest, stats: (
                    stats.effective_detail.startswith("L7")
                    and dict(stats.retained_reasons).get("tested call path", 0) > 0
                    and dict(stats.retained_reasons).get(
                        "cross-file call path", 0) > 0
                ),
            )
            _, private_stats = _first_budget_matching(
                root,
                lambda _digest, stats: (
                    dict(stats.retained_reasons).get("private leaf", 0) > 0
                ),
            )

        self.assertLess(semantic_stats.requested_budget,
                        private_stats.requested_budget)
        self.assertNotIn("private-names",
                         dict(semantic_stats.retained_categories))
        self.assertEqual(dict(semantic_stats.retained_reasons), {
            "cross-file call path": 1,
            "tested call path": 1,
        })
        self.assertIn("checkout(order) ✓ > price", semantic_digest)
        self.assertIn("ship(items) ×0 > reserve", semantic_digest)

    def test_skeleton_drops_the_test_index_and_a_case_restores_its_file(self):
        """Orientation is the first thing a hard budget can spare.

        The floor carries no test index at all — header included — and a
        restored case name must bring its file landmark back with it, or the
        budget would pay for a name with nothing to hang it on.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "checkout.py").write_text(
                "def checkout(order):\n    return order\n")
            (root / "tests" / "test_checkout.py").write_text(
                "from checkout import checkout\n\n"
                "class CheckoutTest:\n"
                "    def test_rejects_expired_card(self):\n"
                "        return checkout(object())\n")
            skeleton = _level_digest(root, detail=7, budget=1)
            restored, stats = _first_budget_matching(
                root,
                lambda digest, _stats: "test_rejects_expired_card" in digest,
            )

        self.assertNotIn("? tests", skeleton)
        self.assertNotIn("test_checkout", skeleton)
        self.assertNotIn("CheckoutTest", skeleton)
        self.assertIn("checkout(order)", skeleton)      # business API survives

        self.assertIn("? tests", restored)
        self.assertIn("test_checkout", restored)
        self.assertIn("test-files", dict(stats.retained_categories))

    def test_structure_floor_strips_every_non_project_annotation(self):
        """L8 states the same facts as L7 in project vocabulary alone.

        Types, decorators, throws, markers, field lists and relations are
        language and framework words; names, parameters and files are this
        project's.  Only the latter survive the deepest level.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shop.py").write_text(
                "import dataclasses\n\n"
                "@dataclasses.dataclass\n"
                "class Quote:\n"
                "    order: str\n"
                "    total_cents: int\n\n"
                "class PricingEngine(PricePort):\n"
                "    def evaluate(self, order: str, items: int) -> Quote:\n"
                "        if not items:\n"
                "            raise UnknownItem(order)\n"
                "        return Quote(order, items)\n\n"
                "def quote_for(order: str, items: int) -> Quote:\n"
                "    raise UnknownItem(order)\n")
            floor = _level_digest(root, detail=7, budget=0)
            plain = _level_digest(root, detail=8, budget=0)

        body = "\n".join(plain.splitlines()[2:-1])
        self.assertIn("shop.py", body)                      # the tree stays
        self.assertIn("Quote(R)", body)
        self.assertIn("PricingEngine(C)", body)
        self.assertIn("quote_for(order,items)", body)       # parameters stay
        for annotation in (":Quote", "@dataclass", "!UnknownItem", "{order",
                           "total_cents", ": PricePort", "←", "sealed:",
                           "✓", "×0", "~"):
            with self.subTest(annotation=annotation):
                self.assertNotIn(annotation, body)
        # a legend may not promise notation the body no longer uses
        legend = plain.splitlines()[1]
        self.assertIn("C/R/I", legend)
        self.assertNotIn("{fields}", legend)
        self.assertNotIn(":Ret", legend)
        self.assertLess(estimate_tokens(plain), estimate_tokens(floor))

    def test_budget_under_the_semantic_floor_lands_on_the_structure_floor(self):
        """Below L7 there used to be nothing but the whole-map fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            for index in range(6):
                (root / f"service_{index}.py").write_text(
                    f"class Service{index}:\n"
                    f"    def handle_request_number_{index}"
                    "(self, payload: dict) -> dict:\n"
                    "        raise NotImplementedError(payload)\n")
            (root / "tests" / "test_services.py").write_text(
                "from service_0 import Service0\n\n"
                "def test_handles():\n    Service0().handle_request_number_0({})\n")
            floor_tokens = _level_tokens(root, detail=7, budget=0)
            plain_tokens = _level_tokens(root, detail=8, budget=0)
            self.assertLess(plain_tokens, floor_tokens)
            budget = plain_tokens + (floor_tokens - plain_tokens) // 2
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                digest, stats = build_digest_with_stats(root, budget=budget)

        self.assertTrue(stats.fits)
        self.assertEqual(err.getvalue(), "")        # a fitting map never warns
        self.assertTrue(stats.effective_detail.startswith("L8"),
                        stats.effective_detail)
        self.assertLessEqual(estimate_tokens(digest), budget)
        self.assertIn(" L8", digest.splitlines()[-1])
        self.assertNotIn("):", digest)              # still no return types
        # `skeleton_tokens` keeps meaning the L7 semantic floor, which is
        # exactly what this budget could not afford
        self.assertGreater(stats.skeleton_tokens, budget)
        self.assertGreaterEqual(stats.skeleton_tokens, floor_tokens)

    def test_selected_member_chain_keeps_owning_method_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "service.py").write_text(
                "class CheckoutService:\n"
                "    def checkout(self, order):\n"
                "        return self.calculate_total(order)\n\n"
                "    def calculate_total(self, order):\n"
                "        return order\n\n"
                "    def unused(self, order):\n"
                "        return order\n")
            (root / "tests" / "test_service.py").write_text(
                "from service import CheckoutService\n\n"
                "def test_checkout():\n"
                "    return CheckoutService().checkout(object())\n")
            digest, stats = _first_budget_matching(
                root,
                lambda _digest, value: any(
                    bundle.startswith("tested-call-chains:")
                    for bundle in value.retained_bundles),
            )

        chain = next(bundle for bundle in stats.retained_bundles
                     if bundle.startswith("tested-call-chains:"))
        key = chain.split(":", 1)[1]
        self.assertTrue(any(
            bundle in stats.retained_bundles
            for bundle in (f"public-methods:{key}", f"cold-methods:{key}")
        ))
        self.assertIn("checkout(order) ✓ > calculate_total", digest)

    def test_equal_tier_selection_preserves_breadth_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for file, owner in (("alpha.py", "Alpha"),
                                ("beta.py", "Beta"),
                                ("gamma.py", "Gamma")):
                source = [f"class {owner}:"]
                for index in range(5):
                    source.extend([
                        f"    def operation_{index}(self, value):",
                        "        return value",
                        "",
                    ])
                (root / file).write_text("\n".join(source))
            _, stats = _first_budget_matching(
                root,
                lambda _digest, value: len([
                    bundle for bundle in value.retained_bundles
                    if bundle.startswith(("public-methods:", "cold-methods:"))
                ]) >= 3,
            )

        members = [bundle for bundle in stats.retained_bundles
                   if bundle.startswith(("public-methods:", "cold-methods:"))]
        first_files = {bundle.split(":", 1)[1].split("|", 1)[0]
                       for bundle in members}
        self.assertEqual(len(members), 3)
        self.assertEqual(first_files, {"alpha.py", "beta.py", "gamma.py"})

    def test_selected_private_inventory_keeps_whole_names_without_duplication(self):
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
            digest, stats = _first_budget_matching(
                root,
                lambda candidate, value: (
                    dict(value.retained_reasons).get("local call path", 0) > 0
                    and dict(value.retained_reasons).get("private leaf", 0) > 0
                    and "_long_private_helper_name_1×0" in candidate
                ),
            )

        self.assertLessEqual(estimate_tokens(digest), stats.requested_budget)
        self.assertEqual(digest.count("_long_private_helper_name_0"), 1)
        self.assertIn("_long_private_helper_name_1×0", digest)
        inventory = "\n".join(line for line in digest.splitlines()
                              if line.startswith((" - ", "   ")))
        self.assertNotIn("_long_private_helper_name_0", inventory)

    def test_private_helper_remains_fallback_when_full_chain_cannot_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_targets = [
                f"business_rule_with_long_name_{index:02d}"
                for index in range(24)
            ]
            source = ["def _fallback():", "    return 1", ""]
            for name in public_targets:
                source.extend([f"def {name}():", "    return 1", ""])
            source.extend([
                "def run():",
                "    return _fallback() + "
                + " + ".join(f"{name}()" for name in public_targets),
            ])
            (root / "app.py").write_text("\n".join(source))
            digest, stats = _first_budget_matching(
                root,
                lambda _digest, value: (
                    dict(value.retained_categories).get("private-names", 0) > 0
                    and "untested-call-chains"
                    not in dict(value.retained_categories)
                ),
            )

        self.assertIn(" - _fallback", digest)
        run_line = next(line for line in digest.splitlines()
                        if line.strip().startswith("run()"))
        self.assertNotIn(" > ", run_line)
        self.assertLessEqual(estimate_tokens(digest), stats.requested_budget)
        self.assertTrue(stats.fits)

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
            BudgetBundle(7, "public-methods", "a.py|A|run",
                         reason="cross-file API"),
            BudgetBundle(3, "private-names", "a.py|_helper",
                         reason="private leaf"),
        }
        retained = {
            BudgetBundle(7, "public-methods", "a.py|A|run",
                         reason="cross-file API"),
        }

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
        self.assertEqual(payload["policy_version"], "adaptive-bundles-v2")
        self.assertEqual(payload["utilization"], 0.9)
        self.assertEqual(payload["retained_categories"], {"public-methods": 1})
        self.assertEqual(payload["dropped_categories"], {"private-names": 1})
        self.assertEqual(payload["retained_reasons"], {"cross-file API": 1})
        self.assertEqual(payload["dropped_reasons"], {"private leaf": 1})
        self.assertEqual(payload["selection_trials"], 0)
        self.assertFalse(payload["search_truncated"])
        self.assertEqual(len(payload["retained_bundles"])
                         + len(payload["dropped_bundles"]), 2)

    def test_trial_cap_reports_unfillable_slack_without_fake_utilization(self):
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
        self.assertLessEqual(stats.selection_trials, 128)
        self.assertTrue(stats.search_truncated)
        self.assertEqual(stats.stop_reason, "trial-limit")
        self.assertTrue(stats.dropped_bundles)
        # `_z0` may remain a dropped fallback bundle because its retained call
        # chain already renders the name. Every unrepresented remainder is too
        # large to fit the slack as one whole fact.
        unrepresented = [bundle for bundle in stats.dropped_bundles
                         if "|_z0|" not in bundle]
        self.assertTrue(all("_oversized_private_fact_" in bundle
                            for bundle in unrepresented))
        oversized_name = f"_oversized_private_fact_0_{'x' * 120}"
        self.assertLess(120 - stats.selected_tokens,
                        estimate_tokens(oversized_name))

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
        self.assertGreaterEqual(stats.selection_candidates, 129)
        self.assertLessEqual(stats.selection_trials, 128)
        self.assertTrue(stats.search_truncated)

    def test_smaller_whole_facts_fill_tier_before_oversized_methods(self):
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
        self.assertGreater(retained.get("const-values", 0), 0)
        self.assertNotIn("public-methods", retained)
        self.assertIn("0=0", digest)
        self.assertEqual(dict(stats.retained_reasons).get("business constant"),
                         retained["const-values"])
        self.assertIn("public API", dict(stats.dropped_reasons))
        self.assertGreater(stats.selected_tokens, base_tokens)
        self.assertLessEqual(stats.selected_tokens, budget)
        self.assertGreaterEqual(stats.selection_candidates, 160)

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
