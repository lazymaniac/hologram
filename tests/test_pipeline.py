from __future__ import annotations

import dataclasses
import hashlib
import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hologram
from hologram.config import default_config
from hologram.model import (
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    Language,
    ProjectIR,
    SourceFile,
    SourceRole,
    SourceSpan,
)
from hologram.pipeline import BuildSnapshot, IncompleteBuildError, build_project
from hologram.resolve import ResolutionResult
from hologram.scan import ScanEntry, ScanResult, ScanStatus, scan_project
from hologram.state import StateResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PYMINI = FIXTURES / "pymini"


class PipelineContractTest(unittest.TestCase):
    def test_public_record_is_exact_frozen_slotted_and_lazy_exported(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(BuildSnapshot)),
            ("scan", "state", "project", "resolution", "complete"),
        )
        self.assertEqual(
            tuple(inspect.signature(build_project).parameters),
            ("root", "config"),
        )
        self.assertIs(hologram.BuildSnapshot, BuildSnapshot)
        self.assertIs(hologram.IncompleteBuildError, IncompleteBuildError)
        self.assertIs(hologram.build_project, build_project)
        script = (
            "import sys, hologram\n"
            "assert 'hologram.pipeline' not in sys.modules\n"
            "from hologram import BuildSnapshot\n"
            "assert BuildSnapshot.__module__ == 'hologram.pipeline'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_require_complete_aggregates_diagnostics_stably(self) -> None:
        root = Path("/project")
        span = SourceSpan("main.py", 3, 1, 3, 4)
        shared = Diagnostic("scan-error", DiagnosticSeverity.ERROR, "scan failed")
        parsed = Diagnostic(
            "python-syntax-error",
            DiagnosticSeverity.ERROR,
            "invalid syntax",
            span,
        )
        resolved = Diagnostic(
            "resolution-error",
            DiagnosticSeverity.ERROR,
            "resolution failed",
        )
        scan = ScanResult((), (shared,), False)
        state = StateResult("0" * 64, (shared,), False)
        project = ProjectIR(root, (), (parsed,), False)
        resolution = ResolutionResult((), (), (), (parsed, resolved))
        snapshot = BuildSnapshot(scan, state, project, resolution, False)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.complete = True  # type: ignore[misc]
        with self.assertRaises(IncompleteBuildError) as raised:
            snapshot.require_complete()

        error = raised.exception
        self.assertIs(error.snapshot, snapshot)
        self.assertEqual(error.diagnostics, (shared, parsed, resolved))
        self.assertEqual(
            str(error),
            "scan-error: scan failed; "
            "python-syntax-error (main.py:3): invalid syntax; "
            "resolution-error: resolution failed",
        )

    def test_empty_incomplete_snapshot_uses_fallback_message(self) -> None:
        snapshot = BuildSnapshot(
            ScanResult((), (), False),
            StateResult("0" * 64, (), False),
            ProjectIR(Path("/project"), (), (), False),
            ResolutionResult((), (), (), ()),
            False,
        )
        with self.assertRaisesRegex(
            IncompleteBuildError,
            "^project extraction incomplete$",
        ):
            snapshot.require_complete()


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        self.config = default_config()

    def source(
        self,
        file: str,
        raw: bytes = b"value = 1\n",
        language: Language = Language.PYTHON,
    ) -> SourceFile:
        return SourceFile(
            self.root / file,
            file,
            language,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )

    def indexed_scan(self, *sources: SourceFile) -> ScanResult:
        return ScanResult(
            tuple(
                ScanEntry(
                    source.path,
                    source.file,
                    source.language,
                    ScanStatus.INDEXED,
                    None,
                    source,
                )
                for source in sources
            ),
            (),
            True,
        )

    def test_build_project_returns_one_complete_snapshot(self) -> None:
        snapshot = build_project(PYMINI, default_config())
        self.assertTrue(snapshot.complete)
        self.assertEqual(len(snapshot.state.value), 64)
        self.assertEqual(
            len(snapshot.resolution.calls),
            sum(len(file_ir.calls) for file_ir in snapshot.project.files),
        )
        by_file = {source.file: source for source in snapshot.scan.sources}
        for file_ir in snapshot.project.files:
            self.assertIs(file_ir.source, by_file[file_ir.source.file])
        self.assertIs(snapshot.require_complete(), snapshot)

    def test_orchestration_is_once_ordered_and_reuses_exact_values(self) -> None:
        source = self.source("main.py")
        scan = self.indexed_scan(source)
        project = ProjectIR(self.root.resolve(), (FileIR(source),), (), True)
        state = StateResult("1" * 64, (), True)
        resolution = ResolutionResult((), (), (), ())
        calls: list[str] = []

        def scan_once(root: Path, config: object) -> ScanResult:
            calls.append("scan")
            self.assertEqual(root, self.root.resolve())
            self.assertIs(config, self.config)
            return scan

        def extract_once(
            root: Path,
            sources: tuple[SourceFile, ...],
        ) -> ProjectIR:
            calls.append("extract")
            self.assertEqual(root, self.root.resolve())
            self.assertEqual(sources, scan.sources)
            self.assertIs(next(iter(sources)), source)
            return project

        def state_once(root: Path, config: object, final: ScanResult) -> StateResult:
            calls.append("state")
            self.assertEqual(root, self.root.resolve())
            self.assertIs(config, self.config)
            self.assertIs(final.entries[0].source, source)
            return state

        def resolve_once(value: ProjectIR) -> ResolutionResult:
            calls.append("resolve")
            self.assertIs(value, project)
            return resolution

        with (
            mock.patch(
                "hologram.pipeline.scan_project", side_effect=scan_once
            ) as scanner,
            mock.patch(
                "hologram.pipeline.extract_project", side_effect=extract_once
            ) as extractor,
            mock.patch(
                "hologram.pipeline.compute_state", side_effect=state_once
            ) as state_call,
            mock.patch(
                "hologram.pipeline.resolve_project", side_effect=resolve_once
            ) as resolver,
        ):
            snapshot = build_project(self.root, self.config)

        self.assertEqual(calls, ["scan", "extract", "state", "resolve"])
        for called in (scanner, extractor, state_call, resolver):
            self.assertEqual(called.call_count, 1)
        self.assertIs(snapshot.scan.entries[0].source, source)
        self.assertIs(snapshot.project, project)
        self.assertIs(snapshot.state, state)
        self.assertIs(snapshot.resolution, resolution)
        self.assertTrue(snapshot.complete)

    def test_parse_error_retains_snapshot_and_finalizes_failed_ledger(self) -> None:
        path = self.root / "broken.py"
        raw = b"before = 1\ndef broken(:\n"
        path.write_bytes(raw)

        snapshot = build_project(self.root, self.config)

        entry = next(item for item in snapshot.scan.entries if item.file == "broken.py")
        self.assertIs(entry.status, ScanStatus.FAILED)
        self.assertEqual(entry.reason, "parse-error")
        self.assertIsNotNone(entry.source)
        assert entry.source is not None
        self.assertEqual(entry.source.raw, raw)
        self.assertEqual(snapshot.scan.sources, ())
        self.assertIs(snapshot.project.files[0].source, entry.source)
        self.assertFalse(snapshot.complete)
        with self.assertRaises(IncompleteBuildError) as raised:
            snapshot.require_complete()
        self.assertIs(raised.exception.snapshot, snapshot)

    def test_error_reason_priority_is_stable_per_file(self) -> None:
        sources = (
            self.source("syntax.py"),
            self.source("missing.py"),
            self.source("crash.py"),
            self.source("future.py"),
        )
        scan = self.indexed_scan(*sources)
        future = Diagnostic("future-parser-error", DiagnosticSeverity.ERROR, "future")
        syntax = Diagnostic("python-syntax-error", DiagnosticSeverity.ERROR, "syntax")
        missing = Diagnostic("missing-parser", DiagnosticSeverity.ERROR, "missing")
        crash = Diagnostic("extractor-crash", DiagnosticSeverity.ERROR, "crash")
        files = (
            FileIR(sources[0], diagnostics=(future, syntax)),
            FileIR(sources[1], diagnostics=(future, missing)),
            FileIR(sources[2], diagnostics=(future, crash)),
            FileIR(sources[3], diagnostics=(future,)),
        )
        project = ProjectIR(
            self.root.resolve(),
            files,
            tuple(
                diagnostic for file_ir in files for diagnostic in file_ir.diagnostics
            ),
            False,
        )
        with (
            mock.patch("hologram.pipeline.scan_project", return_value=scan),
            mock.patch("hologram.pipeline.extract_project", return_value=project),
        ):
            snapshot = build_project(self.root, self.config)

        self.assertEqual(
            [(entry.file, entry.reason) for entry in snapshot.scan.entries],
            [
                ("syntax.py", "parse-error"),
                ("missing.py", "missing-parser"),
                ("crash.py", "extractor-crash"),
                ("future.py", "future-parser-error"),
            ],
        )
        self.assertEqual(snapshot.scan.diagnostics, ())
        for entry, source in zip(snapshot.scan.entries, sources, strict=True):
            self.assertIs(entry.source, source)

    def test_build_uses_scanned_bytes_after_path_mutation_without_path_reads(
        self,
    ) -> None:
        path = self.root / "main.py"
        path.write_bytes(b"original = 1\n")
        scanned = scan_project(self.root, self.config)
        original = scanned.sources[0]
        path.write_bytes(b"changed_after_scan = 2\n")

        real_read_bytes = Path.read_bytes
        real_read_text = Path.read_text

        def guarded_read_bytes(selected: Path) -> bytes:
            if selected == path:
                raise AssertionError("source path reread")
            return real_read_bytes(selected)

        def guarded_read_text(selected: Path, *args: object, **kwargs: object) -> str:
            if selected == path:
                raise AssertionError("source path reread")
            return real_read_text(selected, *args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch("hologram.pipeline.scan_project", return_value=scanned),
            mock.patch.object(Path, "read_bytes", guarded_read_bytes),
            mock.patch.object(Path, "read_text", guarded_read_text),
        ):
            snapshot = build_project(self.root, self.config)

        self.assertIs(snapshot.project.files[0].source, original)
        self.assertEqual(snapshot.project.files[0].source.raw, b"original = 1\n")

    def test_failed_entry_without_source_still_resolves_partial_project(self) -> None:
        source = self.source("main.py")
        scan = ScanResult(
            (
                ScanEntry(
                    self.root / "missing.java",
                    "missing.java",
                    Language.JAVA,
                    ScanStatus.FAILED,
                    "read-error",
                    None,
                ),
                *self.indexed_scan(source).entries,
            ),
            (Diagnostic("scan-read-error", DiagnosticSeverity.ERROR, "denied"),),
            False,
        )
        with (
            mock.patch("hologram.pipeline.scan_project", return_value=scan),
            mock.patch(
                "hologram.pipeline.resolve_project",
                wraps=lambda project: ResolutionResult((), (), (), ()),
            ) as resolver,
        ):
            snapshot = build_project(self.root, self.config)

        self.assertEqual(
            [file_ir.source.file for file_ir in snapshot.project.files], ["main.py"]
        )
        self.assertEqual(resolver.call_count, 1)
        self.assertFalse(snapshot.complete)

    def test_resolution_errors_fail_snapshot_but_warnings_do_not(self) -> None:
        source = self.source("main.py")
        scan = self.indexed_scan(source)
        project = ProjectIR(self.root.resolve(), (FileIR(source),), (), True)
        for severity, expected in (
            (DiagnosticSeverity.WARNING, True),
            (DiagnosticSeverity.ERROR, False),
        ):
            with self.subTest(severity=severity):
                diagnostic = Diagnostic("resolution-note", severity, "detail")
                resolution = ResolutionResult((), (), (), (diagnostic,))
                with (
                    mock.patch("hologram.pipeline.scan_project", return_value=scan),
                    mock.patch(
                        "hologram.pipeline.extract_project", return_value=project
                    ),
                    mock.patch(
                        "hologram.pipeline.resolve_project", return_value=resolution
                    ),
                ):
                    snapshot = build_project(self.root, self.config)
                self.assertEqual(snapshot.complete, expected)


if __name__ == "__main__":
    unittest.main()
