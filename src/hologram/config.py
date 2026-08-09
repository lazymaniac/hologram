from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

from .model import Language


CONFIG_NAME = ".hologram.toml"
CONFIG_SCHEMA_VERSION = 2
ALLOWED_AGENTS = frozenset({"claude", "codex", "gemini"})

_DEFAULT_AGENTS = ("claude", "codex", "gemini")
_DEFAULT_INCLUDE = ("**/*",)
_DEFAULT_EXCLUDE = (
    "**/.git/**",
    "**/.venv/**",
    "**/__pycache__/**",
    "**/bin/**",
    "**/build/**",
    "**/dist/**",
    "**/generated/**",
    "**/node_modules/**",
    "**/obj/**",
    "**/out/**",
    "**/target/**",
    "**/vendor/**",
)
_KNOWN_KEYS = frozenset(
    {
        "schema_version",
        "agents",
        "languages",
        "include",
        "exclude",
        "hot_threshold",
        "output",
    }
)
_RESERVED_OUTPUTS = frozenset(
    {CONFIG_NAME, "CLAUDE.md", "AGENTS.md", "GEMINI.md"}
)
_RESERVED_OUTPUT_CASEFOLDS = frozenset(
    output.casefold() for output in _RESERVED_OUTPUTS
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_T = TypeVar("_T")


class ConfigError(ValueError):
    """A selected Hologram manifest is missing or invalid."""


def _own_tuple(
    value: tuple[_T, ...] | list[_T],
    field: str,
) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    schema_version: int
    agents: tuple[str, ...]
    languages: tuple[Language, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    hot_threshold: int
    output: str | None

    def __post_init__(self) -> None:
        for field in ("agents", "languages", "include", "exclude"):
            object.__setattr__(self, field, _own_tuple(getattr(self, field), field))


def default_config() -> ProjectConfig:
    return ProjectConfig(
        CONFIG_SCHEMA_VERSION,
        _DEFAULT_AGENTS,
        (),
        _DEFAULT_INCLUDE,
        _DEFAULT_EXCLUDE,
        10,
        "PROJECT_DIGEST.md",
    )


def _invalid(path: Path, field: str, detail: str) -> ConfigError:
    return ConfigError(f"{path}: {field}: {detail}")


def _string_list(
    data: dict[str, object],
    field: str,
    default: tuple[str, ...],
    path: Path,
) -> tuple[str, ...]:
    value = data.get(field, list(default))
    if not isinstance(value, list):
        raise _invalid(path, field, "must be an array of strings")
    if any(not isinstance(item, str) for item in value):
        raise _invalid(path, field, "must contain only strings")
    if len(set(value)) != len(value):
        raise _invalid(path, field, "must not contain duplicates")
    return tuple(value)


def _validate_pattern(pattern: str, field: str, path: Path) -> None:
    pure = PurePosixPath(pattern)
    if (
        not pattern
        or not pure.parts
        or pure.is_absolute()
        or _WINDOWS_DRIVE_RE.match(pattern)
        or "\\" in pattern
        or ".." in pure.parts
        or pattern != pure.as_posix()
    ):
        raise _invalid(
            path,
            field,
            f"{pattern!r} must be a normalized root-relative POSIX pattern",
        )


def _patterns(
    data: dict[str, object],
    field: str,
    default: tuple[str, ...],
    path: Path,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    patterns = _string_list(data, field, default, path)
    if not patterns and not allow_empty:
        raise _invalid(path, field, "must not be empty")
    for pattern in patterns:
        _validate_pattern(pattern, field, path)
    return patterns


def _output(data: dict[str, object], path: Path) -> str | None:
    if "output" not in data:
        return None
    output = data["output"]
    if not isinstance(output, str):
        raise _invalid(path, "output", "must be a string")
    pure = PurePosixPath(output)
    if (
        not output
        or not pure.parts
        or pure.is_absolute()
        or _WINDOWS_DRIVE_RE.match(output)
        or "\\" in output
        or ".." in pure.parts
        or output != pure.as_posix()
        or pure.suffix != ".md"
        or output.casefold() in _RESERVED_OUTPUT_CASEFOLDS
        or any(character in output for character in "*?[]")
    ):
        raise _invalid(
            path,
            "output",
            "must be one normalized root-relative .md file and not a reserved file",
        )
    return output


def load_config(root: Path, path: Path | None = None) -> ProjectConfig:
    selected = Path(path) if path is not None else Path(root) / CONFIG_NAME
    try:
        raw = selected.read_bytes()
    except FileNotFoundError as error:
        raise ConfigError(f"missing configuration manifest {selected}") from error
    except OSError as error:
        raise _invalid(selected, "manifest", str(error)) from error

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except ValueError as error:
        raise _invalid(selected, "TOML", str(error)) from error

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise _invalid(selected, unknown[0], "unknown top-level key")

    if "schema_version" not in data:
        raise _invalid(selected, "schema_version", "is required")
    schema_version = data["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CONFIG_SCHEMA_VERSION
    ):
        raise _invalid(
            selected,
            "schema_version",
            f"must be exactly {CONFIG_SCHEMA_VERSION}",
        )

    agents = _string_list(data, "agents", _DEFAULT_AGENTS, selected)
    unknown_agents = sorted(set(agents) - ALLOWED_AGENTS)
    if unknown_agents:
        raise _invalid(
            selected,
            "agents",
            f"unknown agent {unknown_agents[0]!r}",
        )
    agents = tuple(sorted(agents))

    language_names = _string_list(data, "languages", (), selected)
    unknown_languages = sorted(
        value for value in language_names if value not in Language._value2member_map_
    )
    if unknown_languages:
        raise _invalid(
            selected,
            "languages",
            f"unknown language {unknown_languages[0]!r}",
        )
    languages = tuple(sorted((Language(value) for value in language_names), key=str))

    include = _patterns(
        data,
        "include",
        _DEFAULT_INCLUDE,
        selected,
        allow_empty=False,
    )
    exclude = _patterns(
        data,
        "exclude",
        _DEFAULT_EXCLUDE,
        selected,
        allow_empty=True,
    )

    hot_threshold = data.get("hot_threshold", 10)
    if (
        isinstance(hot_threshold, bool)
        or not isinstance(hot_threshold, int)
        or hot_threshold < 1
    ):
        raise _invalid(selected, "hot_threshold", "must be a positive integer")

    output = _output(data, selected)
    if not agents and output is None:
        raise _invalid(
            selected,
            "agents",
            "must select at least one agent when output is omitted",
        )

    return ProjectConfig(
        schema_version,
        agents,
        languages,
        include,
        exclude,
        hot_threshold,
        output,
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def render_config(config: ProjectConfig) -> str:
    agents = tuple(sorted(config.agents))
    languages = tuple(sorted((language.value for language in config.languages)))
    lines = [
        f"schema_version = {config.schema_version}",
        f"agents = {_toml_array(agents)}",
        f"languages = {_toml_array(languages)}",
        f"include = {_toml_array(config.include)}",
        f"exclude = {_toml_array(config.exclude)}",
        f"hot_threshold = {config.hot_threshold}",
    ]
    if config.output is not None:
        lines.append(f"output = {_toml_string(config.output)}")
    return "\n".join(lines) + "\n"


def canonical_config_bytes(config: ProjectConfig) -> bytes:
    return render_config(config).encode("utf-8")


def create_default_manifest(root: Path) -> bool:
    """Exclusively create the canonical default manifest without replacing entries."""
    path = Path(root) / CONFIG_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return False

    created = None
    try:
        created = os.fstat(fd)
        remaining = memoryview(canonical_config_bytes(default_config()))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError(f"failed to write configuration manifest {path}")
            remaining = remaining[written:]
    except BaseException:
        try:
            current = path.lstat()
        except OSError:
            current = None
        if (
            created is not None
            and current is not None
            and current.st_dev == created.st_dev
            and current.st_ino == created.st_ino
        ):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        os.close(fd)
    return True
