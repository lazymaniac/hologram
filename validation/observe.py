from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast

from hologram import analysis, pipeline, render
from hologram.analysis import AnalyzedProject, ZeroReference
from hologram.config import ProjectConfig
from hologram.model import (
    Language,
    ReferenceKind,
    Symbol,
    SymbolId,
    SymbolKind,
)
from hologram.render import RenderIR
from hologram.resolve import ResolutionStatus, canonical_type_key

_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)
_CANONICAL_TYPESCRIPT_SIGNATURE_LANGUAGES = frozenset(
    {Language.TYPESCRIPT, Language.JAVASCRIPT, Language.TSX}
)
_STATIC_DEPENDENCY_LANGUAGES = frozenset(
    {
        Language.TYPESCRIPT,
        Language.JAVASCRIPT,
        Language.TSX,
        Language.VUE,
        Language.SVELTE,
    }
)
_TYPE_OWNER_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.INTERFACE,
        SymbolKind.RECORD,
        SymbolKind.ENUM,
        SymbolKind.TYPE,
    }
)
_MAP_REQUIRED_CATEGORIES = frozenset(
    {
        "declaration",
        "kind",
        "container",
        "visibility",
        "signature",
        "call",
        "call_order",
        "strong_x0",
        "zero_classification",
        "approximate",
    }
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _freeze_json(value: object, seen: frozenset[int] = frozenset()) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("value must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError("value must not contain cycles")
        nested = seen | {identity}
        owned: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("value keys must be strings")
            owned[key] = _freeze_json(item, nested)
        return MappingProxyType(owned)
    if isinstance(value, (tuple, list)):
        identity = id(value)
        if identity in seen:
            raise ValueError("value must not contain cycles")
        nested = seen | {identity}
        return tuple(_freeze_json(item, nested) for item in value)
    raise TypeError("value must contain only JSON-compatible values")


def _symbol_id_value(symbol_id: SymbolId) -> list[object]:
    return [
        symbol_id.language.value,
        symbol_id.file,
        list(symbol_id.container_path),
        symbol_id.kind.value,
        symbol_id.name,
        symbol_id.signature_key,
    ]


def _symbol_id_key(
    symbol_id: SymbolId,
) -> tuple[str, str, tuple[str, ...], str, str, str]:
    return (
        symbol_id.language.value,
        symbol_id.file,
        symbol_id.container_path,
        symbol_id.kind.value,
        symbol_id.name,
        symbol_id.signature_key,
    )


def _subject(symbol_id: SymbolId) -> str:
    return json.dumps(
        _symbol_id_value(symbol_id),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validated_subject(subject: str) -> SymbolId:
    try:
        value = json.loads(subject)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("subject must be a canonical SymbolId") from error
    if (
        not isinstance(value, list)
        or len(value) != 6
        or not isinstance(value[0], str)
        or not isinstance(value[1], str)
        or not isinstance(value[2], list)
        or any(not isinstance(part, str) or not part for part in value[2])
        or not isinstance(value[3], str)
        or not isinstance(value[4], str)
        or not value[4]
        or not isinstance(value[5], str)
    ):
        raise ValueError("subject must be a canonical SymbolId")
    try:
        symbol_id = SymbolId(
            Language(value[0]),
            value[1],
            tuple(value[2]),
            SymbolKind(value[3]),
            value[4],
            value[5],
        )
    except (TypeError, ValueError) as error:
        raise ValueError("subject must be a canonical SymbolId") from error
    if _subject(symbol_id) != subject:
        raise ValueError("subject must be canonical compact JSON")
    return symbol_id


@dataclass(frozen=True)
class ObservedFact:
    category: str
    subject: str
    value: Mapping[str, object]
    corpus: str
    path: str
    line: int
    language: str

    def __post_init__(self) -> None:
        if type(self.category) is not str or not self.category.strip():
            raise ValueError("category must be a nonblank string")
        if type(self.corpus) is not str or not self.corpus.strip():
            raise ValueError("corpus must be a nonblank string")
        if type(self.path) is not str:
            raise TypeError("path must be a string")
        path = PurePosixPath(self.path)
        if (
            not self.path.strip()
            or self.path == "."
            or not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or "\x00" in self.path
            or path.as_posix() != self.path
        ):
            raise ValueError("path must be normalized relative POSIX")
        if type(self.line) is not int or self.line < 1:
            raise ValueError("line must be a positive integer")
        try:
            Language(self.language)
        except (TypeError, ValueError) as error:
            raise ValueError("language must be canonical") from error
        if type(self.subject) is not str:
            raise TypeError("subject must be a string")
        subject = _validated_subject(self.subject)
        if subject.file != self.path or subject.language.value != self.language:
            raise ValueError("subject file and language must match fact metadata")
        if not isinstance(self.value, Mapping):
            raise TypeError("value must be a mapping")
        object.__setattr__(
            self,
            "value",
            cast(Mapping[str, object], _freeze_json(self.value)),
        )


def _fact(
    corpus: str,
    symbol_id: SymbolId,
    line: int,
    category: str,
    value: Mapping[str, object],
) -> ObservedFact:
    return ObservedFact(
        category,
        _subject(symbol_id),
        value,
        corpus,
        symbol_id.file,
        line,
        symbol_id.language.value,
    )


def _fact_key(fact: ObservedFact) -> tuple[str, str, int, str, str, str]:
    return (
        fact.corpus,
        fact.path,
        fact.line,
        fact.category,
        fact.subject,
        _canonical_json(fact.value),
    )


def _core_facts(
    corpus: str,
    symbol_id: SymbolId,
    line: int,
    visibility: str,
    signature: str,
    parameters: tuple[str, ...],
    returns: str | None,
    raises: tuple[str, ...],
) -> list[ObservedFact]:
    signature, parameters, returns, raises = _canonical_signature(
        symbol_id,
        signature,
        parameters,
        returns,
        raises,
    )
    return [
        _fact(corpus, symbol_id, line, "declaration", {"name": symbol_id.name}),
        _fact(corpus, symbol_id, line, "kind", {"kind": symbol_id.kind.value}),
        _fact(
            corpus,
            symbol_id,
            line,
            "container",
            {"container": list(symbol_id.container_path)},
        ),
        _fact(corpus, symbol_id, line, "visibility", {"visibility": visibility}),
        _fact(
            corpus,
            symbol_id,
            line,
            "signature",
            {
                "text": signature,
                "params": list(parameters),
                "returns": returns,
                "raises": list(raises),
            },
        ),
    ]


def _canonical_type_fragment(value: str) -> str:
    if value in {"?", "<?>"}:
        return value
    return canonical_type_key(value)


def _canonical_signature(
    symbol_id: SymbolId,
    signature: str,
    parameters: tuple[str, ...],
    returns: str | None,
    raises: tuple[str, ...],
) -> tuple[str, tuple[str, ...], str | None, tuple[str, ...]]:
    language = symbol_id.language
    kind = symbol_id.kind
    name = symbol_id.name

    if language in _CANONICAL_TYPESCRIPT_SIGNATURE_LANGUAGES:
        if kind in _CALLABLE_KINDS:
            parameters = tuple(_canonical_type_fragment(item) for item in parameters)
            returns = _canonical_type_fragment(returns) if returns is not None else None
            raises = tuple(_canonical_type_fragment(item) for item in raises)
            signature = f"{name}({','.join(parameters)})"
            if returns is not None:
                signature += f":{returns}"
        elif kind in {SymbolKind.CONSTANT, SymbolKind.FIELD}:
            signature, parameters, returns, raises = name, (), None, ()
        elif kind is SymbolKind.PROPERTY:
            declared = (
                _canonical_type_fragment(returns) if returns is not None else None
            )
            signature = name + (f":{declared}" if declared is not None else "")
            parameters, returns, raises = (), None, ()
        elif kind is SymbolKind.TYPE and parameters:
            signature = f"type {name}={_canonical_type_fragment(parameters[0])}"
            parameters, returns, raises = (), None, ()
        elif kind in {
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
            SymbolKind.ENUM,
            SymbolKind.RECORD,
        }:
            signature = f"{kind.value} {name}"
            parameters, returns, raises = (), None, ()

    elif language is Language.JAVA:
        if kind in _CALLABLE_KINDS:
            parameters = tuple(_canonical_type_fragment(item) for item in parameters)
            returns = _canonical_type_fragment(returns) if returns is not None else None
            raises = tuple(_canonical_type_fragment(item) for item in raises)
            signature = f"{name}({','.join(parameters)})"
            if returns not in {None, "void"}:
                signature += f":{returns}"
        elif kind in {SymbolKind.CONSTANT, SymbolKind.FIELD, SymbolKind.PROPERTY}:
            signature, parameters, returns, raises = name, (), None, ()
        elif kind in {
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
            SymbolKind.ENUM,
            SymbolKind.RECORD,
        }:
            signature = f"{kind.value} {name}"
            parameters, returns, raises = (), None, ()

    return signature, parameters, returns, raises


def _zero_facts(
    corpus: str,
    symbol_id: SymbolId,
    line: int,
    zero: str,
) -> list[ObservedFact]:
    facts = [
        _fact(
            corpus,
            symbol_id,
            line,
            "zero_classification",
            {"classification": zero},
        )
    ]
    if zero == ZeroReference.STRONG.value:
        facts.append(
            _fact(
                corpus,
                symbol_id,
                line,
                "strong_x0",
                {"classification": "strong"},
            )
        )
    return facts


def _call_facts(
    corpus: str,
    symbol_id: SymbolId,
    line: int,
    targets: tuple[SymbolId, ...],
) -> list[ObservedFact]:
    values = [_symbol_id_value(target) for target in targets]
    facts = [_fact(corpus, symbol_id, line, "call_order", {"targets": values})]
    facts.extend(
        _fact(
            corpus,
            symbol_id,
            line,
            "call",
            {"ordinal": ordinal, "target": target},
        )
        for ordinal, target in enumerate(values)
    )
    return facts


def _component_name(raw: str) -> str:
    before_type = raw.split(":", 1)[0].strip()
    return before_type.rsplit(" ", 1)[-1]


def _component_target(
    owner: SymbolId,
    raw: str,
    symbols: Mapping[SymbolId, object],
) -> SymbolId | None:
    name = _component_name(raw)
    container = (*owner.container_path, owner.name)
    candidates = [
        candidate
        for candidate in symbols
        if candidate.file == owner.file
        and candidate.container_path == container
        and candidate.name == name
    ]
    return candidates[0] if len(candidates) == 1 else None


def _structural_component_targets(
    owner: SymbolId,
    symbols: Mapping[SymbolId, object],
) -> tuple[SymbolId, ...]:
    if owner.kind not in _TYPE_OWNER_KINDS:
        return ()
    if owner.language in {
        Language.TYPESCRIPT,
        Language.JAVASCRIPT,
        Language.TSX,
        Language.VUE,
        Language.SVELTE,
    }:
        member_kinds = {SymbolKind.METHOD, SymbolKind.PROPERTY}
    elif owner.language in {Language.JAVA, Language.PYTHON}:
        member_kinds = {SymbolKind.FIELD, SymbolKind.CONSTANT}
    else:
        return ()
    container = (*owner.container_path, owner.name)
    return tuple(
        sorted(
            (
                candidate
                for candidate in symbols
                if candidate.file == owner.file
                and candidate.container_path == container
                and candidate.kind in member_kinds
            ),
            key=_symbol_id_key,
        )
    )


def _mentions_type(raw: str, name: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", raw) is not None


def _resolved_relation_target(
    owner: SymbolId,
    raw: str,
    resolved: Mapping[SymbolId, tuple[SymbolId, ...]],
) -> SymbolId | None:
    normalized = canonical_type_key(raw)
    candidates = {
        target
        for target in resolved.get(owner, ())
        if canonical_type_key(target.name) == normalized
        or _mentions_type(normalized, target.name)
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _render_relation_target(
    owner: SymbolId,
    raw: str,
    symbols: Mapping[SymbolId, object],
) -> SymbolId | None:
    normalized = canonical_type_key(raw)
    candidates = [
        symbol_id
        for symbol_id in symbols
        if symbol_id.language is owner.language
        and symbol_id.kind
        in {
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
            SymbolKind.RECORD,
            SymbolKind.ENUM,
            SymbolKind.TYPE,
        }
        and _mentions_type(normalized, symbol_id.name)
    ]
    same_file = [candidate for candidate in candidates if candidate.file == owner.file]
    if len(same_file) == 1:
        return same_file[0]
    return candidates[0] if len(candidates) == 1 else None


def _relation_fact(
    corpus: str,
    owner: SymbolId,
    line: int,
    kind: str,
    target: SymbolId,
) -> ObservedFact:
    return _fact(
        corpus,
        owner,
        line,
        "relation",
        {"kind": kind, "target": {"symbol": _symbol_id_value(target)}},
    )


def _model_facts(corpus: str, analyzed: AnalyzedProject) -> tuple[ObservedFact, ...]:
    symbols = {
        symbol.id: symbol
        for file_ir in analyzed.project.files
        for symbol in file_ir.symbols
    }
    analyzed_by_id = {
        analyzed_symbol.symbol.id: analyzed_symbol
        for analyzed_symbol in analyzed.symbols
    }
    if symbols.keys() != analyzed_by_id.keys():
        raise ValueError("analysis and project symbol ownership differs")

    resolved_types: dict[SymbolId, list[SymbolId]] = defaultdict(list)
    for resolved_reference in analyzed.resolution.references:
        if (
            resolved_reference.status is ResolutionStatus.RESOLVED
            and resolved_reference.target is not None
            and resolved_reference.fact.owner is not None
            and resolved_reference.fact.kind is ReferenceKind.TYPE
        ):
            resolved_types[resolved_reference.fact.owner].append(
                resolved_reference.target
            )
    frozen_resolved_types = {
        owner: tuple(dict.fromkeys(targets))
        for owner, targets in resolved_types.items()
    }

    ordered_calls: dict[SymbolId, list[tuple[tuple[int, int, int, int], SymbolId]]] = (
        defaultdict(list)
    )
    for resolved_call in analyzed.resolution.calls:
        if (
            resolved_call.status is ResolutionStatus.RESOLVED
            and resolved_call.target is not None
            and resolved_call.fact.caller is not None
        ):
            span = resolved_call.fact.span
            ordered_calls[resolved_call.fact.caller].append(
                (
                    (
                        span.start_line,
                        span.start_column,
                        span.end_line,
                        span.end_column,
                    ),
                    resolved_call.target,
                )
            )

    facts: list[ObservedFact] = []
    for symbol_id, symbol in symbols.items():
        analyzed_symbol = analyzed_by_id[symbol_id]
        line = symbol.span.start_line
        facts.extend(
            _core_facts(
                corpus,
                symbol_id,
                line,
                symbol.visibility.value,
                symbol.signature,
                symbol.params,
                symbol.returns,
                symbol.raises,
            )
        )
        facts.extend(
            _zero_facts(
                corpus,
                symbol_id,
                line,
                analyzed_symbol.references.zero.value,
            )
        )
        if symbol_id.kind in _CALLABLE_KINDS:
            targets = tuple(
                target
                for _, target in sorted(
                    ordered_calls.get(symbol_id, ()),
                    key=lambda entry: (entry[0], _symbol_id_key(entry[1])),
                )
            )
            facts.extend(_call_facts(corpus, symbol_id, line, targets))

        component_targets = set(_structural_component_targets(symbol_id, symbols))
        component_targets.update(
            target
            for raw in symbol.components
            if (target := _component_target(symbol_id, raw, symbols)) is not None
        )
        for target in sorted(component_targets, key=_symbol_id_key):
            facts.append(_relation_fact(corpus, symbol_id, line, "component", target))
        for kind, values in (("super", symbol.supers), ("permit", symbol.permits)):
            for raw in values:
                target = _resolved_relation_target(
                    symbol_id,
                    raw,
                    frozen_resolved_types,
                )
                if target is not None:
                    facts.append(_relation_fact(corpus, symbol_id, line, kind, target))

    module_by_file: dict[str, Symbol] = {}
    for file_ir in analyzed.project.files:
        candidates = [
            symbol
            for symbol in file_ir.symbols
            if symbol.kind is SymbolKind.MODULE and not symbol.id.container_path
        ]
        preferred = [
            symbol
            for symbol in candidates
            if file_ir.module is not None and symbol.name == file_ir.module
        ]
        selected = preferred if len(preferred) == 1 else candidates
        if len(selected) == 1:
            module_by_file[file_ir.source.file] = selected[0]
    dependencies: set[tuple[SymbolId, str]] = set()
    for resolved_import in analyzed.resolution.imports:
        if (
            resolved_import.status is ResolutionStatus.EXTERNAL
            and not resolved_import.fact.reexport
            and not resolved_import.fact.module.startswith((".", "/"))
            and (owner := module_by_file.get(resolved_import.source_file)) is not None
            and owner.id.language in _STATIC_DEPENDENCY_LANGUAGES
        ):
            dependencies.add((owner.id, resolved_import.fact.module))
    for owner_id, module in sorted(
        dependencies,
        key=lambda item: (_symbol_id_key(item[0]), item[1]),
    ):
        owner = symbols[owner_id]
        facts.append(
            _fact(
                corpus,
                owner_id,
                owner.span.start_line,
                "relation",
                {"kind": "dependency", "target": {"external": module}},
            )
        )

    duplicate_pairs: set[tuple[SymbolId, SymbolId]] = set()
    for analyzed_symbol in analyzed.symbols:
        for peer in analyzed_symbol.duplicate_peers:
            pair = tuple(sorted((analyzed_symbol.symbol.id, peer), key=_symbol_id_key))
            duplicate_pairs.add(cast(tuple[SymbolId, SymbolId], pair))
    for left, right in sorted(
        duplicate_pairs,
        key=lambda pair: (_symbol_id_key(pair[0]), _symbol_id_key(pair[1])),
    ):
        facts.append(
            _fact(
                corpus,
                left,
                symbols[left].span.start_line,
                "approximate",
                {"peer": _symbol_id_value(right)},
            )
        )

    return tuple(sorted(facts, key=_fact_key))


def _marker_zero(markers: tuple[str, ...]) -> str:
    if "×0" in markers:
        return ZeroReference.STRONG.value
    if "×0?" in markers:
        return ZeroReference.UNCERTAIN.value
    return ZeroReference.NONE.value


def _render_facts(corpus: str, ir: RenderIR) -> tuple[ObservedFact, ...]:
    symbols = {
        symbol.symbol_id: symbol for file_ir in ir.files for symbol in file_ir.symbols
    }
    facts: list[ObservedFact] = []
    for symbol_id, symbol in symbols.items():
        line = symbol.source_line
        facts.extend(
            _core_facts(
                corpus,
                symbol_id,
                line,
                symbol.visibility,
                symbol.signature,
                symbol.parameters,
                symbol.returns,
                symbol.throws,
            )
        )
        facts.extend(_zero_facts(corpus, symbol_id, line, _marker_zero(symbol.markers)))
        if symbol_id.kind in _CALLABLE_KINDS:
            facts.extend(_call_facts(corpus, symbol_id, line, symbol.call_targets))
        component_targets = set(_structural_component_targets(symbol_id, symbols))
        component_targets.update(
            target
            for raw in symbol.components
            if (target := _component_target(symbol_id, raw, symbols)) is not None
        )
        for target in sorted(component_targets, key=_symbol_id_key):
            facts.append(_relation_fact(corpus, symbol_id, line, "component", target))
        for kind, values in (("super", symbol.supers), ("permit", symbol.permits)):
            for raw in values:
                target = _render_relation_target(symbol_id, raw, symbols)
                if target is not None:
                    facts.append(_relation_fact(corpus, symbol_id, line, kind, target))

    duplicate_pairs: set[tuple[SymbolId, SymbolId]] = set()
    for symbol in symbols.values():
        for peer in symbol.duplicate_peers:
            pair = tuple(sorted((symbol.symbol_id, peer), key=_symbol_id_key))
            duplicate_pairs.add(cast(tuple[SymbolId, SymbolId], pair))
    for left, right in sorted(
        duplicate_pairs,
        key=lambda pair: (_symbol_id_key(pair[0]), _symbol_id_key(pair[1])),
    ):
        facts.append(
            _fact(
                corpus,
                left,
                symbols[left].source_line,
                "approximate",
                {"peer": _symbol_id_value(right)},
            )
        )
    return tuple(sorted(facts, key=_fact_key))


def _observe_project_artifact(
    *,
    corpus: str,
    root: Path,
    config: ProjectConfig,
) -> tuple[tuple[ObservedFact, ...], str, int]:
    snapshot = pipeline.build_project(root, config)
    snapshot.require_complete()
    analyzed = analysis.analyze_project(
        snapshot.project,
        snapshot.resolution,
        hot_threshold=config.hot_threshold,
    )
    render_ir = render.project_render_ir(
        analyzed,
        state=snapshot.state.value,
        hot_threshold=config.hot_threshold,
    )
    rendered = render.render_project(render_ir)
    decoded = render.decode_render(rendered)

    model_facts = _model_facts(corpus, analyzed)
    rendered_facts = _render_facts(corpus, decoded)
    model_required = tuple(
        fact for fact in model_facts if fact.category in _MAP_REQUIRED_CATEGORIES
    )
    rendered_required = tuple(
        fact for fact in rendered_facts if fact.category in _MAP_REQUIRED_CATEGORIES
    )
    if model_required != rendered_required:
        raise ValueError("canonical render projection lost observed fact provenance")
    return model_facts, rendered, len(snapshot.project.files)


def observe_project(
    *,
    corpus: str,
    root: Path,
    config: ProjectConfig,
) -> tuple[ObservedFact, ...]:
    facts, _rendered, _file_count = _observe_project_artifact(
        corpus=corpus,
        root=root,
        config=config,
    )
    return facts


def observe_rendered_map(
    *,
    corpus: str,
    rendered: str,
) -> tuple[ObservedFact, ...]:
    return _render_facts(corpus, render.decode_render(rendered))


__all__ = ("ObservedFact", "observe_project", "observe_rendered_map")
