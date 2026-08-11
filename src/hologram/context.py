from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

CONTEXT_START = b"<!-- hologram:start -->"
CONTEXT_END = b"<!-- hologram:end -->"

AGENT_PATHS = {
    "claude": Path("CLAUDE.md"),
    "codex": Path("AGENTS.md"),
    "gemini": Path("GEMINI.md"),
}


class ContextStatus(StrEnum):
    FRESH = "fresh"
    MISSING = "missing"
    STALE = "stale"
    MALFORMED = "malformed"


class ManagedBlockError(ValueError):
    pass


class AtomicWriteError(OSError):
    pass


def _require_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a Path")
    return value


def _validated_mode(mode: object) -> int | None:
    if mode is None:
        return None
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise TypeError("mode must be an integer or None")
    if not 0 <= mode <= 0o7777:
        raise ValueError("mode must contain only permission bits")
    return mode


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    path: Path
    content: bytes
    mode: int | None

    def __post_init__(self) -> None:
        path = _require_path(self.path, "path")
        if not path.is_absolute() or path != _absolute_path(path):
            raise ValueError("PlannedWrite.path must be absolute and lexical")
        _require_bytes(self.content, "content")
        _validated_mode(self.mode)


_MARKERS = (CONTEXT_START, CONTEXT_END)


def _require_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    return value


def _atomic_failure(action: str, path: Path, error: OSError) -> AtomicWriteError:
    return AtomicWriteError(f"{action} failed for {path}: {error}")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _atomic_failure("lstat", path, error) from error


