import dataclasses
import errno
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hologram
from hologram.config import ProjectConfig, default_config
from hologram.model import Diagnostic, DiagnosticSeverity, Language, SourceRole
from hologram.scan import (
    ScanEntry,
    ScanResult,
    ScanStatus,
    detect_language,
    scan_project,
)


def _config(**changes: object) -> ProjectConfig:
    return dataclasses.replace(default_config(), **changes)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")


class LanguageDetectionTest(unittest.TestCase):
    def test_detects_the_complete_suffix_map(self) -> None:
        expected = {
            ".java": Language.JAVA,
            ".py": Language.PYTHON,
            ".ts": Language.TYPESCRIPT,
            ".tsx": Language.TSX,
            ".jsx": Language.TSX,
            ".js": Language.JAVASCRIPT,
            ".mjs": Language.JAVASCRIPT,
            ".vue": Language.VUE,
            ".svelte": Language.SVELTE,
            ".kt": Language.KOTLIN,
            ".kts": Language.KOTLIN,
            ".go": Language.GO,
            ".rs": Language.RUST,
            ".cs": Language.CSHARP,
            ".c": Language.C,
            ".h": Language.C,
            ".cpp": Language.CPP,
            ".cc": Language.CPP,
            ".cxx": Language.CPP,
            ".hpp": Language.CPP,
            ".hh": Language.CPP,
            ".lua": Language.LUA,
            ".html": Language.HTML,
            ".htm": Language.HTML,
            ".yaml": Language.HELM,
            ".yml": Language.HELM,
            ".tpl": Language.HELM,
        }

        for suffix, language in expected.items():
            with self.subTest(suffix=suffix):
                self.assertIs(detect_language(Path(f"file{suffix}")), language)
        self.assertIsNone(detect_language(Path("README.md")))

    def test_root_exports_canonical_detection_and_scan_types(self) -> None:
        self.assertIs(hologram.detect_language, detect_language)
        self.assertIs(hologram.ScanEntry, ScanEntry)
        self.assertIs(hologram.ScanResult, ScanResult)
        self.assertIs(hologram.ScanStatus, ScanStatus)
        self.assertIs(hologram.scan_project, scan_project)
        self.assertIs(detect_language(Path("main.py")), Language.PYTHON)
        self.assertEqual(hologram.legacy.detect_language(Path("main.py")), "python")


class ScanValueTest(unittest.TestCase):
    def test_result_owns_list_inputs_and_sources_follow_entry_order(self) -> None:
        entries: list[ScanEntry] = []
        diagnostics: list[Diagnostic] = []
        result = ScanResult(entries, diagnostics, True)

        entries.append(
            ScanEntry(
                Path("/repo/main.py"),
                "main.py",
                Language.PYTHON,
                ScanStatus.EXCLUDED,
                "include-miss",
                None,
            )
        )
        diagnostics.append(
            Diagnostic("later", DiagnosticSeverity.ERROR, "later")
        )

        self.assertEqual(result.entries, ())
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.sources, ())
        self.assertIsInstance(hash(result), int)


