from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, TypeVar, cast

from hologram.model import Language

__all__ = (
    "CensusRecord",
    "CorpusRegistry",
    "CorpusSpec",
    "Exclusion",
    "GoldFact",
    "GoldSample",
    "load_jsonl",
    "write_jsonl",
)


_FULL_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_FULL_RANK = re.compile(r"[0-9a-f]{64}\Z")
_EXTENSION_TOKEN = re.compile(r"\.[a-z0-9][a-z0-9+-]*\Z")
_CANONICAL_LANGUAGES = frozenset(language.value for language in Language)
_GOLD_CATEGORIES = frozenset(
    {
        "declaration",
        "kind",
        "container",
        "visibility",
        "signature",
        "relation",
        "call",
        "call_order",
        "strong_x0",
        "zero_classification",
        "approximate",
    }
)


def _require_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


def _require_nonnegative_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def _require_positive_integer(value: object, field: str) -> int:
    result = _require_nonnegative_integer(value, field)
    if result < 1:
        raise ValueError(f"{field} must be positive")
    return result


def _validate_revision(revision: object) -> None:
    if type(revision) is not str:
        raise TypeError("revision must be a string")
    if _FULL_REVISION.fullmatch(revision) is None:
        raise ValueError("revision must be a full lowercase 40-hex revision")


def _validate_path(path: object) -> None:
    if type(path) is not str:
        raise TypeError("path must be a string")
    pure = PurePosixPath(path)
    if (
        not path
        or path == "."
        or not pure.parts
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in path
        or "\x00" in path
        or path != pure.as_posix()
    ):
        raise ValueError("path must be a normalized relative POSIX path")


def _validate_language(language: object) -> None:
    if type(language) is not str:
        raise TypeError("language must be a string")
    if language not in _CANONICAL_LANGUAGES:
        raise ValueError(f"language must be canonical, got {language!r}")


def _freeze_json(value: object, *, _seen: frozenset[int] = frozenset()) -> object:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("value must not contain a non-finite number")
        return value

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in _seen:
            raise ValueError("value must not contain a cycle")
        next_seen = _seen | {identity}
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("value mapping keys must be strings")
            frozen[key] = _freeze_json(item, _seen=next_seen)
        return MappingProxyType(frozen)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in _seen:
            raise ValueError("value must not contain a cycle")
        next_seen = _seen | {identity}
        return tuple(_freeze_json(item, _seen=next_seen) for item in value)

    raise TypeError(
        "value must contain only JSON objects, arrays, strings, numbers, "
        "booleans, and null"
    )


@dataclass(frozen=True)
class CorpusSpec:
    name: str
    url: str
    revision: str
    path_env: str
    sample_files: int

    def __post_init__(self) -> None:
        _require_string(self.name, "name")
        _require_string(self.url, "url")
        _validate_revision(self.revision)
        _require_string(self.path_env, "path_env")
        _require_positive_integer(self.sample_files, "sample_files")


@dataclass(frozen=True)
class CorpusRegistry:
    corpora: tuple[CorpusSpec, ...]
    expected_census_files: int
    expected_ordinary_yaml_exclusions: int
    outside_candidate_extensions: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.corpora) is not tuple:
            raise TypeError("corpora must be a tuple")
        if any(type(spec) is not CorpusSpec for spec in self.corpora):
            raise TypeError("corpora must contain only CorpusSpec records")
        names = tuple(spec.name for spec in self.corpora)
        if len(names) != len(set(names)):
            raise ValueError("corpora must have unique names")
        path_envs = tuple(spec.path_env for spec in self.corpora)
        if len(path_envs) != len(set(path_envs)):
            raise ValueError("corpora must have unique path_env values")

        _require_nonnegative_integer(
            self.expected_census_files,
            "expected_census_files",
        )
        _require_nonnegative_integer(
            self.expected_ordinary_yaml_exclusions,
            "expected_ordinary_yaml_exclusions",
        )
        if type(self.outside_candidate_extensions) is not tuple:
            raise TypeError("outside_candidate_extensions must be a tuple")
        for extension in self.outside_candidate_extensions:
            _require_string(extension, "outside_candidate_extensions item")
            if _EXTENSION_TOKEN.fullmatch(extension) is None:
                raise ValueError(
                    "outside_candidate_extensions must contain canonical "
                    "lowercase extension tokens"
                )
        if len(self.outside_candidate_extensions) != len(
            set(self.outside_candidate_extensions)
        ):
            raise ValueError("outside_candidate_extensions must be unique")
        if self.outside_candidate_extensions != tuple(
            sorted(self.outside_candidate_extensions)
        ):
            raise ValueError("outside_candidate_extensions must be sorted")


@dataclass(frozen=True)
class CensusRecord:
    corpus: str
    revision: str
    path: str
    language: str

    def __post_init__(self) -> None:
        _require_string(self.corpus, "corpus")
        _validate_revision(self.revision)
        _validate_path(self.path)
        _validate_language(self.language)


@dataclass(frozen=True)
class GoldSample:
    corpus: str
    revision: str
    path: str
    language: str
    rank: str

    def __post_init__(self) -> None:
        _require_string(self.corpus, "corpus")
        _validate_revision(self.revision)
        _validate_path(self.path)
        _validate_language(self.language)
        if type(self.rank) is not str:
            raise TypeError("rank must be a string")
        if _FULL_RANK.fullmatch(self.rank) is None:
            raise ValueError("rank must be a full lowercase 64-hex rank")


