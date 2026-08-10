from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

from .model import (
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    FileIR,
    Language,
    ProjectIR,
    ReferenceConfidence,
    ReferenceKind,
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


_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NAME_PART = re.compile(r"[^\W_]+", re.UNICODE)
_ACCESSOR_MODIFIERS = frozenset({"accessor", "add", "get", "init", "remove", "set"})
_DELEGATE_KEYWORDS = frozenset({"await", "const", "return", "yield"})
_DELEGATE_STRUCTURAL_OPERATORS = frozenset({"=", "=>"})
_RESOLVABLE_BODY_KINDS = frozenset(
    {
        BodyEventKind.CALL,
        BodyEventKind.CONSTRUCT,
        BodyEventKind.NAME,
        BodyEventKind.TYPE,
    }
)


def _split_name(value: str) -> frozenset[str]:
    parts: set[str] = set()
    for raw_part in _NAME_PART.findall(value):
        separated = _ACRONYM_BOUNDARY.sub(" ", raw_part)
        separated = _CASE_BOUNDARY.sub(" ", separated)
        parts.update(part.casefold() for part in separated.split() if part)
    return frozenset(parts)


def _literal_category(value: str) -> str:
    return {
        "<string>": "STR",
        "<number>": "NUM",
        "<bool>": "BOOL",
        "<null>": "NULL",
    }.get(value, "OTHER")


def _symbol_id_token(symbol_id: SymbolId) -> str:
    serialized = json.dumps(
        [
            symbol_id.language.value,
            symbol_id.file,
            list(symbol_id.container_path),
            symbol_id.kind.value,
            symbol_id.name,
            symbol_id.signature_key,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"REF:{serialized}"


def _empty_body_profile(symbol: Symbol, reason: str) -> BodyProfile:
    return BodyProfile(
        (),
        frozenset(),
        (),
        frozenset(),
        _split_name(symbol.name),
        len(symbol.params),
        canonical_type_key(symbol.returns),
        0,
        reason,
    )


def _accessor(symbol: Symbol) -> bool:
    modifiers = frozenset(value.strip().casefold() for value in symbol.modifiers)
    return symbol.kind is SymbolKind.PROPERTY or bool(modifiers & _ACCESSOR_MODIFIERS)


def _static_body_exclusion(symbol: Symbol, file_ir: FileIR) -> str | None:
    if file_ir.source.role is SourceRole.TEST:
        return "test"
    if file_ir.source.role is SourceRole.GENERATED:
        return "generated"
    if symbol.kind is SymbolKind.CONSTRUCTOR:
        return "constructor"
    if _accessor(symbol):
        return "accessor"
    return None


def _validate_control_stream(events: tuple[BodyEvent, ...]) -> None:
    controls: list[str] = []
    for event in events:
        if event.kind is BodyEventKind.CONTROL_ENTER:
            controls.append(event.text)
        elif event.kind is BodyEventKind.CONTROL_EXIT:
            if not controls:
                raise ValueError(
                    f"body control stack underflow at {event.span.file}:"
                    f"{event.span.start_line}:{event.span.start_column}"
                )
            expected = controls.pop()
            if event.text != expected:
                raise ValueError(
                    f"body control mismatch: expected {expected!r}, "
                    f"found {event.text!r}"
                )
    if controls:
        raise ValueError(f"unclosed body controls: {', '.join(controls)}")


def _span_contains(outer: SourceSpan, inner: SourceSpan) -> bool:
    return (
        outer.file == inner.file
        and (outer.start_line, outer.start_column)
        <= (inner.start_line, inner.start_column)
        and (inner.end_line, inner.end_column) <= (outer.end_line, outer.end_column)
    )


def _rust_postfix_await(
    symbol: Symbol,
    events: tuple[BodyEvent, ...],
    call_index: int,
    call: BodyEvent,
    await_index: int,
) -> bool:
    if symbol.lang is not Language.RUST or await_index != len(events) - 1:
        return False
    await_event = events[await_index]
    if (
        await_index <= call_index
        or call.span.file != await_event.span.file
        or (await_event.span.start_line, await_event.span.start_column)
        < (call.span.end_line, call.span.end_column)
    ):
        return False
    for event in events[call_index + 1 : await_index]:
        if event.kind in {
            BodyEventKind.NAME,
            BodyEventKind.MEMBER,
            BodyEventKind.TYPE,
        } and not _span_contains(call.span, event.span):
            return False
        if event.kind is BodyEventKind.PARAM:
            return False
    return True


def _trivial_delegate(symbol: Symbol, events: tuple[BodyEvent, ...]) -> bool:
    calls = tuple(
        (index, event)
        for index, event in enumerate(events)
        if event.kind in {BodyEventKind.CALL, BodyEventKind.CONSTRUCT}
    )
    if len(calls) != 1:
        return False
    call_index, call = calls[0]
    saw_expression = False
    saw_structural_operator = False
    for event_index, event in enumerate(events):
        if event.kind in {
            BodyEventKind.CONTROL_ENTER,
            BodyEventKind.CONTROL_EXIT,
            BodyEventKind.LITERAL,
        }:
            return False
        if event.kind is BodyEventKind.LOCAL:
            if saw_expression or event.text != symbol.name:
                return False
            continue
        if event.kind is BodyEventKind.OPERATOR:
            if (
                saw_expression
                or saw_structural_operator
                or event.text not in _DELEGATE_STRUCTURAL_OPERATORS
            ):
                return False
            saw_structural_operator = True
            continue
        if event.kind is BodyEventKind.KEYWORD:
            keyword = event.text.casefold()
            if keyword not in _DELEGATE_KEYWORDS:
                return False
            if saw_expression and (
                keyword != "await"
                or not _rust_postfix_await(
                    symbol,
                    events,
                    call_index,
                    call,
                    event_index,
                )
            ):
                return False
        if event.kind in {
            BodyEventKind.CALL,
            BodyEventKind.CONSTRUCT,
            BodyEventKind.MEMBER,
            BodyEventKind.NAME,
        }:
            saw_expression = True
    return True


def _resolved_body_targets(
    resolution: ResolutionResult,
) -> dict[tuple[BodyEventKind, SourceSpan], SymbolId]:
    """Index uniquely resolved call/reference targets by frozen event identity."""
    targets: dict[tuple[BodyEventKind, SourceSpan], SymbolId] = {}

    def add(
        key: tuple[BodyEventKind, SourceSpan],
        target: SymbolId,
    ) -> None:
        existing = targets.get(key)
        if existing is not None and existing != target:
            raise ValueError(
                "conflicting resolved body targets for "
                f"{key[0].value} at {key[1].file}:"
                f"{key[1].start_line}:{key[1].start_column}"
            )
        targets[key] = target

    for resolved_call in resolution.calls:
        if (
            resolved_call.status is not ResolutionStatus.RESOLVED
            or resolved_call.target is None
        ):
            continue
        kind = (
            BodyEventKind.CONSTRUCT
            if resolved_call.fact.kind is CallKind.CONSTRUCT
            else BodyEventKind.CALL
        )
        add((kind, resolved_call.fact.span), resolved_call.target)

    for resolved_reference in resolution.references:
        if (
            resolved_reference.status is not ResolutionStatus.RESOLVED
            or resolved_reference.target is None
        ):
            continue
        kind = (
            BodyEventKind.TYPE
            if resolved_reference.fact.kind is ReferenceKind.TYPE
            else BodyEventKind.NAME
        )
        add((kind, resolved_reference.fact.span), resolved_reference.target)

    return dict(
        sorted(
            targets.items(),
            key=lambda item: (item[0][0].value, item[0][1]),
        )
    )


def canonical_body(
    symbol: Symbol,
    file_ir: FileIR,
    resolved_targets: Mapping[tuple[BodyEventKind, SourceSpan], SymbolId],
) -> BodyProfile:
    """Convert one frozen extractor body into its canonical semantic profile."""
    bodies = tuple(body for body in file_ir.bodies if body.owner == symbol.id)
    if len(bodies) != 1:
        raise ValueError(
            f"expected exactly one body for {symbol.id!r}; found {len(bodies)}"
        )
    return _canonical_body(symbol, file_ir, bodies[0], resolved_targets)


def _canonical_body(
    symbol: Symbol,
    file_ir: FileIR,
    body: BodyIR,
    resolved_targets: Mapping[tuple[BodyEventKind, SourceSpan], SymbolId],
) -> BodyProfile:
    """Canonicalize an already indexed body without rescanning its file."""

    static_exclusion = _static_body_exclusion(symbol, file_ir)
    if static_exclusion is not None:
        _validate_control_stream(body.events)
        return _empty_body_profile(symbol, static_exclusion)
    if _trivial_delegate(symbol, body.events):
        return _empty_body_profile(symbol, "trivial-delegate")

    semantic_tokens: list[str] = []
    control_flow: list[str] = []
    resolved_calls: set[SymbolId] = set()
    name_tokens = _split_name(symbol.name)
    bindings: dict[str, str] = {}
    next_binding = 0
    root_sibling = 0
    control_stack: list[tuple[str, str, int]] = []

    for event in body.events:
        if event.kind is BodyEventKind.CONTROL_ENTER:
            if control_stack:
                parent_kind, parent_path, next_sibling = control_stack[-1]
                control_stack[-1] = (parent_kind, parent_path, next_sibling + 1)
            else:
                parent_path = ""
                next_sibling = root_sibling
                root_sibling += 1
            component = f"{event.text}:{next_sibling}"
            path = f"{parent_path}/{component}" if parent_path else component
            control_flow.append(path)
            control_stack.append((event.text, path, 0))
            semantic_tokens.append(f"CONTROL_ENTER:{event.text}")
            continue
        if event.kind is BodyEventKind.CONTROL_EXIT:
            if not control_stack:
                raise ValueError(
                    f"body control stack underflow at {event.span.file}:"
                    f"{event.span.start_line}:{event.span.start_column}"
                )
            expected, _, _ = control_stack.pop()
            if event.text != expected:
                raise ValueError(
                    f"body control mismatch: expected {expected!r}, "
                    f"found {event.text!r}"
                )
            semantic_tokens.append(f"CONTROL_EXIT:{event.text}")
            continue

        target = (
            resolved_targets.get((event.kind, event.span))
            if event.kind in _RESOLVABLE_BODY_KINDS
            else None
        )
        if target is not None:
            semantic_tokens.append(_symbol_id_token(target))
            if event.kind in {BodyEventKind.CALL, BodyEventKind.CONSTRUCT}:
                resolved_calls.add(target)
            continue

        if event.kind in {BodyEventKind.PARAM, BodyEventKind.LOCAL}:
            binding = bindings.get(event.text)
            if binding is None:
                binding = f"${next_binding}"
                next_binding += 1
                bindings[event.text] = binding
            semantic_tokens.append(f"{event.kind.name}:{binding}")
            continue
        if event.kind is BodyEventKind.NAME and event.text in bindings:
            semantic_tokens.append(f"NAME:{bindings[event.text]}")
            continue
        if event.kind is BodyEventKind.LITERAL:
            semantic_tokens.append(f"LITERAL:{_literal_category(event.text)}")
            continue
        if event.kind is BodyEventKind.TYPE:
            semantic_tokens.append(f"TYPE:{canonical_type_key(event.text)}")
            continue

        semantic_tokens.append(f"{event.kind.name}:{event.text}")

    if control_stack:
        open_controls = ", ".join(frame[0] for frame in control_stack)
        raise ValueError(f"unclosed body controls: {open_controls}")

    tokens = tuple(semantic_tokens)
    shingles = frozenset(zip(tokens, tokens[1:], tokens[2:], tokens[3:], tokens[4:]))
    excluded_reason = "fewer-than-12-semantic-tokens" if len(tokens) < 12 else None
    return BodyProfile(
        tokens,
        shingles,
        tuple(control_flow),
        frozenset(resolved_calls),
        name_tokens,
        len(symbol.params),
        canonical_type_key(symbol.returns),
        len(tokens),
        excluded_reason,
    )


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


def _body_index(project: ProjectIR) -> dict[SymbolId, BodyIR]:
    bodies: dict[SymbolId, BodyIR] = {}
    for file_ir in project.files:
        for body in file_ir.bodies:
            if body.owner in bodies:
                raise ValueError(
                    f"expected exactly one body for {body.owner!r}; found more than one"
                )
            bodies[body.owner] = body
    return bodies


def analyze_project(
    project: ProjectIR,
    resolution: ResolutionResult,
    *,
    hot_threshold: int,
) -> AnalyzedProject:
    del hot_threshold
    references = _reference_index(project, resolution)
    resolved_targets = _resolved_body_targets(resolution)
    files_by_symbol = {
        symbol.id: file_ir for file_ir in project.files for symbol in file_ir.symbols
    }
    bodies_by_owner = _body_index(project)
    symbols = sorted(
        (symbol for file_ir in project.files for symbol in file_ir.symbols),
        key=_symbol_order,
    )
    return AnalyzedProject(
        project,
        resolution,
        tuple(
            AnalyzedSymbol(
                symbol,
                references[symbol.id],
                _canonical_body(
                    symbol,
                    files_by_symbol[symbol.id],
                    bodies_by_owner[symbol.id],
                    resolved_targets,
                )
                if symbol.id in bodies_by_owner
                else None,
                (),
            )
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
    "canonical_body",
]