class GitDiscoveryTest(unittest.TestCase):
    def test_git_union_indexes_staged_and_untracked_but_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _init_repo(root)
            (root / "tracked.py").write_text("TRACKED = True\n")
            (root / "new.py").write_text("NEW = True\n")
            (root / "ignored.py").write_text("IGNORED = True\n")
            (root / ".gitignore").write_text("ignored.py\n")
            _git(root, "add", "tracked.py", ".gitignore")

            with mock.patch(
                "hologram.scan.subprocess.run",
                wraps=subprocess.run,
            ) as run:
                result = scan_project(root, _config(exclude=()))

            discovery_argv = [
                "git",
                "-C",
                str(root.resolve()),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ]
            self.assertEqual(
                1,
                sum(call.args[0] == discovery_argv for call in run.call_args_list),
            )
            self.assertEqual(
                ["new.py", "tracked.py"],
                [source.file for source in result.sources],
            )
            self.assertNotIn("ignored.py", [entry.file for entry in result.entries])

    def test_linked_worktree_with_git_file_uses_git_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            main = base / "main"
            linked = base / "linked"
            _init_repo(main)
            (main / "tracked.py").write_text("TRACKED = True\n")
            _git(main, "add", "tracked.py")
            _git(
                main,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-qm",
                "initial",
            )
            _git(main, "worktree", "add", "-q", "--detach", str(linked), "HEAD")

            self.assertTrue((linked / ".git").is_file())
            result = scan_project(linked, _config(exclude=()))

            self.assertTrue(result.complete)
            self.assertEqual(["tracked.py"], [source.file for source in result.sources])

    def test_missing_tracked_file_is_a_failed_ledger_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _init_repo(root)
            path = root / "missing.py"
            path.write_text("VALUE = 1\n")
            _git(root, "add", "missing.py")
            path.unlink()

            result = scan_project(root, _config(exclude=()))

            self.assertEqual(1, len(result.entries))
            entry = result.entries[0]
            self.assertEqual(("missing.py", ScanStatus.FAILED, "missing"),
                             (entry.file, entry.status, entry.reason))
            self.assertIsNone(entry.source)
            self.assertEqual(["scan-missing"], [d.code for d in result.diagnostics])
            self.assertFalse(result.complete)

    def test_identified_git_failure_never_falls_back_to_filesystem(self) -> None:
        failures = (
            subprocess.CompletedProcess(
                ["git", "ls-files"], 7, stdout=b"", stderr=b"index unavailable"
            ),
            OSError("git missing"),
            subprocess.TimeoutExpired(["git", "ls-files"], 60),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "fallback.py").write_text("FALLBACK = True\n")

                def run(argv: list[str], **kwargs: object):
                    if "rev-parse" in argv:
                        return subprocess.CompletedProcess(argv, 0, b"true\n", b"")
                    if isinstance(failure, BaseException):
                        raise failure
                    return failure

                with mock.patch("hologram.scan.subprocess.run", side_effect=run):
                    result = scan_project(root, _config(exclude=()))

                self.assertEqual(1, len(result.entries))
                entry = result.entries[0]
                self.assertEqual("<git>", entry.file)
                self.assertEqual(ScanStatus.FAILED, entry.status)
                self.assertEqual("git-list-failed", entry.reason)
                self.assertIsNone(entry.source)
                self.assertEqual(
                    [("scan-git-list-failed", DiagnosticSeverity.ERROR, None)],
                    [(d.code, d.severity, d.span) for d in result.diagnostics],
                )
                self.assertFalse(result.complete)

    def test_indeterminate_git_probe_fails_closed_without_filesystem_walk(self) -> None:
        cases = (
            subprocess.TimeoutExpired(["git", "rev-parse"], 60),
            OSError("git executable missing"),
            subprocess.CompletedProcess(
                ["git", "rev-parse"], 0, stdout=b"unexpected\n", stderr=b""
            ),
            subprocess.CompletedProcess(
                ["git", "rev-parse"],
                9,
                stdout=b"",
                stderr=b"fatal: permission denied",
            ),
        )
        for probe_result in cases:
            with self.subTest(probe=type(probe_result).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "fallback.py").write_text("FALLBACK = True\n")

                if isinstance(probe_result, BaseException):
                    side_effect = probe_result
                else:
                    side_effect = None
                with mock.patch(
                    "hologram.scan.subprocess.run",
                    side_effect=side_effect,
                    return_value=(
                        None if isinstance(probe_result, BaseException)
                        else probe_result
                    ),
                ), mock.patch(
                    "hologram.scan.os.walk",
                    side_effect=AssertionError("indeterminate probe must not walk"),
                ):
                    result = scan_project(root, _config(exclude=()))

                self.assertEqual(["<git>"], [entry.file for entry in result.entries])
                self.assertEqual(ScanStatus.FAILED, result.entries[0].status)
                self.assertEqual("git-list-failed", result.entries[0].reason)
                self.assertEqual(
                    ["scan-git-list-failed"],
                    [diagnostic.code for diagnostic in result.diagnostics],
                )
                self.assertFalse(result.complete)

    def test_recognized_not_git_result_uses_filesystem_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fallback.py").write_text("FALLBACK = True\n")
            not_git = subprocess.CompletedProcess(
                ["git", "rev-parse"],
                128,
                stdout=b"",
                stderr=b"fatal: not a git repository (or any parent): .git\n",
            )

            with mock.patch(
                "hologram.scan.subprocess.run",
                return_value=not_git,
            ) as run:
                result = scan_project(root, _config(exclude=()))

            run.assert_called_once()
            self.assertEqual(["fallback.py"], [source.file for source in result.sources])
            self.assertTrue(result.complete)


