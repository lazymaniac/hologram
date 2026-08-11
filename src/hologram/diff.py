from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

from . import pipeline
from .analysis import (
    AnalyzedProject,
    DuplicateMatch,
    DuplicateScore,
    ZeroReference,
    analyze_project,
    find_diff_duplicates,
)
from .config import ProjectConfig
from .model import FileIR, SourceSpan, Symbol, SymbolId
from .render import (
    RenderFile,
    RenderIR,
    RenderReexport,
    RenderSymbol,
    project_render_ir,
)

_T = TypeVar("_T")

_GIT_TIMEOUT_SECONDS = 60
_DISCLAIMER = (
    "Static analysis cannot guarantee semantic deadness or authorize deletion; "
    "inspect source and runtime/framework reachability."
)
_SYMBOL_CHANGE_KINDS = frozenset({"added", "changed", "removed"})
_FILE_CHANGE_KINDS = frozenset({"added", "changed", "removed"})
_DEPENDENCY_CHANGE_KINDS = frozenset({"added", "removed"})
_ADVISORY_KINDS = frozenset({"strong-zero", "uncertain-zero", "duplicate-candidate"})
_ADVISORY_KIND_ORDER = {
    "strong-zero": 0,
    "uncertain-zero": 1,
    "duplicate-candidate": 2,
}
_HEX_BYTES = frozenset(b"0123456789abcdef")


class RevisionError(RuntimeError):
    """The requested Git revision cannot produce a complete safe input."""


def _own_tuple(value: tuple[_T, ...] | list[_T], field: str) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list")
    return tuple(value)


def _require_kind(value: object, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} has an unsupported kind")
    return value


@dataclass(frozen=True, slots=True)
class SymbolChange:
    kind: str
    before: RenderSymbol | None
    after: RenderSymbol | None

    def __post_init__(self) -> None:
        _require_kind(self.kind, _SYMBOL_CHANGE_KINDS, "SymbolChange")
        for value in (self.before, self.after):
            if value is not None and not isinstance(value, RenderSymbol):
                raise TypeError("SymbolChange sides must be RenderSymbol or None")
        _validate_change_sides(self.kind, self.before, self.after, "SymbolChange")


@dataclass(frozen=True, slots=True)
class FileTopology:
    path: str
    language: str
    role: str
    module: str | None
    reexports: tuple[RenderReexport, ...]

    def __post_init__(self) -> None:
        for field in ("path", "language", "role"):
            if not isinstance(getattr(self, field), str):
                raise TypeError(f"FileTopology.{field} must be str")
        if self.module is not None and not isinstance(self.module, str):
            raise TypeError("FileTopology.module must be str or None")
        object.__setattr__(
            self,
            "reexports",
            _own_tuple(self.reexports, "reexports"),
        )
        if any(not isinstance(value, RenderReexport) for value in self.reexports):
            raise TypeError("FileTopology.reexports must contain RenderReexport")


@dataclass(frozen=True, slots=True)
class FileChange:
    kind: str
    before: FileTopology | None
    after: FileTopology | None

    def __post_init__(self) -> None:
        _require_kind(self.kind, _FILE_CHANGE_KINDS, "FileChange")
        for value in (self.before, self.after):
            if value is not None and not isinstance(value, FileTopology):
                raise TypeError("FileChange sides must be FileTopology or None")
        _validate_change_sides(self.kind, self.before, self.after, "FileChange")


@dataclass(frozen=True, slots=True)
class DependencyChange:
    kind: str
    dependency: str

    def __post_init__(self) -> None:
        _require_kind(self.kind, _DEPENDENCY_CHANGE_KINDS, "DependencyChange")
        if not isinstance(self.dependency, str) or not self.dependency:
            raise ValueError("dependency must be a nonempty string")


