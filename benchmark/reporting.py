from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

PRIVATE_GROUP_FIELDS = frozenset({"condition"})
PRIVATE_NUMERIC_FIELDS = frozenset(
    {
        "completed",
        "accepted",
        "rubric_score",
        "reads",
        "searches",
        "turns",
    }
)


@dataclass
class _PrivateTotals:
    runs: int = 0
    completed: int = 0
    accepted: int = 0
    rubric_score: Decimal = Decimal(0)
    exploration: int = 0
    turns: int = 0


def require_outside_worktree(
    path: Path,
    *,
    worktree: Path,
    label: str,
) -> Path:
    """Resolve *path* and reject either direction of worktree containment."""

    try:
        selected = Path(path).expanduser().resolve(strict=False)
        root = Path(worktree).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"{label} must be a safe path outside the worktree") from error
    if selected == root or selected.is_relative_to(root) or root.is_relative_to(selected):
        raise ValueError(f"{label} must be outside the Hologram worktree")
    return selected


def _count(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _score(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    if not result.is_finite() or result < 0 or result > 1:
        return None
    return result


def _decimal_text(value: Decimal) -> str:
    if not value:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _private_values(
    row: Mapping[str, object],
) -> tuple[str, bool, bool, Decimal, int, int, int] | None:
    condition = row.get("condition")
    completed = row.get("completed")
    accepted = row.get("accepted")
    score = _score(row.get("rubric_score"))
    reads = _count(row.get("reads"))
    searches = _count(row.get("searches"))
    turns = _count(row.get("turns"))
    if (
        row.get("visibility") != "private"
        or condition not in {"B", "C"}
        or type(completed) is not bool
        or type(accepted) is not bool
        or (accepted and not completed)
        or score is None
        or reads is None
        or searches is None
        or turns is None
    ):
        return None
    return str(condition), completed, accepted, score, reads, searches, turns


def private_report(rows: Sequence[Mapping[str, object]]) -> str:
    """Render private evidence as two numeric condition totals only."""

    totals = {condition: _PrivateTotals() for condition in ("B", "C")}
    invalid_rows = 0
    validated: list[tuple[str, bool, bool, Decimal, int, int, int]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            invalid_rows += 1
            continue
        values = _private_values(row)
        if values is None:
            invalid_rows += 1
            continue
        validated.append(values)
    if invalid_rows:
        raise ValueError(f"private report rejected {invalid_rows} invalid row(s)")

    for condition, completed, accepted, score, reads, searches, turns in validated:
        bucket = totals[condition]
        bucket.runs += 1
        bucket.completed += int(completed)
        bucket.accepted += int(accepted)
        bucket.rubric_score += score
        bucket.exploration += reads + searches
        bucket.turns += turns

    lines = [
        "# Private benchmark condition totals",
        "",
        (
            "| condition | runs | completed runs | accepted runs | "
            "rubric-score sum | exploration-call sum | turn sum |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("B", "C"):
        bucket = totals[condition]
        lines.append(
            f"| {condition} | {bucket.runs} | {bucket.completed} | "
            f"{bucket.accepted} | {_decimal_text(bucket.rubric_score)} | "
            f"{bucket.exploration} | {bucket.turns} |"
        )
    return "\n".join(lines) + "\n"


__all__ = (
    "PRIVATE_GROUP_FIELDS",
    "PRIVATE_NUMERIC_FIELDS",
    "private_report",
    "require_outside_worktree",
)
