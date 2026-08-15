#!/usr/bin/env python3
"""Fail when private benchmark material enters tracked files or artifacts.

Protected identifiers belong in an external denylist, never in this repository.
The scanner reports only the matching file/member and denylist entry number; it
never prints the protected text itself.
"""
from __future__ import annotations

import argparse
import io
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


_PRIVATE_PATH_PREFIXES = (
    "benchmark/results/",
    "benchmark/archive/",
    "benchmark/tasks/local-",
)
_PRIVATE_PAYLOAD_LABEL = "<private-only location redacted>"
_NESTED_ARCHIVE_LABEL = "<archive member location redacted>"
_ARCHIVE_METADATA_LABEL = "<archive metadata location redacted>"
_ARTIFACT_CONTAINER_LABEL = "<artifact container location redacted>"
_PUBLISHABLE_ARCHIVE_LABEL = "<publishable blob location redacted>"
_HISTORY_ARCHIVE_LABEL = "<historical blob location redacted>"


def _private_path(name: str) -> bool:
    normalized = name.replace("\\", "/").lstrip("./")
    trimmed = normalized.rstrip("/")
    return any(
        normalized.startswith(prefix)
        or f"/{prefix}" in normalized
        or trimmed == prefix.rstrip("/")
        or trimmed.endswith(f"/{prefix.rstrip('/')}")
        for prefix in _PRIVATE_PATH_PREFIXES)


def _deny_terms(path: Path | None) -> list[bytes]:
    if path is None:
        return []
    terms: list[bytes] = []
    for line in path.read_bytes().splitlines():
        term = line.strip()
        if term and not term.startswith(b"#"):
            terms.append(term.lower())
    if not terms:
        raise SystemExit(f"privacy denylist is empty: {path}")
    return terms


def _scan_payload(label: str, payload: bytes,
                  terms: list[bytes]) -> list[str]:
    lowered = payload.lower()
    safe_label = _safe_label(label, terms)
    return [f"{safe_label}: matches protected denylist entry {index}"
            for index, term in enumerate(terms, 1) if term in lowered]


def _safe_label(label: str, terms: list[bytes]) -> str:
    encoded = label.encode(errors="surrogateescape").lower()
    if any(term in encoded for term in terms):
        return "<protected location redacted>"
    return label


def _artifact_label(path: Path, terms: list[bytes]) -> str:
    label = str(path)
    if _private_path(label):
        return "<private-only artifact location redacted>"
    return _safe_label(label, terms)


def _scan_name(kind: str, name: str, terms: list[bytes]) -> list[str]:
    lowered = name.encode(errors="surrogateescape").lower()
    return [f"{kind}: name matches protected denylist entry {index}"
            for index, term in enumerate(terms, 1) if term in lowered]


def _git_output(root: Path, args: list[str], *, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, capture_output=True)
    if result.returncode != 0:
        raise SystemExit("privacy audit requires a readable Git worktree")
    return result.stdout


def _indexed_entries(root: Path) -> dict[str, set[tuple[str, str]]]:
    """Return every indexed path and blob, including unresolved stages."""
    entries: dict[str, set[tuple[str, str]]] = {}
    for record in _git_output(root, ["ls-files", "-z", "--stage"]).split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            _mode, oid, _stage = metadata.split(b" ", 2)
        except ValueError:
            raise SystemExit("could not parse Git index") from None
        name = raw_name.decode(errors="surrogateescape")
        entries.setdefault(name, set()).add(
            (_mode.decode("ascii"), oid.decode("ascii")))
    return entries


def _untracked_files(root: Path) -> list[str]:
    output = _git_output(
        root, ["ls-files", "-z", "--others", "--exclude-standard"])
    return [item.decode(errors="surrogateescape")
            for item in output.split(b"\0") if item]


def _publishable_files(root: Path) -> list[str]:
    """Files Git could publish now: index plus non-ignored additions.

    Including untracked files matters during release preparation, before the
    final commit exists. Ignored benchmark inputs and build outputs remain
    outside the publication set and are audited separately when supplied as
    artifacts.
    """
    return sorted(set(_indexed_entries(root)) | set(_untracked_files(root)))