@dataclass(frozen=True, slots=True)
class DiffAdvisory:
    kind: str
    symbol: SymbolId
    span: SourceSpan
    peer: SymbolId | None
    peer_span: SourceSpan | None
    score: DuplicateScore | None

    def __post_init__(self) -> None:
        _require_kind(self.kind, _ADVISORY_KINDS, "DiffAdvisory")
        if not isinstance(self.symbol, SymbolId):
            raise TypeError("DiffAdvisory.symbol must be SymbolId")
        if not isinstance(self.span, SourceSpan):
            raise TypeError("DiffAdvisory.span must be SourceSpan")
        if self.peer is not None and not isinstance(self.peer, SymbolId):
            raise TypeError("DiffAdvisory.peer must be SymbolId or None")
        if self.peer_span is not None and not isinstance(self.peer_span, SourceSpan):
            raise TypeError("DiffAdvisory.peer_span must be SourceSpan or None")
        if self.score is not None and not isinstance(self.score, DuplicateScore):
            raise TypeError("DiffAdvisory.score must be DuplicateScore or None")
        duplicate = self.kind == "duplicate-candidate"
        if duplicate != (
            self.peer is not None
            and self.peer_span is not None
            and self.score is not None
        ):
            raise ValueError("duplicate advisory peer and score ownership is invalid")
        if not duplicate and any(
            value is not None for value in (self.peer, self.peer_span, self.score)
        ):
            raise ValueError("zero advisory cannot own a peer or score")


@dataclass(frozen=True, slots=True)
class DiffInput:
    analyzed: AnalyzedProject
    render_ir: RenderIR

    def __post_init__(self) -> None:
        if not isinstance(self.analyzed, AnalyzedProject):
            raise TypeError("analyzed must be AnalyzedProject")
        if not isinstance(self.render_ir, RenderIR):
            raise TypeError("render_ir must be RenderIR")


@dataclass(frozen=True, slots=True)
class DiffReport:
    symbol_changes: tuple[SymbolChange, ...]
    file_changes: tuple[FileChange, ...]
    dependency_changes: tuple[DependencyChange, ...]
    advisories: tuple[DiffAdvisory, ...]
    text: str

    def __post_init__(self) -> None:
        typed_fields = (
            ("symbol_changes", SymbolChange),
            ("file_changes", FileChange),
            ("dependency_changes", DependencyChange),
            ("advisories", DiffAdvisory),
        )
        for field, expected_type in typed_fields:
            owned = _own_tuple(getattr(self, field), field)
            if any(not isinstance(value, expected_type) for value in owned):
                raise TypeError(
                    f"{field} must contain only {expected_type.__name__} values"
                )
            object.__setattr__(self, field, owned)
        if not isinstance(self.text, str):
            raise TypeError("text must be str")


def _validate_change_sides(
    kind: str,
    before: object | None,
    after: object | None,
    field: str,
) -> None:
    valid = (
        (kind == "added" and before is None and after is not None)
        or (kind == "removed" and before is not None and after is None)
        or (kind == "changed" and before is not None and after is not None)
    )
    if not valid:
        raise ValueError(f"{field} sides do not match its kind")


@dataclass(frozen=True, slots=True)
class _InputIndexes:
    files: dict[str, FileTopology]
    render_symbols: dict[SymbolId, RenderSymbol]
    analyzed_symbols: dict[SymbolId, Symbol]
    spans: dict[SymbolId, SourceSpan]


def _render_topology(file: RenderFile) -> FileTopology:
    return FileTopology(
        file.path,
        file.language,
        file.role,
        file.module,
        file.reexports,
    )


def _project_symbols(files: Sequence[FileIR]) -> dict[SymbolId, Symbol]:
    result: dict[SymbolId, Symbol] = {}
    for file in files:
        path = file.source.file
        for symbol in file.symbols:
            if (
                symbol.file != path
                or symbol.span.file != path
                or symbol.lang is not file.source.language
            ):
                raise ValueError(f"project symbol ownership mismatch: {symbol.id!r}")
            if symbol.id in result:
                raise ValueError(f"duplicate project SymbolId ownership: {symbol.id!r}")
            result[symbol.id] = symbol
    return result


