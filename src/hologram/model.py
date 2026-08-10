from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypeVar

IR_SCHEMA_VERSION = 2

_T = TypeVar("_T")


class Language(StrEnum):
    JAVA = "java"
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    TSX = "tsx"
    VUE = "vue"
    SVELTE = "svelte"
    KOTLIN = "kotlin"
    GO = "go"
    RUST = "rust"
    CSHARP = "csharp"
    C = "c"
    CPP = "cpp"
    LUA = "lua"
    HTML = "html"
    HELM = "helm"


class SymbolKind(StrEnum):
    CLASS = "class"
    INTERFACE = "interface"
    RECORD = "record"
    ENUM = "enum"
    TYPE = "type"
    FUNCTION = "fn"
    METHOD = "method"
    CONSTRUCTOR = "ctor"
    REEXPORT = "reexport"
    FIELD = "field"
    PROPERTY = "property"
    CONSTANT = "constant"
    MODULE = "module"


class Visibility(StrEnum):
    PUBLIC = "pub"
    PROTECTED = "protected"
    INTERNAL = "internal"
    PRIVATE = "private"


class SourceRole(StrEnum):
    PRODUCTION = "production"
    TEST = "test"
    GENERATED = "generated"


class CallKind(StrEnum):
    CALL = "call"
    CONSTRUCT = "construct"


class ReferenceKind(StrEnum):
    NAME = "name"
    TYPE = "type"


class ReferenceContext(StrEnum):
    CODE = "code"
    TYPE = "type"
    ANNOTATION = "annotation"
    STRING = "string"
    CONFIG = "config"
    REFLECTION = "reflection"


class ReferenceConfidence(StrEnum):
    DEFINITE = "definite"
    POSSIBLE = "possible"


class BodyEventKind(StrEnum):
    PARAM = "param"
    LOCAL = "local"
    NAME = "name"
    TYPE = "type"
    CALL = "call"
    CONSTRUCT = "construct"
    MEMBER = "member"
    LITERAL = "literal"
    OPERATOR = "operator"
    KEYWORD = "keyword"
    CONTROL_ENTER = "control-enter"
    CONTROL_EXIT = "control-exit"


class DiagnosticSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


def _own_tuple(
    value: tuple[_T, ...] | list[_T],
    field: str,
) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list")
    return tuple(value)


def _validate_relative_file(file: str) -> None:
    path = PurePosixPath(file)
    if (
        not file
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in file
        or file != path.as_posix()
    ):
        raise ValueError("file must be a normalized relative POSIX path")


@dataclass(frozen=True, slots=True, order=True)
class SourceSpan:
    """One-based lines, zero-based UTF-8 byte columns, and end-exclusive endpoints."""

    file: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        _validate_relative_file(self.file)
        if self.start_line < 1 or self.end_line < 1:
            raise ValueError("source span lines must be positive")
        if self.end_line < self.start_line:
            raise ValueError("source span end line must not precede start line")
        if self.start_column < 0 or self.end_column < 0:
            raise ValueError("source span columns must be nonnegative")
        if (
            self.start_line == self.end_line
            and self.end_column < self.start_column
        ):
            raise ValueError("same-line source span end must not precede start")


@dataclass(frozen=True, slots=True, order=True)
class SymbolId:
    """Stable identity whose signature key only discriminates overloads."""

    language: Language
    file: str
    container_path: tuple[str, ...]
    kind: SymbolKind
    name: str
    signature_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "container_path",
            _own_tuple(self.container_path, "container_path"),
        )
        _validate_relative_file(self.file)
        if not self.name:
            raise ValueError("symbol name must not be empty")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    file: str
    language: Language
    role: SourceRole
    raw: bytes
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.raw, (bytes, bytearray, memoryview)):
            raise TypeError("raw must be bytes, bytearray, or memoryview")
        object.__setattr__(self, "raw", bytes(self.raw))
        _validate_relative_file(self.file)
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("sha256 must be exactly 64 lowercase hexadecimal digits")
        if self.sha256 != hashlib.sha256(self.raw).hexdigest():
            raise ValueError("sha256 must match raw source bytes")

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", errors="strict")


