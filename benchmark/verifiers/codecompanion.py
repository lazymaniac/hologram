from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .common import (
    Verification,
    changed_paths,
    render_verification,
    verify_navigation_answer,
)

_RUBRIC = Path(__file__).with_name("rubrics") / "codecompanion.json"
_READ_FILE = "lua/codecompanion/interactions/chat/tools/builtin/read_file.lua"
_READ_FILE_TEST = "tests/interactions/chat/tools/builtin/test_read_file.lua"
_READ_FILE_PATHS = frozenset({_READ_FILE, _READ_FILE_TEST})


def verify_file_edited_lifecycle(workspace: Path, answer: Path) -> Verification:
    return verify_navigation_answer(
        workspace,
        answer,
        task_name="file-edited-lifecycle",
        rubric_path=_RUBRIC,
    )


def verify_move_file_plan(workspace: Path, answer: Path) -> Verification:
    return verify_navigation_answer(
        workspace,
        answer,
        task_name="move-file-plan",
        rubric_path=_RUBRIC,
    )


def verify_duplicate_unused_audit(workspace: Path, answer: Path) -> Verification:
    return verify_navigation_answer(
        workspace,
        answer,
        task_name="duplicate-unused-audit",
        rubric_path=_RUBRIC,
    )


def _property_is_integer(source: str, name: str) -> bool:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*\{{(?P<body>.*?)\}}",
        source,
        flags=re.DOTALL,
    )
    return (
        match is not None
        and re.search(r'\btype\s*=\s*["\']integer["\']', match.group("body"))
        is not None
    )


def _has_integer_guard(source: str, name: str) -> bool:
    escaped = re.escape(name)
    patterns = (
        rf"\b{escaped}\s*%\s*1\s*~=\s*0\b",
        rf"\bmath\.floor\(\s*{escaped}\s*\)\s*~=\s*{escaped}\b",
        rf"\b{escaped}\s*~=\s*math\.floor\(\s*{escaped}\s*\)",
    )
    return any(re.search(pattern, source) is not None for pattern in patterns)


def _has_fraction_case(tests: str, name: str) -> bool:
    return (
        re.search(
            rf"\b{re.escape(name)}\b[^\n]*[-+]?\d+\.\d+",
            tests,
        )
        is not None
    )


def _static_integer_range_checks(workspace: Path) -> tuple[str, ...]:
    diagnostics: list[str] = []
    try:
        source = (workspace / _READ_FILE).read_text(encoding="utf-8")
        tests = (workspace / _READ_FILE_TEST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return (f"cannot read the read-file implementation or focused test: {error}",)

    for property_name in (
        "start_line_number_base_zero",
        "end_line_number_base_zero",
    ):
        if not _property_is_integer(source, property_name):
            diagnostics.append(f"{property_name} schema type must be integer")
    for variable in ("start_line_zero", "end_line_zero"):
        if not _has_integer_guard(source, variable):
            diagnostics.append(f"runtime must reject fractional {variable} values")

    preserved = (
        "start_line_zero < 0",
        "end_line_zero < -1",
        "start_line_zero >= #lines",
        "end_line_zero ~= -1",
        "start_line_zero > end_line_zero",
        "math.max(0, #lines - 1)",
    )
    for expression in preserved:
        if expression not in source:
            diagnostics.append(f"existing range behavior is missing: {expression}")

    if len(re.findall(r"\blocal\s+function\s+extract_range\s*\(", source)) != 1:
        diagnostics.append("the canonical extract_range function must remain unique")
    if len(re.findall(r"\bextract_range\s*\(\s*args\s*,", source)) != 1:
        diagnostics.append("the read command must continue to call extract_range")
    parallel = {
        name
        for name in re.findall(r"\blocal\s+function\s+([A-Za-z_][A-Za-z0-9_]*)", source)
        if name != "extract_range" and re.search(r"range|slice", name, re.IGNORECASE)
    }
    if parallel:
        diagnostics.append("a parallel range parser was introduced")

    for property_name in (
        "start_line_number_base_zero",
        "end_line_number_base_zero",
    ):
        if not _has_fraction_case(tests, property_name):
            diagnostics.append(f"focused tests omit fractional {property_name}")
    return tuple(diagnostics)


def _check_command(argv: tuple[str, ...], workspace: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{' '.join(argv)} failed to run: {error}"
    if completed.returncode == 0:
        return True, ""
    detail = (completed.stderr or completed.stdout or "").strip()
    if len(detail) > 1000:
        detail = detail[-1000:]
    return False, f"{' '.join(argv)} failed: {detail or completed.returncode}"


def verify_read_file_integer_ranges(workspace: Path, answer: Path) -> Verification:
    del answer
    selected = Path(workspace)
    diagnostics: list[str] = []
    try:
        paths = changed_paths(selected)
    except (OSError, ValueError) as error:
        return Verification(False, 0.0, (str(error),))
    if paths != _READ_FILE_PATHS:
        diagnostics.append(
            "changes must be limited to the read-file implementation and focused test"
        )
    diagnostics.extend(_static_integer_range_checks(selected))
    if diagnostics:
        return Verification(False, 0.0, tuple(diagnostics))

    commands = (
        ("make", "test_file", f"FILE={_READ_FILE_TEST}"),
        ("stylua", "--check", "."),
        ("git", "diff", "--check"),
        ("make", "test"),
    )
    for command in commands:
        passed, diagnostic = _check_command(command, selected)
        if not passed:
            diagnostics.append(diagnostic)
    if diagnostics:
        return Verification(False, 0.0, tuple(diagnostics))
    return Verification(True, 1.0, ())


_VERIFIERS = {
    "file-edited-lifecycle": verify_file_edited_lifecycle,
    "read-file-integer-ranges": verify_read_file_integer_ranges,
    "move-file-plan": verify_move_file_plan,
    "duplicate-unused-audit": verify_duplicate_unused_audit,
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) not in {2, 3} or arguments[0] not in _VERIFIERS:
        result = Verification(False, 0.0, ("invalid CodeCompanion verifier arguments",))
    else:
        task = arguments[0]
        workspace = Path(arguments[1])
        answer = Path(arguments[2]) if len(arguments) == 3 else workspace / ".unused"
        if task != "read-file-integer-ranges" and len(arguments) != 3:
            result = Verification(
                False, 0.0, ("navigation verifier requires an answer",)
            )
        elif task == "read-file-integer-ranges" and len(arguments) != 2:
            result = Verification(
                False,
                0.0,
                ("implementation verifier does not accept an answer",),
            )
        else:
            result = _VERIFIERS[task](workspace, answer)
    print(render_verification(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "Verification",
    "main",
    "verify_duplicate_unused_audit",
    "verify_file_edited_lifecycle",
    "verify_move_file_plan",
    "verify_read_file_integer_ranges",
)