def _validate_input(value: DiffInput, field: str) -> _InputIndexes:
    if not isinstance(value, DiffInput):
        raise TypeError(f"{field} must be DiffInput")
    project_files: dict[str, FileIR] = {}
    for project_file in value.analyzed.project.files:
        path = project_file.source.file
        if path in project_files:
            raise ValueError(f"duplicate project file ownership: {path}")
        project_files[path] = project_file
    project_symbols = _project_symbols(tuple(project_files.values()))

    analyzed_symbols: dict[SymbolId, Symbol] = {}
    for item in value.analyzed.symbols:
        symbol_id = item.symbol.id
        if symbol_id in analyzed_symbols:
            raise ValueError(f"duplicate analyzed SymbolId ownership: {symbol_id!r}")
        analyzed_symbols[symbol_id] = item.symbol
        seen_peers: set[SymbolId] = set()
        for peer in item.duplicate_peers:
            if peer == symbol_id:
                raise ValueError(f"duplicate peer cannot contain self: {symbol_id!r}")
            if peer in seen_peers:
                raise ValueError(f"duplicate peer is repeated: {peer!r}")
            seen_peers.add(peer)
            if peer not in project_symbols:
                raise ValueError(f"duplicate peer ownership is missing: {peer!r}")
    if analyzed_symbols.keys() != project_symbols.keys():
        raise ValueError("analysis and project symbol ownership differs")
    for symbol_id, symbol in project_symbols.items():
        if analyzed_symbols[symbol_id] != symbol:
            raise ValueError(f"analysis symbol ownership mismatch: {symbol_id!r}")
    seen_map_pairs: set[frozenset[SymbolId]] = set()
    for match in value.analyzed.map_duplicates:
        if match.left == match.right:
            raise ValueError(f"map duplicate cannot contain self: {match.left!r}")
        pair = frozenset((match.left, match.right))
        if pair in seen_map_pairs:
            raise ValueError("map duplicate unordered pair is repeated")
        seen_map_pairs.add(pair)
        for symbol_id, span in (
            (match.left, match.left_span),
            (match.right, match.right_span),
        ):
            try:
                owned = project_symbols[symbol_id].span
            except KeyError as error:
                raise ValueError(
                    f"map duplicate symbol ownership is missing: {symbol_id!r}"
                ) from error
            if span != owned:
                raise ValueError(
                    f"map duplicate span ownership mismatch: {symbol_id!r}"
                )

    render_files: dict[str, FileTopology] = {}
    render_symbols: dict[SymbolId, RenderSymbol] = {}
    for render_file in value.render_ir.files:
        if render_file.path in render_files:
            raise ValueError(f"duplicate rendered file ownership: {render_file.path}")
        try:
            owner = project_files[render_file.path]
        except KeyError as error:
            raise ValueError(
                f"rendered file ownership is missing: {render_file.path}"
            ) from error
        if (
            render_file.language != owner.source.language.value
            or render_file.role != owner.source.role.value
            or render_file.module != owner.module
        ):
            raise ValueError(f"rendered file ownership mismatch: {render_file.path}")
        render_files[render_file.path] = _render_topology(render_file)
        for render_symbol in render_file.symbols:
            symbol_id = render_symbol.symbol_id
            if symbol_id.file != render_file.path:
                raise ValueError(f"rendered symbol ownership mismatch: {symbol_id!r}")
            if symbol_id in render_symbols:
                raise ValueError(
                    f"duplicate rendered SymbolId ownership: {symbol_id!r}"
                )
            try:
                owned_symbol = project_symbols[symbol_id]
            except KeyError as error:
                raise ValueError(
                    f"rendered symbol ownership is missing: {symbol_id!r}"
                ) from error
            if (
                render_symbol.source_line != owned_symbol.span.start_line
                or render_symbol.source_column != owned_symbol.span.start_column
            ):
                raise ValueError(
                    f"rendered symbol source provenance mismatch: {symbol_id!r}"
                )
            render_symbols[symbol_id] = render_symbol

    if render_files.keys() != project_files.keys():
        raise ValueError("render and project file ownership differs")
    if render_symbols.keys() != project_symbols.keys():
        raise ValueError("render and analysis symbol ownership differs")
    if len(set(value.render_ir.dependencies)) != len(value.render_ir.dependencies):
        raise ValueError("render dependencies must not contain duplicates")

    return _InputIndexes(
        render_files,
        render_symbols,
        analyzed_symbols,
        {symbol_id: symbol.span for symbol_id, symbol in project_symbols.items()},
    )


