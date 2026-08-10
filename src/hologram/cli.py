from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import pipeline
from .analysis import AnalyzedProject, analyze_project
from .config import ConfigError, ProjectConfig, load_config
from .context import (
    AGENT_PATHS,
    AtomicWriteError,
    ContextStatus,
    ManagedBlockError,
    PlannedWrite,
    commit_writes,
    inspect_managed_block,
    merge_planned_writes,
    preflight_atomic_write,
    preflight_context_writes,
    read_target_bytes,
    render_managed_block,
)
from .pipeline import BuildSnapshot, IncompleteBuildError
from .render import RenderIR, project_render_ir, render_project

EXIT_OK = 0
EXIT_STALE = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    config: ProjectConfig
    snapshot: BuildSnapshot
    analyzed: AnalyzedProject
    render_ir: RenderIR
    rendered: str


class _DeliveryUsageError(ValueError):
    pass


class _ArtifactError(RuntimeError):
    pass


def create_artifact(root: Path, config: ProjectConfig) -> BuildArtifact:
    """Build one complete immutable artifact from one captured project snapshot."""

    snapshot = pipeline.build_project(root, config)
    snapshot.require_complete()
    try:
        analyzed = analyze_project(
            snapshot.project,
            snapshot.resolution,
            hot_threshold=config.hot_threshold,
        )
        render_ir = project_render_ir(
            analyzed,
            state=snapshot.state.value,
            hot_threshold=config.hot_threshold,
        )
        rendered = render_project(render_ir)
    except (TypeError, ValueError) as error:
        raise _ArtifactError(f"artifact model is invalid: {error}") from error
    return BuildArtifact(config, snapshot, analyzed, render_ir, rendered)


def _require_command_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise _DeliveryUsageError(f"{name} must be a Path")
    return value