@dataclass(frozen=True, slots=True)
class Binding:
    name: str
    type_name: str


@dataclass(frozen=True, slots=True)
class CallRef:
    caller: SymbolId
    span: SourceSpan
    name: str
    receiver: str | None
    kind: CallKind
    arity: int | None


@dataclass(frozen=True, slots=True)
class ImportRef:
    span: SourceSpan
    module: str
    name: str | None
    alias: str | None
    wildcard: bool = False
    reexport: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceRef:
    owner: SymbolId | None
    span: SourceSpan
    name: str
    qualifier: str | None
    kind: ReferenceKind
    context: ReferenceContext
    confidence: ReferenceConfidence


@dataclass(frozen=True, slots=True)
class BodyEvent:
    kind: BodyEventKind
    text: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BodyIR:
    owner: SymbolId
    span: SourceSpan
    events: tuple[BodyEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", _own_tuple(self.events, "events"))


@dataclass(frozen=True, slots=True)
class Symbol:
    id: SymbolId
    span: SourceSpan
    visibility: Visibility
    signature: str
    params: tuple[str, ...] = ()
    returns: str | None = None
    supers: tuple[str, ...] = ()
    permits: tuple[str, ...] = ()
    raises: tuple[str, ...] = ()
    bindings: tuple[Binding, ...] = ()
    components: tuple[str, ...] = ()
    annotations: tuple[str, ...] = ()
    modifiers: tuple[str, ...] = ()
    body_lines: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", _own_tuple(self.params, "params"))
        object.__setattr__(self, "supers", _own_tuple(self.supers, "supers"))
        object.__setattr__(self, "permits", _own_tuple(self.permits, "permits"))
        object.__setattr__(self, "raises", _own_tuple(self.raises, "raises"))
        object.__setattr__(self, "bindings", _own_tuple(self.bindings, "bindings"))
        object.__setattr__(
            self,
            "components",
            _own_tuple(self.components, "components"),
        )
        object.__setattr__(
            self,
            "annotations",
            _own_tuple(self.annotations, "annotations"),
        )
        object.__setattr__(
            self,
            "modifiers",
            _own_tuple(self.modifiers, "modifiers"),
        )

    @property
    def name(self) -> str:
        return self.id.name

    @property
    def kind(self) -> SymbolKind:
        return self.id.kind

    @property
    def file(self) -> str:
        return self.id.file

    @property
    def lang(self) -> Language:
        return self.id.language

    @property
    def container(self) -> str | None:
        if not self.id.container_path:
            return None
        return self.id.container_path[-1]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: DiagnosticSeverity
    message: str
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class FileIR:
    source: SourceFile
    module: str | None = None
    symbols: tuple[Symbol, ...] = ()
    calls: tuple[CallRef, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    references: tuple[ReferenceRef, ...] = ()
    bodies: tuple[BodyIR, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    extractor_version: str = ""
    parser_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _own_tuple(self.symbols, "symbols"))
        object.__setattr__(self, "calls", _own_tuple(self.calls, "calls"))
        object.__setattr__(self, "imports", _own_tuple(self.imports, "imports"))
        object.__setattr__(
            self,
            "references",
            _own_tuple(self.references, "references"),
        )
        object.__setattr__(self, "bodies", _own_tuple(self.bodies, "bodies"))
        object.__setattr__(
            self,
            "diagnostics",
            _own_tuple(self.diagnostics, "diagnostics"),
        )


@dataclass(frozen=True, slots=True)
class ProjectIR:
    root: Path
    files: tuple[FileIR, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", _own_tuple(self.files, "files"))
        object.__setattr__(
            self,
            "diagnostics",
            _own_tuple(self.diagnostics, "diagnostics"),
        )