def _provenance_key(
    symbol_id: SymbolId,
    span: SourceSpan,
) -> tuple[str, int, int, str, tuple[str, ...], str, str, str]:
    return (
        span.file,
        span.start_line,
        span.start_column,
        symbol_id.language.value,
        symbol_id.container_path,
        symbol_id.kind.value,
        symbol_id.name,
        symbol_id.signature_key,
    )


def _symbol_change_key(
    change: SymbolChange,
) -> tuple[str, int, int, str, tuple[str, ...], str, str, str]:
    selected = change.before if change.after is None else change.after
    if selected is None:
        raise AssertionError("symbol change has no selected side")
    return _provenance_key(
        selected.symbol_id,
        SourceSpan(
            selected.symbol_id.file,
            selected.source_line,
            selected.source_column,
            selected.source_line,
            selected.source_column,
        ),
    )


def _advisory_key(
    advisory: DiffAdvisory,
) -> tuple[object, ...]:
    peer_key: tuple[object, ...] = ()
    if advisory.peer is not None and advisory.peer_span is not None:
        peer_key = _provenance_key(advisory.peer, advisory.peer_span)
    return (
        *_provenance_key(advisory.symbol, advisory.span),
        _ADVISORY_KIND_ORDER[advisory.kind],
        peer_key,
    )


def _symbol_changes(
    before: _InputIndexes,
    after: _InputIndexes,
) -> tuple[SymbolChange, ...]:
    changes: list[SymbolChange] = []
    for symbol_id in before.render_symbols.keys() | after.render_symbols.keys():
        old = before.render_symbols.get(symbol_id)
        new = after.render_symbols.get(symbol_id)
        if old is None:
            changes.append(SymbolChange("added", None, new))
        elif new is None:
            changes.append(SymbolChange("removed", old, None))
        elif old != new:
            changes.append(SymbolChange("changed", old, new))
    return tuple(sorted(changes, key=_symbol_change_key))


def _file_changes(
    before: _InputIndexes,
    after: _InputIndexes,
) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    for path in before.files.keys() | after.files.keys():
        old = before.files.get(path)
        new = after.files.get(path)
        if old is None:
            changes.append(FileChange("added", None, new))
        elif new is None:
            changes.append(FileChange("removed", old, None))
        elif old != new:
            changes.append(FileChange("changed", old, new))
    return tuple(
        sorted(
            changes,
            key=lambda change: (
                change.before.path if change.after is None else change.after.path  # type: ignore[union-attr]
            ),
        )
    )


def _dependency_changes(
    before: RenderIR, after: RenderIR
) -> tuple[DependencyChange, ...]:
    old = set(before.dependencies)
    new = set(after.dependencies)
    changes = [DependencyChange("removed", value) for value in old - new]
    changes.extend(DependencyChange("added", value) for value in new - old)
    return tuple(sorted(changes, key=lambda change: change.dependency))


def _validated_match(
    match: DuplicateMatch,
    indexes: _InputIndexes,
) -> None:
    for symbol_id, span in (
        (match.left, match.left_span),
        (match.right, match.right_span),
    ):
        try:
            owned = indexes.spans[symbol_id]
        except KeyError as error:
            raise ValueError(
                f"duplicate match symbol ownership is missing: {symbol_id!r}"
            ) from error
        if owned != span:
            raise ValueError(f"duplicate match span ownership mismatch: {symbol_id!r}")
    if match.left == match.right:
        raise ValueError("duplicate match cannot compare a symbol with itself")


