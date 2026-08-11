from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import hologram.hooks as hooks_module
from hologram.context import PlannedWrite, commit_writes
from hologram.hooks import (
    HOOK_END,
    HOOK_START,
    UnsupportedHookError,
    preflight_precommit,
    render_precommit_command,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", os.fspath(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(base: Path, name: str = "repo") -> Path:
    repo = base / name
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Hologram Test")
    _git(repo, "config", "user.email", "hologram@example.invalid")
    return repo


def _hook_path(repo: Path, name: str = "pre-commit") -> Path:
    result = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    return Path(result.stdout.strip()) / name


def _install(repo: Path, command: bytes) -> PlannedWrite:
    plan = preflight_precommit(repo, command)
    commit_writes((plan,))
    return plan


def _snapshot_file(path: Path) -> tuple[int, int, int, bytes]:
    metadata = path.stat()
    return (
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_mtime_ns,
        path.read_bytes(),
    )


class PreCommitHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = _git_repo(self.base)
        self.config = self.repo / ".hologram.toml"
        self.python = self.base / "python launcher"

    def test_exact_public_hook_surface(self) -> None:
        self.assertEqual(HOOK_START, b"# hologram:start")
        self.assertEqual(HOOK_END, b"# hologram:end")
        self.assertTrue(issubclass(UnsupportedHookError, ValueError))
        self.assertTrue(callable(render_precommit_command))
        self.assertTrue(callable(preflight_precommit))
        self.assertEqual(
            hooks_module.__all__,
            [
                "HOOK_END",
                "HOOK_START",
                "UnsupportedHookError",
                "preflight_precommit",
                "render_precommit_command",
            ],
        )

    def test_render_command_is_exact_dynamic_quoted_and_read_only(self) -> None:
        root = self.base / "repo with $dollar;quote'"
        root.mkdir()
        config = root / "config dir" / 'holo$"gram.toml'
        python = self.base / "python launcher's $path"

        rendered = render_precommit_command(
            root=root,
            config_path=config,
            python=python,
        )

        escaped_config = r"config dir/holo\$\"gram.toml"
        self.assertEqual(
            rendered,
            HOOK_START
            + b"\n"
            + b"hologram_root=$(git rev-parse --show-toplevel) || exit $?\n"
            + (
                f"{shlex.quote(os.fspath(python))} -B -m hologram check "
                f'--root "$hologram_root" '
                f'--config "$hologram_root/{escaped_config}" '
                "--quiet || exit $?\n"
            ).encode()
            + HOOK_END
            + b"\n",
        )
        self.assertNotIn(b" build ", rendered)
        self.assertNotIn(b"|| true", rendered)

    def test_render_rejects_external_multiline_nul_and_wrong_types(self) -> None:
        outside = self.base / "outside.toml"
        cases: tuple[tuple[object, object, object, object], ...] = (
            (self.repo, outside, self.python, "hologram"),
            (self.repo, self.config, Path("relative-python"), "hologram"),
            (Path("relative-root"), self.config, self.python, "hologram"),
            (self.repo / "line\nbreak", self.config, self.python, "hologram"),
            (self.repo, self.repo / "bad\nconfig", self.python, "hologram"),
            (self.repo, self.config, self.base / "bad\x00python", "hologram"),
            (self.repo, self.config, self.python, "bad\nmodule"),
            ("repo", self.config, self.python, "hologram"),
            (self.repo, "config", self.python, "hologram"),
            (self.repo, self.config, "python", "hologram"),
            (self.repo, self.config, self.python, 1),
        )
        for root, config, python, module in cases:
            with (
                self.subTest(root=root, config=config, python=python, module=module),
                self.assertRaises((TypeError, ValueError, UnsupportedHookError)),
            ):
                render_precommit_command(
                    root=cast(Path, root),
                    config_path=cast(Path, config),
                    python=cast(Path, python),
                    module=cast(str, module),
                )

    def test_missing_hook_plan_is_exact_executable_and_preflight_only(self) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)

        plan = preflight_precommit(self.repo, command)

        self.assertEqual(plan.path, hook)
        self.assertEqual(plan.content, b"#!/bin/sh\n" + command)
        self.assertEqual(plan.mode, 0o755)
        self.assertFalse(hook.exists())

    def test_existing_supported_shells_preserve_bytes_mode_and_reposition_block(
        self,
    ) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)
        shells = (
            b"#!/bin/sh\n",
            b"#!/usr/local/bin/bash -e\n",
            b"#!/bin/zsh\n",
            b"#!/usr/bin/env sh\n",
            b"#!/usr/bin/env bash\n",
            b"#!/usr/bin/env zsh\n",
        )
        for index, shebang in enumerate(shells):
            with self.subTest(shebang=shebang):
                old_block = HOOK_START + b"\nold command\n" + HOOK_END + b"\n"
                authored = b"echo before\n\xffraw\r\necho after\n"
                hook.write_bytes(shebang + authored[:12] + old_block + authored[12:])
                mode = 0o700 | (index & 0o7)
                hook.chmod(mode)

                plan = preflight_precommit(self.repo, command)

                self.assertEqual(plan.content, shebang + command + authored)
                self.assertEqual(plan.mode, mode)
                self.assertEqual(
                    hook.read_bytes(),
                    shebang + authored[:12] + old_block + authored[12:],
                )

    def test_existing_shebang_without_newline_gets_a_line_terminator(self) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)
        hook.write_bytes(b"#!/bin/sh")
        hook.chmod(0o755)

        plan = preflight_precommit(self.repo, command)

        self.assertEqual(plan.content, b"#!/bin/sh\n" + command)

    def test_hook_install_is_idempotent_and_preserves_metadata(self) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)
        hook.write_bytes(b"#!/bin/sh\necho authored\n")
        hook.chmod(0o751)

        first = _install(self.repo, command)
        before = _snapshot_file(hook)
        second = preflight_precommit(self.repo, command)
        changed = commit_writes((second,))

        self.assertEqual(first.content, b"#!/bin/sh\n" + command + b"echo authored\n")
        self.assertEqual(changed, ())
        self.assertEqual(_snapshot_file(hook), before)
        self.assertEqual(hook.read_bytes().count(HOOK_START), 1)

    def test_inline_markers_are_authored_bytes_and_get_one_managed_block(self) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)
        authored = (
            b"#!/bin/sh\n"
            b"printf 'inline # hologram:start remains'\n"
            b"printf 'inline # hologram:end remains'\n"
        )
        hook.write_bytes(authored)
        hook.chmod(0o755)

        plan = preflight_precommit(self.repo, command)

        self.assertEqual(plan.content, b"#!/bin/sh\n" + command + authored[10:])
        self.assertEqual(plan.content.count(HOOK_START), 2)

    def test_malformed_markers_unsupported_shell_and_nonexec_are_rejected(
        self,
    ) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)
        malformed = (
            b"#!/bin/sh\n" + HOOK_START + b"\n",
            b"#!/bin/sh\n" + HOOK_END + b"\n" + HOOK_START + b"\n",
            b"#!/bin/sh\n" + HOOK_START + b"\n" + HOOK_START + b"\n" + HOOK_END + b"\n",
            b"#!/bin/sh\n" + HOOK_START + b"\n" + HOOK_END + b"\n" + HOOK_END + b"\n",
        )
        for content in malformed:
            with self.subTest(content=content):
                hook.write_bytes(content)
                hook.chmod(0o755)
                with self.assertRaises(UnsupportedHookError):
                    preflight_precommit(self.repo, command)

        for shebang in (
            b"#!/usr/bin/python\n",
            b"#!/usr/bin/env fish\n",
            b"echo no shebang\n",
        ):
            with self.subTest(shebang=shebang):
                hook.write_bytes(shebang)
                hook.chmod(0o755)
                with self.assertRaisesRegex(UnsupportedHookError, "--no-hook"):
                    preflight_precommit(self.repo, command)

        hook.write_bytes(b"#!/bin/sh\necho authored\n")
        hook.chmod(0o644)
        with self.assertRaisesRegex(UnsupportedHookError, "--no-hook"):
            preflight_precommit(self.repo, command)

    def test_preflight_rejects_non_top_level_and_bad_command_without_writes(
        self,
    ) -> None:
        nested = self.repo / "nested"
        nested.mkdir()
        hook = _hook_path(self.repo)
        for command in (
            b"not a block",
            HOOK_START + b"\n" + HOOK_START + b"\n" + HOOK_END + b"\n",
            HOOK_START + b"\ncommand\n" + HOOK_END,
        ):
            with (
                self.subTest(command=command),
                self.assertRaises(UnsupportedHookError),
            ):
                preflight_precommit(self.repo, command)
        valid = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        with self.assertRaises(UnsupportedHookError):
            preflight_precommit(nested, valid)
        self.assertFalse(hook.exists())

    def test_repo_and_global_relative_hooks_paths_are_allowed_but_shared_is_rejected(
        self,
    ) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        _git(self.repo, "config", "core.hooksPath", ".repo-hooks")
        local_hook = _hook_path(self.repo)

        plan = preflight_precommit(self.repo, command)

        self.assertEqual(plan.path, local_hook)
        self.assertEqual(
            local_hook,
            (self.repo / ".repo-hooks" / "pre-commit").resolve(strict=False),
        )

        _git(self.repo, "config", "--unset", "core.hooksPath")
        global_config = self.base / "global.gitconfig"
        shared = self.base / "shared-hooks"
        environment = {"GIT_CONFIG_GLOBAL": os.fspath(global_config)}
        with mock.patch.dict(os.environ, environment):
            subprocess.run(
                ("git", "config", "--global", "core.hooksPath", ".repo-global-hooks"),
                check=True,
            )
            relative_plan = preflight_precommit(self.repo, command)
            self.assertEqual(
                relative_plan.path,
                (self.repo / ".repo-global-hooks" / "pre-commit").resolve(strict=False),
            )
            subprocess.run(
                ("git", "config", "--global", "core.hooksPath", os.fspath(shared)),
                check=True,
            )
            with self.assertRaisesRegex(UnsupportedHookError, "global"):
                preflight_precommit(self.repo, command)
        self.assertFalse(shared.exists())

    def test_spaced_global_config_origin_cannot_hide_an_absolute_hooks_path(
        self,
    ) -> None:
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        global_config = self.base / "global config"
        absolute_hooks = self.repo / "absolute-global-hooks"
        with mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": os.fspath(global_config)},
        ):
            subprocess.run(
                (
                    "git",
                    "config",
                    "--global",
                    "core.hooksPath",
                    os.fspath(absolute_hooks),
                ),
                check=True,
            )

            with self.assertRaisesRegex(UnsupportedHookError, "global"):
                preflight_precommit(self.repo, command)

        self.assertFalse(absolute_hooks.exists())

    def test_linked_worktree_uses_shared_default_hooks_and_allows_worktree_local(
        self,
    ) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("tracked\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "--quiet", "-m", "base")
        linked = self.base / "linked worktree"
        _git(
            self.repo,
            "worktree",
            "add",
            "--quiet",
            "-b",
            "linked-test",
            os.fspath(linked),
        )
        command = render_precommit_command(
            root=linked,
            config_path=linked / ".hologram.toml",
            python=self.python,
        )

        shared_plan = preflight_precommit(linked, command)

        self.assertEqual(shared_plan.path, _hook_path(self.repo))
        _git(self.repo, "config", "extensions.worktreeConfig", "true")
        _git(linked, "config", "--worktree", "core.hooksPath", ".linked-hooks")
        worktree_plan = preflight_precommit(linked, command)
        self.assertEqual(
            worktree_plan.path,
            (linked / ".linked-hooks" / "pre-commit").resolve(strict=False),
        )

    def test_executed_hook_uses_runtime_root_and_propagates_check_status(self) -> None:
        capture = self.base / "arguments.txt"
        authored_capture = self.base / "authored.txt"
        self.python.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$HOOK_CAPTURE"\nexit "$HOOK_STATUS"\n',
            encoding="utf-8",
        )
        self.python.chmod(0o755)
        command = render_precommit_command(
            root=self.repo,
            config_path=self.config,
            python=self.python,
        )
        hook = _hook_path(self.repo)
        hook.write_text(
            '#!/bin/sh\nprintf authored >> "$AUTHORED_CAPTURE"\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        _install(self.repo, command)
        before = _snapshot_file(hook)
        nested = self.repo / "nested"
        nested.mkdir()
        environment = {
            **os.environ,
            "HOOK_CAPTURE": os.fspath(capture),
            "AUTHORED_CAPTURE": os.fspath(authored_capture),
        }

        for status_code in (1, 2, 3, 127):
            with self.subTest(status=status_code):
                capture.unlink(missing_ok=True)
                authored_capture.unlink(missing_ok=True)
                result = subprocess.run(
                    (os.fspath(hook),),
                    cwd=nested,
                    env={**environment, "HOOK_STATUS": str(status_code)},
                    check=False,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, status_code)
                self.assertFalse(authored_capture.exists())
                arguments = capture.read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    arguments,
                    [
                        "-B",
                        "-m",
                        "hologram",
                        "check",
                        "--root",
                        os.fspath(self.repo.resolve()),
                        "--config",
                        os.fspath(self.config.resolve(strict=False)),
                        "--quiet",
                    ],
                )

        result = subprocess.run(
            (os.fspath(hook),),
            cwd=nested,
            env={**environment, "HOOK_STATUS": "0"},
            check=False,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(authored_capture.read_bytes(), b"authored")
        self.assertEqual(_snapshot_file(hook), before)


if __name__ == "__main__":
    unittest.main()
