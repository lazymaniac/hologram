from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TypeVar

from .config import ProjectConfig
from .model import (
    Diagnostic,
    DiagnosticSeverity,
    Language,
    SourceFile,
    SourceRole,
)

_T = TypeVar("_T")

_SAFE_DESCRIPTOR_READS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
)
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_CHUNK_SIZE = 1024 * 1024


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


class _GitProbeStatus(StrEnum):
    WORKTREE = "worktree"
    NON_WORKTREE = "non-worktree"
    INDETERMINATE = "indeterminate"


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


@dataclass(frozen=True, slots=True)
class _GitProbeResult:
    status: _GitProbeStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _WalkFailure:
    path: Path
    file: str
    detail: str


@dataclass(frozen=True, slots=True)
class _ReadResult:
    raw: bytes | None
    reason: str | None
    detail: str | None = None


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


def _probe_git_worktree(root: Path) -> _GitProbeResult:
    argv = ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _GitProbeResult(_GitProbeStatus.INDETERMINATE, str(error))

    raw_stdout = _output_text(result.stdout)
    stdout = raw_stdout.strip()
    stderr = _output_text(result.stderr).strip()
    if result.returncode == 0 and raw_stdout == "true\n":
        return _GitProbeResult(_GitProbeStatus.WORKTREE)
    if result.returncode == 0 and raw_stdout == "false\n":
        return _GitProbeResult(_GitProbeStatus.NON_WORKTREE)
    if result.returncode != 0 and "not a git repository" in stderr.casefold():
        return _GitProbeResult(_GitProbeStatus.NON_WORKTREE)
    detail = stderr or stdout or f"git rev-parse exited {result.returncode}"
    return _GitProbeResult(_GitProbeStatus.INDETERMINATE, detail)


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
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=60,
            check=False,
        )
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


def _walk_failure(root: Path, error: OSError) -> _WalkFailure:
    filename = error.filename
    if filename is not None:
        try:
            path = Path(filename)
            if not path.is_absolute():
                path = root / path
            relative = path.relative_to(root)
            file = PurePosixPath(*relative.parts).as_posix()
            candidate, unsafe_reason = _candidate_path(root, file)
            if file not in ("", ".") and unsafe_reason is None:
                return _WalkFailure(candidate, file, str(error))
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
    return _WalkFailure(root / "<filesystem>", "<filesystem>", str(error))


def _filesystem_files(root: Path) -> tuple[list[str], tuple[_WalkFailure, ...]]:
    files: list[str] = []
    failures: list[_WalkFailure] = []

    def onerror(error: OSError) -> None:
        failures.append(_walk_failure(root, error))

    for dirpath, dirnames, filenames in os.walk(
        root,
        topdown=True,
        onerror=onerror,
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
    return (
        sorted(set(files)),
        tuple(sorted(failures, key=lambda failure: (failure.file, failure.detail))),
    )


def _glob_match(file: str, pattern: str) -> bool:
    path_parts = PurePosixPath(file).parts
    pattern_parts = PurePosixPath(pattern).parts

    @cache
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


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        # A close failure must not hide the scan/read error already being handled.
        pass


def _open_root_directory(root: Path) -> tuple[int | None, str | None]:
    if not _SAFE_DESCRIPTOR_READS:
        return None, "safe descriptor-relative reads are unavailable"
    current_fd: int | None = None
    try:
        current_fd = os.open("/", _DIRECTORY_OPEN_FLAGS)
        for part in root.parts:
            if part in ("", "/"):
                continue
            next_fd = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=current_fd,
            )
            previous_fd = current_fd
            current_fd = next_fd
            _close_fd(previous_fd)
        return current_fd, None
    except (OSError, NotImplementedError, TypeError) as error:
        if current_fd is not None:
            _close_fd(current_fd)
        return None, str(error)


