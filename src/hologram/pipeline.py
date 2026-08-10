from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .model import Diagnostic, DiagnosticSeverity, FileIR, Language, ProjectIR
from .parsers.api import DEFAULT_REGISTRY, EXTRACTOR_VERSIONS, extract_project
from .resolve import ResolutionResult, resolve_project
from .scan import ScanEntry, ScanResult, ScanStatus, scan_project
from .state import StateResult, compute_state


@dataclass(frozen=True, slots=True)
class BuildSnapshot:
    scan: ScanResult
    state: StateResult
    project: ProjectIR
    resolution: ResolutionResult
    complete: bool

    def require_complete(self) -> BuildSnapshot:
        if not self.complete:
            raise IncompleteBuildError(self)
        return self


class IncompleteBuildError(RuntimeError):
    def __init__(self, snapshot: BuildSnapshot) -> None:
        self.snapshot = snapshot
        self.diagnostics = tuple(
            dict.fromkeys(
                snapshot.scan.diagnostics
                + snapshot.state.diagnostics
                + snapshot.project.diagnostics
                + snapshot.resolution.diagnostics
            )
        )
        messages = [
            (
                f"{diagnostic.code} "
                f"({diagnostic.span.file}:{diagnostic.span.start_line}): "
                f"{diagnostic.message}"
                if diagnostic.span is not None
                else f"{diagnostic.code}: {diagnostic.message}"
            )
            for diagnostic in self.diagnostics
        ]
        super().__init__("; ".join(messages) or "project extraction incomplete")


def _error_diagnostics(file_ir: FileIR) -> tuple[Diagnostic, ...]:
    return tuple(
        diagnostic
        for diagnostic in file_ir.diagnostics
        if diagnostic.severity is DiagnosticSeverity.ERROR
    )


def _failure_reason(diagnostics: tuple[Diagnostic, ...]) -> str:
    if any(diagnostic.code.endswith("syntax-error") for diagnostic in diagnostics):
        return "parse-error"
    if any(diagnostic.code == "missing-parser" for diagnostic in diagnostics):
        return "missing-parser"
    if any(diagnostic.code == "extractor-crash" for diagnostic in diagnostics):
        return "extractor-crash"
    return diagnostics[0].code


def _finalize_scan(provisional: ScanResult, project: ProjectIR) -> ScanResult:
    failures = {
        file_ir.source.file: _failure_reason(diagnostics)
        for file_ir in project.files
        if (diagnostics := _error_diagnostics(file_ir))
    }
    entries: tuple[ScanEntry, ...] = tuple(
        dataclasses.replace(
            entry,
            status=ScanStatus.FAILED,
            reason=failures[entry.file],
        )
        if entry.file in failures
        else entry
        for entry in provisional.entries
    )
    complete = provisional.complete and not any(
        entry.status is ScanStatus.FAILED for entry in entries
    )
    return ScanResult(entries, provisional.diagnostics, complete)


def _active_languages(scan_result: ScanResult) -> tuple[Language, ...]:
    return tuple(
        sorted(
            {
                entry.language
                for entry in scan_result.entries
                if entry.status in (ScanStatus.INDEXED, ScanStatus.FAILED)
                and entry.language is not None
            },
            key=lambda language: language.value,
        )
    )


def build_project(root: Path, config: ProjectConfig) -> BuildSnapshot:
    resolved_root = Path(root).resolve()
    provisional = scan_project(resolved_root, config)
    project = extract_project(resolved_root, provisional.sources)
    final_scan = _finalize_scan(provisional, project)

    active = _active_languages(final_scan)
    parser_versions = DEFAULT_REGISTRY.versions()
    state = compute_state(
        resolved_root,
        config,
        final_scan,
        extractor_versions={
            language.value: EXTRACTOR_VERSIONS[language] for language in active
        },
        parser_versions={
            language.value: parser_versions[language.value] for language in active
        },
    )
    resolution = resolve_project(project)
    complete = (
        final_scan.complete
        and state.complete
        and project.complete
        and not any(
            diagnostic.severity is DiagnosticSeverity.ERROR
            for diagnostic in resolution.diagnostics
        )
    )
    return BuildSnapshot(final_scan, state, project, resolution, complete)


__all__ = ["BuildSnapshot", "IncompleteBuildError", "build_project"]
