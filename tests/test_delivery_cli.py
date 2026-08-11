from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

import hologram.cli as cli_module
import hologram.pipeline as pipeline_module
from hologram.analysis import AnalyzedProject
from hologram.cli import (
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_STALE,
    EXIT_USAGE,
    BuildArtifact,
    build_parser,
    command_build,
    command_check,
    command_diff,
    command_init,
    create_artifact,
    main,
)
from hologram.config import (
    CONFIG_NAME,
    ProjectConfig,
    canonical_config_bytes,
    default_config,
    load_config,
    render_config,
)
from hologram.context import (
    AGENT_PATHS,
    CONTEXT_START,
    PlannedWrite,
    render_managed_block,
)
from hologram.model import Language
from hologram.pipeline import BuildSnapshot, IncompleteBuildError
from hologram.render import RenderIR

_RENDERED = "# hologram fixture Ω\n"


def _configured_root(
    base: Path,
    name: str,
    *,
    agents: tuple[str, ...] = ("claude", "codex", "gemini"),
    output: str | None = "PROJECT_DIGEST.md",
    config_relative: str = CONFIG_NAME,
) -> tuple[Path, Path, ProjectConfig]:
    root = base / name
    root.mkdir()
    config = dataclasses.replace(
        default_config(),
        agents=agents,
        languages=(Language.PYTHON,),
        include=("**/*.py",),
        exclude=(),
        hot_threshold=2,
        output=output,
    )
    config_path = root.joinpath(*Path(config_relative).parts)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(canonical_config_bytes(config))
    return root, config_path, config


def _artifact(config: ProjectConfig, rendered: str = _RENDERED) -> BuildArtifact:
    return BuildArtifact(
        config,
        cast(BuildSnapshot, mock.sentinel.snapshot),
        cast(AnalyzedProject, mock.sentinel.analyzed),
        cast(RenderIR, mock.sentinel.render_ir),
        rendered,
    )


def _artifact_factory(
    root: Path,
    config: ProjectConfig,
) -> BuildArtifact:
    del root
    return _artifact(config)


def _file_metadata(path: Path) -> tuple[int, int, int, bytes]:
    metadata = path.stat()
    return (
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_mtime_ns,
        path.read_bytes(),
    )


def _git_init(root: Path) -> None:
    root.mkdir()
    subprocess.run(
        ("git", "-C", os.fspath(root), "init", "--quiet"),
        check=True,
        capture_output=True,
    )


def _incomplete_error() -> IncompleteBuildError:
    empty = SimpleNamespace(diagnostics=())
    snapshot = cast(
        BuildSnapshot,
        SimpleNamespace(scan=empty, state=empty, project=empty, resolution=empty),
    )
    return IncompleteBuildError(snapshot)


class BuildCheckServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_exact_public_service_surface(self) -> None:
        self.assertEqual(
            (EXIT_OK, EXIT_STALE, EXIT_USAGE, EXIT_INCOMPLETE),
            (0, 1, 2, 3),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(BuildArtifact)),
            ("config", "snapshot", "analyzed", "render_ir", "rendered"),
        )
        self.assertEqual(
            BuildArtifact.__slots__,
            ("config", "snapshot", "analyzed", "render_ir", "rendered"),
        )
        artifact = _artifact(default_config())
        self.assertFalse(hasattr(artifact, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.rendered = "changed"  # type: ignore[misc]
        self.assertEqual(
            cli_module.__all__,
            [
                "EXIT_INCOMPLETE",
                "EXIT_OK",
                "EXIT_STALE",
                "EXIT_USAGE",
                "BuildArtifact",
                "build_parser",
                "command_build",
                "command_check",
                "command_diff",
                "command_init",
                "create_artifact",
                "main",
            ],
        )
        self.assertIn("command_init", cli_module.__all__)
        self.assertEqual(
            tuple(inspect.signature(create_artifact).parameters),
            ("root", "config"),
        )
        self.assertEqual(tuple(inspect.signature(build_parser).parameters), ())
        self.assertEqual(tuple(inspect.signature(main).parameters), ("argv",))
        for command in (command_build, command_check):
            signature = inspect.signature(command)
            self.assertEqual(
                tuple(signature.parameters), ("root", "config_path", "quiet")
            )
            self.assertEqual(
                signature.parameters["quiet"].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
        init_signature = inspect.signature(command_init)
        self.assertEqual(
            tuple(init_signature.parameters),
            ("root", "config_path", "agents", "no_hook", "quiet"),
        )
        for name in ("agents", "no_hook", "quiet"):
            self.assertEqual(
                init_signature.parameters[name].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
        diff_signature = inspect.signature(command_diff)
        self.assertEqual(
            tuple(diff_signature.parameters),
            ("root", "config_path", "rev", "quiet"),
        )
        self.assertEqual(
            diff_signature.parameters["quiet"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def test_create_artifact_uses_one_snapshot_and_exact_identity_order(self) -> None:
        root, _, config = _configured_root(self.base, "artifact")
        order: list[str] = []
        project = mock.sentinel.project
        resolution = mock.sentinel.resolution
        state = SimpleNamespace(value="a" * 64)

        class Snapshot:
            def __init__(self) -> None:
                self.project = project
                self.resolution = resolution
                self.state = state

            def require_complete(self) -> Snapshot:
                order.append("require_complete")
                return self

        snapshot = cast(BuildSnapshot, Snapshot())
        analyzed = cast(AnalyzedProject, mock.sentinel.analyzed_identity)
        render_ir = cast(RenderIR, mock.sentinel.render_identity)

        def analyze(
            received_project: object,
            received_resolution: object,
            *,
            hot_threshold: int,
        ) -> AnalyzedProject:
            order.append("analyze")
            self.assertIs(received_project, project)
            self.assertIs(received_resolution, resolution)
            self.assertEqual(hot_threshold, config.hot_threshold)
            return analyzed

        def project_render(
            received: object,
            *,
            state: str,
            hot_threshold: int,
        ) -> RenderIR:
            order.append("project_render")
            self.assertIs(received, analyzed)
            self.assertEqual(state, "a" * 64)
            self.assertEqual(hot_threshold, config.hot_threshold)
            return render_ir

        def render(received: object) -> str:
            order.append("render")
            self.assertIs(received, render_ir)
            return _RENDERED

        with (
            mock.patch.object(
                cli_module.pipeline,
                "build_project",
                return_value=snapshot,
            ) as build,
            mock.patch.object(cli_module, "analyze_project", side_effect=analyze),
            mock.patch.object(
                cli_module,
                "project_render_ir",
                side_effect=project_render,
            ),
            mock.patch.object(cli_module, "render_project", side_effect=render),
        ):
            artifact = create_artifact(root, config)

        build.assert_called_once_with(root, config)
        self.assertEqual(
            order,
            ["require_complete", "analyze", "project_render", "render"],
        )
        self.assertIs(artifact.config, config)
        self.assertIs(artifact.snapshot, snapshot)
        self.assertIs(artifact.analyzed, analyzed)
        self.assertIs(artifact.render_ir, render_ir)
        self.assertIs(artifact.rendered, _RENDERED)

    def test_build_projects_all_agent_and_raw_output_bytes_from_one_artifact(
        self,
    ) -> None:
        root, config_path, _ = _configured_root(self.base, "all")
        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ) as create:
            result = command_build(root, config_path, quiet=True)

        self.assertEqual(result, EXIT_OK)
        create.assert_called_once()
        expected = render_managed_block(_RENDERED)
        for relative in AGENT_PATHS.values():
            self.assertEqual((root / relative).read_bytes(), expected)
        self.assertEqual((root / "PROJECT_DIGEST.md").read_bytes(), _RENDERED.encode())

    def test_real_pipeline_build_and_check_deliver_one_canonical_artifact(self) -> None:
        root, config_path, _ = _configured_root(self.base, "real-end-to-end")
        (root / "service.py").write_bytes(
            b"def calculate(value: int) -> int:\n"
            b"    adjusted = value + 1\n"
            b"    return adjusted\n"
        )

        self.assertEqual(command_build(root, config_path, quiet=True), EXIT_OK)

        rendered = (root / "PROJECT_DIGEST.md").read_text(encoding="utf-8")
        expected = render_managed_block(rendered)
        for relative in AGENT_PATHS.values():
            self.assertEqual((root / relative).read_bytes(), expected)
        self.assertEqual(command_check(root, config_path, quiet=True), EXIT_OK)

    def test_build_supports_subsets_agents_empty_and_nested_raw_output(self) -> None:
        cases = (
            ("subset", ("codex",), None),
            ("output-only", (), "PROJECT_DIGEST.md"),
            ("nested", ("claude",), "docs/generated/MAP.md"),
        )
        for name, agents, output in cases:
            with self.subTest(name=name):
                root, config_path, _ = _configured_root(
                    self.base,
                    name,
                    agents=agents,
                    output=output,
                )
                with mock.patch.object(
                    cli_module,
                    "create_artifact",
                    side_effect=_artifact_factory,
                ):
                    self.assertEqual(
                        command_build(root, config_path, quiet=True),
                        EXIT_OK,
                    )

                for agent, relative in AGENT_PATHS.items():
                    self.assertEqual((root / relative).exists(), agent in agents)
                if output is not None:
                    self.assertEqual(
                        root.joinpath(*Path(output).parts).read_bytes(),
                        _RENDERED.encode(),
                    )

    def test_repeated_identical_build_preserves_all_target_metadata(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "identical",
            agents=("claude", "codex"),
            output="nested/MAP.md",
        )
        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ):
            self.assertEqual(command_build(root, config_path, quiet=True), EXIT_OK)
            targets = (
                root / "CLAUDE.md",
                root / "AGENTS.md",
                root / "nested" / "MAP.md",
            )
            before = {path: _file_metadata(path) for path in targets}
            self.assertEqual(command_build(root, config_path, quiet=True), EXIT_OK)

        self.assertEqual({path: _file_metadata(path) for path in targets}, before)

    def test_incomplete_artifact_precedes_target_inspection_and_mutation(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "incomplete",
            agents=("claude",),
            output="nested/MAP.md",
        )
        context = root / "CLAUDE.md"
        context.write_bytes(CONTEXT_START + b"\n")
        before = _file_metadata(context)

        for command in (command_build, command_check):
            with (
                self.subTest(command=command.__name__),
                mock.patch.object(
                    cli_module,
                    "create_artifact",
                    side_effect=_incomplete_error(),
                ),
                mock.patch.object(
                    cli_module,
                    "read_target_bytes",
                    side_effect=AssertionError("targets inspected before artifact"),
                ),
                mock.patch.object(
                    cli_module,
                    "preflight_context_writes",
                    side_effect=AssertionError("preflight ran before artifact"),
                ),
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(
                        command(root, config_path, quiet=True),
                        EXIT_INCOMPLETE,
                    )
                self.assertTrue(stderr.getvalue())

        self.assertEqual(_file_metadata(context), before)
        self.assertFalse((root / "nested").exists())

    def test_one_malformed_context_prevents_every_build_write(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "malformed",
            agents=("claude", "codex", "gemini"),
            output="nested/MAP.md",
        )
        claude = root / "CLAUDE.md"
        codex = root / "AGENTS.md"
        claude.write_bytes(b"authored\n" + CONTEXT_START + b"\n")
        codex.write_bytes(b"authored codex\n")
        before = {path: _file_metadata(path) for path in (claude, codex)}

        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ),
            mock.patch.object(
                cli_module,
                "commit_writes",
                wraps=cli_module.commit_writes,
            ) as commit,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = command_build(root, config_path, quiet=True)

        self.assertEqual(result, EXIT_STALE)
        commit.assert_not_called()
        self.assertEqual(
            {path: _file_metadata(path) for path in (claude, codex)},
            before,
        )
        self.assertFalse((root / "GEMINI.md").exists())
        self.assertFalse((root / "nested").exists())

    def test_check_matrix_is_exact_read_only_and_inspects_every_target(self) -> None:
        states = ("fresh", "stale", "missing", "malformed", "raw-mismatch")
        for state in states:
            with self.subTest(state=state):
                root, config_path, _ = _configured_root(
                    self.base,
                    f"check-{state}",
                    agents=("claude", "codex"),
                    output="MAP.md",
                )
                expected = render_managed_block(_RENDERED)
                claude = root / "CLAUDE.md"
                codex = root / "AGENTS.md"
                output = root / "MAP.md"
                claude.write_bytes(expected)
                codex.write_bytes(expected)
                output.write_bytes(_RENDERED.encode())
                if state == "stale":
                    claude.write_bytes(render_managed_block("old\n"))
                elif state == "missing":
                    claude.unlink()
                elif state == "malformed":
                    claude.write_bytes(CONTEXT_START + b"\n")
                elif state == "raw-mismatch":
                    output.write_bytes(b"different")

                existing = tuple(
                    path for path in (claude, codex, output) if path.exists()
                )
                before = {path: _file_metadata(path) for path in existing}
                real_reader = cli_module.read_target_bytes
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        cli_module,
                        "create_artifact",
                        side_effect=_artifact_factory,
                    ),
                    mock.patch.object(
                        cli_module,
                        "read_target_bytes",
                        wraps=real_reader,
                    ) as reader,
                    mock.patch.object(
                        cli_module,
                        "commit_writes",
                        side_effect=AssertionError("check committed writes"),
                    ),
                    mock.patch.object(
                        cli_module,
                        "preflight_context_writes",
                        side_effect=AssertionError("check preflighted writes"),
                    ),
                    mock.patch.object(
                        cli_module,
                        "preflight_atomic_write",
                        side_effect=AssertionError("check preflighted output"),
                    ),
                    mock.patch.object(
                        cli_module.os,
                        "mkdir",
                        side_effect=AssertionError("check created a directory"),
                    ),
                    contextlib.redirect_stderr(stderr),
                ):
                    result = command_check(root, config_path, quiet=True)

                expected_result = EXIT_OK if state == "fresh" else EXIT_STALE
                self.assertEqual(result, expected_result)
                self.assertEqual(
                    {path: _file_metadata(path) for path in existing},
                    before,
                )
                self.assertEqual(claude.exists(), state != "missing")
                read_paths = [call.args[0] for call in reader.call_args_list]
                self.assertIn(codex.resolve(), read_paths)
                self.assertIn(output.resolve(), read_paths)
                self.assertEqual(bool(stderr.getvalue()), state != "fresh")

    def test_unsafe_configured_targets_and_output_ancestors_exit_two(self) -> None:
        cases: list[tuple[str, str]] = []

        symlink_root, symlink_config, _ = _configured_root(
            self.base,
            "unsafe-context-link",
            agents=("claude",),
            output=None,
        )
        owned = self.base / "owned.md"
        owned.write_bytes(b"owned")
        (symlink_root / "CLAUDE.md").symlink_to(owned)
        cases.append((str(symlink_root), str(symlink_config)))

        directory_root, directory_config, _ = _configured_root(
            self.base,
            "unsafe-context-directory",
            agents=("claude",),
            output=None,
        )
        (directory_root / "CLAUDE.md").mkdir()
        cases.append((str(directory_root), str(directory_config)))

        ancestor_root, ancestor_config, _ = _configured_root(
            self.base,
            "unsafe-output-ancestor",
            agents=(),
            output="nested/MAP.md",
        )
        outside = self.base / "outside"
        outside.mkdir()
        (ancestor_root / "nested").symlink_to(outside)
        cases.append((str(ancestor_root), str(ancestor_config)))

        for root_text, config_text in cases:
            root = Path(root_text)
            config_path = Path(config_text)
            for command in (command_build, command_check):
                with (
                    self.subTest(root=root.name, command=command.__name__),
                    mock.patch.object(
                        cli_module,
                        "create_artifact",
                        side_effect=_artifact_factory,
                    ),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        command(root, config_path, quiet=True),
                        EXIT_USAGE,
                    )
        self.assertEqual(owned.read_bytes(), b"owned")
        self.assertEqual(list(outside.iterdir()), [])

    def test_root_and_config_validation_precede_scan(self) -> None:
        good_root, _, _ = _configured_root(self.base, "validation")
        invalid_config = good_root / "invalid.toml"
        invalid_config.write_bytes(b"not = [valid")
        missing_root = self.base / "missing-root"
        file_root = self.base / "file-root"
        file_root.write_bytes(b"x")

        cases: tuple[tuple[object, object], ...] = (
            (missing_root, missing_root / CONFIG_NAME),
            (file_root, file_root / CONFIG_NAME),
            (good_root, invalid_config),
            ("not-a-path", invalid_config),
            (good_root, "not-a-path"),
        )
        for root, config_path in cases:
            with (
                self.subTest(root=root, config=config_path),
                mock.patch.object(
                    cli_module.pipeline,
                    "build_project",
                    side_effect=AssertionError("scan ran for invalid input"),
                ) as build,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    command_build(
                        cast(Path, root),
                        cast(Path, config_path),
                        quiet=True,
                    ),
                    EXIT_USAGE,
                )
            build.assert_not_called()

    def test_nonregular_selected_config_is_rejected_before_open_or_scan(self) -> None:
        root = self.base / "nonregular-config"
        root.mkdir()
        fifo = root / "config.fifo"
        os.mkfifo(fifo)

        with (
            mock.patch.object(
                cli_module,
                "load_config",
                side_effect=AssertionError("nonregular config was opened"),
            ) as load,
            mock.patch.object(
                cli_module.pipeline,
                "build_project",
                side_effect=AssertionError("nonregular config reached scan"),
            ) as build,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_build(root, fifo, quiet=True),
                EXIT_USAGE,
            )
        load.assert_not_called()
        build.assert_not_called()

    def test_relative_and_absolute_external_configs_load_once(self) -> None:
        relative_root, _, _ = _configured_root(
            self.base,
            "relative-config",
            agents=("codex",),
            output=None,
            config_relative="config/hologram.toml",
        )
        external_root = self.base / "external-root"
        external_root.mkdir()
        external_config = self.base / "external.toml"
        external_project = dataclasses.replace(
            default_config(),
            agents=("gemini",),
            languages=(Language.PYTHON,),
            include=("**/*.py",),
            exclude=(),
            output=None,
        )
        external_config.write_bytes(canonical_config_bytes(external_project))
        symlink_root = self.base / "symlink-config-root"
        symlink_root.mkdir()
        symlink_config = symlink_root / "selected.toml"
        symlink_config.symlink_to(external_config)
        real_load = load_config

        cases = (
            (relative_root, Path("config/hologram.toml")),
            (external_root, external_config),
            (symlink_root, symlink_config),
        )
        for root, selected in cases:
            with (
                self.subTest(selected=selected),
                mock.patch.object(
                    cli_module,
                    "load_config",
                    wraps=real_load,
                ) as load,
                mock.patch.object(
                    cli_module,
                    "create_artifact",
                    side_effect=_artifact_factory,
                ),
            ):
                self.assertEqual(
                    command_build(root, selected, quiet=True),
                    EXIT_OK,
                )
            load.assert_called_once()
            expected_selected = (
                selected if selected.is_absolute() else root.resolve() / selected
            )
            self.assertEqual(load.call_args.args, (root.resolve(), expected_selected))

    def test_config_target_collisions_fail_before_scan_including_samefile(self) -> None:
        cases: list[tuple[Path, Path]] = []

        agent_root = self.base / "agent-collision"
        agent_root.mkdir()
        agent_config = agent_root / "CLAUDE.md"
        agent_project = dataclasses.replace(
            default_config(),
            agents=("claude",),
            output=None,
        )
        agent_config.write_bytes(canonical_config_bytes(agent_project))
        cases.append((agent_root, agent_config))

        output_root = self.base / "output-collision"
        output_root.mkdir()
        output_config = output_root / "MAP.md"
        output_project = dataclasses.replace(
            default_config(),
            agents=(),
            output="MAP.md",
        )
        output_config.write_bytes(canonical_config_bytes(output_project))
        cases.append((output_root, output_config))

        samefile_root = self.base / "samefile-collision"
        samefile_root.mkdir()
        selected = samefile_root / "selected.toml"
        output = samefile_root / "MAP.md"
        samefile_project = dataclasses.replace(
            default_config(),
            agents=(),
            output="MAP.md",
        )
        selected.write_bytes(canonical_config_bytes(samefile_project))
        os.link(selected, output)
        cases.append((samefile_root, selected))

        for root, selected_config in cases:
            for command in (command_build, command_check):
                with (
                    self.subTest(root=root.name, command=command.__name__),
                    mock.patch.object(
                        cli_module.pipeline,
                        "build_project",
                        side_effect=AssertionError("scan ran after collision"),
                    ) as build,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        command(root, selected_config, quiet=True),
                        EXIT_USAGE,
                    )
                build.assert_not_called()

    def test_orphan_managed_blocks_are_retained_and_refreshed(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "orphan-refresh",
            agents=("codex",),
            output=None,
        )
        claude = root / "CLAUDE.md"
        gemini = root / "GEMINI.md"
        claude.write_bytes(b"claude rules\n" + render_managed_block("old\n"))
        gemini.write_bytes(b"gemini rules\r\n" + render_managed_block("older\n"))

        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ):
            self.assertEqual(command_build(root, config_path, quiet=True), EXIT_OK)
            self.assertEqual(command_check(root, config_path, quiet=True), EXIT_OK)

        expected = render_managed_block(_RENDERED)
        self.assertEqual(claude.read_bytes(), b"claude rules\n" + expected)
        self.assertEqual(gemini.read_bytes(), b"gemini rules\r\n" + expected)
        self.assertEqual((root / "AGENTS.md").read_bytes(), expected)

    def test_orphan_malformed_marker_returns_one_before_configured_write(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "orphan-malformed",
            agents=("codex",),
            output=None,
        )
        configured = root / "AGENTS.md"
        orphan = root / "CLAUDE.md"
        configured.write_bytes(render_managed_block("configured-old\n"))
        orphan.write_bytes(b"rules\n" + CONTEXT_START + b"\n")
        before = {
            configured: _file_metadata(configured),
            orphan: _file_metadata(orphan),
        }

        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_build(root, config_path, quiet=True),
                EXIT_STALE,
            )
            self.assertEqual(
                command_check(root, config_path, quiet=True),
                EXIT_STALE,
            )

        self.assertEqual(
            {
                configured: _file_metadata(configured),
                orphan: _file_metadata(orphan),
            },
            before,
        )

    def test_marker_free_inline_symlink_and_nonregular_orphans_are_ignored(
        self,
    ) -> None:
        scenarios = ("authored", "inline", "unsafe")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                root, config_path, _ = _configured_root(
                    self.base,
                    f"orphan-{scenario}",
                    agents=("codex",),
                    output=None,
                )
                claude = root / "CLAUDE.md"
                gemini = root / "GEMINI.md"
                owned = self.base / f"owned-{scenario}.md"
                owned.write_bytes(b"owned")
                if scenario == "authored":
                    claude.write_bytes(b"authored only\n")
                    gemini.write_bytes(b"also authored\n")
                elif scenario == "inline":
                    claude.write_bytes(b"inline " + CONTEXT_START + b" prose\n")
                    gemini.write_bytes(b"prefix " + CONTEXT_START + b" suffix\n")
                else:
                    claude.symlink_to(owned)
                    gemini.mkdir()

                before_owned = owned.read_bytes()
                before_claude = os.lstat(claude)
                before_gemini = os.lstat(gemini)
                with mock.patch.object(
                    cli_module,
                    "create_artifact",
                    side_effect=_artifact_factory,
                ):
                    self.assertEqual(
                        command_build(root, config_path, quiet=True),
                        EXIT_OK,
                    )
                    self.assertEqual(
                        command_check(root, config_path, quiet=True),
                        EXIT_OK,
                    )

                self.assertEqual(owned.read_bytes(), before_owned)
                self.assertEqual(os.lstat(claude).st_ino, before_claude.st_ino)
                self.assertEqual(os.lstat(gemini).st_ino, before_gemini.st_ino)

    def test_removing_an_orphan_marker_opts_the_file_out(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "orphan-opt-out",
            agents=("codex",),
            output=None,
        )
        configured = root / "AGENTS.md"
        orphan = root / "CLAUDE.md"
        configured.write_bytes(render_managed_block(_RENDERED))
        orphan.write_bytes(render_managed_block(_RENDERED))
        orphan.write_bytes(b"authored after opt-out\n")
        before = _file_metadata(orphan)

        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ):
            self.assertEqual(command_check(root, config_path, quiet=True), EXIT_OK)
            self.assertEqual(command_build(root, config_path, quiet=True), EXIT_OK)

        self.assertEqual(_file_metadata(orphan), before)

    def test_quiet_suppresses_only_success_while_failures_stay_on_stderr(self) -> None:
        root, config_path, _ = _configured_root(
            self.base,
            "messages",
            agents=("claude",),
            output=None,
        )
        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(command_build(root, config_path, quiet=False), EXIT_OK)
            self.assertTrue(stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(command_check(root, config_path, quiet=True), EXIT_OK)
            self.assertEqual(stdout.getvalue(), "")

            (root / "CLAUDE.md").write_bytes(render_managed_block("stale\n"))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    command_check(root, config_path, quiet=True),
                    EXIT_STALE,
                )
            self.assertTrue(stderr.getvalue())

    def test_captured_snapshot_is_stable_after_source_mutation_without_rereads(
        self,
    ) -> None:
        root, _, config = _configured_root(
            self.base,
            "snapshot",
            agents=("claude",),
            output=None,
        )
        source = root / "app.py"
        source.write_bytes(
            b"def calculate(value: int) -> int:\n"
            b"    adjusted = value + 1\n"
            b"    return adjusted\n"
        )
        snapshot = pipeline_module.build_project(root, config).require_complete()
        with mock.patch.object(
            cli_module.pipeline,
            "build_project",
            return_value=snapshot,
        ):
            before = create_artifact(root, config)

        source.write_bytes(b"def calculate(value):\n    return value * 999\n")
        with (
            mock.patch.object(
                cli_module.pipeline,
                "build_project",
                return_value=snapshot,
            ) as build,
            mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("artifact reread source bytes"),
            ),
            mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("artifact reread source text"),
            ),
        ):
            after = create_artifact(root, config)

        build.assert_called_once_with(root, config)
        self.assertIs(after.snapshot, snapshot)
        self.assertIs(after.snapshot.project, snapshot.project)
        self.assertEqual(after.snapshot.state, before.snapshot.state)
        self.assertEqual(after.analyzed, before.analyzed)
        self.assertEqual(after.render_ir, before.render_ir)
        self.assertEqual(after.rendered, before.rendered)


class CliContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_parser_exposes_only_the_command_surface(self) -> None:
        parser = build_parser()

        build = parser.parse_args(
            ["build", "--root", "project", "--config", "nested/map.toml", "--quiet"]
        )
        self.assertEqual(build.command, "build")
        self.assertEqual(build.root, Path("project"))
        self.assertEqual(build.config, Path("nested/map.toml"))
        self.assertTrue(build.quiet)

        init = parser.parse_args(
            [
                "init",
                "--agent",
                "gemini",
                "--agent",
                "claude",
                "--no-hook",
            ]
        )
        self.assertEqual(init.command, "init")
        self.assertEqual(init.agent, ["gemini", "claude"])
        self.assertTrue(init.no_hook)

        diff = parser.parse_args(["diff"])
        self.assertEqual(diff.command, "diff")
        self.assertEqual(diff.rev, "HEAD~1")
        for argv in (["build", "--conf", "x"], ["init", "--ag", "claude"]):
            with (
                self.subTest(argv=argv),
                self.assertRaises(cli_module._DeliveryUsageError),
            ):
                parser.parse_args(argv)

    def test_main_dispatches_exact_arguments_and_returns_command_codes(self) -> None:
        root = self.base / "dispatch"
        relative_config = Path("config") / "hologram.toml"
        selected_config = root / relative_config
        absolute_config = self.base / "external.toml"
        with (
            mock.patch.object(cli_module, "command_build", return_value=10) as build,
            mock.patch.object(cli_module, "command_check", return_value=11) as check,
            mock.patch.object(cli_module, "command_diff", return_value=12) as diff,
            mock.patch.object(cli_module, "command_init", return_value=13) as init,
        ):
            self.assertEqual(
                main(
                    [
                        "build",
                        "--root",
                        str(root),
                        "--config",
                        str(relative_config),
                        "--quiet",
                    ]
                ),
                10,
            )
            self.assertEqual(
                main(
                    [
                        "check",
                        "--root",
                        str(root),
                        "--config",
                        str(absolute_config),
                    ]
                ),
                11,
            )
            self.assertEqual(main(["diff", "--root", str(root)]), 12)
            self.assertEqual(
                main(
                    [
                        "init",
                        "--root",
                        str(root),
                        "--agent",
                        "gemini",
                        "--agent",
                        "claude",
                        "--no-hook",
                        "--quiet",
                    ]
                ),
                13,
            )

        build.assert_called_once_with(root, selected_config, quiet=True)
        check.assert_called_once_with(root, absolute_config, quiet=False)
        diff.assert_called_once_with(
            root,
            root / CONFIG_NAME,
            "HEAD~1",
            quiet=False,
        )
        init.assert_called_once_with(
            root,
            root / CONFIG_NAME,
            agents=("gemini", "claude"),
            no_hook=True,
            quiet=True,
        )

    def test_main_normalizes_relative_root_before_joining_relative_config(
        self,
    ) -> None:
        working = self.base / "working"
        working.mkdir()
        with (
            contextlib.chdir(working),
            mock.patch.object(cli_module, "command_build", return_value=17) as build,
        ):
            expected_root = Path(os.path.abspath("project"))
            expected_config = expected_root / "nested" / "map.toml"
            code = main(
                [
                    "build",
                    "--root",
                    "project",
                    "--config",
                    "nested/map.toml",
                ]
            )

        self.assertEqual(code, 17)
        build.assert_called_once_with(expected_root, expected_config, quiet=False)

    def test_help_exits_zero_and_invalid_syntax_exits_two(self) -> None:
        for argv in (["--help"], ["build", "--help"], ["diff", "--help"]):
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(main(argv), EXIT_OK)
                self.assertIn("usage: hologram", stdout.getvalue())

        invalid: tuple[list[str], ...] = (
            [],
            ["unknown"],
            ["build", "--agent", "claude"],
            ["check", "--no-hook"],
            ["diff", "--agent", "codex"],
            ["init", "--agent", "unknown"],
            ["build", "--roo", "project"],
        )
        with (
            mock.patch.object(cli_module, "command_build") as build,
            mock.patch.object(cli_module, "command_check") as check,
            mock.patch.object(cli_module, "command_diff") as diff,
            mock.patch.object(cli_module, "command_init") as init,
        ):
            for argv in invalid:
                with self.subTest(argv=argv):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(main(argv), EXIT_USAGE)
                    self.assertTrue(stderr.getvalue().startswith("hologram: "))
                    self.assertNotIn("usage:", stderr.getvalue())
            for command in (build, check, diff, init):
                command.assert_not_called()


