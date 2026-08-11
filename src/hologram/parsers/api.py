from __future__ import annotations

import dataclasses
import importlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from itertools import pairwise
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from hologram.model import (
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    Language,
    ProjectIR,
    SourceFile,
)

from .treesitter import load_parser


class ParserProvider(Protocol):
    def has_parser(self, language: Language) -> bool: ...
    def parser_for(self, language: Language) -> object | None: ...


Extractor = Callable[[SourceFile, object | None], FileIR]
_EXTRACTOR_MODULES: Mapping[Language, str] = MappingProxyType(
    {
        Language.JAVA: "hologram.parsers.java",
        Language.PYTHON: "hologram.parsers.python",
        Language.TYPESCRIPT: "hologram.parsers.typescript",
        Language.JAVASCRIPT: "hologram.parsers.typescript",
        Language.TSX: "hologram.parsers.typescript",
        Language.VUE: "hologram.parsers.typescript",
        Language.SVELTE: "hologram.parsers.typescript",
        Language.KOTLIN: "hologram.parsers.kotlin",
        Language.GO: "hologram.parsers.go",
        Language.RUST: "hologram.parsers.rust",
        Language.CSHARP: "hologram.parsers.csharp",
        Language.C: "hologram.parsers.c_family",
        Language.CPP: "hologram.parsers.c_family",
        Language.LUA: "hologram.parsers.lua",
        Language.HTML: "hologram.parsers.html",
        Language.HELM: "hologram.parsers.helm",
    }
)


def _optional_module(
    loader: Callable[[str], object | None],
    name: str,
) -> object | None:
    try:
        return loader(name)
    except ModuleNotFoundError as error:
        if error.name == name:
            return None
        raise


def _extractors(
    language: Language,
    module_loader: Callable[[str], object | None] = importlib.import_module,
) -> Extractor | None:
    module_name = _EXTRACTOR_MODULES[language]
    module = _optional_module(module_loader, module_name)
    if module is None:
        return None
    extractor = getattr(module, "extract", None)
    return extractor if callable(extractor) else None


@dataclasses.dataclass(frozen=True, slots=True)
class _ParserState:
    available: bool
    parser: object | None
    error: Exception | None


@dataclasses.dataclass(frozen=True, slots=True)
class _ExtractorState:
    extractor: Extractor | None
    error: Exception | None


@dataclasses.dataclass(frozen=True, slots=True)
class _JavaScriptParser:
    primary: object
    fallback: Callable[[], object | None]

    def parse(self, raw: bytes) -> object:
        primary_tree = self.primary.parse(raw)  # type: ignore[attr-defined]
        if not bool(primary_tree.root_node.has_error):  # type: ignore[attr-defined]
            return primary_tree
        fallback = self.fallback()
        if fallback is None:
            return primary_tree
        fallback_tree = fallback.parse(raw)  # type: ignore[attr-defined]
        if bool(fallback_tree.root_node.has_error):  # type: ignore[attr-defined]
            return primary_tree
        return fallback_tree


