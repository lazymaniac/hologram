from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

from .model import (
    Language,
    ProjectIR,
    ReferenceConfidence,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)
from .resolve import ResolutionResult, ResolutionStatus, canonical_type_key


class ZeroReference(StrEnum):
    NONE = "none"
    STRONG = "strong"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ReferenceFacts:
    production_files: tuple[PurePosixPath, ...]
    possible_files: tuple[PurePosixPath, ...]
    test_files: tuple[PurePosixPath, ...]
    generated_files: tuple[PurePosixPath, ...]
    zero: ZeroReference


@dataclass(frozen=True, slots=True)
class BodyProfile:
    semantic_tokens: tuple[str, ...]
    ast_shingles: frozenset[tuple[str, str, str, str, str]]
    control_flow: tuple[str, ...]
    resolved_calls: frozenset[SymbolId]
    name_tokens: frozenset[str]
    arity: int
    return_key: str
    semantic_size: int
    excluded_reason: str | None


@dataclass(frozen=True, slots=True)
class DuplicateScore:
    ast: float
    control_flow: float
    calls: float
    names: float
    total: float
    exact: bool


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    left: SymbolId
    right: SymbolId
    left_span: SourceSpan
    right_span: SourceSpan
    score: DuplicateScore


@dataclass(frozen=True, slots=True)
class AnalyzedSymbol:
    symbol: Symbol
    references: ReferenceFacts
    body: BodyProfile | None
    duplicate_peers: tuple[SymbolId, ...]


@dataclass(frozen=True, slots=True)
class AnalyzedProject:
    project: ProjectIR
    resolution: ResolutionResult
    symbols: tuple[AnalyzedSymbol, ...]
    map_duplicates: tuple[DuplicateMatch, ...]


MAP_AST_MIN = 0.88
MAP_TOTAL_MIN = 0.90
DIFF_AST_MIN = 0.72
DIFF_TOTAL_MIN = 0.78


@dataclass(slots=True)
class _ReferenceEvidence:
    production_files: set[PurePosixPath] = field(default_factory=set)
    possible_files: set[PurePosixPath] = field(default_factory=set)
    test_files: set[PurePosixPath] = field(default_factory=set)
    generated_files: set[PurePosixPath] = field(default_factory=set)
    definite_production: bool = False
    possible_production: bool = False
    reexported: bool = False


def _symbol_order(
    symbol: Symbol,
) -> tuple[str, str, tuple[str, ...], str, str, str]:
    sid = symbol.id
    return (
        sid.language.value,
        sid.file,
        sid.container_path,
        sid.kind.value,
        sid.name,
        sid.signature_key,
    )


def _sorted_paths(paths: set[PurePosixPath]) -> tuple[PurePosixPath, ...]:
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def _intrinsically_reachable(symbol: Symbol) -> bool:
    if symbol.annotations:
        return True
    modifiers = {modifier.strip().casefold() for modifier in symbol.modifiers}
    if modifiers & {"impl", "implementation", "implements", "override"}:
        return True
    sid = symbol.id
    if (
        sid.language in {Language.C, Language.CPP}
        and sid.kind is SymbolKind.FUNCTION
        and not sid.container_path
        and sid.name == "main"
    ):
        return True
    if len(symbol.params) != 1:
        return False
    parameter = canonical_type_key(symbol.params[0]).removeprefix("java.lang.")
    return (
        sid.language is Language.JAVA
        and sid.kind is SymbolKind.METHOD
        and sid.name == "main"
        and "static" in modifiers
        and symbol.visibility is Visibility.PUBLIC
        and parameter in {"String[]", "String..."}
    )