def _require_directory(path: Path, *, allow_missing: bool) -> bool:
    metadata = _lstat(path)
    if metadata is None:
        if allow_missing:
            return False
        raise AtomicWriteError(f"directory does not exist: {path}")
    if stat.S_ISLNK(metadata.st_mode):
        raise AtomicWriteError(f"directory is a symlink: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise AtomicWriteError(f"path is not a directory: {path}")
    return True


def _normalized_target(path: object, root: object = None) -> Path:
    target = _absolute_path(_require_path(path, "path"))
    if root is None:
        current = target.parent
        while not _require_directory(current, allow_missing=True):
            parent = current.parent
            if parent == current:
                raise AtomicWriteError(
                    f"target has no existing directory ancestor: {target}"
                )
            current = parent
        return target
    else:
        boundary = _absolute_path(_require_path(root, "root"))
        try:
            target.relative_to(boundary)
        except ValueError as error:
            raise AtomicWriteError(
                f"target is outside the selected root: {target}"
            ) from error

    relative = target.relative_to(boundary)
    current = boundary
    for part in relative.parts[:-1]:
        current /= part
        if not _require_directory(current, allow_missing=True):
            break
    return target


def _regular_leaf(path: Path) -> os.stat_result | None:
    metadata = _lstat(path)
    if metadata is None:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise AtomicWriteError(f"target is a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AtomicWriteError(f"target is not a regular file: {path}")
    return metadata


def _read_regular_leaf(path: Path) -> tuple[bytes | None, int | None]:
    before = _regular_leaf(path)
    if before is None:
        return None, None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _atomic_failure("open", path, error) from error

    failure: OSError | None = None
    content: bytes | None = None
    mode: int | None = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise AtomicWriteError(f"opened target is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise AtomicWriteError(f"target changed while it was opened: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        mode = stat.S_IMODE(opened.st_mode)
    except OSError as error:
        failure = error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if failure is None:
                failure = error
            else:
                failure.add_note(f"close failed for {path}: {error}")

    if failure is not None:
        if isinstance(failure, AtomicWriteError):
            raise failure
        raise _atomic_failure("read", path, failure) from failure
    return content, mode


def read_target_bytes(path: Path, *, root: Path | None = None) -> bytes | None:
    """Read a regular target without following a leaf or descendant symlink."""

    target = _normalized_target(path, root)
    content, _ = _read_regular_leaf(target)
    return content


def _marker_lines(value: bytes) -> dict[bytes, list[tuple[int, int]]]:
    found: dict[bytes, list[tuple[int, int]]] = {marker: [] for marker in _MARKERS}
    start = 0
    while True:
        newline = value.find(b"\n", start)
        if newline < 0:
            content_end = len(value)
            physical_end = len(value)
        else:
            content_end = newline
            if content_end > start and value[content_end - 1] == 0x0D:
                content_end -= 1
            physical_end = newline + 1

        line = value[start:content_end]
        if line in found:
            found[line].append((start, physical_end))

        if newline < 0:
            return found
        start = physical_end


def _managed_range(
    value: bytes,
) -> tuple[ContextStatus, int | None, int | None]:
    found = _marker_lines(value)
    starts = found[CONTEXT_START]
    ends = found[CONTEXT_END]
    marker_count = sum(len(positions) for positions in found.values())
    if marker_count == 0:
        return ContextStatus.MISSING, None, None

    if len(starts) == 1 and len(ends) == 1:
        start = starts[0][0]
        end = ends[0][1]
        if start < ends[0][0]:
            return ContextStatus.STALE, start, end
    return ContextStatus.MALFORMED, None, None


def _validated_expected(expected: object) -> bytes:
    block = _require_bytes(expected, "expected")
    status, start, end = _managed_range(block)
    if status is not ContextStatus.STALE or start != 0 or end != len(block):
        raise ManagedBlockError("expected must be exactly one canonical managed block")
    return block


def render_managed_block(rendered_map: str) -> bytes:
    if not isinstance(rendered_map, str):
        raise TypeError("rendered_map must be str")
    payload = rendered_map.encode("utf-8")
    return (
        CONTEXT_START
        + b"\n"
        + b"## Project map (generated by Hologram)\n\n"
        + b"Use this complete map for orientation, placement, reuse, and review. "
        + b"Read source bodies before changing behavior.\n\n"
        + b"```text\n"
        + payload
        + b"```\n"
        + b"Regenerate with: `hologram build`\n"
        + CONTEXT_END
        + b"\n"
    )


def inspect_managed_block(existing: bytes, expected: bytes) -> ContextStatus:
    authored = _require_bytes(existing, "existing")
    candidate = _require_bytes(expected, "expected")
    status, start, end = _managed_range(authored)
    if status is ContextStatus.MALFORMED:
        return status
    block = _validated_expected(candidate)
    if status is ContextStatus.MISSING:
        return status
    if start is None or end is None:
        raise AssertionError("valid managed pair is missing its byte range")
    return ContextStatus.FRESH if authored[start:end] == block else ContextStatus.STALE


def replace_managed_block(existing: bytes, expected: bytes) -> bytes:
    authored = _require_bytes(existing, "existing")
    candidate = _require_bytes(expected, "expected")
    status, start, end = _managed_range(authored)
    if status is ContextStatus.MALFORMED:
        raise ManagedBlockError("existing context has malformed managed markers")
    block = _validated_expected(candidate)
    if status is ContextStatus.MISSING:
        separator = b"" if not authored or authored.endswith((b"\n", b"\r")) else b"\n"
        return authored + separator + block
    if start is None or end is None:
        raise AssertionError("valid managed pair is missing its byte range")
    if authored[start:end] == block:
        return existing
    return authored[:start] + block + authored[end:]


def preflight_atomic_write(
    path: Path,
    content: bytes,
    *,
    root: Path | None = None,
    mode: int | None = None,
) -> PlannedWrite:
    """Plan one raw byte replacement without mutating the filesystem."""

    payload = _require_bytes(content, "content")
    requested_mode = _validated_mode(mode)
    target = _normalized_target(path, root)
    current = read_target_bytes(target, root=root)
    if current is None or requested_mode is not None:
        planned_mode = requested_mode
    else:
        metadata = _regular_leaf(target)
        if metadata is None:
            raise AtomicWriteError(f"target disappeared during preflight: {target}")
        planned_mode = stat.S_IMODE(metadata.st_mode)
    return PlannedWrite(target, payload, planned_mode)


def preflight_context_writes(
    targets: Mapping[str, Path],
    expected_block: bytes,
) -> tuple[PlannedWrite, ...]:
    """Plan managed-block replacements for every selected agent target."""

    if not isinstance(targets, Mapping):
        raise TypeError("targets must be a mapping")
    block = _validated_expected(expected_block)
    plans: list[PlannedWrite] = []
    for agent, path in targets.items():
        if not isinstance(agent, str):
            raise TypeError("target names must be str")
        target = _normalized_target(path)
        current = read_target_bytes(target)
        existing = b"" if current is None else current
        replacement = replace_managed_block(existing, block)
        if current is None:
            mode = None
        else:
            metadata = _regular_leaf(target)
            if metadata is None:
                raise AtomicWriteError(f"target disappeared during preflight: {target}")
            mode = stat.S_IMODE(metadata.st_mode)
        plans.append(PlannedWrite(target, replacement, mode))
    return merge_planned_writes(plans)


def merge_planned_writes(
    writes: Iterable[PlannedWrite],
) -> tuple[PlannedWrite, ...]:
    """Own, deduplicate, validate, and sort a collection of write plans."""

    try:
        source = iter(writes)
    except TypeError as error:
        raise TypeError("writes must be iterable") from error

    by_path: dict[Path, PlannedWrite] = {}
    for write in source:
        if not isinstance(write, PlannedWrite):
            raise TypeError("writes must contain only PlannedWrite values")
        previous = by_path.get(write.path)
        if previous is None:
            by_path[write.path] = write
        elif previous.content != write.content or previous.mode != write.mode:
            raise AtomicWriteError(f"conflicting writes for {write.path}")
    return tuple(by_path[path] for path in sorted(by_path, key=os.fspath))


def _record_cleanup_failure(
    primary: BaseException | None, error: OSError
) -> BaseException:
    if primary is None:
        return AtomicWriteError(f"temporary-file cleanup failed: {error}")
    primary.add_note(f"temporary-file cleanup failed: {error}")
    return primary


def atomic_write(
    path: Path,
    content: bytes,
    *,
    mode: int | None = None,
) -> bool:
    """Atomically replace one file, returning whether its bytes changed."""

    payload = _require_bytes(content, "content")
    requested_mode = _validated_mode(mode)
    target = _normalized_target(path)
    current = read_target_bytes(target)
    if current == payload:
        return False

    metadata = _regular_leaf(target)
    current_mode = None if metadata is None else stat.S_IMODE(metadata.st_mode)
    replacement_mode = (
        requested_mode
        if requested_mode is not None
        else current_mode
        if current_mode is not None
        else 0o644
    )
    _require_directory(target.parent, allow_missing=False)

    descriptor: int | None = None
    stream: object | None = None
    temporary: Path | None = None
    installed = False
    primary: BaseException | None = None
    cause: BaseException | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".hologram-tmp-",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        written = stream.write(payload)  # type: ignore[attr-defined]
        if written != len(payload):
            raise AtomicWriteError(
                f"complete write failed for {target}: "
                f"wrote {written} of {len(payload)} bytes"
            )
        stream.flush()  # type: ignore[attr-defined]
        stream_descriptor = stream.fileno()  # type: ignore[attr-defined]
        os.fchmod(stream_descriptor, replacement_mode)
        os.fsync(stream_descriptor)
        closing = stream
        stream = None
        closing.close()  # type: ignore[attr-defined]
        os.replace(temporary, target)
        installed = True
    except OSError as error:
        if isinstance(error, AtomicWriteError):
            primary = error
        else:
            primary = _atomic_failure("atomic write", target, error)
            cause = error
    finally:
        if stream is not None:
            try:
                stream.close()  # type: ignore[attr-defined]
            except OSError as error:
                primary = _record_cleanup_failure(primary, error)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                primary = _record_cleanup_failure(primary, error)
        if temporary is not None and not installed:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError as error:
                primary = _record_cleanup_failure(primary, error)

    if primary is not None:
        if cause is not None:
            raise primary from cause
        raise primary
    return True


def _prevalidate_commit_target(path: Path) -> None:
    target = _normalized_target(path)
    read_target_bytes(target)


def _create_parent_directories(parent: Path) -> None:
    missing: list[Path] = []
    current = parent
    while not _require_directory(current, allow_missing=True):
        missing.append(current)
        ancestor = current.parent
        if ancestor == current:
            raise AtomicWriteError(
                f"target has no existing directory ancestor: {parent}"
            )
        current = ancestor
    for current in reversed(missing):
        try:
            os.mkdir(current)
        except OSError as error:
            raise _atomic_failure("mkdir", current, error) from error


def commit_writes(writes: Sequence[PlannedWrite]) -> tuple[Path, ...]:
    """Commit sorted plans; on failure, earlier replacements remain installed."""

    plans = merge_planned_writes(writes)
    for plan in plans:
        _prevalidate_commit_target(plan.path)

    parents = sorted(
        {plan.path.parent for plan in plans},
        key=lambda value: (len(value.parts), os.fspath(value)),
    )
    for parent in parents:
        _create_parent_directories(parent)

    changed: list[Path] = []
    for plan in plans:
        if atomic_write(plan.path, plan.content, mode=plan.mode):
            changed.append(plan.path)
    return tuple(changed)


__all__ = [
    "AGENT_PATHS",
    "CONTEXT_END",
    "CONTEXT_START",
    "AtomicWriteError",
    "ContextStatus",
    "ManagedBlockError",
    "PlannedWrite",
    "atomic_write",
    "commit_writes",
    "inspect_managed_block",
    "merge_planned_writes",
    "preflight_atomic_write",
    "preflight_context_writes",
    "read_target_bytes",
    "render_managed_block",
    "replace_managed_block",
]
