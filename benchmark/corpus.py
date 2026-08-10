from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from hologram.config import CONFIG_NAME, canonical_config_bytes, default_config
from hologram.context import (
    AGENT_PATHS,
    ContextStatus,
    inspect_managed_block,
    read_target_bytes,
    render_managed_block,
)

if __package__:
    from .schema import BenchmarkCorpus, Challenge, Task
else:
    from schema import (  # type: ignore[import-not-found,no-redef]
        BenchmarkCorpus,
        Challenge,
        Task,
    )

_BASE_CLAUDE_MD = """# Working notes

Complete the requested task directly. Keep changes minimal and idiomatic.
"""
_EMPTY_MANAGED_BLOCK = render_managed_block("")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RunSpec:
    task: Task
    condition: Literal["B", "C"]
    rep: int
    pair_index: int


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _run_checked(
    run: Callable[..., subprocess.CompletedProcess[object]],
    argv: list[str],
    *,
    cwd: Path | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess[object]:
    completed = run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=text,
        timeout=1800,
        check=False,
        env=_environment(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace").strip()
        else:
            detail = str(stderr or "").strip()
        raise ValueError(f"command failed ({' '.join(argv)}): {detail}")
    return completed


def _git_text(
    root: Path,
    *args: str,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> str:
    completed = _run_checked(
        run,
        ["git", "-C", str(root), *args],
        text=True,
    )
    return str(completed.stdout).strip()


def _git_bytes(
    root: Path,
    *args: str,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> bytes:
    completed = _run_checked(run, ["git", "-C", str(root), *args])
    stdout = completed.stdout
    return stdout if isinstance(stdout, bytes) else str(stdout).encode("utf-8")


def _asset_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not path.parts
        or relative != path.as_posix()
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe workspace asset path: {relative!r}")
    return root.joinpath(*path.parts)


def _require_assets(root: Path, assets: Sequence[str]) -> None:
    for relative in assets:
        source = _asset_path(root, relative)
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError as error:
            raise ValueError(f"workspace asset is missing: {relative}") from error
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"workspace asset root is unsafe: {relative}")


def verify_prepared_corpus(
    spec: BenchmarkCorpus,
    root: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> Path:
    selected = Path(root).resolve()
    if not selected.is_dir():
        raise ValueError(f"prepared corpus is not a directory: {selected}")
    if _git_text(selected, "rev-parse", "HEAD", run=run) != spec.revision:
        raise ValueError("prepared corpus HEAD does not match the pinned revision")
    origin = _git_text(selected, "config", "--get", "remote.origin.url", run=run)
    if spec.url is not None and origin != spec.url:
        raise ValueError("prepared corpus origin does not match the manifest")
    status = _git_bytes(
        selected,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        run=run,
    )
    if status:
        raise ValueError("prepared corpus must be clean")
    _require_assets(selected, spec.workspace_assets)
    return selected


def prepare_public_corpus(
    spec: BenchmarkCorpus,
    destination: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> Path:
    if spec.visibility != "public" or spec.url is None:
        raise ValueError("only public corpora can be prepared")
    selected = Path(destination).resolve()
    if selected.exists():
        if not selected.is_dir() or any(selected.iterdir()):
            raise ValueError("public corpus destination must be absent or empty")
    else:
        selected.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_checked(
            run,
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                spec.url,
                str(selected),
            ],
        )
        _run_checked(
            run,
            ["git", "-C", str(selected), "checkout", "--detach", spec.revision],
        )
        if spec.bootstrap_cmd is not None:
            _run_checked(
                run,
                ["/bin/sh", "-c", spec.bootstrap_cmd],
                cwd=selected,
            )
        return verify_prepared_corpus(spec, selected, run=run)
    except BaseException:
        shutil.rmtree(selected, ignore_errors=True)
        raise


def _materialize(
    source: Path,
    destination: Path,
    *,
    asset_root: Path,
    ancestors: frozenset[Path],
) -> None:
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError as error:
        raise ValueError(f"workspace asset entry disappeared: {source}") from error
    selected = source
    if stat.S_ISLNK(mode):
        try:
            selected = source.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"workspace asset symlink is invalid: {source}") from error
        if not selected.is_relative_to(asset_root):
            raise ValueError(f"workspace asset symlink escapes its root: {source}")
        mode = selected.stat().st_mode
    resolved = selected.resolve(strict=True)
    if resolved in ancestors:
        raise ValueError(f"workspace asset contains a symlink cycle: {source}")
    if stat.S_ISDIR(mode):
        destination.mkdir(mode=stat.S_IMODE(mode), parents=True, exist_ok=False)
        next_ancestors = ancestors | {resolved}
        for child in sorted(
            selected.iterdir(), key=lambda item: os.fsencode(item.name)
        ):
            _materialize(
                child,
                destination / child.name,
                asset_root=asset_root,
                ancestors=next_ancestors,
            )
        return
    if not stat.S_ISREG(mode):
        raise ValueError(f"workspace asset contains a special file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(selected.read_bytes())
    destination.chmod(stat.S_IMODE(mode))


def _copy_assets(corpus: Path, workspace: Path, assets: Sequence[str]) -> None:
    _require_assets(corpus, assets)
    for relative in assets:
        source = _asset_path(corpus, relative)
        destination = _asset_path(workspace, relative)
        root = source.resolve(strict=True)
        _materialize(source, destination, asset_root=root, ancestors=frozenset())


def workspace_asset_sha256(root: Path, assets: Sequence[str]) -> str:
    digest = hashlib.sha256(b"hologram-workspace-assets-v1\0")
    for relative in sorted(assets, key=os.fsencode):
        asset = _asset_path(root, relative)
        if not asset.exists() or asset.is_symlink():
            raise ValueError(f"workspace asset is missing or unsafe: {relative}")
        digest.update(relative.encode("utf-8") + b"\0")
        entries = (
            (asset,)
            if asset.is_file()
            else tuple(
                sorted(asset.rglob("*"), key=lambda item: os.fsencode(item.as_posix()))
            )
        )
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(
                    f"materialized workspace asset contains a symlink: {entry}"
                )
            relative_entry = entry.relative_to(root).as_posix().encode("utf-8")
            mode = entry.stat().st_mode
            if entry.is_dir():
                digest.update(b"D\0" + relative_entry + b"\0")
            elif entry.is_file():
                raw = entry.read_bytes()
                digest.update(
                    b"F\0"
                    + relative_entry
                    + b"\0"
                    + f"{stat.S_IMODE(mode):o}".encode("ascii")
                    + b"\0"
                    + len(raw).to_bytes(8, "big")
                    + raw
                )
            else:
                raise ValueError(f"materialized workspace asset is special: {entry}")
    return digest.hexdigest()


def _challenge_tree_sha256(workspace: Path) -> str:
    tree = _git_text(workspace, "write-tree")
    return hashlib.sha256(
        b"hologram-challenged-tree-v1\0" + tree.encode("ascii")
    ).hexdigest()


def _apply_challenge(workspace: Path, challenge: Challenge | None) -> None:
    if challenge is None:
        return
    raw = challenge.patch.read_bytes()
    if hashlib.sha256(raw).hexdigest() != challenge.sha256:
        raise ValueError("challenge patch SHA-256 does not match the manifest")
    _git_text(workspace, "apply", "--check", str(challenge.patch))
    _git_text(workspace, "apply", str(challenge.patch))


def _declared_corpus_output(workspace: Path) -> Path | None:
    manifest = workspace / CONFIG_NAME
    try:
        manifest_stat = manifest.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(manifest_stat.st_mode):
        return None
    raw = read_target_bytes(manifest)
    if raw is None:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    schema = data.get("schema_version")
    output = data.get("output")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != 2
        or not isinstance(output, str)
    ):
        return None
    relative = PurePosixPath(output)
    if (
        not output
        or not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in output
        or re.match(r"^[A-Za-z]:", output)
        or output != relative.as_posix()
        or relative.suffix != ".md"
        or any(character in output for character in "*?[]")
    ):
        return None
    return workspace.joinpath(*relative.parts)


def _benchmark_claude_bytes(workspace: Path) -> bytes:
    claude = b""
    for agent, relative in AGENT_PATHS.items():
        existing = read_target_bytes(workspace / relative)
        if existing is None:
            continue
        if (
            inspect_managed_block(existing, _EMPTY_MANAGED_BLOCK)
            is not ContextStatus.MISSING
        ):
            raise ValueError(f"preexisting Hologram context in {relative}")
        if agent == "claude":
            claude = existing
    standalone = workspace / "PROJECT_DIGEST.md"
    try:
        standalone.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("preexisting Hologram standalone map in PROJECT_DIGEST.md")
    declared_output = _declared_corpus_output(workspace)
    if (
        declared_output is not None
        and read_target_bytes(declared_output, root=workspace) is not None
    ):
        raise ValueError(f"preexisting Hologram standalone map in {declared_output}")
    return claude


def _append_benchmark_instructions(authored: bytes) -> bytes:
    instructions = _BASE_CLAUDE_MD.encode("utf-8")
    if not authored:
        return instructions
    separator = b"\n" if authored.endswith((b"\n", b"\r")) else b"\n\n"
    return authored + separator + instructions


def _hologram_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "hologram", *args]


def _record_provenance(
    workspace: Path,
    challenged_tree: str,
    workspace_assets: str,
) -> None:
    _git_text(
        workspace,
        "config",
        "--local",
        "hologram.challenged-tree-sha256",
        challenged_tree,
    )
    _git_text(
        workspace,
        "config",
        "--local",
        "hologram.workspace-asset-sha256",
        workspace_assets,
    )


def workspace_provenance(workspace: Path) -> tuple[str, str]:
    challenged = _git_text(
        workspace, "config", "--get", "hologram.challenged-tree-sha256"
    )
    assets = _git_text(workspace, "config", "--get", "hologram.workspace-asset-sha256")
    if _HEX64.fullmatch(challenged) is None or _HEX64.fullmatch(assets) is None:
        raise ValueError("workspace provenance is missing or malformed")
    return challenged, assets


def make_workspace(
    corpus: Path,
    workspace: Path,
    condition: str,
    *,
    challenge: Challenge | None = None,
    workspace_assets: Sequence[str] = (),
) -> Path:
    if condition not in {"B", "C"}:
        raise ValueError("benchmark condition must be one of conditions B and C")
    source = Path(corpus).resolve()
    selected = Path(workspace).resolve()
    if selected.exists():
        raise ValueError(f"benchmark workspace already exists: {selected}")
    try:
        revision = _git_text(source, "rev-parse", "HEAD")
        if _git_bytes(source, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError("benchmark source corpus must be clean")
        _run_checked(
            subprocess.run,
            [
                "git",
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                str(source),
                str(selected),
            ],
        )
        _git_text(selected, "checkout", "--detach", revision)
        _copy_assets(source, selected, workspace_assets)
        _apply_challenge(selected, challenge)
        _git_text(selected, "add", "-A")
        challenged_tree = _challenge_tree_sha256(selected)
        asset_hash = workspace_asset_sha256(selected, workspace_assets)
        _record_provenance(selected, challenged_tree, asset_hash)

        claude_path = selected / "CLAUDE.md"
        authored = _benchmark_claude_bytes(selected)
        claude_path.write_bytes(_append_benchmark_instructions(authored))
        if condition == "C":
            base = default_config()
            config = replace(
                base,
                agents=("claude",),
                output=None,
                exclude=(*base.exclude, "**/deps/**"),
            )
            with tempfile.TemporaryDirectory(
                prefix="hologram-bench-condition-"
            ) as temporary:
                config_path = Path(temporary) / CONFIG_NAME
                config_path.write_bytes(canonical_config_bytes(config))
                _run_checked(
                    subprocess.run,
                    _hologram_command(
                        "build",
                        "--root",
                        str(selected),
                        "--config",
                        str(config_path),
                        "--quiet",
                    ),
                )
        _git_text(selected, "add", "-A")
        _git_text(
            selected,
            "-c",
            "user.email=bench@bench",
            "-c",
            "user.name=bench",
            "commit",
            "-qm",
            "bench setup",
        )
        return selected
    except BaseException:
        shutil.rmtree(selected, ignore_errors=True)
        raise


def drop_workspace(corpus: Path, workspace: Path) -> None:
    del corpus
    shutil.rmtree(Path(workspace), ignore_errors=True)


def schedule_runs(
    tasks: Sequence[Task],
    *,
    conditions: Sequence[str],
    reps: int,
    seed: int,
) -> tuple[RunSpec, ...]:
    selected_tasks = tuple(sorted(tasks, key=lambda task: task.id))
    if not selected_tasks:
        raise ValueError("benchmark schedule requires at least one task")
    if len({task.id for task in selected_tasks}) != len(selected_tasks):
        raise ValueError("benchmark schedule task IDs must be unique")
    if len(conditions) != 2 or set(conditions) != {"B", "C"}:
        raise ValueError("benchmark schedule requires one B and one C condition")
    if type(reps) is not int or reps < 1:
        raise ValueError("benchmark schedule reps must be positive")
    if type(seed) is not int or seed < 0:
        raise ValueError("benchmark schedule seed must be nonnegative")
    pairs = [(task, rep) for task in selected_tasks for rep in range(reps)]
    random.Random(seed).shuffle(pairs)
    scheduled: list[RunSpec] = []
    for pair_index, (task, rep) in enumerate(pairs):
        ordered_conditions: tuple[Literal["B", "C"], Literal["B", "C"]] = (
            ("B", "C") if pair_index % 2 == 0 else ("C", "B")
        )
        scheduled.extend(
            RunSpec(task, condition, rep, pair_index)
            for condition in ordered_conditions
        )
    return tuple(scheduled)


__all__ = (
    "RunSpec",
    "drop_workspace",
    "make_workspace",
    "prepare_public_corpus",
    "schedule_runs",
    "verify_prepared_corpus",
    "workspace_asset_sha256",
    "workspace_provenance",
)
