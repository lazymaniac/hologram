from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig, canonical_config_bytes
from .model import Diagnostic
from .scan import ScanEntry, ScanResult, ScanStatus

_STATE_DOMAIN = b"hologram-state"
STATE_HEADER_RE = re.compile(r"(?:^|[ ·])state=([0-9a-f]{64})(?=$|[ ·])")
_STATE_HEADER_MAX_BYTES = 4096  # Includes a trailing newline when present.


@dataclass(frozen=True, slots=True)
class StateResult:
    value: str
    diagnostics: tuple[Diagnostic, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostics, (tuple, list)):
            raise TypeError("diagnostics must be a tuple or list")
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


def _feed(hasher: hashlib._Hash, label: str, value: bytes) -> None:
    label_bytes = label.encode("utf-8")
    hasher.update(len(label_bytes).to_bytes(4, "big"))
    hasher.update(label_bytes)
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _status_bytes(entry: ScanEntry) -> bytes:
    return f"{entry.status.value}\0{entry.reason or ''}".encode()


def _reject_duplicate_files(scan_result: ScanResult) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in scan_result.entries:
        if entry.file in seen:
            duplicates.add(entry.file)
        seen.add(entry.file)
    if duplicates:
        duplicate = min(duplicates)
        raise ValueError(f"duplicate ScanEntry.file {duplicate!r}")


def compute_state(
    root: Path,
    config: ProjectConfig,
    scan_result: ScanResult,
) -> StateResult:
    del root
    _reject_duplicate_files(scan_result)
    hasher = hashlib.sha256()
    _feed(hasher, "format", _STATE_DOMAIN)
    _feed(hasher, "config", canonical_config_bytes(config))

    included = sorted(
        (
            entry
            for entry in scan_result.entries
            if entry.status is ScanStatus.FAILED
            or (entry.status is ScanStatus.INDEXED and entry.language is not None)
        ),
        key=lambda entry: entry.file,
    )
    for entry in included:
        _feed(hasher, f"entry-status:{entry.file}", _status_bytes(entry))
        language_value = entry.language.value if entry.language is not None else ""
        _feed(
            hasher,
            f"entry-language:{entry.file}",
            language_value.encode("utf-8"),
        )

    for entry in included:
        if entry.source is None:
            continue
        _feed(hasher, "source-path", entry.source.file.encode("utf-8"))
        _feed(hasher, "source-role", entry.source.role.value.encode("utf-8"))
        _feed(hasher, "source-bytes", entry.source.raw)

    return StateResult(
        hasher.hexdigest(),
        scan_result.diagnostics,
        scan_result.complete,
    )


def read_digest_state(path: Path) -> str | None:
    try:
        with Path(path).open("rb") as artifact:
            header_bytes = artifact.readline(_STATE_HEADER_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(header_bytes) > _STATE_HEADER_MAX_BYTES:
        return None
    try:
        header = header_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    match = STATE_HEADER_RE.search(header)
    return match.group(1) if match is not None else None