def _resolved_root(root: object) -> Path:
    selected = _require_command_path(root, "root")
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _DeliveryUsageError(
            f"invalid project root {selected}: {error}"
        ) from error
    try:
        metadata = os.lstat(resolved)
    except OSError as error:
        raise _DeliveryUsageError(
            f"cannot inspect project root {resolved}: {error}"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise _DeliveryUsageError(f"project root is not a directory: {resolved}")
    return resolved


def _selected_config(root: Path, config_path: object) -> Path:
    selected = _require_command_path(config_path, "config_path")
    if not selected.is_absolute():
        selected = root / selected
    return Path(os.path.abspath(os.fspath(selected)))


def _require_regular_config(path: Path) -> None:
    try:
        metadata = os.stat(path)
    except OSError as error:
        raise _DeliveryUsageError(
            f"cannot inspect configuration manifest {path}: {error}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _DeliveryUsageError(
            f"configuration manifest is not a regular file: {path}"
        )


def _output_path(root: Path, config: ProjectConfig) -> Path | None:
    if config.output is None:
        return None
    return root.joinpath(*PurePosixPath(config.output).parts)


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _DeliveryUsageError(
            f"cannot compare configuration target {right}: {error}"
        ) from error


def _reject_config_collision(
    root: Path,
    selected: Path,
    config: ProjectConfig,
) -> None:
    targets = [root / AGENT_PATHS[agent] for agent in config.agents]
    output = _output_path(root, config)
    if output is not None:
        targets.append(output)
    for target in targets:
        if _same_file(selected, target):
            raise _DeliveryUsageError(
                f"configuration manifest is also a delivery target: {target}"
            )


def _load_command_inputs(
    root: object,
    config_path: object,
) -> tuple[Path, ProjectConfig]:
    resolved = _resolved_root(root)
    selected = _selected_config(resolved, config_path)
    _require_regular_config(selected)
    config = load_config(resolved, selected)
    _reject_config_collision(resolved, selected, config)
    return resolved, config


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AtomicWriteError(
            f"cannot inspect delivery target {path}: {error}"
        ) from error


def _effective_context_targets(
    root: Path,
    config: ProjectConfig,
    expected_block: bytes,
) -> dict[str, Path]:
    targets = {agent: root / AGENT_PATHS[agent] for agent in config.agents}
    for agent, relative in AGENT_PATHS.items():
        if agent in targets:
            continue
        path = root / relative
        metadata = _safe_lstat(path)
        if metadata is None or not stat.S_ISREG(metadata.st_mode):
            continue
        existing = read_target_bytes(path, root=root)
        if existing is None:
            continue
        if inspect_managed_block(existing, expected_block) is not ContextStatus.MISSING:
            targets[agent] = path
    return targets


def _preflight_artifact_writes(
    root: Path,
    artifact: BuildArtifact,
) -> tuple[PlannedWrite, ...]:
    block = render_managed_block(artifact.rendered)
    targets = _effective_context_targets(root, artifact.config, block)
    plans = list(preflight_context_writes(targets, block))
    output = _output_path(root, artifact.config)
    if output is not None:
        plans.append(
            preflight_atomic_write(
                output,
                artifact.rendered.encode("utf-8"),
                root=root,
            )
        )
    return merge_planned_writes(plans)


def _run_build(root: object, config_path: object) -> tuple[Path, ...]:
    resolved, config = _load_command_inputs(root, config_path)
    artifact = create_artifact(resolved, config)
    plans = _preflight_artifact_writes(resolved, artifact)
    return commit_writes(plans)


def _run_check(root: object, config_path: object) -> bool:
    resolved, config = _load_command_inputs(root, config_path)
    artifact = create_artifact(resolved, config)
    block = render_managed_block(artifact.rendered)
    targets = _effective_context_targets(resolved, config, block)

    fresh = True
    for path in sorted(targets.values(), key=os.fspath):
        existing = read_target_bytes(path, root=resolved)
        candidate = b"" if existing is None else existing
        if inspect_managed_block(candidate, block) is not ContextStatus.FRESH:
            fresh = False

    output = _output_path(resolved, config)
    if output is not None:
        existing_output = read_target_bytes(output, root=resolved)
        if existing_output != artifact.rendered.encode("utf-8"):
            fresh = False
    return fresh


def _validate_quiet(quiet: object) -> bool:
    if not isinstance(quiet, bool):
        raise _DeliveryUsageError("quiet must be bool")
    return quiet


def _diagnose(error: BaseException) -> None:
    message = str(error) or error.__class__.__name__
    print(f"hologram: {message}", file=sys.stderr)


def command_build(root: Path, config_path: Path, *, quiet: bool) -> int:
    """Build and atomically deliver one complete canonical project artifact."""

    try:
        silent = _validate_quiet(quiet)
        changed = _run_build(root, config_path)
    except ManagedBlockError as error:
        _diagnose(error)
        return EXIT_STALE
    except (ConfigError, AtomicWriteError, _DeliveryUsageError, OSError) as error:
        _diagnose(error)
        return EXIT_USAGE
    except (IncompleteBuildError, _ArtifactError) as error:
        _diagnose(error)
        return EXIT_INCOMPLETE
    if not silent:
        print(f"hologram: build complete ({len(changed)} target(s) updated)")
    return EXIT_OK


def command_check(root: Path, config_path: Path, *, quiet: bool) -> int:
    """Check canonical delivery freshness without mutating any target."""

    try:
        silent = _validate_quiet(quiet)
        fresh = _run_check(root, config_path)
    except ManagedBlockError as error:
        _diagnose(error)
        return EXIT_STALE
    except (ConfigError, AtomicWriteError, _DeliveryUsageError, OSError) as error:
        _diagnose(error)
        return EXIT_USAGE
    except (IncompleteBuildError, _ArtifactError) as error:
        _diagnose(error)
        return EXIT_INCOMPLETE
    if not fresh:
        print("hologram: generated output is stale", file=sys.stderr)
        return EXIT_STALE
    if not silent:
        print("hologram: generated output is fresh")
    return EXIT_OK


__all__ = [
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "EXIT_STALE",
    "EXIT_USAGE",
    "BuildArtifact",
    "command_build",
    "command_check",
    "create_artifact",
]