class ParserRegistry:
    def __init__(
        self,
        *,
        module_loader: Callable[[str], object | None] | None = None,
    ) -> None:
        self._module_loader = (
            importlib.import_module if module_loader is None else module_loader
        )
        self._locks = {language: RLock() for language in Language}
        self._parser_states: dict[Language, _ParserState] = {
            Language.PYTHON: _ParserState(True, None, None),
            Language.HELM: _ParserState(True, None, None),
        }
        self._extractor_states: dict[Language, _ExtractorState] = {}

    def has_parser(self, language: Language) -> bool:
        return self._parser_state_for(language).available

    def parser_for(self, language: Language) -> object | None:
        return self._parser_state_for(language).parser

    def _parser_state_for(self, language: Language) -> _ParserState:
        state = self._parser_states.get(language)
        if state is not None:
            return state
        with self._locks[language]:
            state = self._parser_states.get(language)
            if state is not None:
                return state
            try:
                parser = load_parser(language, self._module_loader)
                if parser is not None and language is Language.JAVASCRIPT:
                    parser = _JavaScriptParser(
                        parser,
                        lambda: self._parser_state_for(Language.TSX).parser,
                    )
                state = (
                    _ParserState(True, parser, None)
                    if parser is not None
                    else _ParserState(False, None, None)
                )
            except Exception as error:  # noqa: BLE001 - cached discovery boundary
                state = _ParserState(False, None, error)
            self._parser_states[language] = state
            return state

    def _extractor_for(self, language: Language) -> Extractor | None:
        return self._extractor_state_for(language).extractor

    def _extractor_state_for(self, language: Language) -> _ExtractorState:
        state = self._extractor_states.get(language)
        if state is not None:
            return state
        with self._locks[language]:
            state = self._extractor_states.get(language)
            if state is not None:
                return state
            try:
                extractor = _extractors(language, self._module_loader)
            except Exception as error:  # noqa: BLE001 - cached discovery boundary
                state = _ExtractorState(None, error)
            else:
                state = _ExtractorState(extractor, None)
            self._extractor_states[language] = state
            return state

    def _parser_error(self, language: Language) -> Exception | None:
        state = self._parser_states.get(language)
        return state.error if state is not None else None

    def _extractor_error(self, language: Language) -> Exception | None:
        state = self._extractor_states.get(language)
        return state.error if state is not None else None


DEFAULT_REGISTRY = ParserRegistry()


def _error_file(
    source: SourceFile,
    code: str,
    message: str,
) -> FileIR:
    return FileIR(
        source,
        diagnostics=(Diagnostic(code, DiagnosticSeverity.ERROR, message),),
    )


def _validate_owner(source: SourceFile, owner: object, field: str) -> None:
    file = getattr(owner, "file", None)
    language = getattr(owner, "language", None)
    if file != source.file:
        raise ValueError(
            f"{field}.file {file!r} does not match SourceFile.file {source.file!r}"
        )
    if language is not source.language:
        raise ValueError(
            f"{field}.language {language!r} does not match SourceFile.language "
            f"{source.language!r}"
        )


def _validate_span(source: SourceFile, span: object, field: str) -> None:
    file = getattr(span, "file", None)
    if file != source.file:
        raise ValueError(
            f"{field}.file {file!r} does not match SourceFile.file {source.file!r}"
        )


def _validate_file_ir(source: SourceFile, result: FileIR) -> None:
    if result.source is not source:
        raise ValueError("FileIR.source is not the extracted SourceFile")

    symbol_ids = tuple(symbol.id for symbol in result.symbols)
    if len(symbol_ids) != len(set(symbol_ids)):
        raise ValueError("FileIR.symbols contains duplicate Symbol.id values")
    symbol_counts = Counter(symbol_ids)
    for index, symbol in enumerate(result.symbols):
        _validate_owner(source, symbol.id, f"FileIR.symbols[{index}].id")
        _validate_span(source, symbol.span, f"FileIR.symbols[{index}].span")

    body_owners = tuple(body.owner for body in result.bodies)
    if len(body_owners) != len(set(body_owners)):
        raise ValueError("FileIR.bodies contains duplicate BodyIR.owner values")
    for index, body in enumerate(result.bodies):
        field = f"FileIR.bodies[{index}]"
        _validate_owner(source, body.owner, f"{field}.owner")
        if symbol_counts.get(body.owner, 0) != 1:
            raise ValueError(
                f"{field}.owner must identify exactly one FileIR.symbols entry"
            )
        _validate_span(source, body.span, f"{field}.span")
        for event_index, event in enumerate(body.events):
            _validate_span(
                source,
                event.span,
                f"{field}.events[{event_index}].span",
            )

    for index, call in enumerate(result.calls):
        _validate_owner(source, call.caller, f"FileIR.calls[{index}].caller")
        _validate_span(source, call.span, f"FileIR.calls[{index}].span")
    for index, imported in enumerate(result.imports):
        _validate_span(source, imported.span, f"FileIR.imports[{index}].span")
    for index, reference in enumerate(result.references):
        if reference.owner is not None:
            _validate_owner(
                source,
                reference.owner,
                f"FileIR.references[{index}].owner",
            )
        _validate_span(
            source,
            reference.span,
            f"FileIR.references[{index}].span",
        )
    for index, diagnostic in enumerate(result.diagnostics):
        if diagnostic.span is not None:
            _validate_span(
                source,
                diagnostic.span,
                f"FileIR.diagnostics[{index}].span",
            )


