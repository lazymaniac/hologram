from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchmark import bench
from benchmark.reporting import (
    PRIVATE_GROUP_FIELDS,
    PRIVATE_NUMERIC_FIELDS,
    private_report,
    require_outside_worktree,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TASK_IDS = (
    "sentinel-simple-orientation",
    "sentinel-simple-implementation",
    "sentinel-complex-planning",
    "sentinel-complex-audit",
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _corpus(root: Path) -> tuple[Path, str]:
    corpus = root / "PRIVATE_REPOSITORY_SENTINEL"
    corpus.mkdir()
    _git(corpus, "init", "-q")
    _git(corpus, "config", "user.email", "privacy@example.invalid")
    _git(corpus, "config", "user.name", "Privacy Test")
    (corpus / ".gitignore").write_text("PRIVATE_ASSET_SENTINEL/\n")
    (corpus / "seed.txt").write_text("seed\n")
    _git(corpus, "add", ".")
    _git(corpus, "commit", "-qm", "seed")
    _git(
        corpus,
        "remote",
        "add",
        "origin",
        "https://example.com/private-sentinel.git",
    )
    asset = corpus / "PRIVATE_ASSET_SENTINEL"
    asset.mkdir()
    (asset / "PRIVATE_ASSET_BYTES_SENTINEL").write_text("asset sentinel\n")
    return corpus, _git(corpus, "rev-parse", "HEAD")


def _assets(root: Path) -> tuple[Path, Path, Path]:
    verifier = root / "PRIVATE_VERIFIER_SENTINEL.py"
    verifier.write_text("raise SystemExit(0)\n")
    hidden = root / "PRIVATE_HIDDEN_TEST_SENTINEL.json"
    hidden.write_text('{"hidden":"PRIVATE_HIDDEN_VALUE_SENTINEL"}\n')
    challenge = root / "PRIVATE_CHALLENGE_SENTINEL.patch"
    challenge.write_bytes(
        b"diff --git a/seed.txt b/seed.txt\n"
        b"--- a/seed.txt\n"
        b"+++ b/seed.txt\n"
        b"@@ -1 +1,2 @@\n"
        b" seed\n"
        b"+PRIVATE_CHALLENGE_CONTENT_SENTINEL\n"
    )
    return verifier, hidden, challenge


def _manifest_data(
    *,
    corpus: Path,
    revision: str,
    verifier: Path,
    hidden: Path,
    challenge: Path,
    visibility: str = "private",
    manifest_root: Path | None = None,
) -> dict[str, object]:
    relative_root = challenge.parent if manifest_root is None else manifest_root
    command = " ".join(
        (
            shlex.quote(str(verifier)),
            "--hidden",
            shlex.quote(str(hidden)),
            "{ws}",
        )
    )
    answer_command = command + " {answer}"
    return {
        "corpus": {
            "name": "private-sentinel",
            "visibility": visibility,
            "url": (
                None
                if visibility == "private"
                else "https://example.com/private-sentinel.git"
            ),
            "revision": revision,
            "path_env": "HOLOGRAM_BENCH_PRIVATE_SENTINEL",
            "workspace_assets": ["PRIVATE_ASSET_SENTINEL"],
        },
        "tasks": [
            {
                "id": PRIVATE_TASK_IDS[0],
                "tier": "simple",
                "capability": "orientation",
                "kind": "navigate",
                "visibility": visibility,
                "prompt": "PRIVATE_PROMPT_ORIENTATION_SENTINEL",
                "accept_cmd": answer_command,
            },
            {
                "id": PRIVATE_TASK_IDS[1],
                "tier": "simple",
                "capability": "implementation",
                "kind": "reuse",
                "visibility": visibility,
                "prompt": "PRIVATE_PROMPT_IMPLEMENTATION_SENTINEL",
                "accept_cmd": command,
                "expect_reuse": ["PRIVATE_SYMBOL_SENTINEL"],
            },
            {
                "id": PRIVATE_TASK_IDS[2],
                "tier": "complex",
                "capability": "planning",
                "kind": "navigate",
                "visibility": visibility,
                "prompt": "PRIVATE_PROMPT_PLANNING_SENTINEL",
                "accept_cmd": answer_command,
            },
            {
                "id": PRIVATE_TASK_IDS[3],
                "tier": "complex",
                "capability": "audit",
                "kind": "navigate",
                "visibility": visibility,
                "prompt": "PRIVATE_PROMPT_AUDIT_SENTINEL",
                "accept_cmd": answer_command,
                "challenge": {
                    "patch": os.path.relpath(challenge, relative_root),
                    "sha256": hashlib.sha256(challenge.read_bytes()).hexdigest(),
                },
            },
        ],
        "model": "claude-sonnet-5",
        "claude_code_version": "2.1.224",
        "max_turns": 40,
        "conditions": ["B", "C"],
        "reps": 1,
        "seed": 20260809,
    }


def _write_manifest(
    root: Path,
    data: dict[str, object],
    name: str = "PRIVATE_MANIFEST_SENTINEL.json",
) -> Path:
    path = root / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class PrivateReportingTest(unittest.TestCase):
    def test_private_reporting_surface_and_numeric_totals_are_exact(self) -> None:
        self.assertEqual(PRIVATE_GROUP_FIELDS, frozenset({"condition"}))
        self.assertEqual(
            PRIVATE_NUMERIC_FIELDS,
            frozenset(
                {
                    "completed",
                    "accepted",
                    "rubric_score",
                    "reads",
                    "searches",
                    "turns",
                }
            ),
        )
        sentinels = (
            "PRIVATE_REPOSITORY_IDENTITY_SENTINEL",
            "b" * 40,
            "/PRIVATE/PATH/SENTINEL",
            "PRIVATE_PROMPT_SENTINEL",
            "private-task-id-sentinel",
            "PRIVATE_SYMBOL_SENTINEL",
            "c" * 64,
            "PRIVATE_ANSWER_SENTINEL",
            "PRIVATE_TRANSCRIPT_SENTINEL",
            "PRIVATE_DIFF_SENTINEL",
            "PRIVATE_VERIFIER_LOG_SENTINEL",
        )
        rows: list[dict[str, object]] = []
        for index, condition in enumerate(("B", "B", "C")):
            rows.append(
                {
                    "visibility": "private",
                    "condition": condition,
                    "completed": index != 1,
                    "accepted": index == 0,
                    "rubric_score": (0.25, 0.5, 1)[index],
                    "reads": index + 1,
                    "searches": index + 2,
                    "turns": index + 3,
                    "repository": sentinels[0],
                    "corpus_revision": sentinels[1],
                    "path": sentinels[2],
                    "prompt": sentinels[3],
                    "task": sentinels[4],
                    "symbol": sentinels[5],
                    "patch_sha256": sentinels[6],
                    "answer": sentinels[7],
                    "transcript": sentinels[8],
                    "diff": sentinels[9],
                    "verifier_log": sentinels[10],
                }
            )

        rendered = private_report(rows)

        self.assertEqual(
            rendered,
            "# Private benchmark condition totals\n\n"
            "| condition | runs | completed runs | accepted runs | "
            "rubric-score sum | exploration-call sum | turn sum |\n"
            "|---|---:|---:|---:|---:|---:|---:|\n"
            "| B | 2 | 1 | 1 | 0.75 | 8 | 7 |\n"
            "| C | 1 | 1 | 0 | 1 | 7 | 5 |\n",
        )
        for sentinel in sentinels:
            self.assertNotIn(sentinel, rendered)

    def test_mixed_or_invalid_private_rows_fail_by_count_without_values(self) -> None:
        bad = "PRIVATE_BAD_ROW_SENTINEL"
        base = {
            "visibility": "private",
            "condition": "B",
            "completed": True,
            "accepted": True,
            "rubric_score": 1.0,
            "reads": 1,
            "searches": 2,
            "turns": 3,
            "task": bad,
        }
        cases = (
            [base, {**base, "visibility": "public"}],
            [{**base, "condition": bad}],
            [{**base, "reads": True}],
            [{**base, "rubric_score": float("nan")}],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(
                    ValueError, r"\b[12] invalid row"
                ) as caught:
                    private_report(rows)
                self.assertNotIn(bad, str(caught.exception))


class PrivatePathBoundaryTest(unittest.TestCase):
    def test_guard_resolves_symlinks_and_rejects_both_containment_directions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            target = worktree / "PRIVATE_INSIDE_SENTINEL"
            target.write_text("private\n")
            link = root / "PRIVATE_OUTSIDE_SYMLINK_SENTINEL"
            link.symlink_to(target)
            outside = root / "outside" / "result"

            self.assertEqual(
                require_outside_worktree(
                    outside, worktree=worktree, label="private result"
                ),
                outside.resolve(),
            )
            for selected in (worktree, target, link, root):
                with (
                    self.subTest(selected=selected),
                    self.assertRaisesRegex(ValueError, "outside"),
                ):
                    require_outside_worktree(
                        selected, worktree=worktree, label="private input"
                    )

    def test_private_run_rejects_every_in_worktree_boundary_before_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            external = root / "external"
            external.mkdir()
            corpus, revision = _corpus(external)
            verifier, hidden, challenge = _assets(external)
            base = _manifest_data(
                corpus=corpus,
                revision=revision,
                verifier=verifier,
                hidden=hidden,
                challenge=challenge,
            )
            manifest = _write_manifest(external, base)

            with mock.patch.object(bench, "_WORKTREE", worktree.resolve()):
                inside_manifest = _write_manifest(
                    worktree, base, "PRIVATE_MANIFEST_INSIDE_SENTINEL.json"
                )
                cases: list[tuple[str, Path, Path, Path | None]] = [
                    ("manifest", inside_manifest, corpus, external / "results-a"),
                    ("results", manifest, corpus, worktree / "results"),
                ]
                for label, taskfile, selected_corpus, results in cases:
                    with (
                        self.subTest(label=label),
                        self.assertRaisesRegex(ValueError, "outside"),
                        mock.patch.object(bench, "run_one") as run,
                    ):
                        bench.main(
                            [
                                "run",
                                str(taskfile),
                                "--corpus",
                                str(selected_corpus),
                                "--results",
                                str(results),
                                "--dry-run",
                            ]
                        )
                    run.assert_not_called()

                with (
                    self.subTest(label="default results"),
                    self.assertRaisesRegex(ValueError, "explicit external --results"),
                ):
                    bench.main(
                        [
                            "run",
                            str(manifest),
                            "--corpus",
                            str(corpus),
                            "--dry-run",
                        ]
                    )

    def test_private_corpus_challenge_verifier_hidden_and_assets_are_external(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            external = root / "external"
            external.mkdir()
            corpus, revision = _corpus(external)
            verifier, hidden, challenge = _assets(external)

            def run_case(
                label: str,
                *,
                selected_corpus: Path = corpus,
                selected_verifier: Path = verifier,
                selected_hidden: Path = hidden,
                selected_challenge: Path = challenge,
            ) -> None:
                data = _manifest_data(
                    corpus=selected_corpus,
                    revision=(
                        _git(selected_corpus, "rev-parse", "HEAD")
                        if (selected_corpus / ".git").is_dir()
                        else revision
                    ),
                    verifier=selected_verifier,
                    hidden=selected_hidden,
                    challenge=selected_challenge,
                    manifest_root=external,
                )
                manifest = _write_manifest(external, data, f"{label}.json")
                with (
                    self.subTest(label=label),
                    mock.patch.object(bench, "_WORKTREE", worktree.resolve()),
                    mock.patch.object(bench, "verify_prepared_corpus") as verify,
                    self.assertRaisesRegex(ValueError, "outside"),
                ):
                    bench.main(
                        [
                            "run",
                            str(manifest),
                            "--corpus",
                            str(selected_corpus),
                            "--results",
                            str(external / f"results-{label}"),
                            "--dry-run",
                        ]
                    )
                verify.assert_not_called()

            inside_corpus, _inside_revision = _corpus(worktree)
            run_case("corpus", selected_corpus=inside_corpus)
            inside_verifier = worktree / "PRIVATE_VERIFIER_INSIDE_SENTINEL.py"
            inside_verifier.write_text("raise SystemExit(0)\n")
            run_case("verifier", selected_verifier=inside_verifier)
            inside_hidden = worktree / "PRIVATE_HIDDEN_INSIDE_SENTINEL.json"
            inside_hidden.write_text("{}\n")
            run_case("hidden", selected_hidden=inside_hidden)
            inside_challenge = worktree / "PRIVATE_CHALLENGE_INSIDE_SENTINEL.patch"
            inside_challenge.write_bytes(challenge.read_bytes())
            run_case("challenge", selected_challenge=inside_challenge)

            asset = corpus / "PRIVATE_ASSET_SENTINEL"
            for child in asset.iterdir():
                child.unlink()
            asset.rmdir()
            asset.symlink_to(worktree)
            run_case("workspace-asset")

    def test_private_report_requires_an_explicit_external_raw_results_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            results = worktree / "PRIVATE_RESULTS_SENTINEL"
            results.mkdir(parents=True)
            (results / "runs.jsonl").write_text(
                json.dumps(
                    {
                        "visibility": "private",
                        "condition": "B",
                        "completed": True,
                        "accepted": True,
                        "rubric_score": 1,
                        "reads": 1,
                        "searches": 1,
                        "turns": 1,
                    }
                )
                + "\n"
            )
            with (
                mock.patch.object(bench, "_WORKTREE", worktree.resolve()),
                self.assertRaisesRegex(ValueError, "outside"),
            ):
                bench.main(["report", "--results", str(results)])
            self.assertFalse((results / "report.md").exists())


class PrivateDryRunMatrixTest(unittest.TestCase):
    def test_external_private_and_public_dry_runs_plan_sixteen_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus, revision = _corpus(root)
            verifier, hidden, challenge = _assets(root)
            counts: list[int] = []
            for visibility in ("private", "public"):
                data = _manifest_data(
                    corpus=corpus,
                    revision=revision,
                    verifier=verifier,
                    hidden=hidden,
                    challenge=challenge,
                    visibility=visibility,
                )
                manifest = _write_manifest(root, data, f"{visibility}.json")
                results = root / f"{visibility}-results"
                with mock.patch.object(
                    bench, "run_one", side_effect=AssertionError("paid runner called")
                ) as run:
                    self.assertEqual(
                        bench.main(
                            [
                                "run",
                                str(manifest),
                                "--corpus",
                                str(corpus),
                                "--results",
                                str(results),
                                "--dry-run",
                            ]
                        ),
                        0,
                    )
                run.assert_not_called()
                rows = tuple(
                    json.loads(line)
                    for line in (results / "runs.jsonl").read_text().splitlines()
                )
                counts.append(len(rows))
                self.assertEqual(len(rows), 8)
                self.assertEqual(
                    len({(r["task"], r["condition"], r["rep"]) for r in rows}),
                    8,
                )
                self.assertEqual({r["condition"] for r in rows}, {"B", "C"})
                self.assertEqual({r["tier"] for r in rows}, {"simple", "complex"})
                self.assertEqual(
                    {r["capability"] for r in rows},
                    {"orientation", "implementation", "planning", "audit"},
                )
            self.assertEqual(sum(counts), 16)


if __name__ == "__main__":
    unittest.main()
