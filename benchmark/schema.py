from __future__ import annotations

import json
import os
import re
import shlex
import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from urllib.parse import urlsplit

Visibility = Literal["public", "private"]
Tier = Literal["simple", "complex"]
Capability = Literal["orientation", "planning", "implementation", "audit"]
Kind = Literal["navigate", "reuse"]
Condition = Literal["B", "C"]

_MODEL = "claude-sonnet-5"
_CLAUDE_CODE_VERSION = "2.1.224"
_SAFE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITY_KIND: Mapping[str, str] = {
    "orientation": "navigate",
    "planning": "navigate",
    "implementation": "reuse",
    "audit": "navigate",
}
_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", "(", ")"})
_ASSET_SUFFIXES = frozenset(
    {".json", ".lua", ".patch", ".py", ".sh", ".toml", ".yaml", ".yml"}
)


@dataclass(frozen=True)
class BenchmarkCorpus:
    name: str
    visibility: Visibility
    url: str | None
    revision: str
    path_env: str
    bootstrap_cmd: str | None = None
    workspace_assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Challenge:
    patch: Path
    sha256: str


@dataclass(frozen=True)
class Task:
    id: str
    tier: Tier
    capability: Capability
    kind: Kind
    visibility: Visibility
    prompt: str
    accept_cmd: str
    expect_reuse: tuple[str, ...] = ()
    challenge: Challenge | None = None


@dataclass(frozen=True)
class Config:
    corpus: BenchmarkCorpus
    tasks: tuple[Task, ...]
    model: str
    claude_code_version: str
    max_turns: int
    conditions: tuple[Condition, ...]
    reps: int
    seed: int


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate field {key!r}")
        result[key] = value
    return result


def _object(
    value: object,
    label: str,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    result = cast(dict[str, object], value)
    unknown = set(result) - required - optional
    missing = required - set(result)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)!r}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)!r}")
    return result


def _text(value: object, label: str) -> str:
    if type(value) is not str or not cast(str, value).strip():
        raise ValueError(f"{label} must be a nonblank string")
    result = cast(str, value)
    if "\x00" in result:
        raise ValueError(f"{label} contains NUL")
    return result


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array")
    result = tuple(_text(item, f"{label} item") for item in cast(list[object], value))
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _safe_relative(value: str, label: str, *, allow_parent: bool = False) -> str:
    if "\\" in value:
        raise ValueError(f"{label} must be a POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or value != path.as_posix():
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    if not allow_parent and any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{label} must not traverse parent directories")
    return value


def _public_url(value: object, visibility: Visibility) -> str | None:
    if visibility == "private":
        if value is not None:
            raise ValueError("private corpus url must be null")
        return None
    url = _text(value, "public corpus url")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.strip("/")
    ):
        raise ValueError("public corpus url must be a canonical HTTPS URL")
    return url


def _command_fields(command: str) -> frozenset[str]:
    fields: set[str] = set()
    try:
        parsed = string.Formatter().parse(command)
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in {"ws", "answer"} or format_spec or conversion:
                raise ValueError("accept_cmd has an invalid placeholder")
            fields.add(field_name)
    except ValueError as error:
        raise ValueError("accept_cmd has invalid braces") from error
    return frozenset(fields)


def _accept_command(value: object, kind: Kind) -> str:
    command = _text(value, "task accept_cmd")
    try:
        words = shlex.split(command)
    except ValueError as error:
        raise ValueError("task accept_cmd is not valid shell syntax") from error
    normalized = tuple(word.casefold() for word in words)
    if normalized in {
        ("true",),
        ("/bin/true",),
        ("/usr/bin/true",),
        (":",),
        ("command", "true"),
    }:
        raise ValueError("task accept_cmd must not be a no-op verifier")
    fields = _command_fields(command)
    required = {"ws", "answer"} if kind == "navigate" else {"ws"}
    if not required.issubset(fields):
        raise ValueError(f"{kind} accept_cmd is missing required placeholders")
    return command


def _challenge(value: object, manifest: Path, label: str) -> Challenge | None:
    if value is None:
        return None
    data = _object(
        value,
        label,
        required=frozenset({"patch", "sha256"}),
    )
    raw_patch = _safe_relative(
        _text(data["patch"], f"{label} patch"),
        f"{label} patch",
        allow_parent=True,
    )
    patch = (manifest.parent / raw_patch).resolve()
    if not patch.is_file():
        raise ValueError(f"{label} patch does not exist: {patch}")
    sha256 = _text(data["sha256"], f"{label} sha256")
    if _HEX64.fullmatch(sha256) is None:
        raise ValueError(f"{label} sha256 must be lowercase 64-hex")
    return Challenge(patch, sha256)


