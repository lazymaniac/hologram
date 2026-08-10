#!/usr/bin/env python3
"""hologram: compress a codebase into a single markdown signature listing for LLM sessions.

Deterministic. One layout: a path-compressed package trie of public signatures,
each function's project-internal calls inline after `>`.

Extraction is AST-based everywhere: tree-sitter for Java and TypeScript/JavaScript,
stdlib `ast` for Python.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import difflib
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from . import scan
from .config import (
    ProjectConfig,
    create_default_manifest,
    default_config,
    load_config,
)
from .model import CallKind, Language, SourceFile, SymbolKind, Visibility
from .model import FileIR as CanonicalFileIR
from .parsers.api import DEFAULT_REGISTRY
from .parsers.api import extract_file as extract_canonical_file
from .parsers.treesitter import GRAMMAR_METADATA
from .state import compute_state, read_digest_state

TYPE_KINDS = ("class", "interface", "record", "enum", "type")
LEGACY_EXTRACTOR_VERSIONS = {
    language.value: "legacy-1" for language in Language
}
LEGACY_PARSER_VERSIONS = {
    language.value: "legacy" for language in Language
}
_LEGACY_STATE_FORMAT_VERSION = "hologram-legacy-render-state-v1"


@dataclass
class Symbol:
    name: str
    kind: str
    file: str
    line: int
    signature: str = ""
    params: list[str] = field(default_factory=list)
    returns: str | None = None
    visibility: str = "pub"
    container: str | None = None
    lang: str = ""
    calls: list[str] = field(default_factory=list)
    supers: list[str] = field(default_factory=list)
    permits: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)  # var/param/field name -> declared type
    size: int = 0  # body line count (0 = bodyless/unknown)


def detect_language(path: Path) -> str | None:
    language = scan.detect_language(path)
    return language.value if language is not None else None


def scan_files(root: Path, config: ProjectConfig | None = None) -> list[Path]:
    """Return v1 paths backed by the complete v2 source-candidate ledger."""
    result = scan.scan_project(root.resolve(), config or default_config())
    if not result.complete:
        detail = "; ".join(diagnostic.message for diagnostic in result.diagnostics)
        raise SystemExit(detail or "source scan incomplete")
    return [source.path for source in result.sources]


# ---------------------------------------------------------------------------
# Shared text utilities
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_LINE_COMMENT_RE = re.compile(r"//[^\n]*|#[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def strip_comments_and_strings(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub('"s"', text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


def _base_type(t: str) -> str:
    """Bare type name: Map<K,V> -> Map, list[X] -> list, String[] -> String."""
    return re.sub(r"[<\[(].*", "", t).strip()


# ---------------------------------------------------------------------------
# Parser availability retained for the v1 CLI compatibility surface
# ---------------------------------------------------------------------------

def has_parser(lang: str) -> bool:
    try:
        language = Language(lang)
    except ValueError:
        return False
    return DEFAULT_REGISTRY.has_parser(language)


def _grammar_pkgs(langs) -> list[str]:
    languages = {Language(lang) for lang in langs}
    return ["tree-sitter"] + sorted(
        {
            GRAMMAR_METADATA[language].distribution
            for language in languages
            if language in GRAMMAR_METADATA
        }
    )


_LEGACY_CANONICAL_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.CONSTRUCTOR,
        SymbolKind.ENUM,
        SymbolKind.FUNCTION,
        SymbolKind.INTERFACE,
        SymbolKind.METHOD,
        SymbolKind.PROPERTY,
        SymbolKind.RECORD,
        SymbolKind.REEXPORT,
        SymbolKind.TYPE,
    }
)
_LEGACY_CALLABLE_KINDS = frozenset(
    {
        SymbolKind.CONSTRUCTOR,
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.PROPERTY,
    }
)
_LEGACY_TYPESCRIPT_LANGUAGES = frozenset(
    {
        Language.JAVASCRIPT,
        Language.SVELTE,
        Language.TSX,
        Language.TYPESCRIPT,
        Language.VUE,
    }
)
_LEGACY_TASK5_LANGUAGES = frozenset(
    {Language.CSHARP, Language.GO, Language.KOTLIN, Language.RUST}
)


def _legacy_call_name(call) -> str:
    if call.receiver in {None, "cls", "self", "this"}:
        return call.name
    return f"{call.receiver}.{call.name}"


def _legacy_task5_call_name(language: Language, call) -> str | None:
    if language is Language.GO and call.kind is CallKind.CONSTRUCT:
        return None
    if call.kind is CallKind.CONSTRUCT:
        return call.name
    receiver = call.receiver
    if receiver in {None, "self", "this"}:
        return call.name
    if re.fullmatch(r"(?:[^\W\d]|\$)[\w$]*", receiver, re.UNICODE):
        return f"{receiver}.{call.name}"
    if (
        language is Language.RUST
        and "::" in receiver
        and "(" not in receiver
        and "." not in receiver
    ):
        return f"{receiver.rsplit('::', 1)[-1]}.{call.name}"
    return call.name


def _legacy_typescript_call_name(call) -> str | None:
    if call.kind is CallKind.CONSTRUCT:
        return re.sub(r"<.*", "", call.name)
    if not re.fullmatch(r"(?:[^\W\d]|\$)[\w$]*", call.name, re.UNICODE):
        return None
    if (
        call.receiver is not None
        and call.receiver != "this"
        and re.fullmatch(
            r"(?:[^\W\d]|\$)[\w$]*",
            call.receiver,
            re.UNICODE,
        )
    ):
        return f"{call.receiver}.{call.name}"
    return call.name


def _legacy_span_bytes(file_ir: CanonicalFileIR, span) -> bytes:
    lines = file_ir.source.raw.splitlines(keepends=True)
    start = span.start_line - 1
    end = span.end_line - 1
    if start == end:
        return lines[start][span.start_column : span.end_column]
    return b"".join(
        (
            lines[start][span.start_column :],
            *lines[start + 1 : end],
            lines[end][: span.end_column],
        )
    )


def _legacy_java_call_name(file_ir: CanonicalFileIR, call) -> str | None:
    if call.kind is CallKind.CONSTRUCT:
        if call.name in {"super", "this"}:
            return None
        raw = _legacy_span_bytes(file_ir, call.span).lstrip()
        constructor = raw[4:] if raw.startswith(b"new ") else raw
        bracket = constructor.find(b"[")
        arguments = constructor.find(b"(")
        if bracket >= 0 and (arguments < 0 or bracket < arguments):
            return None
        return call.name
    if call.receiver in {None, "super", "this"}:
        return call.name
    if re.fullmatch(r"(?:[^\W\d]|\$)[\w$]*", call.receiver, re.UNICODE):
        return f"{call.receiver}.{call.name}"
    return call.name


def _legacy_python_call_spans(
    file_ir: CanonicalFileIR,
    symbol,
) -> tuple[tuple[int, int, int, int], ...]:
    """Recover v1's AST walk order only at the compatibility boundary."""
    try:
        tree = ast.parse(file_ir.source.text)
    except SyntaxError:
        return ()
    owner = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol.name
            and node.lineno == symbol.span.start_line
            and node.col_offset == symbol.span.start_column
        ),
        None,
    )
    if owner is None:
        return ()
    return tuple(
        (
            node.lineno,
            node.col_offset,
            node.end_lineno or node.lineno,
            node.end_col_offset or node.col_offset,
        )
        for node in ast.walk(owner)
        if isinstance(node, ast.Call)
    )


