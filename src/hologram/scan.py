from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping, TypeVar

from .config import ProjectConfig
from .model import (
    Diagnostic,
    DiagnosticSeverity,
    Language,
    SourceFile,
    SourceRole,
)


_T = TypeVar("_T")


LANGUAGE_BY_SUFFIX: Mapping[str, Language] = MappingProxyType(
    {
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
)


class ScanStatus(StrEnum):
    INDEXED = "indexed"
    EXCLUDED = "excluded"
    FAILED = "failed"


def _own_tuple(value: tuple[_T, ...] | list[_T], field: str) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ScanEntry:
    path: Path
    file: str
    language: Language | None
    status: ScanStatus
    reason: str | None
    source: SourceFile | None


@dataclass(frozen=True, slots=True)
class ScanResult:
    entries: tuple[ScanEntry, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", _own_tuple(self.entries, "entries"))
        object.__setattr__(
            self,
            "diagnostics",
            _own_tuple(self.diagnostics, "diagnostics"),
        )

    @property
    def sources(self) -> tuple[SourceFile, ...]:
        return tuple(
            entry.source
            for entry in self.entries
            if entry.status is ScanStatus.INDEXED and entry.source is not None
        )


def detect_language(path: Path) -> Language | None:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower())


def _resolve_root(root: Path) -> Path:
    selected = Path(root)
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"scan root does not exist: {selected}") from error
    if not resolved.is_dir():
        raise ValueError(f"scan root is not a directory: {resolved}")
    return resolved


def _output_bytes(output: bytes | str | None) -> bytes:
    if output is None:
        return b""
    if isinstance(output, bytes):
        return output
    return output.encode("utf-8", errors="replace")


def _output_text(output: bytes | str | None) -> str:
    return _output_bytes(output).decode("utf-8", errors="replace")


