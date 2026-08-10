from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import TypeVar

from .analysis import AnalyzedProject, AnalyzedSymbol, ZeroReference
from .model import (
    FileIR,
    Language,
    ReferenceConfidence,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
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


class RenderDecodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ProjectIndexes:
    files: tuple[FileIR, ...]
    files_by_path: dict[str, FileIR]
    symbols: tuple[Symbol, ...]
    symbols_by_id: dict[SymbolId, Symbol]
    analyzed_by_id: dict[SymbolId, AnalyzedSymbol]


_STATE = re.compile(r"[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"&[A-Za-z_][A-Za-z0-9_.:-]*\Z")
_IDENTIFIER_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)

_STRING_TUPLE_FIELDS = (
    "parameters",
    "annotations",
    "modifiers",
    "components",
    "supers",
    "permits",
    "ordered_calls",
    "throws",
    "behaviors",
)


class _AliasTrieNode:
    __slots__ = ("children", "count")

    def __init__(self) -> None:
        self.children: dict[str, _AliasTrieNode] = {}
        self.count = 0


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _valid_relative_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    return bool(
        value
        and path.parts
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and value == path.as_posix()
    )


def _render_symbol_key(
    symbol: RenderSymbol,
) -> tuple[str, str, tuple[str, ...], str, str, str, int, int]:
    symbol_id = symbol.symbol_id
    return (
        symbol_id.language.value,
        symbol_id.file,
        symbol_id.container_path,
        symbol_id.kind.value,
        symbol_id.name,
        symbol_id.signature_key,
        symbol.source_line,
        symbol.source_column,
    )


def _eligible_values(ir: RenderIR) -> Counter[str]:
    values: Counter[str] = Counter()

    def add(value: str | None) -> None:
        if value is not None:
            values[value] += 1

    for dependency in ir.dependencies:
        add(dependency)
    for file_ir in ir.files:
        add(file_ir.module)
        for reexport in file_ir.reexports:
            add(reexport.module)
            add(reexport.name)
            add(reexport.alias)
        for symbol in file_ir.symbols:
            add(symbol.signature)
            add(symbol.returns)
            for field in _STRING_TUPLE_FIELDS:
                for value in getattr(symbol, field):
                    add(value)
    return values


def _encoded_literal(value: str) -> str:
    if value.startswith("&"):
        return f"&{value}"
    return value


def _plan_interns(ir: RenderIR) -> tuple[RenderIntern, ...]:
    occurrences = _eligible_values(ir)
    candidates = {
        value: tuple(_IDENTIFIER_SEGMENT.findall(value))
        for value, count in occurrences.items()
        if count >= 3
    }
    suffixes = _AliasTrieNode()
    for segments in candidates.values():
        node = suffixes
        for segment in reversed(segments):
            child = node.children.get(segment)
            if child is None:
                child = _AliasTrieNode()
                node.children[segment] = child
            child.count += 1
            node = child

    planned: list[RenderIntern] = []
    for value in sorted(candidates):
        segments = candidates[value]
        node = suffixes
        alias_length: int | None = None
        for length, segment in enumerate(reversed(segments), 1):
            node = node.children[segment]
            if node.count == 1:
                alias_length = length
                break
        alias = (
            f"&{'.'.join(segments[-alias_length:])}"
            if alias_length is not None
            else None
        )
        if alias is None:
            continue
        count = occurrences[value]
        literal_bytes = len(_json(_encoded_literal(value)).encode("utf-8"))
        alias_bytes = len(_json(alias).encode("utf-8"))
        declaration_bytes = len(f"· intern {_json(alias)} {_json(value)}\n".encode())
        if count * literal_bytes - count * alias_bytes - declaration_bytes > 0:
            planned.append(RenderIntern(alias, value))
    return tuple(sorted(planned, key=lambda item: item.alias))


def _valid_markers(markers: tuple[str, ...]) -> bool:
    index = 0
    if index < len(markers) and re.fullmatch(
        r"×(?:0\?|0|[1-9][0-9]*)",
        markers[index],
    ):
        index += 1
    if index < len(markers) and markers[index] == "✓":
        index += 1
    if index < len(markers) and re.fullmatch(r"≈[1-9][0-9]*", markers[index]):
        index += 1
    return index == len(markers)


def _validate_render_ir(ir: RenderIR) -> None:
    if not isinstance(ir, RenderIR):
        raise TypeError("ir must be RenderIR")
    if (
        isinstance(ir.schema_version, bool)
        or not isinstance(ir.schema_version, int)
        or ir.schema_version != 2
    ):
        raise ValueError("schema_version must be 2")
    if not isinstance(ir.state, str) or _STATE.fullmatch(ir.state) is None:
        raise ValueError("state must be exactly 64 lowercase hexadecimal digits")

    if any(not isinstance(value, str) for value in ir.dependencies):
        raise TypeError("dependencies must contain strings")
    if tuple(sorted(set(ir.dependencies))) != ir.dependencies:
        raise ValueError("dependencies must be sorted and unique")

    aliases: set[str] = set()
    intern_values: set[str] = set()
    previous_alias: str | None = None
    for intern in ir.interns:
        if not isinstance(intern, RenderIntern):
            raise TypeError("interns must contain RenderIntern values")
        if (
            not isinstance(intern.alias, str)
            or _ALIAS.fullmatch(intern.alias) is None
            or not isinstance(intern.value, str)
        ):
            raise ValueError("invalid intern declaration")
        if intern.alias in aliases or intern.value in intern_values:
            raise ValueError("duplicate intern alias or value")
        if previous_alias is not None and intern.alias <= previous_alias:
            raise ValueError("interns must be sorted by alias")
        aliases.add(intern.alias)
        intern_values.add(intern.value)
        previous_alias = intern.alias

    if any(not isinstance(file, RenderFile) for file in ir.files):
        raise TypeError("files must contain RenderFile values")
    if any(not _valid_relative_path(file.path) for file in ir.files):
        raise ValueError("invalid render file path")
    if tuple(sorted(file.path for file in ir.files)) != tuple(
        file.path for file in ir.files
    ):
        raise ValueError("files must be sorted by path")
    seen_files: set[str] = set()
    seen_symbols: set[SymbolId] = set()
    for file_ir in ir.files:
        if file_ir.path in seen_files:
            raise ValueError("duplicate render file path")
        seen_files.add(file_ir.path)
        try:
            language = Language(file_ir.language)
            SourceRole(file_ir.role)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid render file language or role") from error
        if file_ir.module is not None and not isinstance(file_ir.module, str):
            raise TypeError("render file module must be a string or None")

        seen_reexports: set[tuple[str, str | None, str | None, bool]] = set()
        for reexport in file_ir.reexports:
            if not isinstance(reexport, RenderReexport):
                raise TypeError("reexports must contain RenderReexport values")
            if (
                not isinstance(reexport.module, str)
                or (reexport.name is not None and not isinstance(reexport.name, str))
                or (reexport.alias is not None and not isinstance(reexport.alias, str))
                or not isinstance(reexport.wildcard, bool)
            ):
                raise TypeError("invalid reexport")
            key = (
                reexport.module,
                reexport.name,
                reexport.alias,
                reexport.wildcard,
            )
            if key in seen_reexports:
                raise ValueError("duplicate reexport")
            seen_reexports.add(key)

        for symbol in file_ir.symbols:
            if not isinstance(symbol, RenderSymbol):
                raise TypeError("symbols must contain RenderSymbol values")
            symbol_id = symbol.symbol_id
            if not isinstance(symbol_id, SymbolId):
                raise TypeError("symbol_id must be SymbolId")
            if (
                symbol_id.file != file_ir.path
                or symbol_id.language is not language
                or not all(isinstance(part, str) for part in symbol_id.container_path)
                or not isinstance(symbol_id.kind, SymbolKind)
                or not isinstance(symbol_id.name, str)
                or not symbol_id.name
                or not isinstance(symbol_id.signature_key, str)
            ):
                raise ValueError("render symbol ownership is invalid")
            if symbol_id in seen_symbols:
                raise ValueError("duplicate render SymbolId")
            seen_symbols.add(symbol_id)
            if (
                isinstance(symbol.source_line, bool)
                or not isinstance(symbol.source_line, int)
                or symbol.source_line < 1
                or isinstance(symbol.source_column, bool)
                or not isinstance(symbol.source_column, int)
                or symbol.source_column < 0
            ):
                raise ValueError("invalid symbol source position")
            try:
                Visibility(symbol.visibility)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid symbol visibility") from error
            if not isinstance(symbol.signature, str) or (
                symbol.returns is not None and not isinstance(symbol.returns, str)
            ):
                raise TypeError("invalid symbol signature or return")
            for field in _STRING_TUPLE_FIELDS:
                if any(not isinstance(value, str) for value in getattr(symbol, field)):
                    raise TypeError(f"{field} must contain strings")
            if (
                isinstance(symbol.body_lines, bool)
                or not isinstance(symbol.body_lines, int)
                or symbol.body_lines < 0
            ):
                raise ValueError("body_lines must be a nonnegative integer")
            if any(not isinstance(marker, str) for marker in symbol.markers) or not (
                _valid_markers(symbol.markers)
            ):
                raise ValueError("invalid marker order")
        if tuple(sorted(file_ir.symbols, key=_render_symbol_key)) != file_ir.symbols:
            raise ValueError("symbols must be sorted")

    expected_interns = _plan_interns(replace(ir, interns=()))
    if ir.interns != expected_interns:
        raise ValueError("intern table is not canonical")


def _encoded_value(value: str, aliases: dict[str, str]) -> str:
    alias = aliases.get(value)
    if alias is not None:
        return alias
    return _encoded_literal(value)


def _encoded_values(
    values: tuple[str, ...],
    aliases: dict[str, str],
) -> list[str]:
    return [_encoded_value(value, aliases) for value in values]


def render_project(ir: RenderIR) -> str:
    _validate_render_ir(ir)
    aliases = {intern.value: intern.alias for intern in ir.interns}
    lines = [
        f"# hologram:2 state={ir.state} · regen: hologram build",
    ]
    lines.extend(
        f"· intern {_json(intern.alias)} {_json(intern.value)}" for intern in ir.interns
    )
    lines.append(f"· deps {_json(_encoded_values(ir.dependencies, aliases))}")

    for file_ir in ir.files:
        module = (
            "null"
            if file_ir.module is None
            else _json(_encoded_value(file_ir.module, aliases))
        )
        lines.append(
            f"@ {_json(file_ir.path)} {_json(file_ir.language)} "
            f"{_json(file_ir.role)} {module}"
        )
        if file_ir.reexports:
            reexports = [
                [
                    _encoded_value(reexport.module, aliases),
                    (
                        None
                        if reexport.name is None
                        else _encoded_value(reexport.name, aliases)
                    ),
                    (
                        None
                        if reexport.alias is None
                        else _encoded_value(reexport.alias, aliases)
                    ),
                    reexport.wildcard,
                ]
                for reexport in file_ir.reexports
            ]
            lines.append(f"  reexport {_json(reexports)}")

        for symbol in file_ir.symbols:
            symbol_id = symbol.symbol_id
            local_id = [
                list(symbol_id.container_path),
                symbol_id.kind.value,
                symbol_id.name,
                symbol_id.signature_key,
            ]
            lines.append(
                f"  :{symbol.source_line}:{symbol.source_column} "
                f"{_json(local_id)} {_json(symbol.visibility)}"
            )
            lines.append(
                f"    signature {_json(_encoded_value(symbol.signature, aliases))}"
            )
            if symbol.parameters:
                lines.append(
                    f"    param {_json(_encoded_values(symbol.parameters, aliases))}"
                )
            lines.append(
                "    return "
                + (
                    "null"
                    if symbol.returns is None
                    else _json(_encoded_value(symbol.returns, aliases))
                )
            )
            for key, values in (
                ("annotation", symbol.annotations),
                ("modifier", symbol.modifiers),
                ("component", symbol.components),
                ("super", symbol.supers),
                ("permit", symbol.permits),
                ("call", symbol.ordered_calls),
                ("throw", symbol.throws),
                ("behavior", symbol.behaviors),
            ):
                if values:
                    lines.append(f"    {key} {_json(_encoded_values(values, aliases))}")
            if symbol.body_lines:
                lines.append(f"    body {symbol.body_lines}")
            if symbol.markers:
                lines.append(f"    mark {_json(list(symbol.markers))}")
    rendered = "\n".join(lines) + "\n"
    try:
        rendered.encode()
    except UnicodeEncodeError as error:
        raise ValueError("render values must be valid UTF-8") from error
    return rendered


_JSON_DECODER = json.JSONDecoder()
_HEADER = re.compile(r"# hologram:2 state=([0-9a-f]{64}) · regen: hologram build\Z")
_SYMBOL_LINE = re.compile(r"  :([0-9]+):([0-9]+) (.+)\Z")
_CHILD_ORDER = {
    key: index
    for index, key in enumerate(
        (
            "signature",
            "param",
            "return",
            "annotation",
            "modifier",
            "component",
            "super",
            "permit",
            "call",
            "throw",
            "behavior",
            "body",
            "mark",
        )
    )
}


def _parse_json_at(text: str, position: int) -> tuple[object, int]:
    try:
        value, end = _JSON_DECODER.raw_decode(text, position)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RenderDecodeError("invalid hologram JSON") from error
    if _json(value) != text[position:end]:
        raise RenderDecodeError("noncanonical hologram JSON")
    return value, end


def _parse_json_tokens(text: str, count: int) -> tuple[object, ...]:
    result: list[object] = []
    position = 0
    for index in range(count):
        if index:
            if position >= len(text) or text[position] != " ":
                raise RenderDecodeError("invalid hologram token spacing")
            position += 1
        value, position = _parse_json_at(text, position)
        result.append(value)
    if position != len(text):
        raise RenderDecodeError("unexpected hologram line suffix")
    return tuple(result)


def _parse_json_line(text: str) -> object:
    value, position = _parse_json_at(text, 0)
    if position != len(text):
        raise RenderDecodeError("unexpected hologram JSON suffix")
    return value


def _expanded_value(value: object, aliases: dict[str, str]) -> str:
    if not isinstance(value, str):
        raise RenderDecodeError("eligible value must be a string")
    expanded = aliases.get(value)
    if expanded is not None:
        return expanded
    if value.startswith("&&"):
        return value[1:]
    if value.startswith("&"):
        raise RenderDecodeError("undeclared intern alias")
    return value


def _expanded_values(value: object, aliases: dict[str, str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RenderDecodeError("eligible array must be a JSON array")
    return tuple(_expanded_value(item, aliases) for item in value)


def _decode_structure(text: str) -> RenderIR:
    if not isinstance(text, str):
        raise RenderDecodeError("hologram text must be a string")
    if "\r" in text or not text.endswith("\n") or text.endswith("\n\n"):
        raise RenderDecodeError("hologram text must use one final LF")
    lines = text[:-1].split("\n")
    if any(line.endswith((" ", "\t")) for line in lines):
        raise RenderDecodeError("hologram lines must not have trailing whitespace")
    if not lines:
        raise RenderDecodeError("missing hologram header")
    header = _HEADER.fullmatch(lines[0])
    if header is None:
        raise RenderDecodeError("invalid hologram header")
    state = header.group(1)
    index = 1

    interns: list[RenderIntern] = []
    aliases: dict[str, str] = {}
    values: set[str] = set()
    while index < len(lines) and lines[index].startswith("· intern "):
        alias_value, expanded_value = _parse_json_tokens(
            lines[index][len("· intern ") :],
            2,
        )
        if (
            not isinstance(alias_value, str)
            or _ALIAS.fullmatch(alias_value) is None
            or not isinstance(expanded_value, str)
        ):
            raise RenderDecodeError("invalid intern declaration")
        if alias_value in aliases or expanded_value in values:
            raise RenderDecodeError("duplicate intern alias or value")
        aliases[alias_value] = expanded_value
        values.add(expanded_value)
        interns.append(RenderIntern(alias_value, expanded_value))
        index += 1

    if index >= len(lines) or not lines[index].startswith("· deps "):
        raise RenderDecodeError("missing dependency line")
    dependencies = _expanded_values(
        _parse_json_line(lines[index][len("· deps ") :]),
        aliases,
    )
    index += 1

    files: list[RenderFile] = []
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@ "):
            raise RenderDecodeError("expected file leaf")
        path_value, language_value, role_value, module_value = _parse_json_tokens(
            line[2:],
            4,
        )
        if (
            not isinstance(path_value, str)
            or not isinstance(language_value, str)
            or not isinstance(role_value, str)
            or (module_value is not None and not isinstance(module_value, str))
        ):
            raise RenderDecodeError("invalid file leaf")
        path = path_value
        language_text = language_value
        role = role_value
        try:
            language = Language(language_text)
        except (TypeError, ValueError) as error:
            raise RenderDecodeError("invalid file language") from error
        module = (
            None if module_value is None else _expanded_value(module_value, aliases)
        )
        index += 1

        reexports: tuple[RenderReexport, ...] = ()
        if index < len(lines) and lines[index].startswith("  reexport "):
            raw_reexports = _parse_json_line(lines[index][len("  reexport ") :])
            if not isinstance(raw_reexports, list):
                raise RenderDecodeError("reexports must be a JSON array")
            decoded_reexports: list[RenderReexport] = []
            for raw in raw_reexports:
                if not isinstance(raw, list) or len(raw) != 4:
                    raise RenderDecodeError("invalid reexport entry")
                raw_module, raw_name, raw_alias, wildcard = raw
                if (
                    not isinstance(raw_module, str)
                    or (raw_name is not None and not isinstance(raw_name, str))
                    or (raw_alias is not None and not isinstance(raw_alias, str))
                    or not isinstance(wildcard, bool)
                ):
                    raise RenderDecodeError("invalid reexport entry")
                decoded_reexports.append(
                    RenderReexport(
                        _expanded_value(raw_module, aliases),
                        (
                            None
                            if raw_name is None
                            else _expanded_value(raw_name, aliases)
                        ),
                        (
                            None
                            if raw_alias is None
                            else _expanded_value(raw_alias, aliases)
                        ),
                        wildcard,
                    )
                )
            reexports = tuple(decoded_reexports)
            index += 1

        symbols: list[RenderSymbol] = []
        while index < len(lines) and lines[index].startswith("  :"):
            match = _SYMBOL_LINE.fullmatch(lines[index])
            if match is None:
                raise RenderDecodeError("invalid symbol line")
            source_line = int(match.group(1))
            source_column = int(match.group(2))
            local_value, visibility_value = _parse_json_tokens(match.group(3), 2)
            if (
                not isinstance(local_value, list)
                or len(local_value) != 4
                or not isinstance(local_value[0], list)
                or any(not isinstance(part, str) for part in local_value[0])
                or not all(isinstance(value, str) for value in local_value[1:])
                or not isinstance(visibility_value, str)
            ):
                raise RenderDecodeError("invalid local SymbolId")
            container, kind_value, name, signature_key = local_value
            try:
                kind = SymbolKind(kind_value)
            except (TypeError, ValueError) as error:
                raise RenderDecodeError("invalid symbol kind") from error
            symbol_id = SymbolId(
                language,
                path,
                tuple(container),
                kind,
                name,
                signature_key,
            )
            index += 1

            children: dict[str, object] = {}
            previous_child = -1
            while index < len(lines) and lines[index].startswith("    "):
                child = lines[index][4:]
                if " " not in child:
                    raise RenderDecodeError("invalid symbol child")
                key, raw_value = child.split(" ", 1)
                order = _CHILD_ORDER.get(key)
                if order is None or order <= previous_child:
                    raise RenderDecodeError("unknown, duplicate, or unordered child")
                previous_child = order
                children[key] = _parse_json_line(raw_value)
                index += 1
            if "signature" not in children or "return" not in children:
                raise RenderDecodeError("missing mandatory symbol child")

            signature = _expanded_value(children["signature"], aliases)
            return_value = children["return"]
            if return_value is not None:
                return_value = _expanded_value(return_value, aliases)

            decoded_arrays: dict[str, tuple[str, ...]] = {}
            for key in (
                "param",
                "annotation",
                "modifier",
                "component",
                "super",
                "permit",
                "call",
                "throw",
                "behavior",
            ):
                decoded_arrays[key] = (
                    ()
                    if key not in children
                    else _expanded_values(children[key], aliases)
                )
            body_value = children.get("body", 0)
            if isinstance(body_value, bool) or not isinstance(body_value, int):
                raise RenderDecodeError("body must be an integer")
            markers_value = children.get("mark", [])
            if not isinstance(markers_value, list) or any(
                not isinstance(marker, str) for marker in markers_value
            ):
                raise RenderDecodeError("markers must be a string array")

            symbols.append(
                RenderSymbol(
                    symbol_id,
                    source_line,
                    source_column,
                    visibility_value,
                    signature,
                    decoded_arrays["param"],
                    return_value,
                    decoded_arrays["annotation"],
                    decoded_arrays["modifier"],
                    decoded_arrays["component"],
                    decoded_arrays["super"],
                    decoded_arrays["permit"],
                    decoded_arrays["call"],
                    decoded_arrays["throw"],
                    decoded_arrays["behavior"],
                    body_value,
                    tuple(markers_value),
                )
            )

        files.append(
            RenderFile(
                path,
                language_text,
                role,
                module,
                reexports,
                tuple(symbols),
            )
        )

    return RenderIR(
        2,
        state,
        tuple(interns),
        dependencies,
        tuple(files),
    )


def decode_render(text: str) -> RenderIR:
    try:
        decoded = _decode_structure(text)
        if render_project(decoded) != text:
            raise RenderDecodeError("noncanonical hologram text")
        return decoded
    except RenderDecodeError:
        raise
    except (TypeError, ValueError, UnicodeError) as error:
        raise RenderDecodeError("noncanonical hologram text") from error


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

    expanded = RenderIR(2, state, (), dependencies, tuple(rendered_files))
    return replace(expanded, interns=_plan_interns(expanded))


__all__ = [
    "RenderDecodeError",
    "RenderFile",
    "RenderIR",
    "RenderIntern",
    "RenderReexport",
    "RenderSymbol",
    "decode_render",
    "project_render_ir",
    "render_project",
]