def _legacy_calls(file_ir: CanonicalFileIR, symbol) -> list[str]:
    if symbol.kind not in _LEGACY_CALLABLE_KINDS:
        return []
    if (
        file_ir.source.language in _LEGACY_TYPESCRIPT_LANGUAGES
        and symbol.kind is SymbolKind.CONSTRUCTOR
    ):
        return []
    if file_ir.source.language is Language.HELM:
        return []
    if file_ir.source.language is Language.PYTHON:
        ranks = {
            span: rank
            for rank, span in enumerate(_legacy_python_call_spans(file_ir, symbol))
        }
        owned = [
            call
            for call in file_ir.calls
            if (
                call.span.start_line,
                call.span.start_column,
                call.span.end_line,
                call.span.end_column,
            )
            in ranks
        ]
        owned.sort(
            key=lambda call: ranks[
                (
                    call.span.start_line,
                    call.span.start_column,
                    call.span.end_line,
                    call.span.end_column,
                )
            ]
        )
    else:
        owned = [call for call in file_ir.calls if call.caller == symbol.id]
        if file_ir.source.language in _LEGACY_TASK5_LANGUAGES:
            owned.sort(
                key=lambda call: (
                    call.span.start_line,
                    call.span.start_column,
                    -call.span.end_line,
                    -call.span.end_column,
                )
            )
    result: list[str] = []
    for call in owned:
        if (
            file_ir.source.language is Language.C
            and call.kind is CallKind.CONSTRUCT
        ):
            continue
        if file_ir.source.language is Language.JAVA:
            name = _legacy_java_call_name(file_ir, call)
        elif file_ir.source.language in _LEGACY_TYPESCRIPT_LANGUAGES:
            name = _legacy_typescript_call_name(call)
        elif file_ir.source.language in _LEGACY_TASK5_LANGUAGES:
            name = _legacy_task5_call_name(file_ir.source.language, call)
        else:
            name = _legacy_call_name(call)
        if name is None:
            continue
        if name == symbol.name or name in result:
            continue
        result.append(name)
    return result[:12]


def _legacy_typescript_lang(file_ir: CanonicalFileIR, symbol) -> str:
    language = file_ir.source.language
    if language in {Language.VUE, Language.SVELTE} and symbol.signature.startswith(
        "component "
    ):
        return language.value
    return Language.TYPESCRIPT.value


def _legacy_typescript_bindings(file_ir: CanonicalFileIR, symbol) -> dict[str, str]:
    if symbol.kind not in _LEGACY_CALLABLE_KINDS:
        return {binding.name: binding.type_name for binding in symbol.bindings}
    if symbol.kind is SymbolKind.CONSTRUCTOR:
        return {}
    body = next((body for body in file_ir.bodies if body.owner == symbol.id), None)
    parameter_names = (
        {
            event.text
            for event in body.events
            if event.kind.value == "param"
        }
        if body is not None
        else set()
    )
    declaration = _legacy_span_bytes(file_ir, symbol.span).decode(
        "utf-8",
        errors="replace",
    )
    simple_parameters = {
        name
        for name in parameter_names
        if re.search(
            rf"(?:^|[(,])\s*(?:(?:private|protected|public|readonly)\s+)*"
            rf"{re.escape(name)}\??\s*:",
            declaration,
        )
    }
    explicit_class_bindings = {
        candidate.name
        for candidate in file_ir.symbols
        if candidate.id.container_path == symbol.id.container_path
        and candidate.kind in {SymbolKind.FIELD, SymbolKind.PROPERTY}
        and candidate.returns is not None
    }
    class_binding_names = {
        candidate.name
        for candidate in file_ir.symbols
        if candidate.id.container_path == symbol.id.container_path
        and candidate.kind in {SymbolKind.FIELD, SymbolKind.PROPERTY}
    }
    result: dict[str, str] = {}
    for binding in symbol.bindings:
        if binding.name in parameter_names:
            if binding.name in simple_parameters:
                result[binding.name] = binding.type_name
            continue
        if binding.name in class_binding_names:
            if binding.name in explicit_class_bindings:
                result[binding.name] = binding.type_name
            continue
        result[binding.name] = binding.type_name
    return result


def _legacy_body_binding_names(file_ir: CanonicalFileIR, symbol, kind: str) -> set[str]:
    body = next((item for item in file_ir.bodies if item.owner == symbol.id), None)
    if body is None:
        return set()
    return {event.text for event in body.events if event.kind.value == kind}


def _legacy_task5_bindings(
    file_ir: CanonicalFileIR,
    symbol,
    interface_paths: set[tuple[str, ...]],
) -> dict[str, str]:
    language = file_ir.source.language
    canonical = {binding.name: binding.type_name for binding in symbol.bindings}
    parameters = _legacy_body_binding_names(file_ir, symbol, "param")
    locals_ = _legacy_body_binding_names(file_ir, symbol, "local")
    if not parameters and not locals_:
        parameters = set(canonical)

    if language is Language.GO:
        if symbol.id.container_path in interface_paths:
            return {}
        fields = {
            candidate.name
            for candidate in file_ir.symbols
            if candidate.kind is SymbolKind.FIELD
            and candidate.id.container_path == symbol.id.container_path
        }
        allowed = parameters | fields | {
            name for name in locals_ if canonical.get(name) != "?"
        }
        return {name: value for name, value in canonical.items() if name in allowed}

    if language is Language.RUST:
        allowed = parameters | {
            name for name in locals_ if canonical.get(name) != "?"
        }
        result = {
            name: value
            for name, value in canonical.items()
            if name in allowed and name != "self"
        }
        if (
            symbol.kind is SymbolKind.METHOD
            and symbol.id.container_path not in interface_paths
            and symbol.container is not None
        ):
            result["self"] = (
                symbol.id.container_path[-2]
                if len(symbol.id.container_path) >= 2
                and symbol.id.container_path[-1].startswith("impl ")
                else symbol.container
            )
        return result

    if language is Language.CSHARP:
        fields = {
            candidate.name
            for candidate in file_ir.symbols
            if candidate.kind in {SymbolKind.CONSTANT, SymbolKind.FIELD}
            and candidate.id.container_path == symbol.id.container_path
        }
        allowed = parameters | fields | {
            name for name in locals_ if canonical.get(name) != "?"
        }
        return {name: value for name, value in canonical.items() if name in allowed}

    enclosing_type = next(
        (
            candidate
            for candidate in file_ir.symbols
            if candidate.kind
            in {
                SymbolKind.CLASS,
                SymbolKind.ENUM,
                SymbolKind.INTERFACE,
                SymbolKind.RECORD,
            }
            and (*candidate.id.container_path, candidate.name)
            == symbol.id.container_path
        ),
        None,
    )
    constructor = next(
        (
            candidate
            for candidate in file_ir.symbols
            if candidate.kind is SymbolKind.CONSTRUCTOR
            and candidate.id.container_path == symbol.id.container_path
            and enclosing_type is not None
            and candidate.name == enclosing_type.name
            and candidate.params == enclosing_type.params
        ),
        None,
    )
    class_parameters = (
        {binding.name for binding in constructor.bindings}
        if constructor is not None
        else set()
    )
    allowed = parameters | class_parameters
    return {name: value for name, value in canonical.items() if name in allowed}


def _legacy_task5_supported(
    file_ir: CanonicalFileIR,
    symbol,
    type_paths: set[tuple[str, ...]],
    companion_paths: set[tuple[str, ...]],
) -> bool:
    language = file_ir.source.language
    if language not in _LEGACY_TASK5_LANGUAGES:
        return True
    if symbol.kind in {SymbolKind.PROPERTY, SymbolKind.TYPE}:
        return False
    if (*symbol.id.container_path, symbol.name) in companion_paths:
        return False
    if any(
        symbol.id.container_path[: len(path)] == path for path in companion_paths
    ):
        return False
    if language is Language.KOTLIN and symbol.kind is SymbolKind.CONSTRUCTOR:
        return False
    if symbol.kind is SymbolKind.FUNCTION:
        return not symbol.id.container_path and language in {
            Language.GO,
            Language.KOTLIN,
            Language.RUST,
        }
    if symbol.kind in {SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}:
        if language is Language.RUST:
            return True
        return symbol.id.container_path in type_paths
    return True