def _is_git_worktree(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and _output_text(result.stdout).strip() == "true"


def _git_files(root: Path) -> tuple[list[str] | None, str | None]:
    argv = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if result.returncode != 0:
        detail = _output_text(result.stderr).strip()
        return None, detail or f"git ls-files exited {result.returncode}"

    raw = _output_bytes(result.stdout)
    files = {
        value.decode("utf-8", errors="surrogateescape")
        for value in raw.split(b"\0")
        if value
    }
    return sorted(files), None


def _filesystem_files(root: Path) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        dirnames.sort()
        filenames.sort()

        # os.walk lists directory symlinks separately even when it will not follow
        # them. Keep them in the ledger so scanner safety can classify the path.
        symlink_dirs = [
            name for name in dirnames if (Path(dirpath) / name).is_symlink()
        ]
        dirnames[:] = [name for name in dirnames if name not in symlink_dirs]
        for name in sorted([*filenames, *symlink_dirs]):
            path = Path(dirpath) / name
            relative = path.relative_to(root)
            files.append(PurePosixPath(*relative.parts).as_posix())
    return sorted(set(files))


def _glob_match(file: str, pattern: str) -> bool:
    path_parts = PurePosixPath(file).parts
    pattern_parts = PurePosixPath(pattern).parts

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _matches_any(file: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob_match(file, pattern) for pattern in patterns)


def _source_role(file: str) -> SourceRole:
    path = PurePosixPath(file)
    directory_parts = tuple(part.lower() for part in path.parts[:-1])
    if any(part in {"test", "tests", "spec", "specs"} for part in directory_parts):
        return SourceRole.TEST

    suffix = path.suffix
    name = path.name
    stem = name[: -len(suffix)] if suffix.lower() in LANGUAGE_BY_SUFFIX else name
    lower_stem = stem.lower()
    if (
        lower_stem.startswith("test_")
        or lower_stem.endswith(("_test", ".test", ".spec"))
        or stem.endswith(("Test", "Tests"))
    ):
        return SourceRole.TEST
    if "generated" in directory_parts:
        return SourceRole.GENERATED
    return SourceRole.PRODUCTION


def _candidate_path(root: Path, file: str) -> tuple[Path, str | None]:
    pure = PurePosixPath(file)
    path = root.joinpath(*pure.parts) if not pure.is_absolute() else Path(file)
    if (
        not file
        or not pure.parts
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in file
        or file != pure.as_posix()
    ):
        return path, "unsafe-path"
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path, "unsafe-path"
    if not resolved.is_relative_to(root):
        return path, "outside-root"
    return path, None


def _diagnostic(file: str, reason: str, detail: str | None = None) -> Diagnostic:
    message = f"{file}: {detail or reason.replace('-', ' ')}"
    return Diagnostic(
        f"scan-{reason}",
        DiagnosticSeverity.ERROR,
        message,
        None,
    )


def _git_failure(root: Path, detail: str | None) -> ScanResult:
    reason = "git-list-failed"
    entry = ScanEntry(
        root / "<git>",
        "<git>",
        None,
        ScanStatus.FAILED,
        reason,
        None,
    )
    return ScanResult(
        (entry,),
        (_diagnostic("<git>", reason, detail),),
        False,
    )


def scan_project(root: Path, config: ProjectConfig) -> ScanResult:
    root = _resolve_root(root)
    if _is_git_worktree(root):
        files, detail = _git_files(root)
        if files is None:
            return _git_failure(root, detail)
    else:
        files = _filesystem_files(root)

    entries: list[ScanEntry] = []
    diagnostics: list[Diagnostic] = []
    enabled_languages = frozenset(config.languages)

    for file in sorted(files):
        path, unsafe_reason = _candidate_path(root, file)
        language = detect_language(PurePosixPath(file))

        if unsafe_reason is not None:
            entry = ScanEntry(
                path,
                file,
                language,
                ScanStatus.FAILED,
                unsafe_reason,
                None,
            )
            entries.append(entry)
            diagnostics.append(_diagnostic(file, unsafe_reason))
            continue
        if language is None:
            entries.append(
                ScanEntry(
                    path,
                    file,
                    None,
                    ScanStatus.EXCLUDED,
                    "unsupported-language",
                    None,
                )
            )
            continue
        if enabled_languages and language not in enabled_languages:
            entries.append(
                ScanEntry(
                    path,
                    file,
                    language,
                    ScanStatus.EXCLUDED,
                    "language-disabled",
                    None,
                )
            )
            continue
        if not _matches_any(file, config.include):
            entries.append(
                ScanEntry(
                    path,
                    file,
                    language,
                    ScanStatus.EXCLUDED,
                    "include-miss",
                    None,
                )
            )
            continue
        if _matches_any(file, config.exclude):
            entries.append(
                ScanEntry(
                    path,
                    file,
                    language,
                    ScanStatus.EXCLUDED,
                    "exclude-pattern",
                    None,
                )
            )
            continue

        try:
            path_stat = path.stat()
        except FileNotFoundError:
            reason = "missing"
            entries.append(
                ScanEntry(path, file, language, ScanStatus.FAILED, reason, None)
            )
            diagnostics.append(_diagnostic(file, reason))
            continue
        except OSError as error:
            reason = "read-error"
            entries.append(
                ScanEntry(path, file, language, ScanStatus.FAILED, reason, None)
            )
            diagnostics.append(_diagnostic(file, reason, str(error)))
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            reason = "non-regular"
            entries.append(
                ScanEntry(path, file, language, ScanStatus.FAILED, reason, None)
            )
            diagnostics.append(_diagnostic(file, reason))
            continue

        try:
            raw = path.read_bytes()
        except OSError as error:
            reason = "read-error"
            entries.append(
                ScanEntry(path, file, language, ScanStatus.FAILED, reason, None)
            )
            diagnostics.append(_diagnostic(file, reason, str(error)))
            continue

        source = SourceFile(
            path,
            file,
            language,
            _source_role(file),
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            reason = "invalid-utf8"
            entries.append(
                ScanEntry(
                    path,
                    file,
                    language,
                    ScanStatus.FAILED,
                    reason,
                    source,
                )
            )
            diagnostics.append(_diagnostic(file, reason, str(error)))
            continue

        entries.append(
            ScanEntry(
                path,
                file,
                language,
                ScanStatus.INDEXED,
                None,
                source,
            )
        )

    return ScanResult(entries, diagnostics, not diagnostics)