def _advisories(
    before: _InputIndexes,
    after: _InputIndexes,
    analyzed: AnalyzedProject,
) -> tuple[DiffAdvisory, ...]:
    added = after.render_symbols.keys() - before.render_symbols.keys()
    if not added:
        return ()
    analysis_by_id = {item.symbol.id: item for item in analyzed.symbols}
    result: list[DiffAdvisory] = []
    for symbol_id in added:
        item = analysis_by_id[symbol_id]
        if item.references.zero is ZeroReference.STRONG:
            result.append(
                DiffAdvisory(
                    "strong-zero",
                    symbol_id,
                    item.symbol.span,
                    None,
                    None,
                    None,
                )
            )
        elif item.references.zero is ZeroReference.UNCERTAIN:
            result.append(
                DiffAdvisory(
                    "uncertain-zero",
                    symbol_id,
                    item.symbol.span,
                    None,
                    None,
                    None,
                )
            )

    seen_pairs: set[frozenset[SymbolId]] = set()
    for match in find_diff_duplicates(analyzed):
        _validated_match(match, after)
        pair = frozenset((match.left, match.right))
        if pair in seen_pairs:
            raise ValueError("duplicate broad-match pair")
        seen_pairs.add(pair)
        new_endpoints = tuple(symbol_id for symbol_id in pair if symbol_id in added)
        if not new_endpoints:
            continue
        if len(new_endpoints) == 1:
            symbol_id = new_endpoints[0]
        else:
            symbol_id = min(
                new_endpoints,
                key=lambda item: _provenance_key(item, after.spans[item]),
            )
        peer = match.right if match.left == symbol_id else match.left
        result.append(
            DiffAdvisory(
                "duplicate-candidate",
                symbol_id,
                after.spans[symbol_id],
                peer,
                after.spans[peer],
                match.score,
            )
        )
    return tuple(sorted(result, key=_advisory_key))


def _visible(value: str) -> str:
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    fragments: list[str] = []
    for character in value:
        escaped = escapes.get(character)
        if escaped is not None:
            fragments.append(escaped)
            continue
        codepoint = ord(character)
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or codepoint in {0x2028, 0x2029}
        ):
            fragments.append(f"\\u{codepoint:04x}")
        else:
            fragments.append(character)
    return "".join(fragments)


def _location(span: SourceSpan) -> str:
    return f"{_visible(span.file)}:{span.start_line}"


def _symbol_location(symbol: RenderSymbol) -> str:
    return (
        f"{_visible(symbol.symbol_id.file)}:{symbol.source_line} "
        f"{_visible(symbol.symbol_id.name)}"
    )


def _changed_symbol_fields(before: RenderSymbol, after: RenderSymbol) -> str:
    names = tuple(
        field.name
        for field in dataclasses.fields(RenderSymbol)
        if field.name != "symbol_id"
        and getattr(before, field.name) != getattr(after, field.name)
    )
    return ",".join(names)


def _module(value: str | None) -> str:
    return "∅" if value is None else _visible(value)


def _reexports(value: tuple[RenderReexport, ...]) -> str:
    return _visible(repr(value))


