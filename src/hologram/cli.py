from __future__ import annotations

import dataclasses
import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import pipeline
from .analysis import AnalyzedProject, analyze_project
from .config import (
    ALLOWED_AGENTS,
    ConfigError,
    ProjectConfig,
    canonical_config_bytes,
    default_config,
    load_config,
)
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
from .diff import (
    DiffInput,
    DiffReport,
    RevisionError,
    analyze_revision,
    compare_projects,
)
from .hooks import (
    UnsupportedHookError,
    preflight_precommit,
    remove_legacy_post_hook_lines,
    render_precommit_command,
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


def _run_diff(root: object, config_path: object, rev: object) -> DiffReport:
    if not isinstance(rev, str):
        raise _DeliveryUsageError("rev must be str")
    resolved, config = _load_command_inputs(root, config_path)
    artifact = create_artifact(resolved, config)
    try:
        current = DiffInput(artifact.analyzed, artifact.render_ir)
    except (TypeError, ValueError) as error:
        raise _ArtifactError(f"current diff model is invalid: {error}") from error
    previous = analyze_revision(resolved, config, rev)
    try:
        return compare_projects(previous, current)
    except (TypeError, ValueError) as error:
        raise _ArtifactError(f"semantic diff model is invalid: {error}") from error


def _validate_quiet(quiet: object) -> bool:
    if not isinstance(quiet, bool):
        raise _DeliveryUsageError("quiet must be bool")
    return quiet


def _validate_boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise _DeliveryUsageError(f"{name} must be bool")
    return value


def _normalize_init_agents(agents: object) -> tuple[str, ...]:
    if isinstance(agents, (str, bytes)) or not isinstance(agents, Sequence):
        raise _DeliveryUsageError("agents must be a sequence of agent names")
    selected: list[str] = []
    for agent in agents:
        if not isinstance(agent, str):
            raise _DeliveryUsageError("agents must contain only strings")
        if agent not in ALLOWED_AGENTS:
            raise _DeliveryUsageError(f"unknown agent: {agent}")
        if agent in selected:
            raise _DeliveryUsageError(f"duplicate agent: {agent}")
        selected.append(agent)
    chosen = frozenset(selected)
    return tuple(agent for agent in AGENT_PATHS if agent in chosen)


def _init_config(
    root: Path,
    selected: Path,
    explicit_agents: tuple[str, ...],
) -> tuple[ProjectConfig, bytes | None]:
    metadata = _safe_lstat(selected)
    if metadata is not None:
        _require_regular_config(selected)
        config = load_config(root, selected)
        if explicit_agents and _normalize_init_agents(config.agents) != explicit_agents:
            raise _DeliveryUsageError(
                "explicit agents do not match the existing configuration"
            )
        return config, None

    detected = tuple(
        agent
        for agent, relative in AGENT_PATHS.items()
        if (
            (agent_metadata := _safe_lstat(root / relative)) is not None
            and stat.S_ISREG(agent_metadata.st_mode)
        )
    )
    selected_agents = explicit_agents or detected
    if not selected_agents:
        raise _DeliveryUsageError(
            "no agent context files detected; select at least one agent"
        )
    config = dataclasses.replace(default_config(), agents=selected_agents)
    return config, canonical_config_bytes(config)


def _run_init(
    root: object,
    config_path: object,
    *,
    agents: object,
    no_hook: object,
) -> tuple[Path, ...]:
    skip_hook = _validate_boolean(no_hook, "no_hook")
    explicit_agents = _normalize_init_agents(agents)
    resolved = _resolved_root(root)
    selected = _selected_config(resolved, config_path)
    config, config_content = _init_config(resolved, selected, explicit_agents)
    _reject_config_collision(resolved, selected, config)
    command: bytes | None = None
    if not skip_hook:
        command = render_precommit_command(
            root=resolved,
            config_path=selected,
            python=Path(sys.executable),
        )

    artifact = create_artifact(resolved, config)
    root_plans = list(_preflight_artifact_writes(resolved, artifact))
    if config_content is not None:
        try:
            selected.relative_to(resolved)
        except ValueError:
            config_root = None
        else:
            config_root = resolved
        root_plans.append(
            preflight_atomic_write(
                selected,
                config_content,
                root=config_root,
            )
        )
    planned_root = merge_planned_writes(root_plans)

    planned_hooks: tuple[PlannedWrite, ...] = ()
    if command is not None:
        planned_hooks = merge_planned_writes(
            (
                preflight_precommit(resolved, command),
                *remove_legacy_post_hook_lines(resolved),
            )
        )

    changed = list(commit_writes(planned_root))
    if planned_hooks:
        changed.extend(commit_writes(planned_hooks))
    return tuple(changed)


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


def command_diff(
    root: Path,
    config_path: Path,
    rev: str,
    *,
    quiet: bool,
) -> int:
    """Compare the current canonical model with one committed revision."""

    try:
        silent = _validate_quiet(quiet)
        report = _run_diff(root, config_path, rev)
    except (ConfigError, AtomicWriteError, _DeliveryUsageError, OSError) as error:
        _diagnose(error)
        return EXIT_USAGE
    except (IncompleteBuildError, RevisionError, _ArtifactError) as error:
        _diagnose(error)
        return EXIT_INCOMPLETE
    if not silent:
        sys.stdout.write(report.text)
    return EXIT_OK


def command_init(
    root: Path,
    config_path: Path,
    *,
    agents: Sequence[str],
    no_hook: bool,
    quiet: bool,
) -> int:
    """Initialize canonical delivery targets and an optional read-only hook."""

    try:
        silent = _validate_quiet(quiet)
        changed = _run_init(
            root,
            config_path,
            agents=agents,
            no_hook=no_hook,
        )
    except ManagedBlockError as error:
        _diagnose(error)
        return EXIT_STALE
    except (
        ConfigError,
        AtomicWriteError,
        UnsupportedHookError,
        _DeliveryUsageError,
        OSError,
    ) as error:
        _diagnose(error)
        return EXIT_USAGE
    except (IncompleteBuildError, _ArtifactError) as error:
        _diagnose(error)
        return EXIT_INCOMPLETE
    if not silent:
        print(f"hologram: init complete ({len(changed)} target(s) updated)")
    return EXIT_OK


__all__ = [
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "EXIT_STALE",
    "EXIT_USAGE",
    "BuildArtifact",
    "command_build",
    "command_check",
    "command_diff",
    "command_init",
    "create_artifact",
]
