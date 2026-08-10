from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TypeVar

from .analysis import AnalyzedProject, AnalyzedSymbol, ZeroReference
from .model import (
    FileIR,
    ReferenceConfidence,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
)
from .resolve import ResolutionStatus

_T = TypeVar("_T")


def _owned_tuple(value: tuple[_T, ...] | list[_T], field: str) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class RenderSymbol:
    symbol_id: SymbolId
    source_line: int
    source_column: int
    visibility: str
    signature: str
    parameters: tuple[str, ...]
    returns: str | None
    annotations: tuple[str, ...]
    modifiers: tuple[str, ...]
    components: tuple[str, ...]
    supers: tuple[str, ...]
    permits: tuple[str, ...]
    ordered_calls: tuple[str, ...]
    throws: tuple[str, ...]
    behaviors: tuple[str, ...]
    body_lines: int
    markers: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "parameters",
            "annotations",
            "modifiers",
            "components",
            "supers",
            "permits",
            "ordered_calls",
            "throws",
            "behaviors",
            "markers",
        ):
            object.__setattr__(self, field, _owned_tuple(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class RenderIntern:
    alias: str
    value: str


@dataclass(frozen=True, slots=True)
class RenderReexport:
    module: str
    name: str | None
    alias: str | None
    wildcard: bool


@dataclass(frozen=True, slots=True)
class RenderFile:
    path: str
    language: str
    role: str
    module: str | None
    reexports: tuple[RenderReexport, ...]
    symbols: tuple[RenderSymbol, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reexports",
            _owned_tuple(self.reexports, "reexports"),
        )
        object.__setattr__(self, "symbols", _owned_tuple(self.symbols, "symbols"))


@dataclass(frozen=True, slots=True)
class RenderIR:
    schema_version: int
    state: str
    interns: tuple[RenderIntern, ...]
    dependencies: tuple[str, ...]
    files: tuple[RenderFile, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "interns", _owned_tuple(self.interns, "interns"))
        object.__setattr__(
            self,
            "dependencies",
            _owned_tuple(self.dependencies, "dependencies"),
        )
        object.__setattr__(self, "files", _owned_tuple(self.files, "files"))


@dataclass(frozen=True, slots=True)
class _ProjectIndexes:
    files: tuple[FileIR, ...]
    files_by_path: dict[str, FileIR]
    symbols: tuple[Symbol, ...]
    symbols_by_id: dict[SymbolId, Symbol]
    analyzed_by_id: dict[SymbolId, AnalyzedSymbol]


_STATE = re.compile(r"[0-9a-f]{64}\Z")
_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)


def _symbol_key(
    symbol: Symbol,
) -> tuple[str, str, tuple[str, ...], str, str, str, int, int]:
    symbol_id = symbol.id
    return (
        symbol_id.language.value,
        symbol_id.file,
        symbol_id.container_path,
        symbol_id.kind.value,
        symbol_id.name,
        symbol_id.signature_key,
        symbol.span.start_line,
        symbol.span.start_column,
    )


def _project_indexes(analyzed: AnalyzedProject) -> _ProjectIndexes:
    files_by_path: dict[str, FileIR] = {}
    symbols_by_id: dict[SymbolId, Symbol] = {}

    for file_ir in analyzed.project.files:
        path = file_ir.source.file
        if path in files_by_path:
            raise ValueError(f"duplicate project file: {path}")
        files_by_path[path] = file_ir
        for symbol in file_ir.symbols:
            if (
                symbol.file != path
                or symbol.span.file != path
                or symbol.lang is not file_ir.source.language
            ):
                raise ValueError(
                    f"symbol ownership mismatch: {symbol.id!r} is in {path}"
                )
            if symbol.id in symbols_by_id:
                raise ValueError(f"duplicate SymbolId: {symbol.id!r}")
            symbols_by_id[symbol.id] = symbol

    analyzed_by_id: dict[SymbolId, AnalyzedSymbol] = {}
    for item in analyzed.symbols:
        symbol_id = item.symbol.id
        if symbol_id in analyzed_by_id:
            raise ValueError(f"duplicate SymbolId in analysis: {symbol_id!r}")
        analyzed_by_id[symbol_id] = item

    if symbols_by_id.keys() != analyzed_by_id.keys():
        raise ValueError("analysis and project symbol ownership differs")
    for symbol_id, symbol in symbols_by_id.items():
        if analyzed_by_id[symbol_id].symbol != symbol:
            raise ValueError(f"analysis symbol ownership mismatch: {symbol_id!r}")
    for item in analyzed.symbols:
        for peer in item.duplicate_peers:
            if peer not in symbols_by_id:
                raise ValueError(f"duplicate peer ownership is missing: {peer!r}")

    return _ProjectIndexes(
        tuple(sorted(files_by_path.values(), key=lambda item: item.source.file)),
        files_by_path,
        tuple(sorted(symbols_by_id.values(), key=_symbol_key)),
        symbols_by_id,
        analyzed_by_id,
    )


def _qualified_name(symbol_id: SymbolId) -> str:
    if symbol_id.container_path:
        return f"{'.'.join(symbol_id.container_path)}.{symbol_id.name}"
    return symbol_id.name


def _display_names(symbols: tuple[Symbol, ...]) -> dict[SymbolId, str]:
    candidates: dict[SymbolId, tuple[str, ...]] = {}
    order_by_id = {symbol.id: _symbol_key(symbol) for symbol in symbols}
    for symbol in symbols:
        symbol_id = symbol.id
        qualified = _qualified_name(symbol_id)
        parts = PurePosixPath(symbol.file).parts
        ladder = [symbol.name, qualified]
        ladder.extend(
            f"{'/'.join(parts[-length:])}:{qualified}"
            for length in range(1, len(parts) + 1)
        )
        ladder.append(
            f"{symbol.file}:{qualified}|{symbol.kind.value}|{symbol_id.signature_key}"
        )
        candidates[symbol_id] = tuple(dict.fromkeys(ladder))

    result: dict[SymbolId, str] = {}
    reserved: set[str] = set()
    positions = {symbol.id: 0 for symbol in symbols}
    pending = {symbol.id for symbol in symbols}
    while pending:
        current = {
            symbol_id: candidates[symbol_id][positions[symbol_id]]
            for symbol_id in pending
        }
        counts = Counter(current.values())
        progress = False
        for symbol_id in sorted(pending, key=order_by_id.__getitem__):
            value = current[symbol_id]
            if counts[value] == 1 and value not in reserved:
                result[symbol_id] = value
                reserved.add(value)
                pending.remove(symbol_id)
                progress = True
            elif positions[symbol_id] + 1 < len(candidates[symbol_id]):
                positions[symbol_id] += 1
                progress = True
        if not progress:
            collision = min(current.values())
            raise ValueError(f"final display collision: {collision}")
    return result


def _span_key(span: SourceSpan) -> tuple[int, int, int, int]:
    return (
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
    )


def _module_key(file_ir: FileIR) -> str:
    if file_ir.module is not None and file_ir.module.strip():
        return file_ir.module
    return PurePosixPath(file_ir.source.file).parent.as_posix()


def _reexports(file_ir: FileIR) -> tuple[RenderReexport, ...]:
    facts = sorted(
        (fact for fact in file_ir.imports if fact.reexport),
        key=lambda fact: (
            _span_key(fact.span),
            fact.module,
            fact.name or "",
            fact.alias or "",
            fact.wildcard,
        ),
    )
    seen: set[tuple[str, str | None, str | None, bool]] = set()
    result: list[RenderReexport] = []
    for fact in facts:
        if fact.span.file != file_ir.source.file:
            raise ValueError(
                f"reexport ownership mismatch: {fact.span.file} in "
                f"{file_ir.source.file}"
            )
        value = (fact.module, fact.name, fact.alias, fact.wildcard)
        if value in seen:
            continue
        seen.add(value)
        result.append(RenderReexport(*value))
    return tuple(result)


def _markers(item: AnalyzedSymbol, hot_threshold: int) -> tuple[str, ...]:
    result: list[str] = []
    fan_in = len(item.references.production_files)
    if fan_in >= hot_threshold:
        result.append(f"×{fan_in}")
    elif item.references.zero is ZeroReference.STRONG:
        result.append("×0")
    elif item.references.zero is ZeroReference.UNCERTAIN:
        result.append("×0?")
    if item.references.test_files:
        result.append("✓")
    if item.duplicate_peers:
        result.append(f"≈{len(item.duplicate_peers)}")
    return tuple(result)


def _resolution_projection(
    analyzed: AnalyzedProject,
    indexes: _ProjectIndexes,
    displays: dict[SymbolId, str],
) -> tuple[
    dict[SymbolId, tuple[str, ...]],
    dict[SymbolId, tuple[str, ...]],
    tuple[str, ...],
]:
    calls: dict[SymbolId, list[tuple[tuple[int, int, int, int], SymbolId]]] = (
        defaultdict(list)
    )
    behavior_owners: dict[SymbolId, set[SymbolId]] = defaultdict(set)
    dependencies: set[str] = set()

    def owned_file(path: str, fact_name: str) -> FileIR:
        try:
            return indexes.files_by_path[path]
        except KeyError as error:
            raise ValueError(
                f"{fact_name} source ownership is missing: {path}"
            ) from error

    def owned_symbol(symbol_id: SymbolId, fact_name: str) -> Symbol:
        try:
            return indexes.symbols_by_id[symbol_id]
        except KeyError as error:
            raise ValueError(
                f"{fact_name} target ownership is missing: {symbol_id!r}"
            ) from error

    def add_dependency(source_path: str, target_path: str) -> None:
        source = owned_file(source_path, "resolved dependency")
        target = owned_file(target_path, "resolved dependency")
        if (
            source.source.role is not SourceRole.PRODUCTION
            or target.source.role is not SourceRole.PRODUCTION
        ):
            return
        source_module = _module_key(source)
        target_module = _module_key(target)
        if source_module != target_module:
            dependencies.add(f"{source_module}→{target_module}")

    for resolved_import in analyzed.resolution.imports:
        if resolved_import.status is not ResolutionStatus.RESOLVED:
            continue
        source = owned_file(resolved_import.source_file, "resolved import")
        if resolved_import.fact.span.file != source.source.file:
            raise ValueError("resolved import source ownership mismatch")
        for target_id in resolved_import.target_symbols:
            owned_symbol(target_id, "resolved import")
        for target_file in resolved_import.target_files:
            add_dependency(source.source.file, target_file)

    for resolved_call in analyzed.resolution.calls:
        if resolved_call.status is not ResolutionStatus.RESOLVED:
            continue
        caller_id = resolved_call.fact.caller
        try:
            caller = indexes.symbols_by_id[caller_id]
        except KeyError as error:
            raise ValueError(
                f"resolved call caller ownership is missing: {caller_id!r}"
            ) from error
        if resolved_call.fact.span.file != caller.file:
            raise ValueError("resolved call caller ownership mismatch")
        if resolved_call.target is None:
            raise ValueError("resolved call target is missing")
        try:
            target = indexes.symbols_by_id[resolved_call.target]
        except KeyError as error:
            raise ValueError(
                f"resolved call target ownership is missing: {resolved_call.target!r}"
            ) from error
        calls[caller_id].append((_span_key(resolved_call.fact.span), target.id))
        add_dependency(caller.file, target.file)
        if (
            indexes.files_by_path[caller.file].source.role is SourceRole.TEST
            and caller.kind in _CALLABLE_KINDS
            and indexes.files_by_path[target.file].source.role is SourceRole.PRODUCTION
        ):
            behavior_owners[target.id].add(caller.id)

    for resolved_reference in analyzed.resolution.references:
        if resolved_reference.status is not ResolutionStatus.RESOLVED:
            continue
        source = owned_file(
            resolved_reference.fact.span.file,
            "resolved reference",
        )
        if resolved_reference.target is None:
            raise ValueError("resolved reference target is missing")
        target = owned_symbol(resolved_reference.target, "resolved reference")
        owner: Symbol | None = None
        if resolved_reference.fact.owner is not None:
            try:
                owner = indexes.symbols_by_id[resolved_reference.fact.owner]
            except KeyError as error:
                raise ValueError(
                    "resolved reference owner ownership is missing: "
                    f"{resolved_reference.fact.owner!r}"
                ) from error
            if owner.file != source.source.file:
                raise ValueError("resolved reference owner ownership mismatch")
        add_dependency(source.source.file, target.file)
        if (
            resolved_reference.fact.confidence is ReferenceConfidence.DEFINITE
            and owner is not None
            and source.source.role is SourceRole.TEST
            and owner.kind in _CALLABLE_KINDS
            and indexes.files_by_path[target.file].source.role is SourceRole.PRODUCTION
        ):
            behavior_owners[target.id].add(owner.id)

    rendered_calls = {
        owner: tuple(
            displays[target]
            for _, target in sorted(
                entries,
                key=lambda entry: (
                    entry[0],
                    _symbol_key(indexes.symbols_by_id[entry[1]]),
                ),
            )
        )
        for owner, entries in calls.items()
    }
    rendered_behaviors = {
        target: tuple(sorted(displays[owner] for owner in owners))
        for target, owners in behavior_owners.items()
    }
    return rendered_calls, rendered_behaviors, tuple(sorted(dependencies))


def project_render_ir(
    analyzed: AnalyzedProject,
    *,
    state: str,
    hot_threshold: int,
) -> RenderIR:
    if not isinstance(state, str) or _STATE.fullmatch(state) is None:
        raise ValueError("state must be exactly 64 lowercase hexadecimal digits")
    if isinstance(hot_threshold, bool) or not isinstance(hot_threshold, int):
        raise TypeError("hot_threshold must be an integer")
    if hot_threshold < 1:
        raise ValueError("hot_threshold must be at least 1")

    indexes = _project_indexes(analyzed)
    displays = _display_names(indexes.symbols)
    calls, behaviors, dependencies = _resolution_projection(
        analyzed,
        indexes,
        displays,
    )

    rendered_files: list[RenderFile] = []
    for file_ir in indexes.files:
        symbols = sorted(file_ir.symbols, key=_symbol_key)
        rendered_symbols = tuple(
            RenderSymbol(
                symbol.id,
                symbol.span.start_line,
                symbol.span.start_column,
                symbol.visibility.value,
                symbol.signature,
                symbol.params,
                symbol.returns,
                symbol.annotations,
                symbol.modifiers,
                symbol.components,
                symbol.supers,
                symbol.permits,
                calls.get(symbol.id, ()),
                symbol.raises,
                behaviors.get(symbol.id, ()),
                symbol.body_lines,
                _markers(indexes.analyzed_by_id[symbol.id], hot_threshold),
            )
            for symbol in symbols
        )
        rendered_files.append(
            RenderFile(
                file_ir.source.file,
                file_ir.source.language.value,
                file_ir.source.role.value,
                file_ir.module,
                _reexports(file_ir),
                rendered_symbols,
            )
        )

    return RenderIR(2, state, (), dependencies, tuple(rendered_files))


__all__ = [
    "RenderFile",
    "RenderIR",
    "RenderIntern",
    "RenderReexport",
    "RenderSymbol",
    "project_render_ir",
]
