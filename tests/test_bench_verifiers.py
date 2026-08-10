from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchmark import bench
from benchmark.schema import Task, load_tasks
from benchmark.verifiers.codecompanion import (
    verify_duplicate_unused_audit,
    verify_file_edited_lifecycle,
    verify_move_file_plan,
    verify_read_file_integer_ranges,
)
from benchmark.verifiers.common import (
    Verification,
    changed_paths,
    clean_worktree,
    load_rubrics,
    parse_verifier_output,
)

ROOT = Path(__file__).resolve().parents[1]
RUBRIC = ROOT / "benchmark/verifiers/rubrics/codecompanion.json"
MANIFEST = ROOT / "benchmark/tasks/codecompanion.json"
CHALLENGE = ROOT / "benchmark/challenges/codecompanion-audit.patch"
MODEL = "claude-sonnet-5"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q")


def _commit(root: Path, message: str = "seed") -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Benchmark",
        "-c",
        "user.email=benchmark@example.invalid",
        "commit",
        "-qm",
        message,
    )


EXPLANATIONS = {
    "file-edited-lifecycle": {
        "event_helper": (
            "utils.fire prefixes CodeCompanion to the FileEdited event and emits "
            "the CodeCompanionFileEdited User autocmd."
        ),
        "producers": (
            "ACP write_text_file, the ACP handler, create_file, and "
            "insert_edit_into_file emit FileEdited only after successful writes."
        ),
        "consumers": (
            "edited_files records each payload while code_review tracks its path "
            "for later review."
        ),
        "installation": (
            "codecompanion setup installs both code_review and edited_files consumers."
        ),
        "payloads": (
            "Every payload carries path and tool; ACP and insert edits may carry "
            "line, insert edits carry bufnr, and ACP uses the adapter name."
        ),
        "deletion_consequence": (
            "delete_file does not emit FileEdited, so deletion does not enter "
            "edited_files or code_review tracking."
        ),
    },
    "move-file-plan": {
        "touchpoints": (
            "Add move_file beside create_file and delete_file, register it in config "
            "and the files group, and add focused test_move_file coverage."
        ),
        "rename_reuse": (
            "Call and reuse files.rename directly; avoid a parallel rename abstraction."
        ),
        "containment_reuse": (
            "Validate both source and destination with files.is_path_within_cwd "
            "before moving."
        ),
        "approval_reuse": (
            "Reuse the existing require_approval_before tool option and approval "
            "prompt flow."
        ),
        "registration_reuse": (
            "Register move_file through config and the existing files group so "
            "ToolRegistry.add_single_tool resolves it."
        ),
        "tracking_reuse": (
            "After success, call utils.fire FileEdited for the destination so the "
            "existing edited_files tracker records it."
        ),
        "operation_order": (
            "Normalize source and destination, check containment and collisions, "
            "obtain approval, call files.rename, then fire FileEdited and report success."
        ),
        "boundary_cases": (
            "Test files.is_path_within_cwd outside cwd and symlink-parent cases, "
            "missing source, existing destination, same path, directories, and "
            "files.rename failure."
        ),
        "tests": (
            "Add focused MiniTest cases in test_move_file patterned after the create "
            "and delete tool tests."
        ),
        "commands": (
            "Run make test_file for test_move_file, stylua --check ., git diff "
            "--check, and make test."
        ),
    },
    "duplicate-unused-audit": {
        "active_clone": (
            "active_slug_clone is an active exact clone used by M.active_slug."
        ),
        "canonical_replacement": (
            "Replace active_slug_clone with the existing canonical_slug implementation."
        ),
        "strong_zero": (
            "_unused_private_probe is private, unreferenced, and a strong unused finding."
        ),
        "uncertain_surface": (
            "M.exported_uncertain_surface has zero internal references but is exported, "
            "so its unused status is uncertain."
        ),
        "reachable_decoy": (
            "M.reachable_config_decoy is called by M.active_slug and reaches config; "
            "it is a reachable decoy, not unused."
        ),
    },
}


