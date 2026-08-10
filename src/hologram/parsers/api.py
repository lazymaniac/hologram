from __future__ import annotations

import dataclasses
import importlib
import sys
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

from .treesitter import grammar_version, load_parser


class ParserProvider(Protocol):
    def has_parser(self, language: Language) -> bool: ...
    def parser_for(self, language: Language) -> object | None: ...
    def versions(self) -> Mapping[str, str]: ...


Extractor = Callable[[SourceFile, object | None], FileIR]
EXTRACTOR_VERSIONS: Mapping[Language, str] = MappingProxyType(
    {language: "2" for language in Language}
)


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
    version: str


@dataclasses.dataclass(frozen=True, slots=True)
class _ExtractorState:
    extractor: Extractor | None
    error: Exception | None


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
            Language.PYTHON: _ParserState(
                True,
                None,
                None,
                f"stdlib-ast-{sys.version_info.major}.{sys.version_info.minor}",
            ),
            Language.HELM: _ParserState(True, None, None, "builtin"),
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
                state = (
                    _ParserState(True, parser, None, grammar_version(language))
                    if parser is not None
                    else _ParserState(False, None, None, "missing")
                )
            except Exception as error:  # noqa: BLE001 - cached discovery boundary
                state = _ParserState(False, None, error, "missing")
            self._parser_states[language] = state
            return state

    def versions(self) -> Mapping[str, str]:
        versions = {
            language.value: self._reported_version(language) for language in Language
        }
        return MappingProxyType(dict(sorted(versions.items())))

    def _reported_version(self, language: Language) -> str:
        with self._locks[language]:
            state = self._parser_states.get(language)
            return state.version if state is not None else grammar_version(language)

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


def _parser_version(registry: ParserProvider, language: Language) -> str | None:
    try:
        if isinstance(registry, ParserRegistry):
            return registry._reported_version(language)
        return registry.versions().get(language.value)
    except Exception:  # noqa: BLE001 - diagnostics must not be masked by metadata
        return None


def _error_file(
    source: SourceFile,
    registry: ParserProvider,
    code: str,
    message: str,
) -> FileIR:
    return FileIR(
        source,
        diagnostics=(Diagnostic(code, DiagnosticSeverity.ERROR, message),),
        extractor_version=EXTRACTOR_VERSIONS[source.language],
        parser_version=_parser_version(registry, source.language),
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
                registry,
                "parser-crash",
                f"{source.file}: {language.value} parser discovery crashed: "
                f"{type(error).__name__}: {error}",
            )
        parser_error = None
    if parser_error is not None:
        return _error_file(
            source,
            registry,
            "parser-crash",
            f"{source.file}: {language.value} parser discovery crashed: "
            f"{type(parser_error).__name__}: {parser_error}",
        )
    if not has_parser:
        return _error_file(
            source,
            registry,
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
                registry,
                "extractor-crash",
                f"{source.file}: {language.value} extractor discovery crashed: "
                f"{type(error).__name__}: {error}",
            )
        extractor_error = None
    if extractor_error is not None:
        return _error_file(
            source,
            registry,
            "extractor-crash",
            f"{source.file}: {language.value} extractor discovery crashed: "
            f"{type(extractor_error).__name__}: {extractor_error}",
        )
    if extractor is None:
        return _error_file(
            source,
            registry,
            "missing-extractor",
            f"{source.file}: extractor is unavailable for {language.value}",
        )
    try:
        result = extractor(source, parser)
        if not isinstance(result, FileIR):
            raise TypeError(
                f"extractor returned {type(result).__name__}, expected FileIR"
            )
    except Exception as error:  # noqa: BLE001 - extractor failures are diagnostics
        return _error_file(
            source,
            registry,
            "extractor-crash",
            f"{source.file}: {language.value} extractor crashed: "
            f"{type(error).__name__}: {error}",
        )
    return dataclasses.replace(
        result,
        source=source,
        extractor_version=EXTRACTOR_VERSIONS[language],
        parser_version=_parser_version(registry, language),
    )


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
    "EXTRACTOR_VERSIONS",
    "Extractor",
    "ParserProvider",
    "ParserRegistry",
    "extract_file",
    "extract_project",
]
