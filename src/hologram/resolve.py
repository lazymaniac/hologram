from __future__ import annotations

import posixpath
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import TypeVar

from .model import (
    CallKind,
    CallRef,
    Diagnostic,
    FileIR,
    ImportRef,
    Language,
    ProjectIR,
    ReferenceKind,
    ReferenceRef,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


_T = TypeVar("_T")


def _owned_tuple(value: tuple[_T, ...] | list[_T], field: str) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    source_file: str
    fact: ImportRef
    status: ResolutionStatus
    target_files: tuple[str, ...]
    target_symbols: tuple[SymbolId, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_files", _owned_tuple(self.target_files, "target_files")
        )
        object.__setattr__(
            self,
            "target_symbols",
            _owned_tuple(self.target_symbols, "target_symbols"),
        )


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    fact: CallRef
    status: ResolutionStatus
    target: SymbolId | None
    candidates: tuple[SymbolId, ...]
    display_name: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidates", _owned_tuple(self.candidates, "candidates")
        )


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    fact: ReferenceRef
    status: ResolutionStatus
    target: SymbolId | None
    candidates: tuple[SymbolId, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidates", _owned_tuple(self.candidates, "candidates")
        )


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    imports: tuple[ResolvedImport, ...]
    calls: tuple[ResolvedCall, ...]
    references: tuple[ResolvedReference, ...]
    diagnostics: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "imports", _owned_tuple(self.imports, "imports"))
        object.__setattr__(self, "calls", _owned_tuple(self.calls, "calls"))
        object.__setattr__(
            self, "references", _owned_tuple(self.references, "references")
        )
        object.__setattr__(
            self, "diagnostics", _owned_tuple(self.diagnostics, "diagnostics")
        )


LANGUAGE_FAMILIES: Mapping[Language, str] = MappingProxyType(
    {
        Language.TYPESCRIPT: "typescript",
        Language.JAVASCRIPT: "typescript",
        Language.TSX: "typescript",
        Language.VUE: "typescript",
        Language.SVELTE: "typescript",
        Language.C: "c-family",
        Language.CPP: "c-family",
    }
)

UNKNOWN_TYPE_KEY = "<?>"

_WORD = re.compile(r"(?:[^\W\d]|[$_])(?:\w|[$])*", re.UNICODE)
_TYPE_TOKEN = re.compile(
    r"::|\.\.\.|->|=>|&&|\|\||(?:[^\W\d]|[$_])(?:\w|[$])*|\d+|[^\s]",
    re.UNICODE,
)


def canonical_type_key(value: str | None) -> str:
    """Return a deterministic syntactic key without inferring type identity."""
    if value is None:
        return UNKNOWN_TYPE_KEY
    text = value.strip()
    if text in {"", "?", UNKNOWN_TYPE_KEY}:
        return UNKNOWN_TYPE_KEY
    tokens = _TYPE_TOKEN.findall(text)
    if not tokens:
        return UNKNOWN_TYPE_KEY
    pieces: list[str] = []
    previous = ""
    for token in tokens:
        if pieces and _WORD.fullmatch(previous) and _WORD.fullmatch(token):
            pieces.append(" ")
        pieces.append(token)
        previous = token
    return "".join(pieces)


_TYPE_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.INTERFACE,
        SymbolKind.RECORD,
        SymbolKind.ENUM,
        SymbolKind.TYPE,
    }
)
_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)


def _family(language: Language) -> str:
    return LANGUAGE_FAMILIES.get(language, language.value)


def _stable_ids(values: Iterable[SymbolId]) -> tuple[SymbolId, ...]:
    return tuple(sorted(set(values)))


def _merge_evidence(
    current: ResolutionStatus | None,
    incoming: ResolutionStatus,
) -> ResolutionStatus:
    if current is None:
        return incoming
    rank = {
        ResolutionStatus.UNRESOLVED: 0,
        ResolutionStatus.RESOLVED: 1,
        ResolutionStatus.AMBIGUOUS: 2,
        ResolutionStatus.EXTERNAL: 3,
    }
    return incoming if rank[incoming] > rank[current] else current


def _extensionless(file: str) -> str:
    path = PurePosixPath(file)
    return path.with_suffix("").as_posix()


_EXACT_FILE_PREFIX = "\0file:"


def _exact_file_key(file: str) -> str:
    return f"{_EXACT_FILE_PREFIX}{file}"


def _has_explicit_suffix(value: str) -> bool:
    return bool(PurePosixPath(value).suffix)


def _normal_module(language: Language, value: str) -> str:
    text = value.strip()
    if language is Language.RUST:
        return text.replace("::", "/").strip("/")
    if language in {Language.C, Language.CPP}:
        return text.removeprefix("./")
    return text


def _path_module(file_ir: FileIR) -> str:
    language = file_ir.source.language
    value = _extensionless(file_ir.source.file)
    if language in {Language.PYTHON, Language.LUA}:
        return value.replace("/", ".")
    if language is Language.GO:
        parent = PurePosixPath(file_ir.source.file).parent.as_posix()
        return "" if parent == "." else parent
    if language is Language.RUST:
        parts = list(PurePosixPath(value).parts)
        if parts[:1] == ["src"]:
            parts = parts[1:]
        if parts[-1:] in (["lib"], ["main"]):
            parts = parts[:-1]
        if parts[-1:] == ["mod"]:
            parts = parts[:-1]
        return "/".join(("crate", *parts))
    return value


def _declared_module(file_ir: FileIR) -> str | None:
    if file_ir.module is None:
        return None
    return _normal_module(file_ir.source.language, file_ir.module)


def _module_symbol_key(file_ir: FileIR, symbol: Symbol) -> str:
    parts = (*symbol.id.container_path, symbol.name)
    if file_ir.source.language is Language.RUST:
        path_key = _path_module(file_ir)
        if symbol.name == file_ir.module or "/" in symbol.name:
            return path_key
        return "/".join((*path_key.split("/"), *parts))
    return ".".join(parts)