def _synthetic_navigation_workspace(
    root: Path,
    task: str,
) -> tuple[Path, Path, dict[str, object]]:
    repo = root / "workspace"
    _init_repo(repo)
    rubrics = load_rubrics(RUBRIC)
    task_rubric = rubrics[task]
    claims = task_rubric["claims"]
    assert isinstance(claims, dict)

    source_lines: dict[str, list[str]] = {}
    evidence: list[dict[str, object]] = []
    answer_claims: dict[str, object] = {}
    next_id = 1
    for claim_key, raw_claim in claims.items():
        assert isinstance(raw_claim, dict)
        allowed = raw_claim["evidence"]
        assert isinstance(allowed, list)
        claim_ids: list[str] = []
        for raw_allowed in allowed:
            assert isinstance(raw_allowed, dict)
            path = str(raw_allowed["path"])
            anchor = str(raw_allowed["anchor"])
            lines = source_lines.setdefault(path, [])
            if anchor not in lines:
                lines.append(anchor)
            evidence_id = f"e{next_id}"
            next_id += 1
            claim_ids.append(evidence_id)
            evidence.append(
                {
                    "id": evidence_id,
                    "path": path,
                    "line": lines.index(anchor) + 1,
                    "anchor": anchor,
                }
            )
        answer_claims[claim_key] = {
            "explanation": EXPLANATIONS[task][claim_key],
            "evidence": claim_ids,
        }

    for relative, lines in source_lines.items():
        source_path = repo / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _commit(repo)
    answer_data: dict[str, object] = {
        "schema_version": 1,
        "task": task,
        "claims": answer_claims,
        "evidence": evidence,
    }
    answer = root / "answer.json"
    answer.write_text(
        json.dumps(answer_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return repo, answer, answer_data


def _answer_for_workspace(workspace: Path, task: str, destination: Path) -> Path:
    task_rubric = load_rubrics(RUBRIC)[task]
    claims = task_rubric["claims"]
    assert isinstance(claims, dict)
    evidence: list[dict[str, object]] = []
    answer_claims: dict[str, object] = {}
    next_id = 1
    for claim_key, raw_claim in claims.items():
        assert isinstance(raw_claim, dict)
        allowed = raw_claim["evidence"]
        assert isinstance(allowed, list)
        ids: list[str] = []
        for raw_allowed in allowed:
            assert isinstance(raw_allowed, dict)
            relative = str(raw_allowed["path"])
            anchor = str(raw_allowed["anchor"])
            lines = (workspace / relative).read_text(encoding="utf-8").splitlines()
            matches = [
                index for index, line in enumerate(lines, start=1) if anchor in line
            ]
            if not matches:
                raise AssertionError(f"missing frozen anchor {relative}: {anchor}")
            evidence_id = f"e{next_id}"
            next_id += 1
            ids.append(evidence_id)
            evidence.append(
                {
                    "id": evidence_id,
                    "path": relative,
                    "line": matches[0],
                    "anchor": anchor,
                }
            )
        answer_claims[claim_key] = {
            "explanation": EXPLANATIONS[task][claim_key],
            "evidence": ids,
        }
    destination.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task": task,
                "claims": answer_claims,
                "evidence": evidence,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return destination


class CommonVerifierTest(unittest.TestCase):
    def test_verification_record_and_public_signatures_are_frozen(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(Verification)),
            ("passed", "score", "diagnostics"),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Verification(True, 1.0, ()).score = 0.0  # type: ignore[misc]
        expected = {
            verify_file_edited_lifecycle: ("workspace", "answer"),
            verify_read_file_integer_ranges: ("workspace", "answer"),
            verify_move_file_plan: ("workspace", "answer"),
            verify_duplicate_unused_audit: ("workspace", "answer"),
        }
        for function, parameters in expected.items():
            with self.subTest(function=function.__name__):
                self.assertEqual(
                    tuple(inspect.signature(function).parameters), parameters
                )

    def test_clean_and_changed_paths_cover_staged_unstaged_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            (repo / "tracked.txt").write_text("one\n")
            (repo / "staged.txt").write_text("one\n")
            _commit(repo)
            self.assertTrue(clean_worktree(repo))
            self.assertEqual(changed_paths(repo), frozenset())

            (repo / "tracked.txt").write_text("two\n")
            (repo / "staged.txt").write_text("two\n")
            _git(repo, "add", "staged.txt")
            (repo / "new.txt").write_text("new\n")

            self.assertFalse(clean_worktree(repo))
            self.assertEqual(
                changed_paths(repo),
                frozenset({"tracked.txt", "staged.txt", "new.txt"}),
            )

    def test_final_verifier_object_is_strict_and_exit_consistent(self) -> None:
        good = '{"passed":true,"score":1.0,"diagnostics":[]}\n'
        self.assertEqual(parse_verifier_output(good, 0), Verification(True, 1.0, ()))
        for text, code in (
            ("not json\n", 0),
            ('{"passed":true,"score":1.0,"diagnostics":[],"extra":1}\n', 0),
            ('{"passed":true,"score":1.0,"diagnostics":[]}\n', 1),
            ('{"passed":false,"score":0.0,"diagnostics":[]}\n', 0),
            ('{"passed":true,"score":NaN,"diagnostics":[]}\n', 0),
        ):
            with self.subTest(text=text, code=code):
                result = parse_verifier_output(text, code)
                self.assertFalse(result.passed)
                self.assertEqual(result.score, 0.0)
                self.assertTrue(result.diagnostics)

    def test_runner_captures_verifier_streams_and_uses_only_final_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            answer = root / "answer.json"
            answer.write_text("{}")
            log = root / "verifier.log"
            task = Task(
                "protocol",
                "simple",
                "orientation",
                "navigate",
                "public",
                "Inspect.",
                'printf \'diagnostic\\n{"passed":true,"score":0.75,'
                "\"diagnostics\":[]}\\n'; printf 'warning\\n' >&2",
            )

            result = bench._run_task_verifier(task, workspace, answer, log)

            self.assertEqual(result, Verification(True, 0.75, ()))
            self.assertEqual(
                log.read_text(),
                "stdout:\ndiagnostic\n"
                '{"passed":true,"score":0.75,"diagnostics":[]}\n'
                "\nstderr:\nwarning\n",
            )

            malformed = dataclasses.replace(task, accept_cmd="printf 'not-json\\n'")
            result = bench._run_task_verifier(
                malformed,
                workspace,
                answer,
                root / "malformed.log",
            )
            self.assertFalse(result.passed)
            self.assertEqual(result.score, 0.0)


class NavigationVerifierTest(unittest.TestCase):
    def test_all_three_valid_source_grounded_answers_pass(self) -> None:
        verifiers = {
            "file-edited-lifecycle": verify_file_edited_lifecycle,
            "move-file-plan": verify_move_file_plan,
            "duplicate-unused-audit": verify_duplicate_unused_audit,
        }
        for task, verifier in verifiers.items():
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp:
                workspace, answer, _data = _synthetic_navigation_workspace(
                    Path(tmp), task
                )
                self.assertEqual(
                    verifier(workspace, answer), Verification(True, 1.0, ())
                )

    def test_navigation_rejects_dirty_tree_malformed_shape_and_bad_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, answer, data = _synthetic_navigation_workspace(
                Path(tmp), "file-edited-lifecycle"
            )
            (workspace / "dirty.txt").write_text("dirty\n")
            self.assertFalse(verify_file_edited_lifecycle(workspace, answer).passed)
            (workspace / "dirty.txt").unlink()

            cases: list[tuple[str, object]] = []
            missing = json.loads(json.dumps(data))
            del missing["claims"]["payloads"]
            cases.append(("missing claim", missing))
            unknown = json.loads(json.dumps(data))
            unknown["extra"] = True
            cases.append(("unknown field", unknown))
            bad_path = json.loads(json.dumps(data))
            bad_path["evidence"][0]["path"] = "../outside.lua"
            cases.append(("escaping path", bad_path))
            bad_line = json.loads(json.dumps(data))
            bad_line["evidence"][0]["line"] = 999
            cases.append(("nonexistent line", bad_line))
            bad_anchor = json.loads(json.dumps(data))
            bad_anchor["evidence"][0]["anchor"] = "keyword dump"
            cases.append(("absent anchor", bad_anchor))
            duplicate_id = json.loads(json.dumps(data))
            duplicate_id["evidence"][1]["id"] = duplicate_id["evidence"][0]["id"]
            cases.append(("duplicate evidence", duplicate_id))
            keyword_dump = json.loads(json.dumps(data))
            keyword_dump["claims"]["consumers"]["explanation"] = (
                "edited_files code_review"
            )
            cases.append(("missing relationship", keyword_dump))

            for label, value in cases:
                with self.subTest(label=label):
                    answer.write_text(json.dumps(value), encoding="utf-8")
                    result = verify_file_edited_lifecycle(workspace, answer)
                    self.assertFalse(result.passed)
                    self.assertTrue(result.diagnostics)

            answer.write_text("preface\n" + json.dumps(data), encoding="utf-8")
            self.assertFalse(verify_file_edited_lifecycle(workspace, answer).passed)

    def test_planning_rejects_parallel_replacements_and_audit_rejects_decoy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, answer, data = _synthetic_navigation_workspace(
                Path(tmp), "move-file-plan"
            )
            claims = data["claims"]
            assert isinstance(claims, dict)
            rename_reuse = claims["rename_reuse"]
            assert isinstance(rename_reuse, dict)
            rename_reuse["explanation"] = (
                "Add a new rename helper instead of files.rename."
            )
            answer.write_text(json.dumps(data), encoding="utf-8")
            result = verify_move_file_plan(workspace, answer)
            self.assertFalse(result.passed)
            self.assertLess(result.score, 0.90)

        with tempfile.TemporaryDirectory() as tmp:
            workspace, answer, data = _synthetic_navigation_workspace(
                Path(tmp), "duplicate-unused-audit"
            )
            claims = data["claims"]
            assert isinstance(claims, dict)
            reachable_decoy = claims["reachable_decoy"]
            assert isinstance(reachable_decoy, dict)
            reachable_decoy["explanation"] = (
                "M.reachable_config_decoy is unused and should be deleted."
            )
            answer.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(verify_duplicate_unused_audit(workspace, answer).passed)

    def test_frozen_rubric_anchors_match_pinned_checkout_when_available(self) -> None:
        raw = os.environ.get("HOLOGRAM_BENCH_CODECOMPANION")
        if not raw:
            self.skipTest("HOLOGRAM_BENCH_CODECOMPANION is not configured")
        workspace = Path(raw).resolve(strict=True)
        self.assertEqual(
            _git(workspace, "rev-parse", "HEAD").stdout.decode().strip(),
            "2b959b2bf5fdb13e3b333c078ba549996e477b7c",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for task, verifier in (
                ("file-edited-lifecycle", verify_file_edited_lifecycle),
                ("move-file-plan", verify_move_file_plan),
            ):
                answer = _answer_for_workspace(workspace, task, root / f"{task}.json")
                self.assertEqual(
                    verifier(workspace, answer), Verification(True, 1.0, ())
                )

            challenged = root / "challenged"
            subprocess.run(
                ["git", "clone", "--no-local", "-q", str(workspace), str(challenged)],
                check=True,
            )
            _git(challenged, "apply", str(CHALLENGE))
            _commit(challenged, "benchmark challenge")
            answer = _answer_for_workspace(
                challenged,
                "duplicate-unused-audit",
                root / "audit.json",
            )
            self.assertEqual(
                verify_duplicate_unused_audit(challenged, answer),
                Verification(True, 1.0, ()),
            )


READ_FILE = "lua/codecompanion/interactions/chat/tools/builtin/read_file.lua"
READ_FILE_TEST = "tests/interactions/chat/tools/builtin/test_read_file.lua"

BASE_READ_FILE = """local function extract_range(action, lines)
  local start_line_zero = tonumber(action.start_line_number_base_zero)
  local end_line_zero = tonumber(action.end_line_number_base_zero)
  if start_line_zero < 0 then return { status = "error" } end
  if end_line_zero < -1 then return { status = "error" } end
  if start_line_zero >= #lines then return { status = "error" } end
  if end_line_zero ~= -1 and start_line_zero > end_line_zero then return { status = "error" } end
  end_line_zero = math.max(0, #lines - 1)
  return { status = "success" }
end
local schema = {
  start_line_number_base_zero = { type = "number" },
  end_line_number_base_zero = { type = "number" },
}
return extract_range(args, lines)
"""

FIXED_READ_FILE = BASE_READ_FILE.replace('type = "number"', 'type = "integer"').replace(
    "if start_line_zero < 0 then",
    'if start_line_zero % 1 ~= 0 then return { status = "error" } end\n'
    '  if end_line_zero % 1 ~= 0 then return { status = "error" } end\n'
    "  if start_line_zero < 0 then",
)

FRACTION_TESTS = """T["rejects fractional start"] = function()
  local start_line_number_base_zero = 0.5
end
T["rejects fractional end"] = function()
  local end_line_number_base_zero = 1.5
end
"""


def _implementation_workspace(root: Path) -> tuple[Path, Path]:
    repo = root / "workspace"
    _init_repo(repo)
    source = repo / READ_FILE
    test = repo / READ_FILE_TEST
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text(BASE_READ_FILE)
    test.write_text("local T = {}\n")
    _commit(repo)
    source.write_text(FIXED_READ_FILE)
    test.write_text(FRACTION_TESTS)
    return repo, root / "unused-answer.json"


class ImplementationVerifierTest(unittest.TestCase):
    @staticmethod
    def _commands_pass(argv: tuple[str, ...], workspace: Path) -> tuple[bool, str]:
        return True, ""

    def test_integer_range_fix_requires_reuse_tests_and_all_four_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, answer = _implementation_workspace(Path(tmp))
            with mock.patch(
                "benchmark.verifiers.codecompanion._check_command",
                side_effect=self._commands_pass,
            ) as command:
                result = verify_read_file_integer_ranges(workspace, answer)

            self.assertEqual(result, Verification(True, 1.0, ()))
            self.assertEqual(command.call_count, 4)
            self.assertEqual(
                [call.args[0] for call in command.call_args_list],
                [
                    ("make", "test_file", f"FILE={READ_FILE_TEST}"),
                    ("stylua", "--check", "."),
                    ("git", "diff", "--check"),
                    ("make", "test"),
                ],
            )

    def test_integer_range_fix_rejects_unrelated_missing_guards_and_second_parser(
        self,
    ) -> None:
        mutations = {
            "unrelated path": lambda workspace: (workspace / "extra.lua").write_text(
                "x\n"
            ),
            "fractional acceptance": lambda workspace: (
                workspace / READ_FILE
            ).write_text(
                FIXED_READ_FILE.replace(
                    'if end_line_zero % 1 ~= 0 then return { status = "error" } end\n  ',
                    "",
                )
            ),
            "missing canonical reuse": lambda workspace: (
                workspace / READ_FILE
            ).write_text(
                FIXED_READ_FILE.replace(
                    "extract_range(args, lines)", "inline_range(args, lines)"
                )
            ),
            "second parser": lambda workspace: (workspace / READ_FILE).write_text(
                FIXED_READ_FILE
                + "\nlocal function parse_range(action) return action end\n"
            ),
            "no fractional tests": lambda workspace: (
                workspace / READ_FILE_TEST
            ).write_text("local T = {}\n"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                workspace, answer = _implementation_workspace(Path(tmp))
                mutate(workspace)
                with mock.patch(
                    "benchmark.verifiers.codecompanion._check_command",
                    side_effect=self._commands_pass,
                ):
                    result = verify_read_file_integer_ranges(workspace, answer)
                self.assertEqual(result.score, 0.0)
                self.assertFalse(result.passed)

    def test_any_focused_format_diff_or_full_gate_failure_is_binary(self) -> None:
        for failed_index in range(4):
            with (
                self.subTest(failed_index=failed_index),
                tempfile.TemporaryDirectory() as tmp,
            ):
                workspace, answer = _implementation_workspace(Path(tmp))
                outcomes = [(True, "")] * 4
                outcomes[failed_index] = (False, f"gate {failed_index} failed")
                with mock.patch(
                    "benchmark.verifiers.codecompanion._check_command",
                    side_effect=outcomes,
                ):
                    result = verify_read_file_integer_ranges(workspace, answer)
                self.assertFalse(result.passed)
                self.assertEqual(result.score, 0.0)
                self.assertIn(f"gate {failed_index} failed", result.diagnostics)


class PublicMatrixTest(unittest.TestCase):
    def test_manifest_challenge_and_exact_four_task_matrix(self) -> None:
        self.assertTrue(MANIFEST.is_file())
        self.assertTrue(CHALLENGE.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            corpus.mkdir()
            config = load_tasks(MANIFEST, corpus_override=corpus, environ={})

        self.assertEqual(config.corpus.name, "codecompanion")
        self.assertEqual(config.corpus.visibility, "public")
        self.assertEqual(
            config.corpus.url,
            "https://github.com/olimorris/codecompanion.nvim.git",
        )
        self.assertEqual(
            config.corpus.revision,
            "2b959b2bf5fdb13e3b333c078ba549996e477b7c",
        )
        self.assertEqual(config.corpus.path_env, "HOLOGRAM_BENCH_CODECOMPANION")
        self.assertEqual(config.corpus.bootstrap_cmd, "make deps")
        self.assertEqual(config.corpus.workspace_assets, ("deps",))
        self.assertEqual(config.model, MODEL)
        self.assertEqual(config.claude_code_version, "2.1.224")
        self.assertEqual(config.max_turns, 40)
        self.assertEqual(config.conditions, ("B", "C"))
        self.assertEqual(config.reps, 1)
        self.assertEqual(config.seed, 20260809)
        self.assertEqual(
            tuple(
                (task.id, task.tier, task.capability, task.kind, task.expect_reuse)
                for task in config.tasks
            ),
            (
                ("file-edited-lifecycle", "simple", "orientation", "navigate", ()),
                (
                    "read-file-integer-ranges",
                    "simple",
                    "implementation",
                    "reuse",
                    ("extract_range",),
                ),
                ("move-file-plan", "complex", "planning", "navigate", ()),
                ("duplicate-unused-audit", "complex", "audit", "navigate", ()),
            ),
        )
        audit = config.tasks[-1]
        self.assertIsNotNone(audit.challenge)
        assert audit.challenge is not None
        self.assertEqual(audit.challenge.patch, CHALLENGE.resolve())
        self.assertEqual(
            audit.challenge.sha256,
            hashlib.sha256(CHALLENGE.read_bytes()).hexdigest(),
        )

    def test_dry_run_writes_eight_balanced_rows_without_runner_or_verifier(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            _init_repo(corpus)
            (corpus / "seed.lua").write_text("return {}\n")
            (corpus / ".gitignore").write_text("deps/\n")
            _commit(corpus)
            _git(
                corpus,
                "remote",
                "add",
                "origin",
                "https://github.com/olimorris/codecompanion.nvim.git",
            )
            _git(
                corpus,
                "update-ref",
                "refs/heads/pinned",
                config_revision := _git(corpus, "rev-parse", "HEAD")
                .stdout.decode()
                .strip(),
            )
            (corpus / "deps").mkdir()
            manifest_data = json.loads(MANIFEST.read_text())
            manifest_data["corpus"]["revision"] = config_revision
            challenge_copy = root / "challenge.patch"
            challenge_copy.write_bytes(CHALLENGE.read_bytes())
            manifest_data["tasks"][-1]["challenge"]["patch"] = "challenge.patch"
            manifest = root / "tasks.json"
            manifest.write_text(json.dumps(manifest_data))
            results = root / "results"

            with (
                mock.patch.object(
                    bench, "run_one", side_effect=AssertionError("runner called")
                ),
                mock.patch.object(
                    bench.subprocess,
                    "run",
                    wraps=subprocess.run,
                ) as run,
            ):
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

            self.assertFalse(
                any(
                    isinstance(call.args[0], str)
                    and "benchmark.verifiers" in call.args[0]
                    for call in run.call_args_list
                )
            )
            rows = tuple(
                json.loads(line)
                for line in (results / "runs.jsonl").read_text().splitlines()
            )
            self.assertEqual(len(rows), 8)
            self.assertEqual(
                len({(row["task"], row["condition"], row["rep"]) for row in rows}),
                8,
            )
            self.assertEqual({row["tier"] for row in rows}, {"simple", "complex"})
            self.assertEqual(
                {row["capability"] for row in rows},
                {"orientation", "implementation", "planning", "audit"},
            )
            for pair_index in range(4):
                pair = [row for row in rows if row["pair_index"] == pair_index]
                self.assertEqual({row["condition"] for row in pair}, {"B", "C"})


if __name__ == "__main__":
    unittest.main()