def _legacy_go_type(type_name: str) -> str:
    if type_name.startswith("..."):
        return f"...{type_name[3:].lstrip('*&')}"
    return type_name.lstrip("*&")


def _legacy_go_shape(symbol) -> tuple[list[str], str | None, str]:
    params = [_legacy_go_type(value) for value in symbol.params]
    returns = _legacy_go_type(symbol.returns) if symbol.returns is not None else None
    if symbol.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD}:
        suffix = f":{returns}" if returns else ""
        signature = f"{symbol.name}({','.join(params)}){suffix}"
    else:
        signature = symbol.signature
    return params, returns, signature


def _legacy_kotlin_shape(symbol) -> tuple[list[str], str | None, str]:
    params = list(symbol.params)
    if "extension" not in symbol.modifiers or not params:
        return params, symbol.returns, symbol.signature
    params = params[1:]
    suffix = f":{symbol.returns}" if symbol.returns and symbol.returns != "Unit" else ""
    return params, symbol.returns, f"{symbol.name}({','.join(params)}){suffix}"


def _legacy_task5_container(language: Language, symbol) -> str | None:
    if (
        language is Language.RUST
        and len(symbol.id.container_path) >= 2
        and symbol.id.container_path[-1].startswith("impl ")
    ):
        return symbol.id.container_path[-2]
    return symbol.container


def _legacy_lua_params(file_ir: CanonicalFileIR, symbol) -> list[str]:
    body = next((item for item in file_ir.bodies if item.owner == symbol.id), None)
    if body is None:
        return []
    return list(
        dict.fromkeys(
            event.text for event in body.events if event.kind.value == "param"
        )
    )


def _legacy_c_bindings(file_ir: CanonicalFileIR, symbol) -> dict[str, str]:
    canonical = {binding.name: binding.type_name for binding in symbol.bindings}
    body = next((item for item in file_ir.bodies if item.owner == symbol.id), None)
    if body is None:
        return {} if file_ir.source.language is Language.C else canonical
    parameters = {
        event.text for event in body.events if event.kind.value == "param"
    }
    return {name: value for name, value in canonical.items() if name in parameters}


@lru_cache(maxsize=64)
def _legacy_cpp_declaration_provenance(
    source: SourceFile,
) -> tuple[tuple[tuple[object, ...], int], ...]:
    """Recover v1 member-declaration lines from the immutable source snapshot."""
    parser = DEFAULT_REGISTRY.parser_for(Language.CPP)
    if parser is None or not callable(getattr(parser, "parse", None)):
        return ()
    from .parsers._treesitter_common import walk_all
    from .parsers.c_family import (
        _direct_declarators,
        _function_parts,
        _parameters,
        _qualified_parts,
        _Scopes,
    )

    root = parser.parse(source.raw).root_node  # type: ignore[attr-defined]
    scopes = _Scopes(root)
    type_paths = frozenset(scopes.types.values())
    values: list[tuple[tuple[object, ...], int]] = []
    for node in walk_all(root):
        if node.type not in {"declaration", "field_declaration"}:
            continue
        for declarator in _direct_declarators(node):
            parts = _function_parts(declarator)
            if parts is None:
                continue
            function, name_node = parts
            qualified = _qualified_parts(name_node.text.decode("utf-8"))
            if not qualified:
                continue
            owner = scopes.owner(node, callables=False)
            if len(qualified) > 1:
                owner = (*scopes.namespace_owner(node), *qualified[:-1])
            if owner not in type_paths:
                continue
            name = qualified[-1]
            kind = (
                SymbolKind.CONSTRUCTOR
                if owner and name == owner[-1]
                else SymbolKind.METHOD
            )
            signature_key = f"({','.join(p.type_name for p in _parameters(function))})"
            key = (owner, kind, name, signature_key)
            values.append((key, node.start_point[0] + 1))
    return tuple(values)


def _legacy_cpp_declaration_line(
    file_ir: CanonicalFileIR,
    symbol,
) -> int | None:
    key = (
        symbol.id.container_path,
        symbol.kind,
        symbol.name,
        symbol.id.signature_key,
    )
    return dict(_legacy_cpp_declaration_provenance(file_ir.source)).get(key)


def _legacy_task6_container(language: Language, symbol) -> str | None:
    if language in {Language.C, Language.HTML}:
        return None
    if language is Language.CPP:
        if symbol.kind in {
            SymbolKind.CLASS,
            SymbolKind.ENUM,
            SymbolKind.INTERFACE,
            SymbolKind.RECORD,
            SymbolKind.TYPE,
        }:
            return None
        return symbol.container
    if language is Language.LUA:
        if symbol.kind is SymbolKind.FUNCTION:
            return None
        return symbol.id.container_path[0] if symbol.id.container_path else None
    return _legacy_task5_container(language, symbol)


def _canonical_to_legacy(file_ir: CanonicalFileIR) -> list[Symbol]:
    projected: list[Symbol] = []
    type_paths = {
        (*symbol.id.container_path, symbol.name)
        for symbol in file_ir.symbols
        if symbol.kind
        in {
            SymbolKind.CLASS,
            SymbolKind.ENUM,
            SymbolKind.INTERFACE,
            SymbolKind.RECORD,
        }
    }
    companion_paths = {
        (*symbol.id.container_path, symbol.name)
        for symbol in file_ir.symbols
        if file_ir.source.language is Language.KOTLIN
        and symbol.kind is SymbolKind.CLASS
        and "companion" in symbol.modifiers
    }
    interface_paths = {
        (*symbol.id.container_path, symbol.name)
        for symbol in file_ir.symbols
        if symbol.kind is SymbolKind.INTERFACE
    }
    for symbol in file_ir.symbols:
        if symbol.kind not in _LEGACY_CANONICAL_KINDS:
            continue
        if not _legacy_task5_supported(
            file_ir,
            symbol,
            type_paths,
            companion_paths,
        ):
            continue
        if file_ir.source.language in _LEGACY_TYPESCRIPT_LANGUAGES:
            if symbol.kind is SymbolKind.PROPERTY:
                continue
            if (
                symbol.kind is SymbolKind.METHOD
                and symbol.id.container_path in interface_paths
            ):
                continue
        if file_ir.source.language is Language.PYTHON:
            if symbol.kind in {
                SymbolKind.CLASS,
                SymbolKind.ENUM,
                SymbolKind.FUNCTION,
            }:
                supported = not symbol.id.container_path
            elif symbol.kind in {SymbolKind.METHOD, SymbolKind.PROPERTY}:
                supported = len(symbol.id.container_path) == 1
            else:
                supported = False
            if not supported:
                continue
        if file_ir.source.language is Language.GO:
            params, returns, signature = _legacy_go_shape(symbol)
        elif file_ir.source.language is Language.KOTLIN:
            params, returns, signature = _legacy_kotlin_shape(symbol)
        elif file_ir.source.language is Language.LUA:
            params = _legacy_lua_params(file_ir, symbol)
            returns = symbol.returns
            signature = f"{symbol.name}({','.join(params)})"
        else:
            params, returns, signature = (
                list(symbol.params),
                symbol.returns,
                symbol.signature,
            )
        projected.append(
            Symbol(
                name=symbol.name,
                kind=(
                    SymbolKind.METHOD.value
                    if symbol.kind is SymbolKind.PROPERTY
                    else symbol.kind.value
                ),
                file=symbol.file,
                line=(
                    _legacy_cpp_declaration_line(file_ir, symbol)
                    or symbol.span.start_line
                    if file_ir.source.language is Language.CPP
                    else symbol.span.start_line
                ),
                signature=signature,
                params=params,
                returns=returns,
                visibility=(
                    "pub"
                    if symbol.visibility is Visibility.PUBLIC
                    or (
                        file_ir.source.language is Language.JAVA
                        and symbol.visibility is Visibility.INTERNAL
                    )
                    else "priv"
                ),
                container=(
                    None
                    if file_ir.source.language
                    in {Language.JAVA, *_LEGACY_TASK5_LANGUAGES}
                    and symbol.kind
                    in {
                        SymbolKind.CLASS,
                        SymbolKind.ENUM,
                        SymbolKind.INTERFACE,
                        SymbolKind.RECORD,
                        SymbolKind.TYPE,
                    }
                    else _legacy_task6_container(file_ir.source.language, symbol)
                ),
                lang=(
                    _legacy_typescript_lang(file_ir, symbol)
                    if file_ir.source.language in _LEGACY_TYPESCRIPT_LANGUAGES
                    else symbol.lang.value
                ),
                calls=_legacy_calls(file_ir, symbol),
                supers=list(symbol.supers),
                permits=list(symbol.permits),
                raises=list(symbol.raises),
                bindings=(
                    _legacy_typescript_bindings(file_ir, symbol)
                    if file_ir.source.language in _LEGACY_TYPESCRIPT_LANGUAGES
                    else _legacy_task5_bindings(
                        file_ir,
                        symbol,
                        interface_paths,
                    )
                    if file_ir.source.language in _LEGACY_TASK5_LANGUAGES
                    else {}
                    if file_ir.source.language is Language.LUA
                    else _legacy_c_bindings(file_ir, symbol)
                    if file_ir.source.language in {Language.C, Language.CPP}
                    else {
                        binding.name: binding.type_name
                        for binding in symbol.bindings
                    }
                ),
                size=(
                    0
                    if (
                        file_ir.source.language in _LEGACY_TYPESCRIPT_LANGUAGES
                        or file_ir.source.language is Language.CSHARP
                    )
                    and symbol.kind is SymbolKind.CONSTRUCTOR
                    else 0
                    if file_ir.source.language is Language.CPP
                    and _legacy_cpp_declaration_line(file_ir, symbol) is not None
                    else symbol.body_lines
                ),
            )
        )
    return projected


