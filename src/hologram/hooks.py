from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

from .context import PlannedWrite, preflight_atomic_write, read_target_bytes

HOOK_START = b"# hologram:v2:start"
HOOK_END = b"# hologram:v2:end"

_HOOK_NAMES = ("post-commit", "post-merge", "post-checkout")
_SHELLS = frozenset({"sh", "bash", "zsh"})


class UnsupportedHookError(ValueError):
    pass


def _require_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a Path")
    if not value.is_absolute():
        raise ValueError(f"{name} must be absolute")
    _validate_shell_text(os.fspath(value), name)
    return value


def _validate_shell_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{name} must be one nonempty shell-safe line")
    return value


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _double_quote_fragment(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def render_precommit_command(
    *,
    root: Path,
    config_path: Path,
    python: Path,
    module: str = "hologram",
) -> bytes:
    """Render the read-only managed shell block installed in pre-commit."""

    selected_root = _require_path(root, "root")
    selected_config = _require_path(config_path, "config_path")
    selected_python = _require_path(python, "python")
    selected_module = _validate_shell_text(module, "module")
    try:
        relative_config = selected_config.resolve(strict=False).relative_to(
            selected_root.resolve(strict=False)
        )
    except ValueError as error:
        raise UnsupportedHookError(
            "pre-commit requires a root-relative config; use --no-hook"
        ) from error
    relative_text = _double_quote_fragment(relative_config.as_posix())
    command = (
        f"{shlex.quote(os.fspath(selected_python))} -B -m "
        f"{shlex.quote(selected_module)} check "
        '--root "$hologram_v2_root" '
        f'--config "$hologram_v2_root/{relative_text}" '
        "--quiet || exit $?\n"
    ).encode()
    return (
        HOOK_START
        + b"\n"
        + b"hologram_v2_root=$(git rev-parse --show-toplevel) || exit $?\n"
        + command
        + HOOK_END
        + b"\n"
    )


def _physical_lines(value: bytes) -> tuple[tuple[int, int, bytes], ...]:
    lines: list[tuple[int, int, bytes]] = []
    start = 0
    while start < len(value):
        newline = value.find(b"\n", start)
        if newline < 0:
            physical_end = len(value)
            content_end = physical_end
        else:
            physical_end = newline + 1
            content_end = newline
            if content_end > start and value[content_end - 1] == 0x0D:
                content_end -= 1
        lines.append((start, physical_end, value[start:content_end]))
        start = physical_end
    return tuple(lines)


def _managed_range(value: bytes) -> tuple[int, int] | None:
    starts = [line for line in _physical_lines(value) if line[2] == HOOK_START]
    ends = [line for line in _physical_lines(value) if line[2] == HOOK_END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0][0] >= ends[0][0]:
        raise UnsupportedHookError("pre-commit has malformed Hologram hook markers")
    return starts[0][0], ends[0][1]


def _validated_command(command: object) -> bytes:
    if not isinstance(command, bytes):
        raise TypeError("command must be bytes")
    managed = _managed_range(command)
    if managed != (0, len(command)) or not command.endswith(HOOK_END + b"\n"):
        raise UnsupportedHookError("command must be exactly one canonical hook block")
    return command


def _supported_shebang(value: bytes) -> int:
    lines = _physical_lines(value)
    if not lines or not value.startswith(b"#!"):
        raise UnsupportedHookError(
            "existing pre-commit is not a supported shell hook; use --no-hook"
        )
    _, physical_end, raw = lines[0]
    try:
        words = shlex.split(raw[2:].decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise UnsupportedHookError(
            "existing pre-commit has an unsupported shebang; use --no-hook"
        ) from error
    supported = bool(words and Path(words[0]).name in _SHELLS)
    if words and Path(words[0]).name == "env":
        supported = len(words) == 2 and words[1] in _SHELLS
    if not supported:
        raise UnsupportedHookError(
            "existing pre-commit is not sh/bash/zsh; use --no-hook"
        )
    return physical_end


def _git_capture(
    repo: Path,
    *args: str,
    optional: bool = False,
) -> str | None:
    try:
        result = subprocess.run(
            ("git", "-C", os.fspath(repo), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UnsupportedHookError(f"cannot inspect Git hooks: {error}") from error
    if optional and result.returncode == 1 and not result.stdout:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or "Git command failed"
        raise UnsupportedHookError(f"cannot inspect Git hooks: {detail}")
    return result.stdout.rstrip("\n")


def _git_path(repo: Path, *args: str) -> Path:
    value = _git_capture(repo, *args)
    assert value is not None
    if not value or any(character in value for character in ("\x00", "\n", "\r")):
        raise UnsupportedHookError("Git returned an unsafe hook path")
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return Path(os.path.abspath(os.fspath(path)))


def _effective_hooks_config(repo: Path) -> tuple[str, Path, str] | None:
    value = _git_capture(
        repo,
        "config",
        "--null",
        "--show-origin",
        "--show-scope",
        "--get",
        "core.hooksPath",
        optional=True,
    )
    if value is None:
        return None
    fields = value.split("\0")
    if len(fields) != 4 or fields[-1] != "":
        raise UnsupportedHookError("Git returned malformed core.hooksPath metadata")
    scope, origin, configured_path = fields[:3]
    if not scope or not origin or not configured_path:
        raise UnsupportedHookError("Git returned malformed core.hooksPath metadata")
    if not origin.startswith("file:"):
        raise UnsupportedHookError(f"unsupported {scope} core.hooksPath; use --no-hook")
    origin_path = Path(origin.removeprefix("file:"))
    if not origin_path.is_absolute():
        origin_path = repo / origin_path
    return scope, Path(os.path.abspath(os.fspath(origin_path))), configured_path


def _hook_directory(repo: Path) -> Path:
    selected = _require_path(repo, "repo").resolve(strict=False)
    top = _git_path(selected, "rev-parse", "--show-toplevel").resolve(strict=False)
    if top != selected:
        raise UnsupportedHookError("hook installation root must be the Git top-level")
    hooks = _git_path(
        selected,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "hooks",
    )
    git_dir = _git_path(selected, "rev-parse", "--absolute-git-dir")
    common_dir = _git_path(
        selected,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    configured = _effective_hooks_config(selected)
    if configured is None:
        if not (_inside(hooks, git_dir) or _inside(hooks, common_dir)):
            raise UnsupportedHookError(
                "unexplained shared Git hooks path; use --no-hook"
            )
        return hooks

    scope, origin, configured_path = configured
    if scope in {"global", "system"}:
        if Path(configured_path).is_absolute() or not _inside(hooks, selected):
            raise UnsupportedHookError(
                f"unsupported {scope} shared hooks path; use --no-hook"
            )
        return hooks
    if scope not in {"local", "worktree"}:
        raise UnsupportedHookError(f"unsupported {scope} hooks path; use --no-hook")
    if not (_inside(origin, git_dir) or _inside(origin, common_dir)):
        raise UnsupportedHookError("hooksPath is not repository-local; use --no-hook")
    return hooks


def preflight_precommit(repo: Path, command: bytes) -> PlannedWrite:
    """Plan a canonical pre-commit hook without mutating Git state."""

    block = _validated_command(command)
    hooks = _hook_directory(repo)
    hook = hooks / "pre-commit"
    existing = read_target_bytes(hook, root=hooks)
    if existing is None:
        return preflight_atomic_write(
            hook,
            b"#!/bin/sh\n" + block,
            root=hooks,
            mode=0o755,
        )

    metadata = os.lstat(hook)
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o111 == 0:
        raise UnsupportedHookError(
            "existing pre-commit is not executable; use --no-hook"
        )
    shebang_end = _supported_shebang(existing)
    managed = _managed_range(existing)
    if managed is None:
        authored = existing[shebang_end:]
    else:
        start, end = managed
        authored = existing[shebang_end:start] + existing[end:]
    shebang = existing[:shebang_end]
    if not shebang.endswith(b"\n"):
        shebang += b"\n"
    updated = shebang + block + authored
    return preflight_atomic_write(hook, updated, root=hooks, mode=mode)


def _legacy_options_match(options: list[str], repo: Path) -> bool:
    if len(options) < 3 or options[0] != "--root":
        return False
    root = Path(options[1])
    if not root.is_absolute() or root.resolve(strict=False) != repo.resolve(
        strict=False
    ):
        return False
    tail = options[2:]
    languages: list[str] = []
    while tail[:1] == ["--lang"]:
        if len(tail) < 2 or not tail[1] or tail[1].startswith("-"):
            return False
        languages.append(tail[1])
        tail = tail[2:]
    if languages != sorted(set(languages)):
        return False
    if tail[:1] == ["--embed"]:
        tail = tail[1:]
    return tail == ["--quiet"]


def _legacy_script() -> Path | None:
    source = Path(__file__).resolve()
    source_directory = source.parent.parent
    if source_directory.name != "src":
        return None
    return source_directory.parent / "hologram.py"


def _is_legacy_line(line: bytes, repo: Path) -> bool:
    try:
        text = line.decode("utf-8").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError:
        return False
    if text != text.strip() or not text.endswith(" || true"):
        return False
    try:
        arguments = shlex.split(text.removesuffix(" || true"))
    except ValueError:
        return False
    if arguments[1:4] == ["-m", "hologram", "build"]:
        return _legacy_options_match(arguments[4:], repo)
    legacy_script = _legacy_script()
    return bool(
        legacy_script is not None
        and len(arguments) >= 3
        and Path(arguments[1]).is_absolute()
        and Path(arguments[1]).resolve(strict=False)
        == legacy_script.resolve(strict=False)
        and arguments[2] == "build"
        and _legacy_options_match(arguments[3:], repo)
    )


def remove_legacy_post_hook_lines(repo: Path) -> tuple[PlannedWrite, ...]:
    """Plan exact legacy generated-line removal, preserving all authored bytes."""

    selected = _require_path(repo, "repo").resolve(strict=False)
    hooks = _hook_directory(selected)
    plans: list[PlannedWrite] = []
    for name in _HOOK_NAMES:
        hook = hooks / name
        existing = read_target_bytes(hook, root=hooks)
        if existing is None:
            continue
        kept = tuple(
            existing[start:end]
            for start, end, _ in _physical_lines(existing)
            if not _is_legacy_line(existing[start:end], selected)
        )
        updated = b"".join(kept)
        if updated == existing:
            continue
        plans.append(preflight_atomic_write(hook, updated, root=hooks))
    return tuple(plans)


__all__ = [
    "HOOK_END",
    "HOOK_START",
    "UnsupportedHookError",
    "preflight_precommit",
    "remove_legacy_post_hook_lines",
    "render_precommit_command",
]
