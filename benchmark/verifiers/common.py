from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import cast

_EVIDENCE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


@dataclass(frozen=True)
class Verification:
    passed: bool
    score: float
    diagnostics: tuple[str, ...]


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value}")


def _json_object(raw: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, _DuplicateKey, ValueError) as error:
        raise ValueError(f"{label} is not strict JSON: {error}") from error
    if type(value) is not dict:
        raise ValueError(f"{label} must be one JSON object")
    return cast(dict[str, object], value)


def _exact_object(
    value: object,
    label: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    result = cast(dict[str, object], value)
    if set(result) != fields:
        raise ValueError(
            f"{label} fields must equal {sorted(fields)!r}; found {sorted(result)!r}"
        )
    return result


def _text(value: object, label: str) -> str:
    if type(value) is not str or not cast(str, value).strip():
        raise ValueError(f"{label} must be a nonblank string")
    result = cast(str, value)
    if "\x00" in result:
        raise ValueError(f"{label} contains NUL")
    return result


def _string_list(value: object, label: str) -> list[str]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array")
    result = [_text(item, f"{label} item") for item in cast(list[object], value)]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _git(workspace: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=False,
        capture_output=True,
        timeout=60,
        env=_environment(),
    )
    if completed.returncode != 0:
        raise ValueError("benchmark verifier could not inspect the Git worktree")
    return completed.stdout


def clean_worktree(workspace: Path) -> bool:
    return not _git(
        Path(workspace),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )


def _paths(raw: bytes, label: str) -> set[str]:
    result: set[str] = set()
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            path = item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"{label} contains a non-UTF-8 path") from error
        result.add(path)
    return result


def changed_paths(workspace: Path) -> frozenset[str]:
    selected = Path(workspace)
    tracked = _paths(
        _git(selected, "diff", "--name-only", "-z", "HEAD", "--"),
        "tracked changes",
    )
    untracked = _paths(
        _git(selected, "ls-files", "--others", "--exclude-standard", "-z"),
        "untracked changes",
    )
    return frozenset(tracked | untracked)


def parse_verifier_output(stdout: str, returncode: int) -> Verification:
    try:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise ValueError("verifier emitted no result")
        data = _exact_object(
            _json_object(lines[-1], "verifier result"),
            "verifier result",
            frozenset({"passed", "score", "diagnostics"}),
        )
        if type(data["passed"]) is not bool:
            raise ValueError("verifier passed must be boolean")
        passed = cast(bool, data["passed"])
        score_value = data["score"]
        if type(score_value) not in {int, float}:
            raise ValueError("verifier score must be numeric")
        score = float(cast(int | float, score_value))
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("verifier score must be finite and between zero and one")
        diagnostics = tuple(_string_list(data["diagnostics"], "diagnostics"))
        if passed and returncode != 0:
            raise ValueError("passing verifier exited nonzero")
        if not passed and returncode == 0:
            raise ValueError("failing verifier exited zero")
        if not passed and not diagnostics:
            raise ValueError("failing verifier must provide diagnostics")
        return Verification(passed, score, diagnostics)
    except (KeyError, TypeError, ValueError) as error:
        return Verification(False, 0.0, (f"malformed verifier result: {error}",))