def extract_file(path: Path, root: Path, text: str | None = None) -> list[Symbol]:
    lang = detect_language(path)
    if lang is None:
        return []
    language = Language(lang)
    if language in GRAMMAR_METADATA and not has_parser(lang):
        raise SystemExit(f"{lang} extraction requires tree-sitter: "
                         f"pip install {' '.join(_grammar_pkgs([lang]))}")
    if text is None:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return []
    rel = path.relative_to(root).as_posix()
    raw = text.encode("utf-8")
    source = SourceFile(
        path,
        rel,
        language,
        scan._source_role(rel),
        raw,
        hashlib.sha256(raw).hexdigest(),
    )
    return _canonical_to_legacy(extract_canonical_file(source))


# ---------------------------------------------------------------------------
# Gather + fan-in
# ---------------------------------------------------------------------------

def _effective_config(
    config: ProjectConfig,
    langs: set[str] | None,
) -> ProjectConfig:
    return dataclasses.replace(
        config,
        languages=(
            tuple(Language(value) for value in sorted(langs))
            if langs is not None
            else config.languages
        ),
    )


def _legacy_state(
    base_state: str,
    *,
    private_sigs: bool,
    behaviors: bool,
) -> str:
    modes = json.dumps(
        {
            "behaviors": behaviors,
            "private_sigs": private_sigs,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    hasher = hashlib.sha256()
    for label, value in (
        ("format", _LEGACY_STATE_FORMAT_VERSION.encode("utf-8")),
        ("base-state", base_state.encode("ascii")),
        ("render-modes", modes),
    ):
        label_bytes = label.encode("utf-8")
        hasher.update(len(label_bytes).to_bytes(4, "big"))
        hasher.update(label_bytes)
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)
    return hasher.hexdigest()


def _gather(
    root: Path,
    langs: set[str] | None,
    config: ProjectConfig,
    private_sigs: bool = False,
    behaviors: bool = False,
    *,
    scan_result: scan.ScanResult | None = None,
):
    """Extract symbols, identifier-token sets per file, and the corpus state hash.
    `langs` restricts to those languages (e.g. {"java"}); None means all."""
    root = root.resolve()
    effective_config = _effective_config(config, langs)
    if scan_result is None:
        scan_result = scan.scan_project(root, effective_config)
    if not scan_result.complete:
        detail = "; ".join(
            diagnostic.message for diagnostic in scan_result.diagnostics
        )
        raise SystemExit(detail or "source scan incomplete")

    files = [source.path for source in scan_result.sources]
    missing = _missing_parser_langs(files)
    if missing:
        _bootstrap_or_die(missing, [])

    symbols: list[Symbol] = []
    file_tokens: dict[str, set[str]] = {}
    loc = 0
    for source in scan_result.sources:
        rel = source.file
        text = source.text
        symbols.extend(extract_file(source.path, root, text))
        file_tokens[rel] = set(_IDENT_RE.findall(strip_comments_and_strings(text)))
        loc += text.count("\n") + 1

    state = compute_state(
        root,
        effective_config,
        scan_result,
        extractor_versions=LEGACY_EXTRACTOR_VERSIONS,
        parser_versions=LEGACY_PARSER_VERSIONS,
    )
    rendered_state = _legacy_state(
        state.value,
        private_sigs=private_sigs,
        behaviors=behaviors,
    )
    return files, symbols, file_tokens, rendered_state, loc


def _state_hash(
    root: Path,
    config: ProjectConfig,
    langs: set[str] | None = None,
    private_sigs: bool = False,
    behaviors: bool = False,
    *,
    scan_result: scan.ScanResult | None = None,
) -> str:
    """Compute the versioned state over one immutable scanner snapshot."""
    root = root.resolve()
    effective_config = _effective_config(config, langs)
    if scan_result is None:
        scan_result = scan.scan_project(root, effective_config)
    state = compute_state(
        root,
        effective_config,
        scan_result,
        extractor_versions=LEGACY_EXTRACTOR_VERSIONS,
        parser_versions=LEGACY_PARSER_VERSIONS,
    )
    if not state.complete:
        detail = "; ".join(
            diagnostic.message for diagnostic in state.diagnostics
        )
        raise SystemExit(detail or "source scan incomplete")
    return _legacy_state(
        state.value,
        private_sigs=private_sigs,
        behaviors=behaviors,
    )


def _digest_state(out_path: Path) -> str | None:
    """The `state` stamp recorded in an existing digest's header, if any."""
    return read_digest_state(out_path)


def _fan_in_from_tokens(symbols: list[Symbol], file_tokens: dict[str, set[str]]) -> dict[str, float]:
    """Cross-file references per defining file: names defined everywhere (main, build) score low."""
    defined: dict[str, set[str]] = {}
    for s in symbols:
        defined.setdefault(s.name, set()).add(s.file)
    return {
        name: sum(1 for rel, tokens in file_tokens.items()
                  if name in tokens and rel not in own_files) / len(own_files)
        for name, own_files in defined.items()
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F",
               "type": "T"}


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def git_head(root: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "worktree"
    except (OSError, subprocess.TimeoutExpired):
        return "worktree"


def _is_test_path(rel: str) -> bool:
    parts = [p.lower() for p in Path(rel).parts]
    stem = Path(rel).stem
    return (any(p in ("test", "tests") for p in parts)
            or stem.endswith("Test") or stem.startswith("test_"))


def _tree_lines(payload_by_dir: dict[str, list[str]]) -> list[str]:
    """Render dir paths as a path-compressed trie: shared prefixes stated once.
    Payload lines carry their own relative indent; the trie adds depth indent."""
    tree: dict = {}
    for d in sorted(payload_by_dir):
        node = tree
        for part in Path(d).parts:
            node = node.setdefault(part, {})
        node.setdefault("\0", []).extend(payload_by_dir[d])

    out: list[str] = []

    def emit(node: dict, label: str | None, depth: int) -> None:
        children = {k: v for k, v in node.items() if k != "\0"}
        payload = node.get("\0", [])
        while label is not None and len(children) == 1 and not payload:
            (k, child), = children.items()
            label = f"{label}/{k}"
            payload = child.get("\0", [])
            children = {kk: vv for kk, vv in child.items() if kk != "\0"}
        base = depth
        if label is not None:
            out.append(" " * depth + label)
            base = depth + 1
        for ln in payload:
            out.append(" " * base + ln)
        for k in sorted(children):
            emit(children[k], k, base)

    emit(tree, None, 0)
    return out


def _strip_exc(name: str) -> str:
    return name.removesuffix("Exception") or name


def _sccs(edges: dict[str, set[str]]) -> dict[str, int]:
    """Tarjan SCC (iterative) -> node -> component id. `edges` must contain every
    node as a key (empty set for leaves)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp: dict[str, int] = {}
    counter = ncomp = 0

    for root in sorted(edges):
        if root in index:
            continue
        work: list[tuple[str, list[str], int]] = [(root, sorted(edges[root]), 0)]
        index[root] = low[root] = counter; counter += 1
        stack.append(root); on_stack.add(root)
        while work:
            v, succs, i = work[-1]
            if i < len(succs):
                work[-1] = (v, succs, i + 1)
                w = succs[i]
                if w not in index:
                    index[w] = low[w] = counter; counter += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, sorted(edges[w]), 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                while True:
                    w = stack.pop(); on_stack.discard(w)
                    comp[w] = ncomp
                    if w == v:
                        break
                ncomp += 1
    return comp


def _reduce_calls(edges: dict[str, set[str]],
                  nodes_by_sym: dict[int, list[str]],
                  kept_by_sym: dict[int, list[str]]) -> dict[int, list[str]]:
    """Transitive reduction per call list: drop an entry whose callee is already
    reachable through a sibling entry. Nodes are Type.method where the receiver was
    resolved (precise) and bare names otherwise (conservative merge); SCC-safe."""
    all_nodes = set(edges) | {c for cs in edges.values() for c in cs}
    edges = {n: set(edges.get(n, ())) for n in all_nodes}
    comp = _sccs(edges)
    cedges: dict[int, set[int]] = {}
    for src, dsts in edges.items():
        for d in dsts:
            if comp[src] != comp[d]:
                cedges.setdefault(comp[src], set()).add(comp[d])
    reach_memo: dict[int, set[int]] = {}

    def reach(c: int) -> set[int]:
        if c in reach_memo:
            return reach_memo[c]
        reach_memo[c] = set()  # DAG of SCCs: placeholder never read
        out: set[int] = set()
        for d in cedges.get(c, ()):
            out.add(d)
            out |= reach(d)
        reach_memo[c] = out
        return out

    reduced: dict[int, list[str]] = {}
    for sid, calls in kept_by_sym.items():
        nodes = nodes_by_sym[sid]
        keep = []
        for i, c in enumerate(calls):
            ci = comp.get(nodes[i])
            implied = ci is not None and any(
                comp.get(nodes[j]) not in (None, ci) and ci in reach(comp[nodes[j]])
                for j in range(len(calls)) if j != i)
            if not implied:
                keep.append(c)
        reduced[sid] = keep
    return reduced


_BOILERPLATE_PARTS = ("src", "main", "java", "kotlin", "test", "tests", "lib")


def _dep_lines(symbols: list[Symbol], file_tokens: dict[str, set[str]],
               min_refs: int = 2) -> list[str]:
    """Module dependency edges (`a→b` = code in a references types defined in b),
    from data already in hand. Modules are top path segments after boilerplate
    and the corpus-wide shared prefix."""
    type_dir: dict[str, str] = {}
    for s in symbols:
        if s.kind in TYPE_KINDS and not _is_test_path(s.file):
            type_dir.setdefault(s.name, str(Path(s.file).parent))
    dirs = {str(Path(rel).parent) for rel in file_tokens} | set(type_dir.values())
    stripped = {d: [p for p in Path(d).parts if p not in _BOILERPLATE_PARTS]
                for d in dirs}
    common: list[str] = []
    lists = [p for p in stripped.values() if p]
    while lists and all(len(p) > len(common) + 1 for p in lists) \
            and len({p[len(common)] for p in lists}) == 1:
        common.append(lists[0][len(common)])

    def label(d: str) -> str:
        parts = stripped[d]
        if common and parts[:len(common)] == common and len(parts) > len(common):
            parts = parts[len(common):]
        return parts[0] if parts else "."

    counts: dict[tuple[str, str], int] = {}
    for rel, toks in file_tokens.items():
        if _is_test_path(rel):
            continue
        m_from = label(str(Path(rel).parent))
        for t in toks & set(type_dir):
            m_to = label(type_dir[t])
            if m_from != m_to:
                counts[(m_from, m_to)] = counts.get((m_from, m_to), 0) + 1
    by_src: dict[str, list[str]] = {}
    for (a, b), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= min_refs:
            by_src.setdefault(a, []).append(b)
    cells = [f"{a}→{','.join(bs)}" for a, bs in sorted(by_src.items())]
    lines, cur = [], ""
    for c in cells:
        if cur and len(cur) + len(c) + 3 > 110:
            lines.append(f"· deps {cur}")
            cur = c
        else:
            cur = f"{cur} | {c}" if cur else c
    if cur:
        lines.append(f"· deps {cur}")
    return lines


def _ubiquitous_calls(fns_by_lang: dict[str, list[Symbol]]) -> set[str]:
    """Callees named by >25% of a language's functions (log/guard helpers): noise."""
    ubiquitous: set[str] = set()
    for lang_fns in fns_by_lang.values():
        if len(lang_fns) < 20:
            continue
        df: dict[str, int] = {}
        for s in lang_fns:
            for c in set(s.calls):
                df[c] = df.get(c, 0) + 1
        ubiquitous |= {c for c, n in df.items() if n / len(lang_fns) > 0.25}
    return ubiquitous


def _total_loc(files: list[Path]) -> int:
    loc = 0
    for f in files:
        try:
            loc += f.read_text(errors="replace").count("\n") + 1
        except OSError:
            pass
    return loc


def render_simple(root: Path, symbols: list[Symbol], files: list[Path],
                  regen_cmd: str, scores: dict[str, float] | None = None,
                  private_sigs: bool = False, tested: set[str] | None = None,
                  behaviors: bool = False, state: str = "",
                  deps: list[str] | None = None,
                  loc: int | None = None) -> str:
    """Signatures only, as a package trie; each function's calls inline after `>`.
    Private members render as packed name lists (`- a,b`), or as full `-`-prefixed
    signatures when `private_sigs` is set.

    pkg
      Class(K: components)
        sig > callee, callee
        - privateName,privateName
    """
    prod = [s for s in symbols if not _is_test_path(s.file)]
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if (s.kind in TYPE_KINDS + ("fn",) and s.container is None
                and (s.visibility == "pub" or private_sigs)):
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    # owner keys carry lang so same-named types from different languages in one
    # dir (Pricer in go + rust) don't merge their method lists
    methods_by_owner: dict[tuple[str, str, str], list[Symbol]] = {}
    for s in prod:
        if (s.container and s.kind == "method"
                and (s.visibility == "pub" or private_sigs)):
            methods_by_owner.setdefault(
                (str(Path(s.file).parent), s.container, s.lang), []).append(s)
    # names-only private inventory (used when private_sigs is off)
    priv_methods_by_owner: dict[tuple[str, str, str], list[str]] = {}
    priv_top_by_file: dict[tuple[str, str], list[str]] = {}
    if not private_sigs:
        for s in prod:
            if s.visibility != "priv":
                continue
            if s.container and s.kind == "method":
                key = (str(Path(s.file).parent), s.container, s.lang)
                priv_methods_by_owner.setdefault(key, []).append(s.name)
            elif s.container is None and s.kind in TYPE_KINDS + ("fn",):
                key = (str(Path(s.file).parent), Path(s.file).name)
                priv_top_by_file.setdefault(key, []).append(s.name)

    defined = {s.name for s in symbols}
    project_types = {s.name for s in prod if s.kind in TYPE_KINDS}
    by_lang: dict[str, list[Symbol]] = {}
    for s in prod:
        if s.kind in ("fn", "method"):
            by_lang.setdefault(s.lang, []).append(s)
    ubiquitous = _ubiquitous_calls(by_lang)

    # A call is shown only if it names something defined in this project — platform
    # calls carry no project semantics. Receivers with a known declared type are
    # resolved: rendered as Type.method when the type is project-owned, dropped when
    # it is a platform type (kills name-collision noise like bigint.signum). Unknown
    # receivers fall back to the name-based rule. Ubiquitous helpers always drop.
    def _filter_calls(sym: Symbol) -> list[str]:
        kept: list[str] = []
        for c in sym.calls:
            recv, _, m = c.rpartition(".")
            if m in ubiquitous:
                continue
            if not recv:
                if m in defined:
                    kept.append(m)
                continue
            if recv in sym.bindings:
                t = _base_type(sym.bindings[recv])
                if t in project_types and m in defined:
                    kept.append(f"{t}.{m}")
                continue
            if recv in project_types:
                if m in defined:
                    kept.append(c)
                continue
            if recv[:1].isupper():
                continue  # unresolved TypeName (List, SpringApplication) -> platform
            if m in defined:
                kept.append(c)
        return list(dict.fromkeys(kept))

    # Call graph for transitive reduction. A method of a project type is the node
    # Type.name; a resolved call targets exactly that node, a bare call targets the
    # name node, which fans out to every Type.name defining it.
    def _call_node(entry: str) -> str:
        recv, _, m = entry.rpartition(".")
        return entry if recv in project_types else m

    fns = [s for s in prod if s.kind in ("fn", "method")]
    kept_by_sym = {id(s): _filter_calls(s) for s in fns}
    nodes_by_sym = {sid: [_call_node(c) for c in calls]
                    for sid, calls in kept_by_sym.items()}
    edges: dict[str, set[str]] = {}
    for s in fns:
        src = (f"{s.container}.{s.name}"
               if s.container in project_types else s.name)
        edges.setdefault(src, set()).update(nodes_by_sym[id(s)])
        if "." in src:  # bare-name node fans out to each qualified definition
            edges.setdefault(s.name, set()).add(src)
    kept_by_sym = _reduce_calls(edges, nodes_by_sym, kept_by_sym)

    def _norm(text: str, own: str) -> str:
        return re.sub(rf"\b{re.escape(own)}\b", "⟨X⟩", text)

    def _sig_line(sym: Symbol, own: str, grouped: bool) -> str:
        sig = sym.signature or sym.name
        if sym.visibility == "priv":
            sig = f"-{sig}"
        if sym.size >= 40:
            sig = f"{sig} ⋮{sym.size}"
        if tested and sym.visibility == "pub" and sym.name in tested:
            sig = f"{sig} ✓"
        kept = kept_by_sym.get(id(sym), [])
        if sym.raises:
            sig = f"{sig} !{','.join(_strip_exc(r) for r in sym.raises)}"
        if grouped:
            kept = [_norm(c, own) for c in kept]
            sig = _norm(sig, own)
        return f"{sig} > {','.join(kept)}" if kept else sig

    ctors_by_owner: dict[tuple[str, str, str], list[str]] = {}
    for s in prod:
        if s.kind == "ctor" or (s.kind == "method" and s.name == "__init__"):
            key = (str(Path(s.file).parent), s.container, s.lang)
            if len(s.params) > len(ctors_by_owner.get(key, [])):
                ctors_by_owner[key] = s.params

    payload_by_dir: dict[str, list[str]] = {}
    for d, types in sorted(types_by_dir.items()):
        payload = payload_by_dir.setdefault(d, [])
        groups: dict[tuple, list[Symbol]] = {}
        for t in sorted(types, key=lambda s: (s.kind == "fn", s.name)):
            if t.kind == "fn":
                payload.append(_sig_line(t, t.name, False))
                continue
            components = t.params or ctors_by_owner.get((d, t.name, t.lang), [])
            key = (t.kind, t.visibility, tuple(components), tuple(t.supers), tuple(t.permits))
            groups.setdefault(key, []).append(t)
        for (kind, vis, components, supers, permits), members in groups.items():
            members.sort(key=lambda s: s.name)
            names = ",".join(("-" if vis == "priv" else "") + m.name for m in members)
            letter = KIND_LETTER.get(kind, "?")
            if permits:
                inner = f"{letter} sealed: {'|'.join(permits)}"
            elif components:
                inner = f"{letter}: {','.join(components)}"
            else:
                inner = letter
            rel_suffix = f" : {','.join(supers)}" if supers else ""
            hot = max((scores.get(m.name, 0) for m in members), default=0) if scores else 0
            hot_suffix = f" ×{int(hot)}" if hot >= 10 else ""
            payload.append(f"{names}({inner}){rel_suffix}{hot_suffix}")
            # Methods shared by every member print once (⟨X⟩-normalized); each
            # member's remaining methods print on its own `Name: …` line.
            member_methods = {id(m): methods_by_owner.get((d, m.name, m.lang), [])
                              for m in members}
            head = members[0]
            def _priv_line(m: Symbol, prefix: str = "") -> str | None:
                names_only = priv_methods_by_owner.get((d, m.name, m.lang))
                if not names_only:
                    return None
                return f" {prefix}- {','.join(dict.fromkeys(names_only))}"

            if len(members) == 1:
                for ms in member_methods[id(head)]:
                    payload.append(" " + _sig_line(ms, head.name, False))
                if (pl := _priv_line(head)) is not None:
                    payload.append(pl)
                continue
            normed = {id(m): [_sig_line(ms, m.name, True)
                              for ms in member_methods[id(m)]] for m in members}
            shared = set.intersection(*(set(v) for v in normed.values()))
            emitted: set[str] = set()
            for line in normed[id(head)]:
                if line in shared and line not in emitted:
                    payload.append(" " + line)
                    emitted.add(line)
            for m in members:
                extras = [ms for ms, ln in zip(member_methods[id(m)], normed[id(m)])
                          if ln not in shared]
                if extras:
                    payload.append(f" {m.name}: "
                                   + "; ".join(_sig_line(ms, m.name, False)
                                               for ms in extras))
                if (pl := _priv_line(m, f"{m.name} ")) is not None:
                    payload.append(pl)

    for (d, stem), names_only in sorted(priv_top_by_file.items()):
        payload_by_dir.setdefault(d, []).append(
            f"- {stem}: {','.join(dict.fromkeys(names_only))}")
    reex_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "reexport":
            key = (str(Path(s.file).parent), Path(s.file).name)
            names_r = reex_by_file.setdefault(key, [])
            if s.name not in names_r:
                names_r.append(s.name)
    for (d, fname), names_r in sorted(reex_by_file.items()):
        payload_by_dir.setdefault(d, []).append(f"» {fname}: {','.join(names_r)}")

    tail = ""
    if behaviors:
        by_owner: dict[str, list[str]] = {}
        for s in symbols:
            if _is_test_path(s.file) and s.kind in ("fn", "method"):
                owner = re.sub(r"(Test|Tests|IT|Spec)$", "",
                               s.container or Path(s.file).stem) or s.container
                if s.name not in by_owner.setdefault(owner, []):
                    by_owner[owner].append(s.name)
        blines = []
        for owner, names_b in sorted(by_owner.items()):
            cur = f"? {owner}:"
            for n in names_b:
                if len(cur) + len(n) + 1 > 150:
                    blines.append(cur)
                    cur = f"? {owner}: {n}"
                else:
                    cur += f" {n},"
            blines.append(cur.rstrip(","))
        if blines:
            tail = "\n" + "\n".join(blines)

    if loc is None:
        loc = _total_loc(files)
    state_part = f" · state={state}" if state else ""
    head = (f"# {root.name} @{git_head(root)} {date.today().isoformat()} · "
            f"{loc:,} LOC{state_part} · regen: {regen_cmd}\n"
            "· legend: (C)lass (R)ecord (I)nterface (E)num (F)n (T)ype-alias · "
            "(R: …)=components (C: …)=ctor deps (E: …)=values · "
            "name(params):Ret, no :Ret=void · : X=extends/implements · "
            "sealed: A|B=permits · sig > calls, project-only, transitively reduced · "
            "!E=throws, Exception suffix dropped · ⟨X⟩=member's own name · "
            "×N=referenced from N files · ⋮N=body lines · ✓=referenced from tests · "
            "»file: re-exports · "
            + ("-sig=private" if private_sigs else "- x,y=private members, names only")
            + (" · ? Owner: test names" if behaviors else "")
            + "\n"
            "· query this file, don't read it: who calls X → grep '> .*X' · "
            "find a symbol → grep -i 'name(' · only tested APIs → grep '✓' · "
            "heavily-used types → grep '×' · a class's internals → its '- ' line · "
            "module coupling → the 'deps' line\n")
    dep_part = ("\n".join(deps) + "\n") if deps else ""
    return head + dep_part + "\n".join(_tree_lines(payload_by_dir)) + tail + "\n"


def _build_digest(
    root: Path,
    regen_cmd: str,
    langs: set[str] | None,
    private_sigs: bool,
    behaviors: bool,
    config: ProjectConfig,
    *,
    scan_result: scan.ScanResult | None = None,
) -> str:
    files, symbols, file_tokens, state, loc = _gather(
        root,
        langs,
        config,
        private_sigs,
        behaviors,
        scan_result=scan_result,
    )
    scores = _fan_in_from_tokens(symbols, file_tokens)
    test_tokens: set[str] = set()
    for rel, toks in file_tokens.items():
        if _is_test_path(rel):
            test_tokens |= toks
    tested = {s.name for s in symbols
              if not _is_test_path(s.file) and s.visibility == "pub"
              and s.kind in ("fn", "method")} & test_tokens
    deps = _dep_lines(symbols, file_tokens)
    return render_simple(root, symbols, files, regen_cmd, scores, private_sigs,
                         tested=tested, behaviors=behaviors, state=state,
                         deps=deps, loc=loc)


def build_digest(root: Path, regen_cmd: str = "hologram build",
                 langs: set[str] | None = None, private_sigs: bool = False,
                 behaviors: bool = False,
                 config: ProjectConfig | None = None) -> str:
    if config is None:
        config = default_config()
    return _build_digest(
        root,
        regen_cmd,
        langs,
        private_sigs,
        behaviors,
        config,
    )


# ---------------------------------------------------------------------------
# Embed: put the digest INSIDE CLAUDE.md so every agent session starts with
# the whole map in context — push, not pull; no retrieval decision to lose.
# ---------------------------------------------------------------------------

_EMBED_START = "<!-- hologram:start — generated, do not edit; refreshed by git hooks -->"
_EMBED_END = "<!-- hologram:end -->"

_EMBED_PREFACE = """## Project map (generated — the whole codebase at a glance)

The block below is the complete symbol map of this repository: every type,
signature, relation, and resolved call chain. You already have the holistic
view — use it directly for planning, placement, and reuse decisions instead of
exploring first. Before writing any new function, find the existing one here.
Grep the source only for implementation bodies.
"""


def _reduce_for_embed(digest: str, max_tokens: int) -> tuple[str, str]:
    """Graded degradation to fit the embed budget, holism-first:
    full → drop call chains → drop private/re-export/method lines (types keep
    the shape) → hard truncate. Returns (body, tier-name)."""
    if estimate_tokens(digest) <= max_tokens:
        return digest, "full"
    lines = digest.splitlines()
    no_chains = [ln.split(" > ")[0] if " > " in ln else ln for ln in lines]
    body = "\n".join(no_chains) + "\n"
    if estimate_tokens(body) <= max_tokens:
        return body, "no-chains"
    types_only = [ln for ln in no_chains
                  if not ln.strip().startswith(("-", "»", "?"))
                  and not ("(" in ln and ln.strip()[:1].islower())]
    body = "\n".join(types_only) + "\n"
    if estimate_tokens(body) <= max_tokens:
        return body, "types-only"
    keep, used = [], 0
    for ln in types_only:
        used += len(ln) // 4 + 1
        if used > max_tokens:
            keep.append("… (truncated to fit embed budget — full map in PROJECT_DIGEST.md)")
            break
        keep.append(ln)
    return "\n".join(keep) + "\n", "truncated"


def embed_digest(claude_path: Path, digest: str, max_tokens: int = 30000) -> str:
    """Insert or refresh the digest block in CLAUDE.md. Degrades gracefully to
    fit the budget; returns the tier used ('full', 'no-chains', 'types-only',
    'truncated')."""
    body, tier = _reduce_for_embed(digest, max_tokens)
    block = (f"{_EMBED_START}\n{_EMBED_PREFACE}\n```\n{body.rstrip()}\n```\n"
             f"{_EMBED_END}")
    existing = claude_path.read_text() if claude_path.exists() else ""
    if _EMBED_START in existing and _EMBED_END in existing:
        pre = existing.split(_EMBED_START, 1)[0]
        post = existing.split(_EMBED_END, 1)[1]
        updated = pre + block + post
    else:
        sep = "\n\n" if existing.strip() else ""
        updated = existing.rstrip("\n") + sep + block + "\n"
    claude_path.write_text(updated)
    return tier


def _missing_parser_langs(files: list[Path]) -> set[str]:
    """Languages present in `files` that need a tree-sitter parser we don't have."""
    return {
        language
        for language in {detect_language(path) for path in files}
        if language is not None
        and Language(language) in GRAMMAR_METADATA
        and not has_parser(language)
    }


def _bootstrap_or_die(missing: set[str], argv: list[str]) -> None:
    """Exit with installation guidance when requested parsers are unavailable."""
    del argv
    packages = " ".join(_grammar_pkgs(missing))
    raise SystemExit(
        f"missing tree-sitter parser for: {', '.join(sorted(missing))}\n"
        f"install with: {sys.executable} -m pip install "
        "'hologram-code-map[parsers]'\n"
        f"required packages: {packages}"
    )


# ---------------------------------------------------------------------------
# CLI: build / init (self-installing git hooks)
# ---------------------------------------------------------------------------

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout")


def _hook_python() -> str:
    """Return the interpreter used to generate repository hooks."""
    return sys.executable


def _hologram_build_command(*args: str) -> str:
    """Render a shell-safe command through the installed module entry point."""
    return shlex.join([_hook_python(), "-m", "hologram", "build", *args])


def _hook_options_match_repo(options: list[str], repo: Path) -> bool:
    """Validate the exact option grammar emitted by `_install_hooks`."""
    if len(options) < 3 or options[:1] != ["--root"]:
        return False
    root_arg = Path(options[1])
    if not root_arg.is_absolute() or root_arg.resolve() != repo.resolve():
        return False
    tail = options[2:]
    langs = []
    while tail[:1] == ["--lang"]:
        if len(tail) < 2 or not tail[1] or tail[1].startswith("-"):
            return False
        langs.append(tail[1])
        tail = tail[2:]
    if langs != sorted(set(langs)):
        return False
    if tail[:1] == ["--embed"]:
        tail = tail[1:]
    return tail == ["--quiet"]


def _legacy_hook_script() -> Path | None:
    """Prior root-level script path when running from this repository's src layout."""
    source = Path(__file__).resolve()
    src_dir = source.parent.parent
    if src_dir.name != "src":
        return None
    return src_dir.parent / "hologram.py"


def _is_generated_hologram_hook_line(line: str, repo: Path) -> bool:
    """Whether `line` is an exact Hologram-owned command for `repo`."""
    text = line.removesuffix("\n").removesuffix("\r")
    if text != text.strip() or not text.endswith(" || true"):
        return False
    command = text.removesuffix(" || true")
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if argv[1:4] == ["-m", "hologram", "build"]:
        return _hook_options_match_repo(argv[4:], repo)
    legacy_script = _legacy_hook_script()
    return (legacy_script is not None
            and len(argv) >= 3
            and Path(argv[1]).is_absolute()
            and Path(argv[1]).resolve() == legacy_script.resolve()
            and argv[2] == "build"
            and _hook_options_match_repo(argv[3:], repo))


def _install_hooks(repo: Path, quiet: bool, langs: set[str] | None = None,
                   embed: bool = False) -> None:
    hook_args = ["--root", str(repo.resolve())]
    for lang in sorted(langs or set()):
        hook_args.extend(["--lang", lang])
    if embed:
        hook_args.append("--embed")
    hook_args.append("--quiet")
    hook_line = f"{_hologram_build_command(*hook_args)} || true\n"
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        if hook.exists():
            content = hook.read_bytes()
            kept = []
            for line in content.splitlines(keepends=True):
                try:
                    owned = _is_generated_hologram_hook_line(line.decode(), repo)
                except UnicodeDecodeError:
                    owned = False
                if not owned:
                    kept.append(line)
            preserved = b"".join(kept)
            if preserved and not preserved.endswith((b"\n", b"\r")):
                preserved += b"\n"
            hook.write_bytes(preserved + hook_line.encode())
        else:
            hook.write_text("#!/bin/sh\n" + hook_line)
        hook.chmod(0o755)
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if "PROJECT_DIGEST.md" not in existing:
        gitignore.write_text(existing.rstrip("\n") + ("\n" if existing else "")
                             + "PROJECT_DIGEST.md\n")
    if not quiet:
        print(f"hooks installed: {', '.join(HOOK_NAMES)}; PROJECT_DIGEST.md gitignored")


def _digest_regen_command(root: Path, out_path: Path,
                          langs: set[str] | None, private_sigs: bool,
                          behaviors: bool, embed: bool,
                          embed_max_tokens: int) -> str:
    """Reproduce the active content-affecting build configuration."""
    regen_args = ["--root", str(root.resolve()),
                  "--out", str(out_path.resolve())]
    for lang in sorted(langs or set()):
        regen_args.extend(["--lang", lang])
    if private_sigs:
        regen_args.append("--private")
    if behaviors:
        regen_args.append("--behaviors")
    if embed:
        regen_args.extend(["--embed", "--embed-max-tokens",
                           str(embed_max_tokens)])
    return _hologram_build_command(*regen_args)


def run_cli(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path.cwd())
    common.add_argument("--lang", action="append", default=None,
                        help="restrict to language(s), repeatable or comma-separated "
                             "(java, python, typescript, javascript)")
    common.add_argument("--private", action="store_true",
                        help="full signatures for private members "
                             "(default: names only)")
    common.add_argument("--behaviors", action="store_true",
                        help="append test-method names as behavior specs "
                             "(costly on test-heavy repos)")
    common.add_argument("--embed", action="store_true",
                        help="also inject the digest into CLAUDE.md so every "
                             "agent session starts with the whole map in context")
    common.add_argument("--embed-max-tokens", type=int, default=30000,
                        help="embed budget; larger digests degrade gracefully "
                             "(chains, then methods, then truncation)")
    common.add_argument("--out", type=Path, default=None)
    common.add_argument("--quiet", action="store_true")

    parser = argparse.ArgumentParser(prog="hologram", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", parents=[common],
                             help="(re)generate the digest file")
    p_build.add_argument("--if-stale", action="store_true",
                         help="skip the rebuild when the digest's state stamp "
                              "matches the current sources")
    sub.add_parser("init", parents=[common],
                   help="install git hooks and gitignore entry, then build")
    sub.add_parser("check", parents=[common],
                   help="exit 0 if the digest is fresh, 1 if stale or missing")
    p_diff = sub.add_parser("diff", parents=[common],
                            help="diff the digest against another git revision")
    p_diff.add_argument("rev", nargs="?", default="HEAD~1")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if args.cmd == "init":
        create_default_manifest(root)
    config = load_config(root)
    langs = None
    if getattr(args, "lang", None):
        langs = {l.strip() for arg in args.lang for l in arg.split(",") if l.strip()}
    elif config.languages:
        langs = {language.value for language in config.languages}
    out_path = (args.out or root / "PROJECT_DIGEST.md").resolve()

    freshness_scan: scan.ScanResult | None = None
    fresh = False
    if args.cmd == "check" or (
        args.cmd == "build" and args.if_stale
    ):
        effective_config = _effective_config(config, langs)
        freshness_scan = scan.scan_project(root, effective_config)
        current_state = _state_hash(
            root,
            config,
            langs,
            private_sigs=args.private,
            behaviors=args.behaviors,
            scan_result=freshness_scan,
        )
        fresh = _digest_state(out_path) == current_state

    if args.cmd == "check":
        if not args.quiet:
            print(f"{out_path}: {'fresh' if fresh else 'stale or missing'}")
        return 0 if fresh else 1
    if args.cmd == "build" and args.if_stale and fresh:
        if not args.quiet:
            print(f"{out_path}: fresh, skipping rebuild")
        return 0

    if args.cmd == "diff":
        with tempfile.TemporaryDirectory(prefix="hologram-diff-") as tmp:
            wt = Path(tmp) / "wt"
            r = subprocess.run(
                ["git", "-C", str(root), "worktree", "add", "--detach", "-f",
                 str(wt), args.rev],
                capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"git worktree failed: {r.stderr.strip()}")
            try:
                old = build_digest(
                    wt,
                    langs=langs,
                    private_sigs=args.private,
                    behaviors=args.behaviors,
                    config=config,
                )
                new = build_digest(
                    root,
                    langs=langs,
                    private_sigs=args.private,
                    behaviors=args.behaviors,
                    config=config,
                )
            finally:
                subprocess.run(["git", "-C", str(root), "worktree", "remove",
                                "--force", str(wt)], capture_output=True)
        body_old = old.splitlines()[2:]  # drop header+legend: date/state/path noise
        body_new = new.splitlines()[2:]
        for ln in difflib.unified_diff(body_old, body_new, fromfile=args.rev,
                                       tofile="worktree", lineterm=""):
            print(ln)
        return 0

    if args.cmd == "init":
        _install_hooks(root, args.quiet, langs, embed=args.embed)
    regen_cmd = _digest_regen_command(
        root,
        out_path,
        langs,
        args.private,
        args.behaviors,
        args.embed,
        args.embed_max_tokens,
    )
    if args.cmd == "build" and args.if_stale:
        assert freshness_scan is not None
        digest = _build_digest(
            root,
            regen_cmd,
            langs,
            args.private,
            args.behaviors,
            config,
            scan_result=freshness_scan,
        )
    else:
        digest = build_digest(
            root,
            regen_cmd=regen_cmd,
            langs=langs,
            private_sigs=args.private,
            behaviors=args.behaviors,
            config=config,
        )
    out_path.write_text(digest)
    if not args.quiet:
        print(f"{out_path} written: {estimate_tokens(digest)} tokens")
    if args.embed:
        tier = embed_digest(root / "CLAUDE.md", digest, args.embed_max_tokens)
        if not args.quiet:
            print(f"CLAUDE.md: digest embedded ({tier})")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