def _report_text(
    symbol_changes: tuple[SymbolChange, ...],
    file_changes: tuple[FileChange, ...],
    dependency_changes: tuple[DependencyChange, ...],
    advisories: tuple[DiffAdvisory, ...],
) -> str:
    lines = ["Hologram semantic diff", "symbols:"]
    if not symbol_changes:
        lines.append("  (none)")
    for symbol_change in symbol_changes:
        selected_symbol = (
            symbol_change.before if symbol_change.after is None else symbol_change.after
        )
        if selected_symbol is None:
            raise AssertionError("symbol change has no selected side")
        prefix = {"added": "+", "changed": "~", "removed": "-"}[symbol_change.kind]
        line = f"  {prefix} {_symbol_location(selected_symbol)}"
        if symbol_change.kind == "changed":
            if symbol_change.before is None or symbol_change.after is None:
                raise AssertionError("changed symbol is missing one side")
            line += " fields=" + _changed_symbol_fields(
                symbol_change.before,
                symbol_change.after,
            )
        lines.append(line)

    lines.append("files:")
    if not file_changes:
        lines.append("  (none)")
    for file_change in file_changes:
        selected_file = (
            file_change.before if file_change.after is None else file_change.after
        )
        if selected_file is None:
            raise AssertionError("file change has no selected side")
        if file_change.kind in {"added", "removed"}:
            prefix = "+" if file_change.kind == "added" else "-"
            lines.append(
                f"  {prefix} file {_visible(selected_file.path)} "
                f"language={_visible(selected_file.language)} "
                f"role={_visible(selected_file.role)} "
                f"module={_module(selected_file.module)} "
                f"reexports={_reexports(selected_file.reexports)}"
            )
            continue
        if file_change.before is None or file_change.after is None:
            raise AssertionError("changed file is missing one side")
        for field in ("language", "role", "module", "reexports"):
            old = getattr(file_change.before, field)
            new = getattr(file_change.after, field)
            if old == new:
                continue
            if field == "module":
                old = _module(old)
                new = _module(new)
            elif field == "reexports":
                old = _reexports(old)
                new = _reexports(new)
            else:
                old = _visible(old)
                new = _visible(new)
            lines.append(f"  ~ {field} {_visible(selected_file.path)}: {old}→{new}")

    lines.append("dependencies:")
    if not dependency_changes:
        lines.append("  (none)")
    for dependency_change in dependency_changes:
        prefix = "+" if dependency_change.kind == "added" else "-"
        lines.append(f"  {prefix} dependency {_visible(dependency_change.dependency)}")

    lines.append("new-code advisories:")
    if not advisories:
        lines.append("  (none)")
    for advisory in advisories:
        if advisory.kind == "strong-zero":
            lines.append(
                f"  new strong ×0: {_location(advisory.span)} "
                f"{_visible(advisory.symbol.name)}"
            )
        elif advisory.kind == "uncertain-zero":
            lines.append(
                f"  new uncertain ×0?: {_location(advisory.span)} "
                f"{_visible(advisory.symbol.name)}"
            )
        else:
            if (
                advisory.peer is None
                or advisory.peer_span is None
                or advisory.score is None
            ):
                raise AssertionError("duplicate advisory is incomplete")
            score = advisory.score
            lines.append(
                f"  new duplicate candidate: {_location(advisory.span)} "
                f"{_visible(advisory.symbol.name)} ↔ "
                f"{_location(advisory.peer_span)} "
                f"{_visible(advisory.peer.name)} "
                f"ast={score.ast:.2f} total={score.total:.2f} "
                f"control_flow={score.control_flow:.2f} calls={score.calls:.2f} "
                f"names={score.names:.2f} exact={'true' if score.exact else 'false'}"
            )
    lines.append(_DISCLAIMER)
    return "\n".join(lines) + "\n"


def compare_projects(before: DiffInput, after: DiffInput) -> DiffReport:
    """Compare canonical semantic models and derive new-code advisories."""

    old = _validate_input(before, "before")
    new = _validate_input(after, "after")
    symbol_changes = _symbol_changes(old, new)
    file_changes = _file_changes(old, new)
    dependency_changes = _dependency_changes(before.render_ir, after.render_ir)
    advisories = _advisories(old, new, after.analyzed)
    return DiffReport(
        symbol_changes,
        file_changes,
        dependency_changes,
        advisories,
        _report_text(
            symbol_changes,
            file_changes,
            dependency_changes,
            advisories,
        ),
    )


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    oid: bytes
    mode: int


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