def _worktree_payload(path: Path) -> bytes | None:
    """Read publishable bytes without following a worktree symlink."""
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    if path.is_file():
        return path.read_bytes()
    return None


def _scan_link_target(kind: str, target: str,
                      terms: list[bytes]) -> list[str]:
    failures = _scan_name(f"{kind} target", target, terms)
    if _private_path(target):
        failures.append(f"{kind} target: private-only path <redacted>")
    return failures


def audit_tree(root: Path, terms: list[bytes]) -> list[str]:
    failures: list[str] = []
    indexed = _indexed_entries(root)
    names = sorted(set(indexed) | set(_untracked_files(root)))
    for name in names:
        failures.extend(_scan_name("publishable path", name, terms))
        private_path = _private_path(name)
        if private_path:
            failures.append("tracked private-only path: <redacted>")
        # The index and working tree are separate publication candidates. Scan
        # both: an unstaged benign overwrite must not hide protected staged
        # bytes, and protected unstaged edits must not hide behind a clean
        # index blob.
        for mode, oid in sorted(indexed.get(name, ())):
            payload = _git_output(root, ["cat-file", "blob", oid])
            label = (_PRIVATE_PAYLOAD_LABEL if private_path
                     else f"{name} (index)")
            failures.extend(
                _scan_payload(label, payload, terms))
            if mode == "120000":
                target = payload.decode(errors="surrogateescape")
                failures.extend(_scan_link_target(
                    "indexed symlink", target, terms))
            elif _is_archive_payload(payload):
                failures.append(
                    f"{_PUBLISHABLE_ARCHIVE_LABEL}: archive payload is not "
                    "allowed")
        worktree_path = root / name
        payload = _worktree_payload(worktree_path)
        if payload is not None:
            label = (_PRIVATE_PAYLOAD_LABEL if private_path
                     else f"{name} (worktree)")
            failures.extend(
                _scan_payload(label, payload, terms))
            if worktree_path.is_symlink():
                target = payload.decode(errors="surrogateescape")
                failures.extend(_scan_link_target(
                    "worktree symlink", target, terms))
            elif _is_archive_payload(payload):
                failures.append(
                    f"{_PUBLISHABLE_ARCHIVE_LABEL}: archive payload is not "
                    "allowed")
    return failures