def render_verification(result: Verification) -> str:
    return json.dumps(
        {
            "passed": result.passed,
            "score": result.score,
            "diagnostics": list(result.diagnostics),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _safe_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or value != path.as_posix()
        or "\\" in value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    return value


def _fraction(value: object, label: str) -> Fraction:
    text = _text(value, label)
    try:
        result = Fraction(text)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError(f"{label} must be a rational number") from error
    if not 0 < result <= 1:
        raise ValueError(f"{label} must be in (0, 1]")
    return result


def load_rubrics(path: Path) -> dict[str, dict[str, object]]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read verifier rubrics: {error}") from error
    root = _exact_object(
        _json_object(raw, "verifier rubrics"),
        "verifier rubrics",
        frozenset({"tasks"}),
    )
    if type(root["tasks"]) is not dict or not root["tasks"]:
        raise ValueError("verifier rubrics tasks must be a nonempty object")

    tasks: dict[str, dict[str, object]] = {}
    for task_name, raw_task in cast(dict[str, object], root["tasks"]).items():
        task = _exact_object(
            raw_task,
            f"rubric task {task_name}",
            frozenset({"pass_score", "claims", "forbidden_terms"}),
        )
        pass_score = _fraction(task["pass_score"], f"{task_name} pass_score")
        forbidden = tuple(
            _string_list(task["forbidden_terms"], f"{task_name} forbidden_terms")
        )
        if type(task["claims"]) is not dict or not task["claims"]:
            raise ValueError(f"{task_name} claims must be a nonempty object")
        claims: dict[str, object] = {}
        total = Fraction(0)
        for claim_name, raw_claim in cast(dict[str, object], task["claims"]).items():
            claim = _exact_object(
                raw_claim,
                f"{task_name}.{claim_name}",
                frozenset(
                    {
                        "weight",
                        "evidence",
                        "allowed_symbols",
                        "required_terms",
                        "forbidden_terms",
                    }
                ),
            )
            weight = _fraction(claim["weight"], f"{task_name}.{claim_name} weight")
            total += weight
            if type(claim["evidence"]) is not list or not claim["evidence"]:
                raise ValueError(f"{task_name}.{claim_name} evidence must be nonempty")
            evidence: list[dict[str, str]] = []
            for index, raw_evidence in enumerate(cast(list[object], claim["evidence"])):
                item = _exact_object(
                    raw_evidence,
                    f"{task_name}.{claim_name} evidence {index + 1}",
                    frozenset({"path", "anchor"}),
                )
                evidence.append(
                    {
                        "path": _safe_relative(
                            _text(item["path"], "rubric evidence path"),
                            "rubric evidence path",
                        ),
                        "anchor": _text(item["anchor"], "rubric evidence anchor"),
                    }
                )
            if len({(item["path"], item["anchor"]) for item in evidence}) != len(
                evidence
            ):
                raise ValueError(f"{task_name}.{claim_name} evidence is duplicated")
            allowed_symbols = tuple(
                _string_list(
                    claim["allowed_symbols"],
                    f"{task_name}.{claim_name} allowed_symbols",
                )
            )
            if not allowed_symbols:
                raise ValueError(
                    f"{task_name}.{claim_name} allowed_symbols must be nonempty"
                )
            if type(claim["required_terms"]) is not list:
                raise ValueError(
                    f"{task_name}.{claim_name} required_terms must be an array"
                )
            required_terms: list[tuple[str, ...]] = []
            for group in cast(list[object], claim["required_terms"]):
                required_terms.append(
                    tuple(_string_list(group, "required term alternative group"))
                )
                if not required_terms[-1]:
                    raise ValueError("required term alternative group must be nonempty")
            claims[claim_name] = {
                "weight": weight,
                "evidence": evidence,
                "allowed_symbols": allowed_symbols,
                "required_terms": tuple(required_terms),
                "forbidden_terms": tuple(
                    _string_list(
                        claim["forbidden_terms"],
                        f"{task_name}.{claim_name} forbidden_terms",
                    )
                ),
            }
        if total != 1:
            raise ValueError(f"{task_name} rubric weights must sum to 1")
        tasks[task_name] = {
            "pass_score": pass_score,
            "claims": claims,
            "forbidden_terms": forbidden,
        }
    return tasks


def _read_answer(path: Path) -> dict[str, object]:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read final answer: {error}") from error
    if len(raw) > 2_000_000:
        raise ValueError("final answer is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("final answer is not UTF-8") from error
    return _json_object(text, "final answer")


def verify_navigation_answer(
    workspace: Path,
    answer: Path,
    *,
    task_name: str,
    rubric_path: Path,
) -> Verification:
    diagnostics: list[str] = []
    try:
        selected = Path(workspace).resolve(strict=True)
        if not clean_worktree(selected):
            raise ValueError("navigation verifier requires a clean worktree")
        task_rubric = load_rubrics(rubric_path).get(task_name)
        if task_rubric is None:
            raise ValueError(f"missing rubric for {task_name}")
        data = _exact_object(
            _read_answer(answer),
            "final answer",
            frozenset({"task", "claims", "evidence"}),
        )
        if data["task"] != task_name:
            raise ValueError(f"final answer task must equal {task_name!r}")
        expected_claims = cast(dict[str, object], task_rubric["claims"])
        claims = _exact_object(
            data["claims"],
            "final answer claims",
            frozenset(expected_claims),
        )
        if type(data["evidence"]) is not list:
            raise ValueError("final answer evidence must be an array")

        evidence_by_id: dict[str, tuple[str, str]] = {}
        for index, raw_evidence in enumerate(cast(list[object], data["evidence"])):
            item = _exact_object(
                raw_evidence,
                f"final answer evidence {index + 1}",
                frozenset({"id", "path", "line", "anchor"}),
            )
            evidence_id = _text(item["id"], "evidence id")
            if _EVIDENCE_ID.fullmatch(evidence_id) is None:
                raise ValueError(f"invalid evidence id {evidence_id!r}")
            if evidence_id in evidence_by_id:
                raise ValueError(f"duplicate evidence id {evidence_id!r}")
            relative = _safe_relative(
                _text(item["path"], "evidence path"), "evidence path"
            )
            line_value = item["line"]
            if type(line_value) is not int or cast(int, line_value) < 1:
                raise ValueError("evidence line must be a positive integer")
            anchor = _text(item["anchor"], "evidence anchor")
            source = selected.joinpath(*PurePosixPath(relative).parts)
            try:
                resolved_source = source.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(
                    f"evidence source does not exist: {relative}"
                ) from error
            if not resolved_source.is_relative_to(selected) or not source.is_file():
                raise ValueError(f"evidence source is unsafe: {relative}")
            try:
                lines = source.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as error:
                raise ValueError(f"cannot read evidence source: {relative}") from error
            line = cast(int, line_value)
            if line > len(lines):
                raise ValueError(f"evidence line does not exist: {relative}:{line}")
            if anchor not in lines[line - 1]:
                raise ValueError(f"evidence anchor is absent: {relative}:{line}")
            evidence_by_id[evidence_id] = (relative, anchor)

        used: set[str] = set()
        score = Fraction(0)
        all_explanations: list[str] = []
        for claim_name, raw_rubric_claim in expected_claims.items():
            rubric_claim = cast(dict[str, object], raw_rubric_claim)
            claim = _exact_object(
                claims[claim_name],
                f"claim {claim_name}",
                frozenset({"explanation", "evidence"}),
            )
            explanation = _text(claim["explanation"], f"claim {claim_name} explanation")
            all_explanations.append(explanation)
            ids = _string_list(claim["evidence"], f"claim {claim_name} evidence")
            actual: list[tuple[str, str]] = []
            for evidence_id in ids:
                if evidence_id not in evidence_by_id:
                    raise ValueError(
                        f"claim {claim_name} references unknown evidence {evidence_id!r}"
                    )
                used.add(evidence_id)
                actual.append(evidence_by_id[evidence_id])
            required = [
                (item["path"], item["anchor"])
                for item in cast(list[dict[str, str]], rubric_claim["evidence"])
            ]
            claim_errors: list[str] = []
            if sorted(actual) != sorted(required):
                claim_errors.append(
                    f"claim {claim_name} does not cite its exact required evidence"
                )
            folded = explanation.casefold()
            if not any(
                symbol.casefold() in folded
                for symbol in cast(tuple[str, ...], rubric_claim["allowed_symbols"])
            ):
                claim_errors.append(
                    f"claim {claim_name} does not identify an allowed symbol"
                )
            for alternatives in cast(
                tuple[tuple[str, ...], ...], rubric_claim["required_terms"]
            ):
                if not any(term.casefold() in folded for term in alternatives):
                    claim_errors.append(
                        f"claim {claim_name} omits a required relationship"
                    )
                    break
            for forbidden in cast(tuple[str, ...], rubric_claim["forbidden_terms"]):
                if forbidden.casefold() in folded:
                    claim_errors.append(
                        f"claim {claim_name} asserts a forbidden conclusion"
                    )
                    break
            if claim_errors:
                diagnostics.extend(claim_errors)
            else:
                score += cast(Fraction, rubric_claim["weight"])

        if used != set(evidence_by_id):
            diagnostics.append("final answer contains unreferenced evidence")
        joined = "\n".join(all_explanations).casefold()
        for forbidden in cast(tuple[str, ...], task_rubric["forbidden_terms"]):
            if forbidden.casefold() in joined:
                diagnostics.append("final answer proposes a forbidden replacement")
                if task_name == "move-file-plan":
                    score = min(score, Fraction(89, 100))
                break
        passed = not diagnostics and score >= cast(Fraction, task_rubric["pass_score"])
        if not passed and not diagnostics:
            diagnostics.append("verifier score is below the passing threshold")
        return Verification(passed, float(score), tuple(diagnostics))
    except (KeyError, OSError, TypeError, ValueError) as error:
        return Verification(False, 0.0, (str(error),))


__all__ = (
    "Verification",
    "changed_paths",
    "clean_worktree",
    "load_rubrics",
    "parse_verifier_output",
    "render_verification",
    "verify_navigation_answer",
)
