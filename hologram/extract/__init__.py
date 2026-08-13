"""Extractor dispatch: one language-neutral Symbol stream per file."""
from __future__ import annotations

from pathlib import Path

from ..symbols import Symbol, detect_language
from ..treesitter import _GRAMMAR_MODULES, _grammar_pkgs, has_parser
from .c_cpp import _extract_c, _extract_cpp
from .csharp import _extract_cs
from .go import _extract_go
from .java import _extract_java
from .kotlin import _extract_kotlin
from .misc import (_extract_bash, _extract_css, _extract_helm, _extract_html,
                   _extract_lua)
from .php import _extract_php
from .python import _extract_python
from .ruby import _extract_ruby
from .rust import _extract_rust
from .scala import _extract_scala
from .swift import _extract_swift
from .ts import _extract_sfc, _extract_ts, _extract_tsx


EXTRACTORS = {
    "java": _extract_java,
    "python": _extract_python,
    "typescript": _extract_ts,
    "javascript": _extract_ts,
    "tsx": _extract_tsx,
    "vue": _extract_sfc,
    "svelte": _extract_sfc,
    "kotlin": _extract_kotlin,
    "go": _extract_go,
    "rust": _extract_rust,
    "csharp": _extract_cs,
    "c": _extract_c,
    "cpp": _extract_cpp,
    "lua": _extract_lua,
    "html": _extract_html,
    "helm": _extract_helm,
    "bash": _extract_bash,
    "css": _extract_css,
    "ruby": _extract_ruby,
    "php": _extract_php,
    "swift": _extract_swift,
    "scala": _extract_scala,
}


def extract_file(path: Path, root: Path, text: str | None = None) -> list[Symbol]:
    lang = detect_language(path)
    if lang is None:
        return []
    extractor = EXTRACTORS.get(lang)
    if extractor is None:
        return []
    if lang in _GRAMMAR_MODULES and not has_parser(lang):
        raise SystemExit(f"{lang} extraction requires tree-sitter: "
                         f"pip install {' '.join(_grammar_pkgs([lang]))}")
    if text is None:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return []
    return extractor(text, str(path.relative_to(root)))