def _reference_index(
    project: ProjectIR,
    resolution: ResolutionResult,
) -> dict[SymbolId, ReferenceFacts]:
    """Fold resolved definite, possible/dynamic, and test edges by target ID.

    The defining file is omitted only from displayed production and possible
    fan-in. Its evidence is retained for zero classification. Test and generated
    origins stay in their own evidence channels, and ambiguous candidates are
    possible rather than definite references.
    """
    symbols = tuple(symbol for file_ir in project.files for symbol in file_ir.symbols)
    evidence = {symbol.id: _ReferenceEvidence() for symbol in symbols}
    roles = {file_ir.source.file: file_ir.source.role for file_ir in project.files}

    def record(target: SymbolId, source_file: str, *, definite: bool) -> None:
        target_evidence = evidence.get(target)
        role = roles.get(source_file)
        if target_evidence is None or role is None:
            return
        source_path = PurePosixPath(source_file)
        if role is SourceRole.TEST:
            target_evidence.test_files.add(source_path)
        elif role is SourceRole.GENERATED:
            target_evidence.generated_files.add(source_path)
        elif definite:
            target_evidence.definite_production = True
            if source_file != target.file:
                target_evidence.production_files.add(source_path)
        else:
            target_evidence.possible_production = True
            if source_file != target.file:
                target_evidence.possible_files.add(source_path)

    for resolved_call in resolution.calls:
        if (
            resolved_call.status is ResolutionStatus.RESOLVED
            and resolved_call.target is not None
        ):
            record(resolved_call.target, resolved_call.fact.span.file, definite=True)
        elif resolved_call.status is ResolutionStatus.AMBIGUOUS:
            for candidate in resolved_call.candidates:
                record(candidate, resolved_call.fact.span.file, definite=False)

    for resolved_reference in resolution.references:
        if (
            resolved_reference.status is ResolutionStatus.RESOLVED
            and resolved_reference.target is not None
        ):
            record(
                resolved_reference.target,
                resolved_reference.fact.span.file,
                definite=(
                    resolved_reference.fact.confidence is ReferenceConfidence.DEFINITE
                ),
            )
        elif resolved_reference.status is ResolutionStatus.AMBIGUOUS:
            for candidate in resolved_reference.candidates:
                record(candidate, resolved_reference.fact.span.file, definite=False)

    for resolved_import in resolution.imports:
        if resolved_import.fact.reexport:
            source_role = roles.get(resolved_import.source_file)
            for target in resolved_import.target_symbols:
                if source_role is SourceRole.PRODUCTION:
                    target_evidence = evidence.get(target)
                    if target_evidence is not None:
                        target_evidence.reexported = True
                elif (
                    source_role in {SourceRole.TEST, SourceRole.GENERATED}
                    and resolved_import.fact.name is not None
                    and not resolved_import.fact.wildcard
                ):
                    record(target, resolved_import.source_file, definite=False)
            continue
        if resolved_import.fact.wildcard:
            continue
        if resolved_import.status is ResolutionStatus.RESOLVED:
            for target in resolved_import.target_symbols:
                record(target, resolved_import.source_file, definite=True)
        elif resolved_import.status is ResolutionStatus.AMBIGUOUS:
            for target in resolved_import.target_symbols:
                record(target, resolved_import.source_file, definite=False)

    facts: dict[SymbolId, ReferenceFacts] = {}
    for symbol in symbols:
        symbol_evidence = evidence[symbol.id]
        declaration_role = roles[symbol.file]
        if (
            declaration_role is not SourceRole.PRODUCTION
            or symbol_evidence.definite_production
        ):
            zero = ZeroReference.NONE
        elif (
            symbol.visibility in {Visibility.PUBLIC, Visibility.PROTECTED}
            or symbol.kind is SymbolKind.REEXPORT
            or symbol_evidence.reexported
        ):
            zero = ZeroReference.UNCERTAIN
        elif not (
            symbol_evidence.possible_production
            or symbol_evidence.test_files
            or symbol_evidence.generated_files
            or _intrinsically_reachable(symbol)
        ):
            zero = ZeroReference.STRONG
        else:
            zero = ZeroReference.UNCERTAIN
        facts[symbol.id] = ReferenceFacts(
            _sorted_paths(symbol_evidence.production_files),
            _sorted_paths(symbol_evidence.possible_files),
            _sorted_paths(symbol_evidence.test_files),
            _sorted_paths(symbol_evidence.generated_files),
            zero,
        )
    return facts


def analyze_project(
    project: ProjectIR,
    resolution: ResolutionResult,
    *,
    hot_threshold: int,
) -> AnalyzedProject:
    del hot_threshold
    references = _reference_index(project, resolution)
    symbols = sorted(
        (symbol for file_ir in project.files for symbol in file_ir.symbols),
        key=_symbol_order,
    )
    return AnalyzedProject(
        project,
        resolution,
        tuple(
            AnalyzedSymbol(symbol, references[symbol.id], None, ())
            for symbol in symbols
        ),
        (),
    )


__all__ = [
    "DIFF_AST_MIN",
    "DIFF_TOTAL_MIN",
    "MAP_AST_MIN",
    "MAP_TOTAL_MIN",
    "AnalyzedProject",
    "AnalyzedSymbol",
    "BodyProfile",
    "DuplicateMatch",
    "DuplicateScore",
    "ReferenceFacts",
    "ZeroReference",
    "analyze_project",
]