@dataclass(frozen=True)
class GoldFact:
    id: str
    corpus: str
    revision: str
    path: str
    line: int
    language: str
    category: Literal[
        "declaration",
        "kind",
        "container",
        "visibility",
        "signature",
        "relation",
        "call",
        "call_order",
        "strong_x0",
        "zero_classification",
        "approximate",
    ]
    subject: str
    value: Mapping[str, object]
    expected: bool

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.corpus, "corpus")
        _validate_revision(self.revision)
        _validate_path(self.path)
        _require_positive_integer(self.line, "line")
        _validate_language(self.language)
        if type(self.category) is not str:
            raise TypeError("category must be a string")
        if self.category not in _GOLD_CATEGORIES:
            raise ValueError(f"category must be canonical, got {self.category!r}")
        _require_string(self.subject, "subject")
        if not isinstance(self.value, Mapping):
            raise TypeError("value must be a mapping")
        object.__setattr__(
            self, "value", cast(Mapping[str, object], _freeze_json(self.value))
        )
        if type(self.expected) is not bool:
            raise TypeError("expected must be a boolean")


@dataclass(frozen=True)
class Exclusion:
    id: str
    corpus: str
    revision: str
    path: str
    line: int | None
    language: str
    scope: str
    reason: str

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.corpus, "corpus")
        _validate_revision(self.revision)
        _validate_path(self.path)
        if self.line is not None:
            _require_positive_integer(self.line, "line")
        _validate_language(self.language)
        _require_string(self.scope, "scope")
        _require_string(self.reason, "reason")


T = TypeVar("T")

_JSONL_RECORD_TYPES = (CorpusSpec, CensusRecord, GoldSample, GoldFact, Exclusion)
_ALLOWED_FIELDS = {
    record_type: frozenset(field.name for field in dataclasses.fields(record_type))
    for record_type in _JSONL_RECORD_TYPES
}


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _location(path: Path, line: int, message: str) -> ValueError:
    return ValueError(f"{path}:{line}: {message}")


def _record_key(record: object) -> tuple[str, ...] | None:
    if type(record) in (GoldFact, Exclusion):
        return (cast(GoldFact | Exclusion, record).id,)
    if type(record) in (CensusRecord, GoldSample):
        row = cast(CensusRecord | GoldSample, record)
        return (row.corpus, row.path)
    return None


def _validate_record_order(path: Path, records: tuple[object, ...]) -> None:
    if not records:
        return

    record_type = type(records[0])
    for line, record in enumerate(records, start=1):
        if type(record) is not record_type:
            raise _location(path, line, "records must have the same record type")

    if record_type not in (GoldFact, Exclusion, CensusRecord, GoldSample):
        return

    seen: set[tuple[str, ...]] = set()
    previous: tuple[str, ...] | None = None
    for line, record in enumerate(records, start=1):
        key = _record_key(record)
        assert key is not None
        if key in seen:
            if record_type in (GoldFact, Exclusion):
                raise _location(path, line, f"duplicate id {key[0]!r}")
            raise _location(
                path,
                line,
                f"duplicate (corpus, path) key {key!r}",
            )
        if previous is not None and key < previous:
            if record_type in (GoldFact, Exclusion):
                raise _location(path, line, "records must be sorted by id")
            raise _location(path, line, "records must be sorted by (corpus, path)")
        seen.add(key)
        previous = key


def load_jsonl(path: Path, record_type: type[T]) -> tuple[T, ...]:
    """Load a strict JSON Lines file into immutable validation records."""

    if record_type not in _ALLOWED_FIELDS:
        raise TypeError(f"unsupported JSONL record type: {record_type!r}")

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _location(path, 1, "UTF-8 BOM is not allowed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        raise _location(path, line, "invalid UTF-8") from exc

    if not text:
        return ()

    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()

    allowed = next(
        fields
        for candidate, fields in _ALLOWED_FIELDS.items()
        if record_type is candidate
    )
    loaded: list[T] = []
    for line_number, line_text in enumerate(lines, start=1):
        if not line_text.strip():
            raise _location(path, line_number, "blank line is not allowed")
        try:
            payload = json.loads(
                line_text,
                object_pairs_hook=_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise _location(path, line_number, f"malformed JSON: {exc}") from exc
        if type(payload) is not dict:
            raise _location(path, line_number, "record must be a JSON object")

        fields = frozenset(payload)
        unknown = fields - allowed
        if unknown:
            names = ", ".join(repr(name) for name in sorted(unknown))
            raise _location(path, line_number, f"unknown field(s): {names}")
        missing = allowed - fields
        if missing:
            names = ", ".join(repr(name) for name in sorted(missing))
            raise _location(path, line_number, f"missing field(s): {names}")

        try:
            record = record_type(**payload)
        except (TypeError, ValueError) as exc:
            raise _location(path, line_number, str(exc)) from exc
        loaded.append(record)

    result = tuple(loaded)
    _validate_record_order(path, cast(tuple[object, ...], result))
    return result


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def write_jsonl(path: Path, records: Iterable[object]) -> None:
    """Write validation records in canonical, byte-stable JSON Lines form."""

    owned = tuple(records)
    if not owned:
        path.write_bytes(b"")
        return

    record_type = type(owned[0])
    if record_type not in _ALLOWED_FIELDS:
        raise TypeError(f"unsupported JSONL record type: {record_type!r}")
    _validate_record_order(path, owned)

    lines: list[str] = []
    for line_number, record in enumerate(owned, start=1):
        if type(record) is not record_type:
            raise _location(path, line_number, "records must have the same record type")
        payload = {
            field.name: _thaw_json(getattr(record, field.name))
            for field in dataclasses.fields(record)
        }
        try:
            lines.append(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise _location(path, line_number, str(exc)) from exc

    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