def _git_run(
    root: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    argv = ["git", "-C", os.fspath(root), *arguments]
    try:
        result = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise RevisionError(f"Git command failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RevisionError(
            detail or f"Git command exited {result.returncode}: {' '.join(arguments)}"
        )
    return result


def _valid_oid(value: bytes) -> bool:
    return len(value) in {40, 64} and all(byte in _HEX_BYTES for byte in value)


def _resolve_revision(root: Path, rev: str) -> bytes:
    result = _git_run(
        root,
        ("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"),
    )
    lines = result.stdout.splitlines()
    if len(lines) != 1 or not _valid_oid(lines[0]):
        raise RevisionError("git rev-parse returned a malformed commit object ID")
    return lines[0]


def _normalized_tree_path(value: bytes) -> str:
    try:
        path = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RevisionError("committed path is not valid UTF-8") from error
    pure = PurePosixPath(path)
    if (
        not path
        or not pure.parts
        or pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or "\\" in path
        or path != pure.as_posix()
    ):
        raise RevisionError(f"unsafe committed path: {path!r}")
    return path


def _parse_tree(output: bytes) -> tuple[_TreeEntry, ...]:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise RevisionError("git ls-tree returned an unterminated record")
    entries: list[_TreeEntry] = []
    paths: set[str] = set()
    file_prefixes: set[str] = set()
    directory_prefixes: set[str] = set()
    casefold_spellings: dict[str, str] = {}
    for record in output[:-1].split(b"\0"):
        if b"\t" not in record:
            raise RevisionError("git ls-tree returned malformed metadata")
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.split(b" ")
        if len(fields) != 3:
            raise RevisionError("git ls-tree returned malformed metadata")
        raw_mode, object_type, oid = fields
        if object_type != b"blob" or raw_mode not in {b"100644", b"100755"}:
            detail = metadata.decode("ascii", errors="replace")
            raise RevisionError(f"unsupported tree entry: {detail}")
        if not _valid_oid(oid):
            raise RevisionError("git ls-tree returned a malformed object ID")
        path = _normalized_tree_path(raw_path)
        if path in paths:
            raise RevisionError(f"duplicate committed path: {path}")
        parts = PurePosixPath(path).parts
        prefixes = tuple(
            PurePosixPath(*parts[:index]).as_posix() for index in range(1, len(parts))
        )
        for spelling in (*prefixes, path):
            folded = spelling.casefold()
            previous = casefold_spellings.get(folded)
            if previous is not None and previous != spelling:
                raise RevisionError(
                    "case-insensitive committed path or prefix collision: "
                    f"{previous!r} and {spelling!r}"
                )
            casefold_spellings[folded] = spelling
        if path in directory_prefixes or set(prefixes) & file_prefixes:
            raise RevisionError(f"committed file/directory collision: {path}")
        paths.add(path)
        file_prefixes.add(path)
        directory_prefixes.update(prefixes)
        entries.append(_TreeEntry(path, oid, 0o755 if raw_mode == b"100755" else 0o644))
    return tuple(entries)


def _tree_entries(root: Path, commit: bytes) -> tuple[_TreeEntry, ...]:
    result = _git_run(
        root,
        ("ls-tree", "-rz", commit.decode("ascii"), "--", "."),
    )
    return _parse_tree(result.stdout)


def _parse_batch(
    output: bytes,
    requested: tuple[bytes, ...],
) -> dict[bytes, bytes]:
    offset = 0
    blobs: dict[bytes, bytes] = {}
    for expected in requested:
        newline = output.find(b"\n", offset)
        if newline < 0:
            raise RevisionError("git cat-file batch header is truncated")
        header = output[offset:newline]
        fields = header.split(b" ")
        if len(fields) != 3:
            raise RevisionError("git cat-file batch header is malformed")
        oid, object_type, raw_size = fields
        if oid != expected or object_type != b"blob" or not raw_size.isdigit():
            raise RevisionError("git cat-file batch object ownership is invalid")
        size = int(raw_size)
        start = newline + 1
        end = start + size
        if end >= len(output) or output[end : end + 1] != b"\n":
            raise RevisionError("git cat-file batch body is truncated")
        blobs[expected] = output[start:end]
        offset = end + 1
    if offset != len(output):
        raise RevisionError("git cat-file batch returned trailing data")
    return blobs


def _read_blobs(root: Path, entries: tuple[_TreeEntry, ...]) -> dict[bytes, bytes]:
    requested = tuple(sorted({entry.oid for entry in entries}))
    if not requested:
        return {}
    result = _git_run(
        root,
        ("cat-file", "--batch"),
        input_bytes=b"".join(oid + b"\n" for oid in requested),
    )
    return _parse_batch(result.stdout, requested)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while materializing revision")
        remaining = remaining[written:]


def _materialize(
    root: Path,
    entries: tuple[_TreeEntry, ...],
    blobs: dict[bytes, bytes],
) -> None:
    for entry in sorted(entries, key=lambda item: os.fsencode(item.path)):
        target = root.joinpath(*PurePosixPath(entry.path).parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags, 0o600)
            failure: BaseException | None = None
            try:
                _write_all(descriptor, blobs[entry.oid])
                os.fchmod(descriptor, entry.mode)
            except BaseException as error:
                failure = error
                raise
            finally:
                try:
                    os.close(descriptor)
                except OSError as error:
                    if failure is None:
                        raise
                    failure.add_note(f"closing materialized file failed: {error}")
        except (OSError, KeyError) as error:
            raise RevisionError(
                f"cannot materialize committed path {entry.path}: {error}"
            ) from error


def _revision_input(root: Path, config: ProjectConfig) -> DiffInput:
    snapshot = pipeline.build_project(root, config)
    snapshot.require_complete()
    try:
        analyzed = analyze_project(
            snapshot.project,
            snapshot.resolution,
            hot_threshold=config.hot_threshold,
        )
        render_ir = project_render_ir(
            analyzed,
            state=snapshot.state.value,
            hot_threshold=config.hot_threshold,
        )
        result = DiffInput(analyzed, render_ir)
        _validate_input(result, "revision")
        return result
    except (TypeError, ValueError) as error:
        raise RevisionError(f"revision model is invalid: {error}") from error


def analyze_revision(root: Path, config: ProjectConfig, rev: str) -> DiffInput:
    """Analyze one committed subtree without touching the selected worktree."""

    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if not isinstance(config, ProjectConfig):
        raise TypeError("config must be ProjectConfig")
    if not isinstance(rev, str):
        raise TypeError("rev must be str")
    try:
        selected = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RevisionError(f"invalid revision root {root}: {error}") from error
    if not selected.is_dir():
        raise RevisionError(f"revision root is not a directory: {selected}")

    commit = _resolve_revision(selected, rev)
    entries = _tree_entries(selected, commit)
    blobs = _read_blobs(selected, entries)
    try:
        temporary = Path(tempfile.mkdtemp(prefix="hologram-diff-"))
    except OSError as error:
        raise RevisionError(f"cannot create revision workspace: {error}") from error

    primary: BaseException | None = None
    try:
        _materialize(temporary, entries, blobs)
        return _revision_input(temporary, config)
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            shutil.rmtree(temporary)
        except OSError as error:
            if primary is not None:
                primary.add_note(f"revision workspace cleanup failed: {error}")
            else:
                raise RevisionError(
                    f"revision workspace cleanup failed: {error}"
                ) from error


__all__ = [
    "DependencyChange",
    "DiffAdvisory",
    "DiffInput",
    "DiffReport",
    "FileChange",
    "FileTopology",
    "RevisionError",
    "SymbolChange",
    "analyze_revision",
    "compare_projects",
]