def _import_module(file_ir: FileIR, raw: str) -> str:
    language = file_ir.source.language
    if language is Language.PYTHON and raw.startswith("."):
        level = len(raw) - len(raw.lstrip("."))
        rest = raw[level:]
        current = _declared_module(file_ir) or _path_module(file_ir)
        is_package_file = PurePosixPath(file_ir.source.file).stem == "__init__"
        package = current if is_package_file else current.rpartition(".")[0]
        parts = [part for part in package.split(".") if part]
        trim = max(0, level - 1)
        if not package or trim >= len(parts) and trim:
            return "<invalid-relative-import>"
        elif trim:
            parts = parts[:-trim]
        if rest:
            parts.extend(part for part in rest.split(".") if part)
        return ".".join(parts)
    if language in {
        Language.TYPESCRIPT,
        Language.JAVASCRIPT,
        Language.TSX,
        Language.VUE,
        Language.SVELTE,
    } and raw.startswith("."):
        base = PurePosixPath(_extensionless(file_ir.source.file)).parent.as_posix()
        joined = posixpath.normpath(posixpath.join(base, raw))
        joined = joined.removeprefix("./")
        return _exact_file_key(joined) if _has_explicit_suffix(raw) else joined
    if language is Language.RUST:
        parts = [part for part in raw.replace("::", "/").split("/") if part]
        rust_current = [part for part in _path_module(file_ir).split("/") if part]
        if parts[:1] == ["crate"]:
            return "/".join(parts)
        if parts[:1] == ["self"]:
            return "/".join((*rust_current, *parts[1:]))
        while parts[:1] == ["super"]:
            rust_current = rust_current[:-1]
            parts = parts[1:]
        return (
            "/".join((*rust_current, *parts))
            if raw.startswith("super")
            else "/".join(parts)
        )
    normalized = _normal_module(language, raw)
    if language in {Language.C, Language.CPP} and _has_explicit_suffix(raw):
        return _exact_file_key(normalized)
    return normalized


def _type_head(value: str | None) -> str:
    key = canonical_type_key(value)
    if key == UNKNOWN_TYPE_KEY:
        return key
    key = re.sub(r"^(?:const|volatile|readonly|mut|ref|in|out)\s+", "", key)
    depth = 0
    end = len(key)
    for index, character in enumerate(key):
        if character == "<" and depth == 0:
            end = index
            break
        if character in "([":
            depth += 1
        elif character in ")]" and depth:
            depth -= 1
    key = key[:end].rstrip("?[]*& ")
    return key or UNKNOWN_TYPE_KEY


@dataclass(frozen=True, slots=True)
class _Alias:
    status: ResolutionStatus
    symbols: tuple[SymbolId, ...]
    files: tuple[str, ...]
    module_keys: tuple[str, ...]
    namespace: bool = False
    external: bool = False
    unresolved: bool = False