def _corpus(value: object) -> BenchmarkCorpus:
    data = _object(
        value,
        "corpus",
        required=frozenset({"name", "visibility", "url", "revision", "path_env"}),
        optional=frozenset({"bootstrap_cmd", "workspace_assets"}),
    )
    name = _text(data["name"], "corpus name")
    if _SAFE_ID.fullmatch(name) is None:
        raise ValueError("corpus name is unsafe")
    visibility_value = data["visibility"]
    if visibility_value not in {"public", "private"}:
        raise ValueError("corpus visibility must be public or private")
    visibility = cast(Visibility, visibility_value)
    url = _public_url(data["url"], visibility)
    revision = _text(data["revision"], "corpus revision")
    if _HEX40.fullmatch(revision) is None:
        raise ValueError("corpus revision must be lowercase full 40-hex")
    path_env = _text(data["path_env"], "corpus path_env")
    if _ENVIRONMENT_NAME.fullmatch(path_env) is None:
        raise ValueError("corpus path_env must be a canonical environment name")
    bootstrap_value = data.get("bootstrap_cmd")
    bootstrap_cmd = (
        None
        if bootstrap_value is None
        else _text(bootstrap_value, "corpus bootstrap_cmd")
    )
    workspace_assets = _string_tuple(
        data.get("workspace_assets", []), "corpus workspace_assets"
    )
    workspace_assets = tuple(
        _safe_relative(item, "workspace asset") for item in workspace_assets
    )
    return BenchmarkCorpus(
        name,
        visibility,
        url,
        revision,
        path_env,
        bootstrap_cmd,
        workspace_assets,
    )


def _task(value: object, manifest: Path, index: int) -> Task:
    label = f"task {index + 1}"
    data = _object(
        value,
        label,
        required=frozenset(
            {"id", "tier", "capability", "kind", "visibility", "prompt", "accept_cmd"}
        ),
        optional=frozenset({"expect_reuse", "challenge"}),
    )
    task_id = _text(data["id"], f"{label} id")
    if _SAFE_ID.fullmatch(task_id) is None:
        raise ValueError(f"{label} id is unsafe")
    tier_value = data["tier"]
    if tier_value not in {"simple", "complex"}:
        raise ValueError(f"{label} tier must be simple or complex")
    capability_value = data["capability"]
    if capability_value not in _CAPABILITY_KIND:
        raise ValueError(f"{label} capability is invalid")
    kind_value = data["kind"]
    if kind_value not in {"navigate", "reuse"}:
        raise ValueError(f"{label} kind is invalid")
    if _CAPABILITY_KIND[cast(str, capability_value)] != kind_value:
        raise ValueError(f"{label} capability/kind pair is invalid")
    visibility_value = data["visibility"]
    if visibility_value not in {"public", "private"}:
        raise ValueError(f"{label} visibility is invalid")
    kind = cast(Kind, kind_value)
    prompt = _text(data["prompt"], f"{label} prompt")
    accept_cmd = _accept_command(data["accept_cmd"], kind)
    expect_reuse = _string_tuple(data.get("expect_reuse", []), f"{label} expect_reuse")
    if kind == "reuse" and not expect_reuse:
        raise ValueError(f"{label} reuse task must declare expected reuse")
    if kind == "navigate" and expect_reuse:
        raise ValueError(f"{label} navigation task must not declare expected reuse")
    return Task(
        task_id,
        cast(Tier, tier_value),
        cast(Capability, capability_value),
        kind,
        cast(Visibility, visibility_value),
        prompt,
        accept_cmd,
        expect_reuse,
        _challenge(data.get("challenge"), manifest, f"{label} challenge"),
    )