def _archive_members(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if archive.comment:
                yield None, None, False, None, (archive.comment,)
            for info in archive.infolist():
                payload = None if info.is_dir() else archive.read(info)
                mode = (info.external_attr >> 16) & 0xffff
                is_link = stat.S_ISLNK(mode)
                link_target = (payload.decode(errors="surrogateescape")
                               if is_link and payload is not None else None)
                metadata = tuple(
                    item for item in (info.comment, info.extra) if item)
                yield (info.filename, payload,
                       not info.is_dir() and not is_link,
                       link_target, metadata)
        return
    try:
        with tarfile.open(path, "r:*") as archive:
            global_metadata = tuple(
                f"{key}\0{value}".encode(errors="surrogateescape")
                for key, value in archive.pax_headers.items())
            if global_metadata:
                yield None, None, False, None, global_metadata
            for info in archive.getmembers():
                if info.isfile():
                    stream = archive.extractfile(info)
                    payload = stream.read() if stream is not None else b""
                elif info.issym() or info.islnk():
                    payload = info.linkname.encode(
                        errors="surrogateescape")
                else:
                    payload = None
                link_target = info.linkname if info.issym() or info.islnk() \
                    else None
                metadata = [
                    value.encode(errors="surrogateescape")
                    for value in (info.uname, info.gname) if value]
                metadata.extend(
                    f"{key}\0{value}".encode(errors="surrogateescape")
                    for key, value in info.pax_headers.items())
                yield (info.name, payload, info.isfile(), link_target,
                       tuple(metadata))
        return
    except tarfile.TarError:
        pass
    yield path.name, path.read_bytes(), False, None, ()


def _is_archive_payload(payload: bytes) -> bool:
    """Return whether file bytes are a ZIP or TAR-family archive."""
    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        return True
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:*"):
            return True
    except (tarfile.TarError, OSError, EOFError):
        return False


def _scan_archive_metadata(payload: bytes,
                           terms: list[bytes]) -> list[str]:
    failures = _scan_payload(_ARCHIVE_METADATA_LABEL, payload, terms)
    value = payload.decode(errors="surrogateescape").replace("\\", "/")
    if any(prefix.rstrip("/") in value
           for prefix in _PRIVATE_PATH_PREFIXES):
        failures.append(
            f"{_ARCHIVE_METADATA_LABEL}: private-only path <redacted>")
    return failures


def audit_artifact(path: Path, terms: list[bytes]) -> list[str]:
    failures = _scan_payload(
        _ARTIFACT_CONTAINER_LABEL, path.read_bytes(), terms)
    artifact_label = _artifact_label(path, terms)
    for member, payload, regular_file, link_target, metadata in \
            _archive_members(path):
        for metadata_payload in metadata:
            failures.extend(_scan_archive_metadata(metadata_payload, terms))
        if member is None:
            continue
        failures.extend(_scan_name(
            f"{artifact_label}: archive member",
            member, terms))
        private_path = _private_path(member)
        if private_path:
            failures.append(
                f"{artifact_label}: contains private-only path "
                "<redacted>")
        if link_target is not None:
            failures.extend(_scan_link_target(
                "archive link", link_target, terms))
        label = (_PRIVATE_PAYLOAD_LABEL if private_path
                 else f"{artifact_label}:{member}")
        if payload is not None:
            failures.extend(
                _scan_payload(label, payload, terms))
            if regular_file and _is_archive_payload(payload):
                failures.append(
                    f"{_NESTED_ARCHIVE_LABEL}: nested archive payload is "
                    "not allowed")
    return failures


def audit_history(root: Path, terms: list[bytes]) -> list[str]:
    """Scan every local Git blob/commit/tag without revealing matched text."""
    if not terms:
        raise SystemExit("--history requires a non-empty external --denylist")
    # stderr goes to a file, never a second pipe: this loop only drains stdout,
    # so a git that fills a stderr pipe buffer would block forever and hang the
    # release gate instead of failing it.
    error_log = tempfile.TemporaryFile()
    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch-all-objects",
         "--batch=%(objectname) %(objecttype) %(objectsize)"],
        stdout=subprocess.PIPE, stderr=error_log)
    assert process.stdout is not None
    failures: list[str] = []
    while header := process.stdout.readline():
        try:
            oid, kind, raw_size = header.rstrip(b"\n").split(b" ", 2)
            size = int(raw_size)
        except (ValueError, TypeError):
            process.kill()
            raise SystemExit("could not parse git object stream") from None
        payload = process.stdout.read(size)
        process.stdout.read(1)  # batch output terminates each object with LF
        if kind in (b"blob", b"commit", b"tag", b"tree"):
            # Object IDs are hashes of the protected content. Keep them out of
            # CI logs under the same no-derived-identifiers policy.
            label = f"git object ({kind.decode()})"
            failures.extend(_scan_payload(label, payload, terms))
            if kind == b"blob" and _is_archive_payload(payload):
                failures.append(
                    f"{_HISTORY_ARCHIVE_LABEL}: archive payload is not "
                    "allowed")
    process.communicate()
    with error_log:
        error_log.seek(0)
        stderr = error_log.read()
    if process.returncode:
        message = stderr.decode(errors="replace").strip()
        raise SystemExit(f"git history privacy scan failed: {message}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--denylist", type=Path,
        help="external newline-delimited protected terms (never check it in)")
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument(
        "--history", action="store_true",
        help="scan all local Git objects; requires an external denylist")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    terms = _deny_terms(args.denylist)
    failures = audit_tree(root, terms)
    if args.history:
        failures.extend(audit_history(root, terms))
    for artifact in args.artifact:
        failures.extend(audit_artifact(artifact.resolve(), terms))
    if failures:
        print("release privacy audit failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("release privacy audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