class SelfConfigTest(unittest.TestCase):
    def test_tracked_self_config_is_canonical_digest_only_config(self) -> None:
        expected = dataclasses.replace(default_config(), agents=())
        path = Path(__file__).resolve().parents[1] / CONFIG_NAME

        self.assertEqual(path.read_bytes(), render_config(expected).encode("utf-8"))
        self.assertEqual(load_config(path.parent, path), expected)
        self.assertEqual(expected.output, "PROJECT_DIGEST.md")

    def test_readme_documents_the_complete_delivery_contract(self) -> None:
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )

        for text in (
            "## Configuration",
            "CLAUDE.md",
            "AGENTS.md",
            "GEMINI.md",
            "hologram init",
            "hologram build",
            "hologram check",
            "hologram diff",
            "Atomic replacement is per target",
            "Static evidence cannot prove semantic deadness or authorize deletion",
            "supported source that cannot be read",
            "CLI orchestration",
            "canonical phase APIs",
        ):
            with self.subTest(text=text):
                self.assertIn(text, readme)


class InitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_detects_existing_regular_agent_files_in_fixed_order(self) -> None:
        root = self.base / "detected"
        root.mkdir()
        (root / "GEMINI.md").write_bytes(b"gemini authored\n")
        (root / "CLAUDE.md").write_bytes(b"claude authored\n")
        config_path = root / CONFIG_NAME

        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ) as create,
            mock.patch.object(
                cli_module,
                "preflight_precommit",
                side_effect=AssertionError("--no-hook touched pre-commit"),
            ),
        ):
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=(),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_OK,
            )

        loaded = load_config(root, config_path)
        expected = dataclasses.replace(
            default_config(),
            agents=("claude", "gemini"),
        )
        self.assertEqual(loaded, expected)
        self.assertEqual(config_path.read_bytes(), canonical_config_bytes(expected))
        create.assert_called_once()
        self.assertEqual(create.call_args.args, (root.resolve(), expected))

    def test_explicit_agents_are_fixed_order_and_invalid_selections_write_nothing(
        self,
    ) -> None:
        root = self.base / "explicit"
        root.mkdir()
        config_path = root / CONFIG_NAME
        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ):
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=("gemini", "claude"),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_OK,
            )
        self.assertEqual(
            load_config(root, config_path).agents,
            ("claude", "gemini"),
        )

        for name, agents in (
            ("none", ()),
            ("duplicate", ("codex", "codex")),
            ("unknown", ("copilot",)),
        ):
            with self.subTest(name=name):
                candidate = self.base / name
                candidate.mkdir()
                selected = candidate / CONFIG_NAME
                with (
                    mock.patch.object(
                        cli_module,
                        "create_artifact",
                        side_effect=AssertionError("invalid selection built artifact"),
                    ) as create,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        command_init(
                            candidate,
                            selected,
                            agents=agents,
                            no_hook=True,
                            quiet=True,
                        ),
                        EXIT_USAGE,
                    )
                create.assert_not_called()
                self.assertFalse(selected.exists())

    def test_existing_config_is_preserved_and_explicit_agents_must_match(self) -> None:
        root, config_path, config = _configured_root(
            self.base,
            "existing",
            agents=("claude", "codex"),
            output="PROJECT_DIGEST.md",
        )
        config_path.write_bytes(config_path.read_bytes() + b"# authored formatting\n")
        before = _file_metadata(config_path)
        with mock.patch.object(
            cli_module,
            "create_artifact",
            side_effect=_artifact_factory,
        ):
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=("codex", "claude"),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_OK,
            )
        self.assertEqual(_file_metadata(config_path), before)
        self.assertEqual(load_config(root, config_path), config)

        before_tree = _file_metadata(config_path)
        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=AssertionError("mismatched config built artifact"),
            ) as create,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=("gemini",),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_USAGE,
            )
        create.assert_not_called()
        self.assertEqual(_file_metadata(config_path), before_tree)

    def test_incomplete_or_malformed_init_preflights_without_any_commit(self) -> None:
        incomplete = self.base / "incomplete-init"
        incomplete.mkdir()
        incomplete_config = incomplete / CONFIG_NAME
        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_incomplete_error(),
            ),
            mock.patch.object(
                cli_module,
                "commit_writes",
                side_effect=AssertionError("incomplete init committed"),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_init(
                    incomplete,
                    incomplete_config,
                    agents=("codex",),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_INCOMPLETE,
            )
        self.assertFalse(incomplete_config.exists())
        self.assertFalse((incomplete / "AGENTS.md").exists())

        malformed = self.base / "malformed-init"
        malformed.mkdir()
        malformed_config = malformed / CONFIG_NAME
        context = malformed / "CLAUDE.md"
        context.write_bytes(CONTEXT_START + b"\n")
        before = _file_metadata(context)
        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ),
            mock.patch.object(
                cli_module,
                "commit_writes",
                side_effect=AssertionError("malformed init committed"),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_init(
                    malformed,
                    malformed_config,
                    agents=("claude",),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_STALE,
            )
        self.assertFalse(malformed_config.exists())
        self.assertEqual(_file_metadata(context), before)

    def test_no_hook_calls_no_hook_or_git_api_and_repeat_is_metadata_stable(
        self,
    ) -> None:
        root = self.base / "no-hook"
        root.mkdir()
        config_path = root / CONFIG_NAME
        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ) as create,
            mock.patch.object(
                cli_module,
                "render_precommit_command",
                side_effect=AssertionError("--no-hook rendered hook"),
            ),
            mock.patch.object(
                cli_module,
                "preflight_precommit",
                side_effect=AssertionError("--no-hook preflighted hook"),
            ),
        ):
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=("codex",),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_OK,
            )
            targets = (
                config_path,
                root / "AGENTS.md",
                root / "PROJECT_DIGEST.md",
            )
            before = {path: _file_metadata(path) for path in targets}
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=("codex",),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_OK,
            )
        self.assertEqual({path: _file_metadata(path) for path in targets}, before)
        self.assertEqual(create.call_count, 2)

    def test_hook_init_preflights_everything_then_commits_root_before_hooks(
        self,
    ) -> None:
        root = self.base / "hooked"
        _git_init(root)
        config_path = root / CONFIG_NAME
        events: list[str] = []
        real_artifact_preflight = cli_module._preflight_artifact_writes
        real_precommit = cli_module.preflight_precommit
        real_commit = cli_module.commit_writes

        def artifact_preflight(*args: object, **kwargs: object) -> object:
            events.append("artifact-preflight")
            return real_artifact_preflight(*args, **kwargs)  # type: ignore[arg-type]

        def precommit(*args: object, **kwargs: object) -> object:
            events.append("precommit-preflight")
            return real_precommit(*args, **kwargs)  # type: ignore[arg-type]

        committed: list[tuple[Path, ...]] = []

        def commit(plans: object) -> tuple[Path, ...]:
            events.append("commit")
            owned: tuple[PlannedWrite, ...] = tuple(plans)  # type: ignore[arg-type]
            committed.append(tuple(plan.path for plan in owned))
            return real_commit(owned)

        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ) as create,
            mock.patch.object(
                cli_module,
                "_preflight_artifact_writes",
                side_effect=artifact_preflight,
            ),
            mock.patch.object(
                cli_module,
                "preflight_precommit",
                side_effect=precommit,
            ),
            mock.patch.object(cli_module, "commit_writes", side_effect=commit),
        ):
            self.assertEqual(
                command_init(
                    root,
                    config_path,
                    agents=("codex",),
                    no_hook=False,
                    quiet=True,
                ),
                EXIT_OK,
            )

        self.assertEqual(
            events,
            [
                "artifact-preflight",
                "precommit-preflight",
                "commit",
                "commit",
            ],
        )
        self.assertEqual(len(committed), 2)
        self.assertTrue(all("hooks" not in path.parts for path in committed[0]))
        self.assertTrue(all("hooks" in path.parts for path in committed[1]))
        self.assertEqual(create.call_count, 1)
        hook = root / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook.exists())
        self.assertIn(b" check ", hook.read_bytes())
        self.assertNotIn(b" build ", hook.read_bytes())

    def test_external_config_requires_no_hook(self) -> None:
        root = self.base / "external-init"
        root.mkdir()
        external = self.base / "external-init.toml"
        config = dataclasses.replace(default_config(), agents=("codex",))
        external.write_bytes(canonical_config_bytes(config))

        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=_artifact_factory,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_init(
                    root,
                    external,
                    agents=(),
                    no_hook=False,
                    quiet=True,
                ),
                EXIT_USAGE,
            )
            self.assertEqual(
                command_init(
                    root,
                    external,
                    agents=(),
                    no_hook=True,
                    quiet=True,
                ),
                EXIT_OK,
            )
        self.assertEqual(external.read_bytes(), canonical_config_bytes(config))

    def test_external_hook_requirement_precedes_artifact_creation(self) -> None:
        root = self.base / "external-precedence"
        root.mkdir()
        external = self.base / "external-precedence.toml"
        config = dataclasses.replace(default_config(), agents=("codex",))
        external.write_bytes(canonical_config_bytes(config))

        with (
            mock.patch.object(
                cli_module,
                "create_artifact",
                side_effect=AssertionError(
                    "external hook configuration built artifact"
                ),
            ) as create,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_init(
                    root,
                    external,
                    agents=(),
                    no_hook=False,
                    quiet=True,
                ),
                EXIT_USAGE,
            )
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
