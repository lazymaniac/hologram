from __future__ import annotations

import copy
import inspect
import unittest
from pathlib import Path

from benchmark import reporting
from benchmark.reporting import (
    matched_pairs,
    private_report,
    public_report,
    report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _row(
    task: str,
    condition: str,
    *,
    tier: str = "simple",
    capability: str = "orientation",
    kind: str | None = None,
    model: str = "model-one",
    version: str = "1.0.0",
    completed: bool = True,
    accepted: bool = True,
    terminal_status: str = "success",
    rubric_score: float = 1.0,
    reads: int = 1,
    searches: int = 2,
    map_hits: int = 3,
    turns: int = 4,
    tokens_in: int = 5,
    tokens_out: int = 6,
    reused: tuple[str, ...] = (),
    duplicated: tuple[str, ...] = (),
    rep: int = 0,
    pair_index: int = 0,
    tree_hash: str = "b" * 64,
    asset_hash: str = "c" * 64,
) -> dict[str, object]:
    selected_kind = (
        ("reuse" if capability == "implementation" else "navigate")
        if kind is None
        else kind
    )
    return {
        "task": task,
        "kind": selected_kind,
        "condition": condition,
        "rep": rep,
        "terminal_status": terminal_status,
        "completed": completed,
        "verifier_passed": accepted,
        "accepted": accepted,
        "model": model,
        "claude_code_version": version,
        "max_turns": 40,
        "corpus_revision": "a" * 40,
        "seed": 20260809,
        "pair_index": pair_index,
        "challenged_tree_sha256": tree_hash,
        "workspace_asset_sha256": asset_hash,
        "tier": tier,
        "capability": capability,
        "visibility": "public",
        "rubric_score": rubric_score,
        "reused": list(reused),
        "duplicated": list(duplicated),
        "new_lines": 1,
        "reads": reads,
        "searches": searches,
        "edits": 0,
        "map_hits": map_hits,
        "turns": turns,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


class MatchedPairTest(unittest.TestCase):
    def test_requires_one_b_and_one_c_with_the_complete_identity(self) -> None:
        valid = [_row("valid", "B"), _row("valid", "C")]
        failed = [
            _row("failed", "B", accepted=False),
            _row("failed", "C", accepted=False),
        ]
        duplicate = [
            _row("duplicate", "B"),
            _row("duplicate", "B"),
            _row("duplicate", "C"),
        ]
        mismatched_tree = [
            _row("tree", "B", tree_hash="d" * 64),
            _row("tree", "C", tree_hash="e" * 64),
        ]
        mismatched_asset = [
            _row("asset", "B", asset_hash="f" * 64),
            _row("asset", "C", asset_hash="0" * 64),
        ]
        missing = [_row("missing", "B")]
        rows = [
            *valid,
            *failed,
            *duplicate,
            *mismatched_tree,
            *mismatched_asset,
            *missing,
        ]

        pairs = matched_pairs(tuple(reversed(rows)))

        self.assertEqual(
            tuple((left["task"], right["task"]) for left, right in pairs),
            (("failed", "failed"), ("valid", "valid")),
        )
        for left, right in pairs:
            self.assertEqual((left["condition"], right["condition"]), ("B", "C"))
        self.assertIsNot(pairs[-1][0], valid[0])
        self.assertEqual(matched_pairs(rows), pairs)

    def test_every_frozen_identity_field_participates_in_matching(self) -> None:
        fields_and_values = {
            "visibility": "private",
            "corpus_revision": "d" * 40,
            "task": "other-task",
            "rep": 1,
            "pair_index": 2,
            "model": "model-two",
            "claude_code_version": "2.0.0",
            "max_turns": 41,
            "seed": 7,
            "challenged_tree_sha256": "e" * 64,
            "workspace_asset_sha256": "f" * 64,
            "tier": "complex",
            "capability": "planning",
            "kind": "reuse",
        }
        for field, value in fields_and_values.items():
            with self.subTest(field=field):
                left = _row("task", "B")
                right = _row("task", "C")
                right[field] = value
                self.assertEqual(matched_pairs((left, right)), ())

    def test_missing_malformed_and_non_bc_rows_never_match(self) -> None:
        base = _row("task", "B")
        required = (
            "visibility",
            "corpus_revision",
            "task",
            "rep",
            "pair_index",
            "model",
            "claude_code_version",
            "max_turns",
            "seed",
            "challenged_tree_sha256",
            "workspace_asset_sha256",
            "tier",
            "capability",
            "kind",
        )
        for field in required:
            with self.subTest(field=field):
                left = dict(base)
                right = _row("task", "C")
                del left[field]
                self.assertEqual(matched_pairs((left, right)), ())
        renamed = _row("task", "B")
        renamed["task_id"] = renamed.pop("task")
        self.assertEqual(matched_pairs((renamed, _row("task", "C"))), ())
        self.assertEqual(matched_pairs((_row("task", "X"), _row("task", "C"))), ())
        malformed = _row("task", "B")
        malformed["rep"] = True
        self.assertEqual(matched_pairs((malformed, _row("task", "C"))), ())


class PublicReportTest(unittest.TestCase):
    def _rows(self) -> list[dict[str, object]]:
        rows = [
            _row(
                "orientation-ok",
                "B",
                rubric_score=0.8,
                reads=2,
                searches=4,
                map_hits=1,
                turns=6,
                tokens_in=100,
                tokens_out=10,
            ),
            _row(
                "orientation-ok",
                "C",
                rubric_score=0.9,
                reads=4,
                searches=6,
                map_hits=3,
                turns=8,
                tokens_in=200,
                tokens_out=20,
            ),
            _row(
                "orientation-failed",
                "B",
                completed=False,
                accepted=False,
                terminal_status="error_max_turns",
                rubric_score=0.1,
                reads=901,
                searches=902,
                map_hits=903,
                turns=904,
                tokens_in=905,
                tokens_out=906,
            ),
            _row(
                "orientation-failed",
                "C",
                accepted=False,
                rubric_score=0.2,
                reads=911,
                searches=912,
                map_hits=913,
                turns=914,
                tokens_in=915,
                tokens_out=916,
            ),
            _row(
                "orientation-unmatched",
                "B",
                rubric_score=1.0,
                reads=921,
                searches=922,
                map_hits=923,
                turns=924,
                tokens_in=925,
                tokens_out=926,
            ),
            _row(
                "planning-ok",
                "B",
                tier="complex",
                capability="planning",
                reads=20,
                searches=21,
                map_hits=22,
                turns=23,
                tokens_in=240,
                tokens_out=25,
            ),
            _row(
                "planning-ok",
                "C",
                tier="complex",
                capability="planning",
                reads=30,
                searches=31,
                map_hits=32,
                turns=33,
                tokens_in=340,
                tokens_out=35,
            ),
            _row(
                "implementation-ok",
                "B",
                capability="implementation",
                reused=("canonical",),
            ),
            _row(
                "implementation-ok",
                "C",
                capability="implementation",
                duplicated=("parallel",),
            ),
            _row(
                "implementation-failed",
                "B",
                capability="implementation",
                accepted=False,
                reused=(),
                duplicated=("FAILED_DUPLICATION_SENTINEL",),
            ),
            _row(
                "implementation-failed",
                "C",
                capability="implementation",
                accepted=False,
                reused=("FAILED_REUSE_SENTINEL",),
            ),
            _row(
                "implementation-unmatched",
                "B",
                capability="implementation",
                duplicated=("UNMATCHED_DUPLICATION_SENTINEL",),
            ),
            _row(
                "audit-empty",
                "B",
                tier="complex",
                capability="audit",
                completed=False,
                accepted=False,
                terminal_status="error_max_turns",
                rubric_score=0,
            ),
            _row(
                "second-model",
                "B",
                tier="complex",
                capability="audit",
                model="model-two",
                version="2.0.0",
            ),
            _row(
                "second-model",
                "C",
                tier="complex",
                capability="audit",
                model="model-two",
                version="2.0.0",
            ),
            _row(
                "second-version",
                "B",
                model="model-one",
                version="2.0.0",
            ),
            _row(
                "second-version",
                "C",
                model="model-one",
                version="2.0.0",
            ),
        ]
        return rows

    def test_partitions_model_version_tier_capability_and_condition(self) -> None:
        rendered = public_report(self._rows())

        headings = tuple(
            line
            for line in rendered.splitlines()
            if line.startswith(("## ", "### ", "#### "))
        )
        self.assertEqual(
            headings,
            (
                "## model-one / 1.0.0",
                "### Tier complex",
                "#### Capability audit",
                "#### Capability planning",
                "### Tier simple",
                "#### Capability implementation",
                "#### Capability orientation",
                "## model-one / 2.0.0",
                "### Tier simple",
                "#### Capability orientation",
                "## model-two / 2.0.0",
                "### Tier complex",
                "#### Capability audit",
            ),
        )
        self.assertIn("unique tasks", rendered)
        self.assertIn("runs", rendered)

    def test_navigation_efficiency_uses_only_accepted_matched_pairs(self) -> None:
        rendered = public_report(self._rows())

        self.assertIn(
            "| B | 3 | 3 | 2/3 (67%) | 2/3 (67%) | 1 | 0.63 | 1 | "
            "2.0 | 4.0 | 1.0 | 6.0 | 100.0 | 10.0 |",
            rendered,
        )
        self.assertIn(
            "| C | 2 | 2 | 2/2 (100%) | 1/2 (50%) | 0 | 0.55 | 1 | "
            "4.0 | 6.0 | 3.0 | 8.0 | 200.0 | 20.0 |",
            rendered,
        )
        self.assertIn(
            "| B | 1 | 1 | 1/1 (100%) | 1/1 (100%) | 0 | 1 | 1 | "
            "20.0 | 21.0 | 22.0 | 23.0 | 240.0 | 25.0 |",
            rendered,
        )
        for excluded_metric in range(901, 927):
            self.assertNotIn(f"{excluded_metric}.0", rendered)

    def test_mismatched_provenance_stays_counted_but_has_no_efficiency(self) -> None:
        rows = (
            _row("mismatch", "B", reads=731, tree_hash="d" * 64),
            _row("mismatch", "C", reads=732, tree_hash="e" * 64),
        )
        rendered = public_report(rows)

        self.assertIn("| B | 1 | 1 | 1/1 (100%) | 1/1 (100%) |", rendered)
        self.assertIn("| C | 1 | 1 | 1/1 (100%) | 1/1 (100%) |", rendered)
        self.assertNotIn("731.0", rendered)
        self.assertNotIn("732.0", rendered)
        self.assertIn("| 1 | — | — | — | — | — | — | — |", rendered)

    def test_implementation_denominators_exclude_failed_and_unmatched_rows(
        self,
    ) -> None:
        rendered = public_report(self._rows())

        self.assertIn(
            "| condition | unique tasks | runs | completed | accepted | "
            "max-turn failures | rubric-score mean | eligible pairs | reuse | "
            "duplication |",
            rendered,
        )
        self.assertIn(
            "| B | 3 | 3 | 3/3 (100%) | 2/3 (67%) | 0 | 1 | 1 | 100% | 0% |",
            rendered,
        )
        self.assertIn(
            "| C | 2 | 2 | 2/2 (100%) | 1/2 (50%) | 0 | 1 | 1 | 0% | 100% |",
            rendered,
        )
        self.assertNotIn("FAILED_DUPLICATION_SENTINEL", rendered)
        self.assertNotIn("FAILED_REUSE_SENTINEL", rendered)
        self.assertNotIn("UNMATCHED_DUPLICATION_SENTINEL", rendered)

    def test_missing_and_failed_pairs_show_counts_but_empty_means_are_dashes(
        self,
    ) -> None:
        rendered = public_report(self._rows())
        audit_start = rendered.index(
            "#### Capability audit", rendered.index("model-one")
        )
        audit_end = rendered.index("#### Capability planning", audit_start)
        audit = rendered[audit_start:audit_end]

        self.assertIn(
            "| B | 1 | 1 | 0/1 (0%) | 0/1 (0%) | 1 | 0 | — | — | — | — | — | — | — |",
            audit,
        )
        self.assertIn(
            "| C | 0 | 0 | 0/0 (0%) | 0/0 (0%) | 0 | — | — | — | — | — | — | — | — |",
            audit,
        )

    def test_report_is_permutation_stable(self) -> None:
        rows = self._rows()
        expected = public_report(rows)
        for permutation in (reversed(rows), rows[::2] + rows[1::2]):
            self.assertEqual(public_report(tuple(permutation)), expected)

    def test_rows_missing_partition_metadata_are_rejected(self) -> None:
        incomplete = {
            "task": "incomplete",
            "condition": "B",
            "model": "mutable-alias",
            "accepted": True,
        }
        with self.assertRaisesRegex(ValueError, "partition metadata"):
            public_report((incomplete,))


class ReportDispatchTest(unittest.TestCase):
    def test_public_reporting_api_signatures_are_exact(self) -> None:
        self.assertEqual(
            reporting.__all__,
            (
                "PRIVATE_GROUP_FIELDS",
                "PRIVATE_NUMERIC_FIELDS",
                "matched_pairs",
                "private_report",
                "public_report",
                "report",
                "require_outside_worktree",
            ),
        )
        for function in (matched_pairs, public_report, report):
            with self.subTest(function=function.__name__):
                self.assertEqual(
                    tuple(inspect.signature(function).parameters),
                    ("rows",),
                )

    def test_homogeneous_visibility_dispatch_and_mixed_rejection(self) -> None:
        public_rows = (_row("task", "B"), _row("task", "C"))
        private_rows = tuple(
            {
                "visibility": "private",
                "condition": condition,
                "completed": True,
                "accepted": True,
                "rubric_score": 1,
                "reads": 1,
                "searches": 1,
                "turns": 1,
            }
            for condition in ("B", "C")
        )

        self.assertEqual(report(public_rows), public_report(public_rows))
        self.assertEqual(report(private_rows), private_report(private_rows))
        with self.assertRaisesRegex(ValueError, "mixed visibility"):
            report((public_rows[0], private_rows[0]))
        self.assertEqual(report(()), "no runs recorded\n")

    def test_inputs_are_not_mutated(self) -> None:
        rows = [_row("task", "B"), _row("task", "C")]
        frozen = copy.deepcopy(rows)
        public_report(rows)
        matched_pairs(rows)
        self.assertEqual(rows, frozen)


class BenchmarkDocumentationTest(unittest.TestCase):
    def test_runbooks_document_the_current_contract(self) -> None:
        paths = (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "benchmark" / "README.md",
        )
        documents = {
            path: path.read_text(encoding="utf-8").casefold() for path in paths
        }
        combined = "\n".join(documents.values())
        for phrase in (
            "no paid sessions",
            "external private results",
            "managed canonical block",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

        runbook = documents[PROJECT_ROOT / "benchmark" / "README.md"]
        for phrase in (
            "codecompanion.json",
            "b/c",
            "claude-sonnet-5",
            "2.1.224",
            "--dry-run",
            "prepare",
            "report",
            "748",
            "103",
            "33",
            "three-run",
        ):
            with self.subTest(runbook_phrase=phrase):
                self.assertIn(phrase, runbook)
        self.assertNotIn("benchmark/tasks/spring.json", runbook)


if __name__ == "__main__":
    unittest.main()