class FilesystemDiscoveryTest(unittest.TestCase):
    def test_non_git_fallback_and_relative_posix_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "z").mkdir()
            (root / "z" / "nested.py").write_text("NESTED = True\n")
            (root / "b.py").write_text("B = True\n")
            (root / "a.py").write_text("A = True\n")

            result = scan_project(root, _config(exclude=()))

            self.assertEqual(
                ["a.py", "b.py", "z/nested.py"],
                [entry.file for entry in result.entries],
            )
            self.assertEqual(
                ["a.py", "b.py", "z/nested.py"],
                [source.file for source in result.sources],
            )
            self.assertTrue(result.complete)

    def test_default_include_matches_root_and_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "svc.py").write_text("ROOT = True\n")
            (root / "src" / "svc.py").write_text("NESTED = True\n")

            result = scan_project(root, _config(exclude=()))

            self.assertEqual(
                ["src/svc.py", "svc.py"],
                [source.file for source in result.sources],
            )

    def test_nested_include_and_include_miss_share_one_glob_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "nested").mkdir(parents=True)
            (root / "src" / "direct.py").write_text("DIRECT = True\n")
            (root / "src" / "nested" / "deep.py").write_text("DEEP = True\n")
            (root / "root.py").write_text("ROOT = True\n")

            result = scan_project(
                root,
                _config(include=("src/**",), exclude=()),
            )

            self.assertEqual(
                ["src/direct.py", "src/nested/deep.py"],
                [source.file for source in result.sources],
            )
            root_entry = next(entry for entry in result.entries if entry.file == "root.py")
            self.assertEqual(ScanStatus.EXCLUDED, root_entry.status)
            self.assertEqual("include-miss", root_entry.reason)

    def test_default_exclusions_match_root_and_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in (root / "target", root / "src" / "generated"):
                directory.mkdir(parents=True)
                (directory / "ignored.py").write_text("IGNORED = True\n")
            (root / "main.py").write_text("MAIN = True\n")

            result = scan_project(root, default_config())

            self.assertEqual(["main.py"], [source.file for source in result.sources])
            excluded = {
                entry.file: entry.reason
                for entry in result.entries
                if entry.status is ScanStatus.EXCLUDED
            }
            self.assertEqual(
                {
                    "src/generated/ignored.py": "exclude-pattern",
                    "target/ignored.py": "exclude-pattern",
                },
                excluded,
            )

    def test_symlink_escaping_root_fails_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            outside = base / "outside.py"
            outside.write_text("SECRET = True\n")
            link = root / "escape.py"
            link.symlink_to(outside)

            result = scan_project(root, _config(exclude=()))

            entry = result.entries[0]
            self.assertEqual((ScanStatus.FAILED, "outside-root"),
                             (entry.status, entry.reason))
            self.assertIsNone(entry.source)
            self.assertEqual(["scan-outside-root"], [d.code for d in result.diagnostics])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires os.mkfifo")
    def test_non_regular_path_fails_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "events.py"
            os.mkfifo(fifo)

            result = scan_project(root, _config(exclude=()))

            entry = result.entries[0]
            self.assertEqual((ScanStatus.FAILED, "non-regular"),
                             (entry.status, entry.reason))
            self.assertIsNone(entry.source)
            self.assertEqual(["scan-non-regular"], [d.code for d in result.diagnostics])

    def test_root_must_resolve_to_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            missing = base / "missing"
            regular = base / "file"
            regular.write_text("not a directory")

            for root in (missing, regular):
                with self.subTest(root=root), self.assertRaises(ValueError):
                    scan_project(root, default_config())

    def test_walk_errors_are_failed_ordered_ledger_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "main.py").write_text("MAIN = True\n")
            not_git = subprocess.CompletedProcess(
                ["git", "rev-parse"],
                128,
                stdout=b"",
                stderr=b"fatal: not a git repository\n",
            )

            def walk(
                selected: Path,
                *,
                topdown: bool,
                onerror,
                followlinks: bool,
            ):
                self.assertEqual(root, selected)
                self.assertTrue(topdown)
                self.assertFalse(followlinks)
                onerror(PermissionError(errno.EACCES, "denied", root / "z-blocked"))
                onerror(PermissionError(errno.EACCES, "denied", root / "a-blocked"))
                yield str(root), [], ["main.py"]

            with mock.patch(
                "hologram.scan.subprocess.run",
                return_value=not_git,
            ), mock.patch("hologram.scan.os.walk", side_effect=walk):
                result = scan_project(root, _config(exclude=()))

            failed = [
                entry for entry in result.entries if entry.status is ScanStatus.FAILED
            ]
            self.assertEqual(["a-blocked", "z-blocked"], [entry.file for entry in failed])
            self.assertEqual(["walk-error", "walk-error"], [entry.reason for entry in failed])
            self.assertEqual(
                ["scan-walk-error", "scan-walk-error"],
                [diagnostic.code for diagnostic in result.diagnostics],
            )
            self.assertFalse(result.complete)


