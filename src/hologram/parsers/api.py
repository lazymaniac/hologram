from __future__ import annotations

import dataclasses
import importlib
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
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
    except ImportError:
        return None


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


class ParserRegistry:
    def __init__(
        self,
        *,
        module_loader: Callable[[str], object | None] | None = None,
    ) -> None:
        self._module_loader = module_loader or importlib.import_module
        self._parser_cache: dict[Language, object | None] = {}
        self._version_cache: dict[Language, str] = {}
        self._extractor_cache: dict[Language, Extractor | None] = {}

    def has_parser(self, language: Language) -> bool:
        if language in {Language.PYTHON, Language.HELM}:
            return True
        return self.parser_for(language) is not None

    def parser_for(self, language: Language) -> object | None:
        if language in {Language.PYTHON, Language.HELM}:
            return None
        if language in self._parser_cache:
            return self._parser_cache[language]
        parser = load_parser(language, self._module_loader)
        self._parser_cache[language] = parser
        self._version_cache[language] = (
            grammar_version(language) if parser is not None else "missing"
        )
        return parser

    def versions(self) -> Mapping[str, str]:
        versions = {
            language.value: self._reported_version(language) for language in Language
        }
        return MappingProxyType(dict(sorted(versions.items())))

    def _reported_version(self, language: Language) -> str:
        if language is Language.PYTHON:
            return f"stdlib-ast-{sys.version_info.major}.{sys.version_info.minor}"
        if language is Language.HELM:
            return "builtin"
        cached = self._version_cache.get(language)
        return cached if cached is not None else grammar_version(language)

    def _extractor_for(self, language: Language) -> Extractor | None:
        if language not in self._extractor_cache:
            self._extractor_cache[language] = _extractors(
                language,
                self._module_loader,
            )
        return self._extractor_cache[language]


DEFAULT_REGISTRY = ParserRegistry()


def _parser_version(registry: ParserProvider, language: Language) -> str | None:
    if isinstance(registry, ParserRegistry):
        return registry._reported_version(language)
    return registry.versions().get(language.value)


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
    if not registry.has_parser(language):
        return _error_file(
            source,
            registry,
            "missing-parser",
            f"{source.file}: parser is unavailable for {language.value}",
        )
    parser = registry.parser_for(language)
    extractor = (
        registry._extractor_for(language)
        if isinstance(registry, ParserRegistry)
        else _extractors(language)
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
    files = tuple(
        extract_file(source, registry=registry)
        for source in sorted(sources, key=lambda item: item.file)
    )
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