class _Resolver:
    def __init__(self, project: ProjectIR) -> None:
        self.project = project
        self.files = tuple(sorted(project.files, key=lambda item: item.source.file))
        self.file_by_name = {item.source.file: item for item in self.files}
        self.symbols = tuple(
            sorted(
                (symbol for file_ir in self.files for symbol in file_ir.symbols),
                key=lambda item: item.id,
            )
        )
        self.symbol_by_id = {symbol.id: symbol for symbol in self.symbols}
        self.by_family_name: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self.by_file_name: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self.by_container_name: dict[tuple[str, tuple[str, ...], str], list[Symbol]] = (
            defaultdict(list)
        )
        self.type_by_file_path: dict[tuple[str, tuple[str, ...]], list[Symbol]] = (
            defaultdict(list)
        )
        self.type_by_family_key: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self._type_cache: dict[tuple[str, str], tuple[Symbol, ...]] = {}
        self._member_cache: dict[
            tuple[tuple[SymbolId, ...], str], tuple[Symbol, ...]
        ] = {}
        self._same_module_cache: dict[tuple[str, str], tuple[Symbol, ...]] = {}
        for symbol in self.symbols:
            family = _family(symbol.lang)
            self.by_family_name[family, symbol.name].append(symbol)
            self.by_file_name[symbol.file, symbol.name].append(symbol)
            self.by_container_name[
                symbol.file, symbol.id.container_path, symbol.name
            ].append(symbol)
            if symbol.kind in _TYPE_KINDS:
                self.type_by_file_path[
                    symbol.file, (*symbol.id.container_path, symbol.name)
                ].append(symbol)
                names = {
                    symbol.name,
                    ".".join((*symbol.id.container_path, symbol.name)),
                    "::".join((*symbol.id.container_path, symbol.name)),
                }
                for name in names:
                    if name:
                        self.type_by_family_key[
                            family, canonical_type_key(name)
                        ].append(symbol)

        self.file_keys: dict[str, tuple[str, ...]] = {}
        self.base_file_keys: dict[str, tuple[str, ...]] = {}
        self.module_files: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.module_owners: dict[tuple[str, str], set[SymbolId]] = defaultdict(set)
        self.exports: dict[tuple[str, str], dict[str, set[SymbolId]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.declarations: dict[tuple[str, str], dict[str, set[SymbolId]]] = (
            defaultdict(lambda: defaultdict(set))
        )
        self.export_evidence: dict[tuple[str, str, str], ResolutionStatus] = {}
        self.namespace_reexports: dict[tuple[str, str, str], set[str]] = defaultdict(
            set
        )
        self.namespace_reexport_evidence: dict[
            tuple[str, str, str], ResolutionStatus
        ] = {}
        self._index_modules()
        self._propagate_reexports()
        self.imports = self._resolve_imports()
        self.external_wildcard_files: set[str] = set()
        self.aliases = self._build_aliases()

    def _index_modules(self) -> None:
        for file_ir in self.files:
            language = file_ir.source.language
            family = _family(language)
            base_keys = {_path_module(file_ir)}
            if declared := _declared_module(file_ir):
                base_keys.add(declared)
            if language in {
                Language.TYPESCRIPT,
                Language.JAVASCRIPT,
                Language.TSX,
                Language.VUE,
                Language.SVELTE,
            }:
                base_keys.add(_exact_file_key(file_ir.source.file))
            if language in {Language.C, Language.CPP}:
                base_keys.add(_exact_file_key(file_ir.source.file))
            self.base_file_keys[file_ir.source.file] = tuple(
                sorted(key for key in base_keys if key)
            )
            keys = set(base_keys)
            module_symbols = [
                symbol for symbol in file_ir.symbols if symbol.kind is SymbolKind.MODULE
            ]
            for symbol in module_symbols:
                full = _module_symbol_key(file_ir, symbol)
                if full:
                    keys.add(full)
            normalized = tuple(sorted(key for key in keys if key))
            self.file_keys[file_ir.source.file] = normalized
            for key in normalized:
                self.module_files[family, key].add(file_ir.source.file)

            public = [
                symbol
                for symbol in file_ir.symbols
                if symbol.visibility is Visibility.PUBLIC
                and symbol.kind is not SymbolKind.REEXPORT
            ]
            all_declared = [
                symbol
                for symbol in file_ir.symbols
                if symbol.kind is not SymbolKind.REEXPORT
            ]
            module_public = [
                symbol
                for symbol in public
                if not symbol.id.container_path
                or language in {Language.C, Language.CPP, Language.LUA}
            ]
            module_declared = [
                symbol
                for symbol in all_declared
                if not symbol.id.container_path
                or language in {Language.C, Language.CPP, Language.LUA}
            ]
            for key in sorted(key for key in base_keys if key):
                for symbol in module_public:
                    self.exports[family, key][symbol.name].add(symbol.id)
                    if "default" in symbol.modifiers:
                        self.exports[family, key]["default"].add(symbol.id)
                for symbol in module_declared:
                    self.declarations[family, key][symbol.name].add(symbol.id)
                    if "default" in symbol.modifiers:
                        self.declarations[family, key]["default"].add(symbol.id)

            static_module_types = (
                all_declared
                if language in {Language.JAVA, Language.KOTLIN, Language.RUST}
                else ()
            )
            for type_symbol in (
                symbol for symbol in static_module_types if symbol.kind in _TYPE_KINDS
            ):
                type_parts = (*type_symbol.id.container_path, type_symbol.name)
                direct_container = type_parts
                base_keys = set(normalized)
                if language is Language.RUST:
                    base_keys = {_path_module(file_ir)}
                elif declared_module := _declared_module(file_ir):
                    base_keys = {declared_module}
                for base in base_keys:
                    separator = "/" if language is Language.RUST else "."
                    type_key = (
                        separator.join((base, *type_parts))
                        if base
                        else separator.join(type_parts)
                    )
                    self.module_files[family, type_key].add(file_ir.source.file)
                    self.module_owners[family, type_key].add(type_symbol.id)
                    for member in public:
                        if member.id.container_path == direct_container:
                            self.exports[family, type_key][member.name].add(member.id)
                    for member in all_declared:
                        if member.id.container_path == direct_container:
                            self.declarations[family, type_key][member.name].add(
                                member.id
                            )

            for module_symbol in module_symbols:
                namespace = (*module_symbol.id.container_path, module_symbol.name)
                key = _module_symbol_key(file_ir, module_symbol)
                if not key:
                    continue
                self.module_files[family, key].add(file_ir.source.file)
                self.module_owners[family, key].add(module_symbol.id)
                for symbol in public:
                    if symbol.id.container_path == namespace:
                        self.exports[family, key][symbol.name].add(symbol.id)
                    if (
                        symbol.kind in _TYPE_KINDS
                        and symbol.id.container_path == namespace
                    ):
                        type_key = f"{key}.{symbol.name}"
                        self.module_files[family, type_key].add(file_ir.source.file)
                        for member in public:
                            if member.id.container_path == (*namespace, symbol.name):
                                self.exports[family, type_key][member.name].add(
                                    member.id
                                )
                for symbol in all_declared:
                    if symbol.id.container_path == namespace:
                        self.declarations[family, key][symbol.name].add(symbol.id)
                    if (
                        symbol.kind in _TYPE_KINDS
                        and symbol.id.container_path == namespace
                    ):
                        type_key = f"{key}.{symbol.name}"
                        for member in all_declared:
                            if member.id.container_path == (*namespace, symbol.name):
                                self.declarations[family, type_key][member.name].add(
                                    member.id
                                )

    def _module_scope(
        self, file_ir: FileIR, raw: str
    ) -> tuple[str, tuple[str, ...], dict[str, set[SymbolId]]]:
        key = _import_module(file_ir, raw)
        family = _family(file_ir.source.language)
        files = tuple(sorted(self.module_files.get((family, key), ())))
        if not files and file_ir.source.language in {Language.C, Language.CPP}:
            parent = PurePosixPath(file_ir.source.file).parent.as_posix()
            relative = posixpath.normpath(posixpath.join(parent, raw))
            relative = relative.removeprefix("./")
            if _has_explicit_suffix(raw):
                relative = _exact_file_key(relative)
            relative_files = tuple(
                sorted(self.module_files.get((family, relative), ()))
            )
            if relative_files:
                key = relative
                files = relative_files
        exports = self.exports.get((family, key), {})
        return key, files, exports

    def _propagate_reexports(self) -> None:
        edges: list[tuple[tuple[str, ...], ImportRef]] = []
        dependents: dict[tuple[str, str], list[int]] = defaultdict(list)
        missing_events: list[
            tuple[str, str, str, frozenset[SymbolId], ResolutionStatus]
        ] = []
        for file_ir in self.files:
            family = _family(file_ir.source.language)
            source_keys = self.base_file_keys.get(file_ir.source.file, ())
            for fact in file_ir.imports:
                if not fact.reexport:
                    continue
                target_key = _import_module(file_ir, fact.module)
                if fact.wildcard and fact.alias:
                    target_files = self.module_files.get((family, target_key), set())
                    target_status = (
                        ResolutionStatus.EXTERNAL
                        if not target_files
                        else ResolutionStatus.AMBIGUOUS
                        if len(target_files) > 1
                        else None
                    )
                    reexport_ids = {
                        symbol.id
                        for symbol in file_ir.symbols
                        if symbol.kind is SymbolKind.REEXPORT
                        and symbol.name == fact.alias
                    }
                    for source_key in source_keys:
                        self.namespace_reexports[family, source_key, fact.alias].add(
                            target_key
                        )
                        self.exports[family, source_key][fact.alias].update(
                            reexport_ids
                        )
                        self.declarations[family, source_key][fact.alias].update(
                            reexport_ids
                        )
                        if target_status is not None:
                            evidence_key = (family, source_key, fact.alias)
                            self.namespace_reexport_evidence[evidence_key] = (
                                _merge_evidence(
                                    self.namespace_reexport_evidence.get(evidence_key),
                                    target_status,
                                )
                            )
                    continue
                edge_index = len(edges)
                edges.append((source_keys, fact))
                dependents[family, target_key].append(edge_index)
                target_files = self.module_files.get((family, target_key), set())
                if fact.name is not None:
                    target_exports = self.exports.get((family, target_key), {}).get(
                        fact.name, set()
                    )
                    target_declarations = self.declarations.get(
                        (family, target_key), {}
                    ).get(fact.name, set())
                    private_targets = target_declarations.difference(target_exports)
                    if private_targets:
                        local_name = fact.alias or fact.name
                        for source_key in source_keys:
                            self.exports[family, source_key][local_name].update(
                                private_targets
                            )
                if not target_files:
                    missing_events.append(
                        (
                            family,
                            target_key,
                            "*" if fact.wildcard else fact.name or "*",
                            frozenset(),
                            ResolutionStatus.EXTERNAL,
                        )
                    )
                elif (
                    fact.name is not None
                    and not self.exports.get((family, target_key), {}).get(fact.name)
                    and not self.declarations.get((family, target_key), {}).get(
                        fact.name
                    )
                ):
                    missing_events.append(
                        (
                            family,
                            target_key,
                            fact.name,
                            frozenset(),
                            ResolutionStatus.UNRESOLVED,
                        )
                    )

        events: deque[
            tuple[
                str,
                str,
                str,
                frozenset[SymbolId],
                ResolutionStatus | None,
            ]
        ] = deque()
        for (family, module_key), names in self.exports.items():
            for name, values in names.items():
                if values:
                    events.append((family, module_key, name, frozenset(values), None))
        for (family, module_key, name), status in self.export_evidence.items():
            events.append((family, module_key, name, frozenset(), status))
        events.extend(missing_events)

        while events:
            family, target_key, changed_name, changed_ids, changed_status = (
                events.popleft()
            )
            for edge_index in dependents.get((family, target_key), ()):
                source_keys, fact = edges[edge_index]
                if fact.wildcard:
                    local_name = changed_name
                elif fact.name is not None and changed_name in {fact.name, "*"}:
                    local_name = fact.alias or fact.name
                else:
                    continue
                for source_key in source_keys:
                    if changed_ids:
                        bucket = self.exports[family, source_key][local_name]
                        delta = changed_ids.difference(bucket)
                        if delta:
                            bucket.update(delta)
                            events.append(
                                (
                                    family,
                                    source_key,
                                    local_name,
                                    frozenset(delta),
                                    None,
                                )
                            )
                    if changed_status is not None:
                        evidence_key = (family, source_key, local_name)
                        current = self.export_evidence.get(evidence_key)
                        merged = _merge_evidence(current, changed_status)
                        if current is not merged:
                            self.export_evidence[evidence_key] = merged
                            events.append(
                                (
                                    family,
                                    source_key,
                                    local_name,
                                    frozenset(),
                                    merged,
                                )
                            )

        namespace_events: deque[
            tuple[
                str,
                str,
                str,
                frozenset[str],
                ResolutionStatus | None,
            ]
        ] = deque()
        for (family, module_key, name), targets in self.namespace_reexports.items():
            namespace_events.append(
                (
                    family,
                    module_key,
                    name,
                    frozenset(targets),
                    self.namespace_reexport_evidence.get((family, module_key, name)),
                )
            )
        while namespace_events:
            family, target_key, changed_name, changed_keys, changed_status = (
                namespace_events.popleft()
            )
            for edge_index in dependents.get((family, target_key), ()):
                source_keys, fact = edges[edge_index]
                if fact.wildcard:
                    local_name = changed_name
                elif fact.name == changed_name:
                    local_name = fact.alias or fact.name
                else:
                    continue
                assert local_name is not None
                for source_key in source_keys:
                    namespace_key = (family, source_key, local_name)
                    namespace_bucket = self.namespace_reexports[namespace_key]
                    namespace_delta = changed_keys.difference(namespace_bucket)
                    status_changed = False
                    merged_status = self.namespace_reexport_evidence.get(namespace_key)
                    if changed_status is not None:
                        next_status = _merge_evidence(
                            merged_status,
                            changed_status,
                        )
                        status_changed = next_status is not merged_status
                        if status_changed:
                            self.namespace_reexport_evidence[namespace_key] = (
                                next_status
                            )
                            merged_status = next_status
                    if namespace_delta:
                        namespace_bucket.update(namespace_delta)
                    if namespace_delta or status_changed:
                        namespace_events.append(
                            (
                                family,
                                source_key,
                                local_name,
                                frozenset(namespace_delta),
                                merged_status if status_changed else None,
                            )
                        )

    @staticmethod
    def _import_status(
        files: tuple[str, ...],
        symbols: tuple[SymbolId, ...],
        named: bool,
    ) -> ResolutionStatus:
        if not files:
            return ResolutionStatus.EXTERNAL
        if named:
            if not symbols:
                return ResolutionStatus.UNRESOLVED
            return (
                ResolutionStatus.RESOLVED
                if len(symbols) == 1
                else ResolutionStatus.AMBIGUOUS
            )
        return (
            ResolutionStatus.RESOLVED if len(files) == 1 else ResolutionStatus.AMBIGUOUS
        )

    def _resolve_one_import(self, file_ir: FileIR, fact: ImportRef) -> ResolvedImport:
        key, files, exports = self._module_scope(file_ir, fact.module)
        if fact.name is not None:
            family = _family(file_ir.source.language)
            symbols = _stable_ids(
                (
                    *exports.get(fact.name, ()),
                    *self.declarations.get((family, key), {}).get(fact.name, ()),
                )
            )
            if not symbols and file_ir.source.language is Language.PYTHON:
                child_key = ".".join(part for part in (key, fact.name) if part)
                child_files = tuple(
                    sorted(self.module_files.get((family, child_key), ()))
                )
                if child_files:
                    key = child_key
                    files = child_files
                    exports = self.exports.get((family, key), {})
                    symbols = _stable_ids(self.module_owners.get((family, key), ()))
        elif fact.wildcard:
            symbols = _stable_ids(
                symbol for values in exports.values() for symbol in values
            )
        else:
            symbols = (
                _stable_ids(
                    self.module_owners.get((_family(file_ir.source.language), key), ())
                )
                if fact.alias is not None
                else ()
            )
        status = self._import_status(
            files,
            symbols,
            fact.name is not None,
        )
        if fact.name is not None and files:
            family = _family(file_ir.source.language)
            evidence = self.export_evidence.get(
                (family, key, fact.name),
                self.export_evidence.get((family, key, "*")),
            )
            if evidence is not None:
                if symbols and evidence is ResolutionStatus.EXTERNAL:
                    status = ResolutionStatus.AMBIGUOUS
                elif not symbols:
                    status = evidence
        return ResolvedImport(file_ir.source.file, fact, status, files, symbols)

    def _ordered_facts(
        self, field: str
    ) -> list[tuple[FileIR, ImportRef | CallRef | ReferenceRef]]:
        values: list[tuple[FileIR, ImportRef | CallRef | ReferenceRef]] = []
        for file_ir in self.files:
            facts: tuple[ImportRef | CallRef | ReferenceRef, ...] = getattr(
                file_ir, field
            )
            values.extend((file_ir, fact) for fact in facts)
        return sorted(
            values,
            key=lambda item: (
                item[0].source.file,
                item[1].span,
            ),
        )

    def _resolve_imports(self) -> tuple[ResolvedImport, ...]:
        values: list[ResolvedImport] = []
        for file_ir, fact in self._ordered_facts("imports"):
            if not isinstance(fact, ImportRef):
                raise TypeError("imports field contains a non-import fact")
            values.append(self._resolve_one_import(file_ir, fact))
        return tuple(values)

    def _build_aliases(self) -> dict[str, dict[str, _Alias]]:
        aliases: dict[str, dict[str, _Alias]] = defaultdict(dict)

        def add(file: str, name: str, value: _Alias) -> None:
            existing = aliases[file].get(name)
            if existing is None:
                aliases[file][name] = value
                return
            symbols = _stable_ids((*existing.symbols, *value.symbols))
            files = tuple(sorted({*existing.files, *value.files}))
            module_keys = tuple(sorted({*existing.module_keys, *value.module_keys}))
            external = existing.external or value.external
            unresolved = existing.unresolved or value.unresolved
            if symbols and (external or unresolved):
                status = ResolutionStatus.AMBIGUOUS
            elif symbols:
                status = (
                    ResolutionStatus.RESOLVED
                    if len(symbols) == 1
                    else ResolutionStatus.AMBIGUOUS
                )
            elif external:
                status = ResolutionStatus.EXTERNAL
            else:
                status = ResolutionStatus.UNRESOLVED
            aliases[file][name] = _Alias(
                status,
                symbols,
                files,
                module_keys,
                existing.namespace or value.namespace,
                external,
                unresolved,
            )

        for item in self.imports:
            file_ir = self.file_by_name[item.source_file]
            fact = item.fact
            key, _, exports = self._module_scope(file_ir, fact.module)
            namespace_targets = (
                self.namespace_reexports.get(
                    (_family(file_ir.source.language), key, fact.name),
                    set(),
                )
                if fact.name is not None
                else set()
            )
            namespace_status = (
                self.namespace_reexport_evidence.get(
                    (_family(file_ir.source.language), key, fact.name)
                )
                if fact.name is not None
                else None
            )
            open_scope = fact.wildcard or (
                file_ir.source.language in {Language.C, Language.CPP}
                and fact.name is None
                and fact.alias is None
            )
            if open_scope and fact.alias in {None, "."}:
                wildcard_evidence = self.export_evidence.get(
                    (_family(file_ir.source.language), key, "*")
                )
                has_external_scope = (
                    item.status is ResolutionStatus.EXTERNAL
                    or wildcard_evidence is ResolutionStatus.EXTERNAL
                )
                if has_external_scope:
                    self.external_wildcard_files.add(item.source_file)
                if item.status is ResolutionStatus.EXTERNAL:
                    continue
                for name, symbols in exports.items():
                    ids = _stable_ids(symbols)
                    status = self._import_status(item.target_files, ids, True)
                    add(
                        item.source_file,
                        name,
                        _Alias(
                            status,
                            ids,
                            item.target_files,
                            (key,),
                            external=status is ResolutionStatus.EXTERNAL
                            or has_external_scope,
                            unresolved=status is ResolutionStatus.UNRESOLVED,
                        ),
                    )
                continue
            if (
                file_ir.source.language is Language.CSHARP
                and fact.name is not None
                and fact.alias is None
            ):
                imported_types = tuple(
                    symbol
                    for symbol in self._symbols(item.target_symbols)
                    if symbol.kind in _TYPE_KINDS
                )
                if imported_types:
                    for imported_type in imported_types:
                        container = (
                            *imported_type.id.container_path,
                            imported_type.name,
                        )
                        members = (
                            symbol
                            for symbol in self.symbols
                            if symbol.file == imported_type.file
                            and symbol.id.container_path == container
                            and symbol.visibility is Visibility.PUBLIC
                        )
                        for member in members:
                            add(
                                item.source_file,
                                member.name,
                                _Alias(
                                    ResolutionStatus.RESOLVED,
                                    (member.id,),
                                    item.target_files,
                                    (key,),
                                ),
                            )
                    continue
            local = fact.alias or fact.name
            python_prefix: str | None = None
            if local is None and fact.module:
                language = file_ir.source.language
                if language is Language.GO:
                    declared = {
                        self.file_by_name[file].module
                        for file in item.target_files
                        if self.file_by_name[file].module
                    }
                    local = (
                        next(iter(declared))
                        if len(declared) == 1
                        else fact.module.rstrip("/").rsplit("/", 1)[-1]
                    )
                elif language is Language.RUST:
                    local = fact.module.rsplit("::", 1)[-1]
                elif (
                    language is Language.PYTHON
                    and fact.alias is None
                    and "." in fact.module
                ):
                    python_prefix = fact.module.split(".", 1)[0]
                    local = fact.module
                elif language in {Language.PYTHON, Language.LUA}:
                    local = fact.module.split(".", 1)[0]
            if local:
                target_symbols = self._symbols(item.target_symbols)
                alias_module_keys = set(namespace_targets)
                family = _family(file_ir.source.language)
                separator = "/" if file_ir.source.language is Language.RUST else "."
                for target_symbol in target_symbols:
                    if target_symbol.kind is not SymbolKind.MODULE:
                        continue
                    symbol_key = separator.join(
                        (*target_symbol.id.container_path, target_symbol.name)
                    )
                    candidate_keys = {symbol_key}
                    if key and target_symbol.name:
                        candidate_keys.add(separator.join((key, target_symbol.name)))
                    alias_module_keys.update(
                        candidate
                        for candidate in candidate_keys
                        if (family, candidate) in self.module_files
                    )
                namespace = (
                    bool(namespace_targets)
                    or (fact.wildcard and fact.alias not in {None, "."})
                    or any(
                        symbol.kind is SymbolKind.MODULE for symbol in target_symbols
                    )
                    or (
                        fact.name is None
                        and not any(
                            symbol.kind in _TYPE_KINDS for symbol in target_symbols
                        )
                    )
                )
                alias_symbols = item.target_symbols
                if namespace:
                    namespace_owners = _stable_ids(
                        symbol
                        for module_key in alias_module_keys
                        for symbol in self.module_owners.get((family, module_key), ())
                    )
                    if namespace_owners:
                        alias_symbols = namespace_owners
                add(
                    item.source_file,
                    local,
                    _Alias(
                        (
                            ResolutionStatus.AMBIGUOUS
                            if namespace_status is ResolutionStatus.AMBIGUOUS
                            else item.status
                        ),
                        alias_symbols,
                        item.target_files,
                        tuple(sorted(alias_module_keys)) or (key,),
                        namespace,
                        item.status is ResolutionStatus.EXTERNAL
                        or namespace_status is ResolutionStatus.EXTERNAL,
                        item.status is ResolutionStatus.UNRESOLVED
                        or namespace_status is ResolutionStatus.UNRESOLVED,
                    ),
                )
                if python_prefix is not None:
                    prefix_owners = _stable_ids(
                        self.module_owners.get((family, python_prefix), ())
                    )
                    add(
                        item.source_file,
                        python_prefix,
                        _Alias(
                            item.status,
                            prefix_owners,
                            item.target_files,
                            (python_prefix,),
                            True,
                            item.status is ResolutionStatus.EXTERNAL,
                            item.status is ResolutionStatus.UNRESOLVED,
                        ),
                    )
        return aliases

    def _symbols(self, ids: Iterable[SymbolId]) -> tuple[Symbol, ...]:
        return tuple(
            self.symbol_by_id[value]
            for value in _stable_ids(ids)
            if value in self.symbol_by_id
        )

    @staticmethod
    def _matches_kind(symbol: Symbol, kind: object) -> bool:
        if kind is CallKind.CONSTRUCT:
            return symbol.kind is SymbolKind.CONSTRUCTOR or symbol.kind in _TYPE_KINDS
        if kind is CallKind.CALL:
            return symbol.kind in _CALLABLE_KINDS
        if kind is ReferenceKind.TYPE:
            return symbol.kind in _TYPE_KINDS
        if kind is ReferenceKind.NAME:
            return True
        return symbol.kind is not SymbolKind.MODULE

    def _filtered(self, values: Iterable[Symbol], kind: object) -> tuple[Symbol, ...]:
        return tuple(
            sorted(
                {
                    symbol.id: symbol
                    for symbol in values
                    if self._matches_kind(symbol, kind)
                }.values(),
                key=lambda item: item.id,
            )
        )

    def _types_named(self, file_ir: FileIR, name: str) -> tuple[Symbol, ...]:
        cache_key = (file_ir.source.file, name)
        if cache_key in self._type_cache:
            return self._type_cache[cache_key]

        def done(candidates: Iterable[Symbol]) -> tuple[Symbol, ...]:
            result = tuple(
                sorted(
                    {value.id: value for value in candidates}.values(),
                    key=lambda item: item.id,
                )
            )
            self._type_cache[cache_key] = result
            return result

        family = _family(file_ir.source.language)
        head = _type_head(name)
        values: list[Symbol] = []
        alias = self.aliases.get(file_ir.source.file, {}).get(head)
        if alias is not None:
            values.extend(
                symbol
                for symbol in self._symbols(alias.symbols)
                if symbol.kind in _TYPE_KINDS
            )
            if values or alias.status in {
                ResolutionStatus.EXTERNAL,
                ResolutionStatus.UNRESOLVED,
            }:
                return done(values)
        keys = {canonical_type_key(head)}
        separator = "::" if "::" in head else "." if "." in head else None
        if separator is not None:
            module, _, local_name = head.rpartition(separator)
            module_key, files, exports = self._module_scope(file_ir, module)
            declarations = self.declarations.get((family, module_key), {})
            qualified = tuple(
                symbol
                for symbol in self._symbols(
                    (*exports.get(local_name, ()), *declarations.get(local_name, ()))
                )
                if symbol.kind in _TYPE_KINDS
            )
            if qualified or files:
                return done(qualified)

        def matching(candidates: Iterable[Symbol]) -> tuple[Symbol, ...]:
            found = {
                value.id: value
                for value in candidates
                if value.kind in _TYPE_KINDS
                and any(
                    canonical_type_key(candidate) in keys
                    for candidate in (
                        value.name,
                        ".".join((*value.id.container_path, value.name)),
                        "::".join((*value.id.container_path, value.name)),
                    )
                )
            }
            return tuple(sorted(found.values(), key=lambda item: item.id))

        leaf = head.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        same_file = matching(self.by_file_name.get((file_ir.source.file, leaf), ()))
        if same_file:
            return done(same_file)
        same_module = matching(self._same_module(file_ir, head.rsplit(".", 1)[-1]))
        if same_module:
            return done(same_module)
        for key in keys:
            values.extend(self.type_by_family_key.get((family, key), ()))
        return done(values)

    def _members(self, types: tuple[Symbol, ...], name: str) -> tuple[Symbol, ...]:
        if not types:
            return ()
        cache_key = (tuple(sorted(value.id for value in types)), name)
        if cache_key in self._member_cache:
            return self._member_cache[cache_key]
        found: dict[SymbolId, Symbol] = {}
        for root in types:
            visited: set[SymbolId] = set()
            level: tuple[Symbol, ...] = (root,)
            while level:
                level_values: list[Symbol] = []
                next_types: dict[SymbolId, Symbol] = {}
                for type_symbol in level:
                    if type_symbol.id in visited:
                        continue
                    visited.add(type_symbol.id)
                    container = (*type_symbol.id.container_path, type_symbol.name)
                    level_values.extend(
                        self.by_container_name.get(
                            (type_symbol.file, container, name), ()
                        )
                    )
                    file_ir = self.file_by_name[type_symbol.file]
                    for super_name in type_symbol.supers:
                        for candidate in self._types_named(file_ir, super_name):
                            if candidate.id not in visited:
                                next_types[candidate.id] = candidate
                if level_values:
                    for value in level_values:
                        found[value.id] = value
                    break
                level = tuple(sorted(next_types.values(), key=lambda item: item.id))
        result = tuple(sorted(found.values(), key=lambda item: item.id))
        self._member_cache[cache_key] = result
        return result

    def _enclosing_types(self, file_ir: FileIR, owner: Symbol) -> tuple[Symbol, ...]:
        container = owner.id.container_path
        if not container:
            return ()
        for length in range(len(container), 0, -1):
            values = self.type_by_file_path.get(
                (file_ir.source.file, container[:length]), ()
            )
            if values:
                return tuple(sorted(values, key=lambda item: item.id))
        return ()

    def _direct_supers(self, types: Iterable[Symbol]) -> tuple[Symbol, ...]:
        values: dict[SymbolId, Symbol] = {}
        for type_symbol in types:
            file_ir = self.file_by_name[type_symbol.file]
            for super_name in type_symbol.supers:
                for candidate in self._types_named(file_ir, super_name):
                    values[candidate.id] = candidate
        return tuple(sorted(values.values(), key=lambda item: item.id))

    def _alias_scope(
        self, file: str, local: str, member: str | None, kind: object
    ) -> tuple[ResolutionStatus | None, tuple[Symbol, ...]]:
        alias = self.aliases.get(file, {}).get(local)
        if alias is None:
            return None, ()
        if not alias.symbols and alias.status in {
            ResolutionStatus.EXTERNAL,
            ResolutionStatus.UNRESOLVED,
        }:
            return alias.status, ()
        values = self._symbols(alias.symbols)
        if member is not None:
            if not alias.namespace:
                type_values = tuple(
                    value for value in values if value.kind in _TYPE_KINDS
                )
                member_values = self._members(type_values, member)
                if member_values:
                    terminal = (
                        ResolutionStatus.AMBIGUOUS
                        if alias.external or alias.unresolved
                        else None
                    )
                    return terminal, self._filtered(member_values, kind)
                if alias.status is ResolutionStatus.AMBIGUOUS:
                    return ResolutionStatus.AMBIGUOUS, ()
                if alias.external:
                    return ResolutionStatus.EXTERNAL, ()
                return ResolutionStatus.UNRESOLVED, ()
            else:
                family = _family(self.file_by_name[file].source.language)
                module_values: list[Symbol] = []
                for module_key in alias.module_keys:
                    module_exports = self.exports.get((family, module_key), {})
                    module_values.extend(self._symbols(module_exports.get(member, ())))
                values = tuple(
                    sorted(
                        {value.id: value for value in module_values}.values(),
                        key=lambda item: item.id,
                    )
                )
            if not values:
                if alias.status is ResolutionStatus.AMBIGUOUS:
                    return ResolutionStatus.AMBIGUOUS, ()
                if alias.external:
                    return ResolutionStatus.EXTERNAL, ()
                if alias.unresolved:
                    return ResolutionStatus.UNRESOLVED, ()
        terminal = (
            ResolutionStatus.AMBIGUOUS
            if alias.status is ResolutionStatus.AMBIGUOUS
            or values
            and (alias.external or alias.unresolved)
            else None
        )
        filtered = self._filtered(values, kind)
        if terminal is not None or filtered:
            return terminal, filtered
        if alias.external:
            return ResolutionStatus.EXTERNAL, ()
        return ResolutionStatus.UNRESOLVED, ()

    def _same_module(self, file_ir: FileIR, name: str) -> tuple[Symbol, ...]:
        cache_key = (file_ir.source.file, name)
        if cache_key in self._same_module_cache:
            return self._same_module_cache[cache_key]
        family = _family(file_ir.source.language)
        keys = self.file_keys.get(file_ir.source.file, ())
        values: list[Symbol] = []
        for key in keys:
            for target_file in self.module_files.get((family, key), ()):
                values.extend(self.by_file_name.get((target_file, name), ()))
        result = tuple(
            sorted(
                {item.id: item for item in values}.values(), key=lambda item: item.id
            )
        )
        self._same_module_cache[cache_key] = result
        return result

    def _scope(
        self,
        *,
        file_ir: FileIR,
        owner: Symbol | None,
        name: str,
        qualifier: str | None,
        kind: object,
    ) -> tuple[ResolutionStatus | None, tuple[Symbol, ...]]:
        bindings = (
            {binding.name: binding.type_name for binding in owner.bindings}
            if owner
            else {}
        )

        if qualifier is None and name in bindings:
            return ResolutionStatus.UNRESOLVED, ()

        if qualifier in {"this", "self", "cls"} and owner is not None:
            direct = self._filtered(
                self._members(self._enclosing_types(file_ir, owner), name), kind
            )
            return None, direct

        if qualifier in {"super", "base"} and owner is not None:
            parents = self._direct_supers(self._enclosing_types(file_ir, owner))
            return None, self._filtered(self._members(parents, name), kind)

        if qualifier is None and owner is not None and owner.id.container_path:
            direct = self._filtered(
                self._members(self._enclosing_types(file_ir, owner), name), kind
            )
            if direct:
                return None, direct

        if qualifier is not None and qualifier in bindings:
            type_name = bindings[qualifier]
            if canonical_type_key(type_name) == UNKNOWN_TYPE_KEY:
                return ResolutionStatus.UNRESOLVED, ()
            type_head = _type_head(type_name)
            separator = "::" if "::" in type_head else "." if "." in type_head else None
            if separator is not None:
                namespace, _, local_type = type_head.rpartition(separator)
                namespace_status, namespace_types = self._alias_scope(
                    file_ir.source.file,
                    namespace,
                    local_type,
                    ReferenceKind.TYPE,
                )
                if namespace_status is not None or namespace_types:
                    members = self._filtered(
                        self._members(namespace_types, name),
                        kind,
                    )
                    if namespace_status is not None:
                        return namespace_status, members
                    return (
                        (None, members)
                        if members
                        else (ResolutionStatus.UNRESOLVED, ())
                    )
            type_alias = self.aliases.get(file_ir.source.file, {}).get(type_head)
            if type_alias is not None and type_alias.status in {
                ResolutionStatus.EXTERNAL,
                ResolutionStatus.UNRESOLVED,
            }:
                return type_alias.status, ()
            types = self._types_named(file_ir, type_name)
            members = self._filtered(self._members(types, name), kind)
            if (
                type_alias is not None
                and type_alias.status is ResolutionStatus.AMBIGUOUS
            ):
                return ResolutionStatus.AMBIGUOUS, members
            return (None, members) if members else (ResolutionStatus.UNRESOLVED, ())

        if qualifier is not None:
            terminal, values = self._alias_scope(
                file_ir.source.file, qualifier, name, kind
            )
            if terminal is not None or values:
                return terminal, values
            qualifier_head = _type_head(qualifier)
            separator = (
                "::"
                if "::" in qualifier_head
                else "."
                if "." in qualifier_head
                else None
            )
            if separator is not None:
                namespace, _, local_type = qualifier_head.rpartition(separator)
                if namespace in self.aliases.get(file_ir.source.file, {}):
                    namespace_status, namespace_types = self._alias_scope(
                        file_ir.source.file,
                        namespace,
                        local_type,
                        ReferenceKind.TYPE,
                    )
                    members = self._filtered(
                        self._members(namespace_types, name),
                        kind,
                    )
                    if namespace_status is not None:
                        return namespace_status, members
                    return (
                        (None, members)
                        if members
                        else (ResolutionStatus.UNRESOLVED, ())
                    )
            types = self._types_named(file_ir, qualifier)
            members = self._filtered(self._members(types, name), kind)
            if members:
                return None, members
            return ResolutionStatus.UNRESOLVED, ()

        terminal, imported = self._alias_scope(file_ir.source.file, name, None, kind)
        if terminal is not None or imported:
            return terminal, imported

        if file_ir.source.file in self.external_wildcard_files:
            return ResolutionStatus.EXTERNAL, ()

        same_file = self._filtered(
            self.by_file_name.get((file_ir.source.file, name), ()), kind
        )
        if same_file:
            return None, same_file

        same_module = self._filtered(self._same_module(file_ir, name), kind)
        if same_module:
            return None, same_module

        family = self._filtered(
            self.by_family_name.get((_family(file_ir.source.language), name), ()),
            kind,
        )
        return (None, family) if family else (ResolutionStatus.UNRESOLVED, ())

    def _finish(
        self,
        terminal: ResolutionStatus | None,
        candidates: tuple[Symbol, ...],
        *,
        kind: object,
        arity: int | None,
    ) -> tuple[ResolutionStatus, tuple[SymbolId, ...]]:
        if terminal is not None and terminal is not ResolutionStatus.AMBIGUOUS:
            return terminal, ()
        values = candidates
        if kind is CallKind.CONSTRUCT:
            construction_values: dict[SymbolId, Symbol] = {}
            associated_constructors: set[SymbolId] = set()
            for type_symbol in (value for value in values if value.kind in _TYPE_KINDS):
                container = (*type_symbol.id.container_path, type_symbol.name)
                explicit = tuple(
                    value
                    for value in self.by_container_name.get(
                        (type_symbol.file, container, type_symbol.name), ()
                    )
                    if value.kind is SymbolKind.CONSTRUCTOR
                )
                associated_constructors.update(value.id for value in explicit)
                if explicit:
                    for constructor in explicit:
                        if arity is None or len(constructor.params) == arity:
                            construction_values[constructor.id] = constructor
                else:
                    construction_values[type_symbol.id] = type_symbol
            for constructor in (
                value for value in values if value.kind is SymbolKind.CONSTRUCTOR
            ):
                if constructor.id not in associated_constructors and (
                    arity is None or len(constructor.params) == arity
                ):
                    construction_values[constructor.id] = constructor
            values = tuple(
                sorted(
                    construction_values.values(),
                    key=lambda item: item.id,
                )
            )
        elif (
            arity is not None
            and values
            and all(value.kind in _CALLABLE_KINDS for value in values)
        ):
            values = tuple(value for value in values if len(value.params) == arity)
        ids = _stable_ids(value.id for value in values)
        if terminal is ResolutionStatus.AMBIGUOUS:
            return ResolutionStatus.AMBIGUOUS, ids
        if len(ids) == 1:
            return ResolutionStatus.RESOLVED, ids
        if len(ids) > 1:
            return ResolutionStatus.AMBIGUOUS, ids
        return ResolutionStatus.UNRESOLVED, ()

    def resolve_calls(self) -> tuple[ResolvedCall, ...]:
        results: list[ResolvedCall] = []
        for file_ir, value in self._ordered_facts("calls"):
            fact = value
            assert isinstance(fact, CallRef)
            owner = self.symbol_by_id.get(fact.caller)
            if owner is None:
                results.append(
                    ResolvedCall(
                        fact,
                        ResolutionStatus.UNRESOLVED,
                        None,
                        (),
                        None,
                    )
                )
                continue
            terminal, candidates = self._scope(
                file_ir=file_ir,
                owner=owner,
                name=fact.name,
                qualifier=fact.receiver,
                kind=fact.kind,
            )
            status, ids = self._finish(
                terminal, candidates, kind=fact.kind, arity=fact.arity
            )
            target = ids[0] if status is ResolutionStatus.RESOLVED else None
            display = None
            if target is not None:
                display = (
                    f"{target.container_path[-1]}.{target.name}"
                    if target.container_path
                    else target.name
                )
            results.append(ResolvedCall(fact, status, target, ids, display))
        return tuple(results)

    def resolve_references(self) -> tuple[ResolvedReference, ...]:
        results: list[ResolvedReference] = []
        for file_ir, value in self._ordered_facts("references"):
            fact = value
            assert isinstance(fact, ReferenceRef)
            owner = (
                self.symbol_by_id.get(fact.owner) if fact.owner is not None else None
            )
            if fact.owner is not None and owner is None:
                results.append(
                    ResolvedReference(
                        fact,
                        ResolutionStatus.UNRESOLVED,
                        None,
                        (),
                    )
                )
                continue
            terminal, candidates = self._scope(
                file_ir=file_ir,
                owner=owner,
                name=fact.name,
                qualifier=fact.qualifier,
                kind=fact.kind,
            )
            status, ids = self._finish(terminal, candidates, kind=fact.kind, arity=None)
            target = ids[0] if status is ResolutionStatus.RESOLVED else None
            results.append(ResolvedReference(fact, status, target, ids))
        return tuple(results)


def resolve_project(project: ProjectIR) -> ResolutionResult:
    resolver = _Resolver(project)
    return ResolutionResult(
        resolver.imports,
        resolver.resolve_calls(),
        resolver.resolve_references(),
        (),
    )


__all__ = [
    "LANGUAGE_FAMILIES",
    "UNKNOWN_TYPE_KEY",
    "ResolutionResult",
    "ResolutionStatus",
    "ResolvedCall",
    "ResolvedImport",
    "ResolvedReference",
    "canonical_type_key",
    "resolve_project",
]