def extract_file(
    source: SourceFile,
    *,
    registry: ParserProvider = DEFAULT_REGISTRY,
) -> FileIR:
    language = source.language
    if isinstance(registry, ParserRegistry):
        parser_state = registry._parser_state_for(language)
        has_parser = parser_state.available
        parser = parser_state.parser
        parser_error = parser_state.error
    else:
        try:
            has_parser = registry.has_parser(language)
            parser = registry.parser_for(language) if has_parser else None
        except Exception as error:  # noqa: BLE001 - provider discovery boundary
            return _error_file(
                source,
                "parser-crash",
                f"{source.file}: {language.value} parser discovery crashed: "
                f"{type(error).__name__}: {error}",
            )
        parser_error = None
    if parser_error is not None:
        return _error_file(
            source,
            "parser-crash",
            f"{source.file}: {language.value} parser discovery crashed: "
            f"{type(parser_error).__name__}: {parser_error}",
        )
    if not has_parser:
        return _error_file(
            source,
            "missing-parser",
            f"{source.file}: parser is unavailable for {language.value}",
        )
    if isinstance(registry, ParserRegistry):
        extractor_state = registry._extractor_state_for(language)
        extractor = extractor_state.extractor
        extractor_error = extractor_state.error
    else:
        try:
            extractor = _extractors(language)
        except Exception as error:  # noqa: BLE001 - extractor discovery boundary
            return _error_file(
                source,
                "extractor-crash",
                f"{source.file}: {language.value} extractor discovery crashed: "
                f"{type(error).__name__}: {error}",
            )
        extractor_error = None
    if extractor_error is not None:
        return _error_file(
            source,
            "extractor-crash",
            f"{source.file}: {language.value} extractor discovery crashed: "
            f"{type(extractor_error).__name__}: {extractor_error}",
        )
    if extractor is None:
        return _error_file(
            source,
            "missing-extractor",
            f"{source.file}: extractor is unavailable for {language.value}",
        )
    try:
        result = extractor(source, parser)
        if not isinstance(result, FileIR):
            raise TypeError(
                f"extractor returned {type(result).__name__}, expected FileIR"
            )
        _validate_file_ir(source, result)
        result = dataclasses.replace(result, source=source)
    except Exception as error:  # noqa: BLE001 - extractor failures are diagnostics
        return _error_file(
            source,
            "extractor-crash",
            f"{source.file}: {language.value} extractor crashed: "
            f"{type(error).__name__}: {error}",
        )
    return result


def extract_project(
    root: Path,
    sources: Iterable[SourceFile],
    *,
    registry: ParserProvider = DEFAULT_REGISTRY,
) -> ProjectIR:
    ordered_sources = tuple(sorted(sources, key=lambda item: item.file))
    for previous, current in pairwise(ordered_sources):
        if previous.file == current.file:
            raise ValueError(f"duplicate SourceFile.file {current.file!r}")
    files = tuple(extract_file(source, registry=registry) for source in ordered_sources)
    diagnostics = tuple(
        diagnostic for file_ir in files for diagnostic in file_ir.diagnostics
    )
    complete = not any(
        diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in diagnostics
    )
    return ProjectIR(Path(root), files, diagnostics, complete)


__all__ = [
    "DEFAULT_REGISTRY",
    "Extractor",
    "ParserProvider",
    "ParserRegistry",
    "extract_file",
    "extract_project",
]