class ClassificationTest(unittest.TestCase):
    def test_ledger_records_indexed_excluded_and_unsupported_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("MAIN = True\n")
            (root / "excluded.py").write_text("EXCLUDED = True\n")
            (root / "notes.txt").write_text("notes\n")

            result = scan_project(
                root,
                _config(exclude=("excluded.py",)),
            )

            entries = {entry.file: entry for entry in result.entries}
            self.assertEqual(
                (Language.PYTHON, ScanStatus.INDEXED, None),
                (entries["main.py"].language, entries["main.py"].status,
                 entries["main.py"].reason),
            )
            self.assertIsNotNone(entries["main.py"].source)
            self.assertEqual(
                (Language.PYTHON, ScanStatus.EXCLUDED, "exclude-pattern"),
                (entries["excluded.py"].language, entries["excluded.py"].status,
                 entries["excluded.py"].reason),
            )
            self.assertIsNone(entries["excluded.py"].source)
            self.assertEqual(
                (None, ScanStatus.EXCLUDED, "unsupported-language"),
                (entries["notes.txt"].language, entries["notes.txt"].status,
                 entries["notes.txt"].reason),
            )
            self.assertIsNone(entries["notes.txt"].source)
            self.assertEqual((), result.diagnostics)
            self.assertTrue(result.complete)

    def test_disabled_language_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Main.java").write_text("class Main {}\n")
            (root / "main.py").write_text("MAIN = True\n")

            result = scan_project(
                root,
                _config(languages=(Language.JAVA,), exclude=()),
            )

            entries = {entry.file: entry for entry in result.entries}
            self.assertEqual(ScanStatus.INDEXED, entries["Main.java"].status)
            self.assertEqual(
                (ScanStatus.EXCLUDED, "language-disabled"),
                (entries["main.py"].status, entries["main.py"].reason),
            )

    def test_roles_are_path_only_and_test_precedes_generated(self) -> None:
        expected = {
            "src/main.py": SourceRole.PRODUCTION,
            "test/helper.py": SourceRole.TEST,
            "tests/helper.py": SourceRole.TEST,
            "spec/helper.py": SourceRole.TEST,
            "specs/helper.py": SourceRole.TEST,
            "src/test_helper.py": SourceRole.TEST,
            "src/helper_test.py": SourceRole.TEST,
            "src/helper.test.py": SourceRole.TEST,
            "src/helper.spec.py": SourceRole.TEST,
            "src/XTest.py": SourceRole.TEST,
            "src/XTests.py": SourceRole.TEST,
            "src/generated/Widget.py": SourceRole.GENERATED,
            "src/generated/XTest.py": SourceRole.TEST,
            "src/generated/tests/main.py": SourceRole.TEST,
            "src/contest.java": SourceRole.PRODUCTION,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for file in expected:
                path = root.joinpath(*file.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("value = 1\n")

            result = scan_project(root, _config(exclude=()))

            roles = {source.file: source.role for source in result.sources}
            self.assertEqual(expected, roles)

    def test_chart_layout_yaml_is_an_indexed_helm_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart = root / "chart" / "Chart.yaml"
            chart.parent.mkdir()
            chart.write_text("apiVersion: v2\nname: sample\n")

            result = scan_project(root, _config(exclude=()))

            source = result.sources[0]
            self.assertEqual(Language.HELM, source.language)
            self.assertEqual(ScanStatus.INDEXED, result.entries[0].status)


class SnapshotFailureTest(unittest.TestCase):
    def test_successful_source_is_read_once_and_owned_after_disk_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "svc.py"
            original_raw = b"VALUE = 1\n"
            path.write_bytes(original_raw)
            real_open = os.open
            opens = 0

            def counted_open(
                candidate,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal opens
                if candidate == "svc.py" and dir_fd is not None:
                    opens += 1
                return real_open(candidate, flags, mode, dir_fd=dir_fd)

            with mock.patch("hologram.scan.os.open", side_effect=counted_open):
                result = scan_project(root, _config(exclude=()))
            path.write_bytes(b"VALUE = 2\n")

            source = result.entries[0].source
            self.assertIsNotNone(source)
            assert source is not None
            self.assertEqual(1, opens)
            self.assertEqual(original_raw, source.raw)
            self.assertEqual(hashlib.sha256(original_raw).hexdigest(), source.sha256)

    def test_invalid_utf8_retains_snapshot_and_emits_one_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "invalid.py"
            raw = b"value = \xff\n"
            path.write_bytes(raw)

            result = scan_project(root, _config(exclude=()))

            entry = result.entries[0]
            self.assertEqual((ScanStatus.FAILED, "invalid-utf8"),
                             (entry.status, entry.reason))
            self.assertIsNotNone(entry.source)
            assert entry.source is not None
            self.assertEqual(raw, entry.source.raw)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry.source.sha256)
            self.assertEqual(
                [("scan-invalid-utf8", DiagnosticSeverity.ERROR, None)],
                [(d.code, d.severity, d.span) for d in result.diagnostics],
            )
            self.assertFalse(result.complete)

    def test_read_error_has_no_snapshot_and_emits_one_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "denied.py"
            path.write_text("VALUE = 1\n")
            real_open = os.open

            def denied_open(
                candidate,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if candidate == "denied.py" and dir_fd is not None:
                    raise PermissionError(errno.EACCES, "permission denied")
                return real_open(candidate, flags, mode, dir_fd=dir_fd)

            with mock.patch("hologram.scan.os.open", side_effect=denied_open):
                result = scan_project(root, _config(exclude=()))

            entry = result.entries[0]
            self.assertEqual((ScanStatus.FAILED, "read-error"),
                             (entry.status, entry.reason))
            self.assertIsNone(entry.source)
            self.assertEqual(
                [("scan-read-error", DiagnosticSeverity.ERROR, None)],
                [(d.code, d.severity, d.span) for d in result.diagnostics],
            )
            self.assertFalse(result.complete)

    def test_final_open_race_to_outside_symlink_never_reads_outside_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "project"
            root.mkdir()
            path = root / "svc.py"
            path.write_bytes(b"SAFE = True\n")
            outside = base / "outside.py"
            outside_raw = b"SECRET_OUTSIDE = True\n"
            outside.write_bytes(outside_raw)
            real_open = os.open
            swapped = False

            def racing_open(
                candidate,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal swapped
                if candidate == "svc.py" and dir_fd is not None:
                    path.unlink()
                    path.symlink_to(outside)
                    swapped = True
                return real_open(candidate, flags, mode, dir_fd=dir_fd)

            with mock.patch("hologram.scan.os.open", side_effect=racing_open):
                result = scan_project(root, _config(exclude=()))

            self.assertTrue(swapped)
            self.assertFalse(result.complete)
            self.assertEqual(ScanStatus.FAILED, result.entries[0].status)
            self.assertIsNone(result.entries[0].source)
            self.assertNotIn(outside_raw, [source.raw for source in result.sources])


class LegacyAdapterTest(unittest.TestCase):
    def test_scan_files_returns_scanner_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "main.py"
            source.write_text("MAIN = True\n")

            self.assertEqual(
                [source.resolve()],
                hologram.legacy.scan_files(root, _config(exclude=())),
            )

    def test_scan_files_exits_with_scanner_diagnostics_when_incomplete(self) -> None:
        diagnostic = Diagnostic(
            "scan-read-error",
            DiagnosticSeverity.ERROR,
            "main.py: could not read source",
        )
        failed = ScanResult(
            [
                ScanEntry(
                    Path("/repo/main.py"),
                    "main.py",
                    Language.PYTHON,
                    ScanStatus.FAILED,
                    "read-error",
                    None,
                )
            ],
            [diagnostic],
            False,
        )

        with mock.patch.object(
            hologram.legacy.scan,
            "scan_project",
            return_value=failed,
        ):
            with self.assertRaisesRegex(SystemExit, "could not read source"):
                hologram.legacy.scan_files(Path("/repo"), default_config())


if __name__ == "__main__":
    unittest.main()
