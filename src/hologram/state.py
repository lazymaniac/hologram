from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig, canonical_config_bytes
from .model import Diagnostic, IR_SCHEMA_VERSION
from .scan import ScanEntry, ScanResult, ScanStatus


STATE_FORMAT_VERSION = "hologram-state-v3"
STATE_HEADER_RE = re.compile(
    r"(?:^|[ ·])state=([0-9a-f]{64})(?=$|[ ·])"
)
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


def _feed(hasher: "hashlib._Hash", label: str, value: bytes) -> None:
    label_bytes = label.encode("utf-8")
    hasher.update(len(label_bytes).to_bytes(4, "big"))
    hasher.update(label_bytes)
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _version_bytes(
    versions: Mapping[str, str],
    active_languages: set[str],
    field: str,
) -> bytes:
    active_versions: dict[str, str] = {}
    for language in sorted(active_languages):
        if language not in versions:
            raise ValueError(
                f"{field} missing active language {language!r}"
            )
        version = versions[language]
        if not isinstance(version, str):
            raise TypeError(
                f"{field} version for active language {language!r} "
                "must be a string"
            )
        if not version:
            raise ValueError(
                f"{field} version for active language {language!r} "
                "must not be empty"
            )
        active_versions[language] = version
    return json.dumps(
        active_versions,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _status_bytes(entry: ScanEntry) -> bytes:
    return f"{entry.status.value}\0{entry.reason or ''}".encode("utf-8")


def _reject_duplicate_files(scan_result: ScanResult) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in scan_result.entries:
        if entry.file in seen:
            duplicates.add(entry.file)
        seen.add(entry.file)
    if duplicates:
        duplicate = sorted(duplicates)[0]
        raise ValueError(f"duplicate ScanEntry.file {duplicate!r}")


def compute_state(
    root: Path,
    config: ProjectConfig,
    scan_result: ScanResult,
    *,
    extractor_versions: Mapping[str, str],
    parser_versions: Mapping[str, str],
) -> StateResult:
    del root
    _reject_duplicate_files(scan_result)
    hasher = hashlib.sha256()
    _feed(hasher, "format", STATE_FORMAT_VERSION.encode("utf-8"))
    _feed(hasher, "ir-schema", str(IR_SCHEMA_VERSION).encode("ascii"))
    _feed(hasher, "config", canonical_config_bytes(config))

    language_entries = sorted(
        (
            entry
            for entry in scan_result.entries
            if entry.status in (ScanStatus.INDEXED, ScanStatus.FAILED)
            and entry.language is not None
        ),
        key=lambda entry: entry.file,
    )
    active_languages = {
        entry.language.value for entry in language_entries
    }
    _feed(
        hasher,
        "extractors",
        _version_bytes(
            extractor_versions,
            active_languages,
            "extractor_versions",
        ),
    )
    _feed(
        hasher,
        "parsers",
        _version_bytes(
            parser_versions,
            active_languages,
            "parser_versions",
        ),
    )

    included = sorted(
        (
            entry
            for entry in scan_result.entries
            if entry.status is ScanStatus.FAILED
            or (
                entry.status is ScanStatus.INDEXED
                and entry.language is not None
            )
        ),
        key=lambda entry: entry.file,
    )
    for entry in included:
        _feed(hasher, f"entry-status:{entry.file}", _status_bytes(entry))
        language = entry.language.value if entry.language is not None else ""
        _feed(
            hasher,
            f"entry-language:{entry.file}",
            language.encode("utf-8"),
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