def _read_failure(error: BaseException) -> _ReadResult:
    if isinstance(error, OSError):
        if error.errno == errno.ENOENT:
            return _ReadResult(None, "missing", str(error))
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            return _ReadResult(None, "unsafe-path", str(error))
    return _ReadResult(None, "read-error", str(error))


def _read_from_root(root_fd: int, file: str) -> _ReadResult:
    parts = PurePosixPath(file).parts
    directory_fds: list[int] = []
    source_fd: int | None = None
    current_fd = root_fd
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=current_fd,
            )
            try:
                directory_fds.append(next_fd)
            except BaseException:
                _close_fd(next_fd)
                raise
            current_fd = next_fd

        source_fd = os.open(
            parts[-1],
            _FILE_OPEN_FLAGS,
            dir_fd=current_fd,
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return _ReadResult(None, "non-regular")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(source_fd, _READ_CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        return _ReadResult(b"".join(chunks), None)
    except (OSError, NotImplementedError, TypeError) as error:
        return _read_failure(error)
    finally:
        if source_fd is not None:
            _close_fd(source_fd)
        for directory_fd in reversed(directory_fds):
            _close_fd(directory_fd)


def _classify_discovery(
    root: Path,
    config: ProjectConfig,
    root_fd: int,
    files: list[str],
    walk_failures: tuple[_WalkFailure, ...],
) -> ScanResult:
    entries: list[ScanEntry] = []
    diagnostics: list[Diagnostic] = []
    enabled_languages = frozenset(config.languages)
    discovery_items = [
        (file, None) for file in files
    ] + [
        (failure.file, failure) for failure in walk_failures
    ]

    for file, walk_failure in sorted(
        discovery_items,
        key=lambda item: (
            item[0],
            "" if item[1] is None else item[1].detail,
        ),
    ):
        if walk_failure is not None:
            reason = "walk-error"
            entries.append(
                ScanEntry(
                    walk_failure.path,
                    walk_failure.file,
                    None,
                    ScanStatus.FAILED,
                    reason,
                    None,
                )
            )
            diagnostics.append(
                _diagnostic(walk_failure.file, reason, walk_failure.detail)
            )
            continue

        path, unsafe_reason = _candidate_path(root, file)
        language = detect_language(Path(file))

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

        read_result = _read_from_root(root_fd, file)
        if read_result.reason is not None:
            entries.append(
                ScanEntry(
                    path,
                    file,
                    language,
                    ScanStatus.FAILED,
                    read_result.reason,
                    None,
                )
            )
            diagnostics.append(
                _diagnostic(file, read_result.reason, read_result.detail)
            )
            continue

        raw = read_result.raw
        assert raw is not None
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

    return ScanResult(tuple(entries), tuple(diagnostics), not diagnostics)


def _root_failure(root: Path, detail: str | None) -> ScanResult:
    reason = "root-open-failed"
    file = "<filesystem>"
    entry = ScanEntry(
        root / file,
        file,
        None,
        ScanStatus.FAILED,
        reason,
        None,
    )
    return ScanResult(
        (entry,),
        (_diagnostic(file, reason, detail),),
        False,
    )


def scan_project(root: Path, config: ProjectConfig) -> ScanResult:
    root = _resolve_root(root)
    root_fd, root_detail = _open_root_directory(root)
    if root_fd is None:
        return _root_failure(root, root_detail)

    try:
        probe = _probe_git_worktree(root)
        if probe.status is _GitProbeStatus.INDETERMINATE:
            return _git_failure(root, probe.detail)
        if probe.status is _GitProbeStatus.WORKTREE:
            files, detail = _git_files(root)
            if files is None:
                return _git_failure(root, detail)
            walk_failures: tuple[_WalkFailure, ...] = ()
        else:
            files, walk_failures = _filesystem_files(root)
        return _classify_discovery(
            root,
            config,
            root_fd,
            files,
            walk_failures,
        )
    finally:
        _close_fd(root_fd)