def task_asset_paths(task: Task, *, manifest: Path) -> tuple[Path, ...]:
    """Return verifier and hidden-test paths named by a private command.

    Private commands must expose filesystem-backed verifier inputs so the
    harness can prove they live outside its own worktree. Workspace and answer
    placeholders are session-owned and therefore intentionally excluded.
    """

    try:
        words = shlex.split(task.accept_cmd)
    except ValueError as error:  # pragma: no cover - load_tasks already validates
        raise ValueError("task accept_cmd is not valid shell syntax") from error
    if task.visibility == "private" and any(
        word in {"-c", "--command"} for word in words
    ):
        raise ValueError("private verifier commands must not hide assets in shell code")
    assets: list[Path] = []
    module_argument = False
    for word in words:
        if module_argument:
            if task.visibility == "private":
                raise ValueError(
                    "private verifier modules are not externally path-auditable"
                )
            module_argument = False
            continue
        if word == "-m":
            module_argument = True
            continue
        if word in _SHELL_OPERATORS or "{ws}" in word or "{answer}" in word:
            continue
        candidate = word
        if candidate.startswith((">>", "<<")):
            candidate = candidate[2:]
        elif candidate.startswith((">", "<")):
            candidate = candidate[1:]
        if "=" in candidate and not candidate.startswith(("/", "./", "../", "~")):
            _name, candidate = candidate.split("=", 1)
        if not candidate or candidate.startswith("-"):
            continue
        path = Path(candidate).expanduser()
        pathlike = (
            path.is_absolute()
            or candidate.startswith(("./", "../", "~"))
            or "/" in candidate
            or path.suffix.casefold() in _ASSET_SUFFIXES
        )
        if not pathlike:
            continue
        if not path.is_absolute():
            path = manifest.parent / path
        resolved = path.resolve(strict=False)
        if resolved not in assets:
            assets.append(resolved)
    return tuple(assets)


def resolve_corpus_path(
    spec: BenchmarkCorpus,
    *,
    corpus_override: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    selected: Path
    if corpus_override is not None:
        selected = Path(corpus_override)
    else:
        environment = os.environ if environ is None else environ
        raw = environment.get(spec.path_env)
        if raw is None or not raw.strip():
            raise ValueError(
                f"missing corpus path environment variable {spec.path_env}"
            )
        selected = Path(raw)
    try:
        resolved = selected.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"invalid corpus path: {selected}") from error
    if not resolved.is_dir():
        raise ValueError(f"corpus path is not a directory: {resolved}")
    return resolved


def load_tasks(
    path: Path,
    *,
    corpus_override: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    manifest = Path(path).resolve()
    try:
        data = json.loads(
            manifest.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as error:
        raise ValueError(f"invalid benchmark manifest {manifest}: {error}") from error
    root = _object(
        data,
        "manifest",
        required=frozenset(
            {
                "corpus",
                "tasks",
                "model",
                "claude_code_version",
                "max_turns",
                "conditions",
                "reps",
                "seed",
            }
        ),
    )
    corpus = _corpus(root["corpus"])
    raw_tasks = root["tasks"]
    if type(raw_tasks) is not list or not raw_tasks:
        raise ValueError("tasks must be a nonempty array")
    tasks = tuple(
        _task(item, manifest, index)
        for index, item in enumerate(cast(list[object], raw_tasks))
    )
    task_ids = tuple(task.id for task in tasks)
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task IDs must be unique")
    if {task.tier for task in tasks} != {"simple", "complex"}:
        raise ValueError("manifest must contain both simple and complex tiers")
    if any(task.visibility != corpus.visibility for task in tasks):
        raise ValueError("task and corpus visibility must match")
    if root["model"] != _MODEL:
        raise ValueError(f"model must equal {_MODEL!r}")
    if root["claude_code_version"] != _CLAUDE_CODE_VERSION:
        raise ValueError(f"claude_code_version must equal {_CLAUDE_CODE_VERSION!r}")
    if type(root["max_turns"]) is not int or root["max_turns"] != 40:
        raise ValueError("max_turns must equal 40")
    if root["conditions"] != ["B", "C"]:
        raise ValueError("conditions must equal ['B', 'C']")
    if type(root["reps"]) is not int or root["reps"] != 1:
        raise ValueError("reps must equal 1")
    if type(root["seed"]) is not int or cast(int, root["seed"]) < 0:
        raise ValueError("seed must be a nonnegative integer")
    resolve_corpus_path(
        corpus,
        corpus_override=corpus_override,
        environ=environ,
    )
    return Config(
        corpus,
        tasks,
        _MODEL,
        _CLAUDE_CODE_VERSION,
        40,
        ("B", "C"),
        1,
        cast(int, root["seed"]),
    )


__all__ = (
    "BenchmarkCorpus",
    "Challenge",
    "Config",
    "Task",
    "load_tasks",
    "task_asset_paths",
)
