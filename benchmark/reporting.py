from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import TypeAlias, cast

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


_PairKey: TypeAlias = tuple[
    str,
    str,
    str,
    int,
    int,
    str,
    str,
    int,
    int,
    str,
    str,
    str,
    str,
    str,
]
_Partition: TypeAlias = tuple[str, str, str, str]
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


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
    if (
        selected == root
        or selected.is_relative_to(root)
        or root.is_relative_to(selected)
    ):
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


def _text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    return value if type(value) is str and value.strip() else None


def _task_id(row: Mapping[str, object]) -> str | None:
    old = _text(row, "task")
    new = _text(row, "task_id")
    if old is not None and new is not None and old != new:
        return None
    return new or old


def _pair_key(row: Mapping[str, object]) -> _PairKey | None:
    strings = (
        _text(row, "visibility"),
        _text(row, "corpus_revision"),
        _task_id(row),
        _text(row, "model"),
        _text(row, "claude_code_version"),
        _text(row, "challenged_tree_sha256"),
        _text(row, "workspace_asset_sha256"),
        _text(row, "tier"),
        _text(row, "capability"),
        _text(row, "kind"),
    )
    rep = _count(row.get("rep"))
    pair_index = _count(row.get("pair_index"))
    max_turns = _count(row.get("max_turns"))
    seed = _count(row.get("seed"))
    if any(value is None for value in strings) or None in {
        rep,
        pair_index,
        max_turns,
        seed,
    }:
        return None
    text_values = tuple(cast(str, value) for value in strings)
    (
        visibility,
        revision,
        task_id,
        model,
        version,
        tree_hash,
        asset_hash,
        tier,
        capability,
        kind,
    ) = text_values
    assert rep is not None
    assert pair_index is not None
    assert max_turns is not None
    assert seed is not None
    if (
        visibility not in {"public", "private"}
        or _HEX40.fullmatch(revision) is None
        or _HEX64.fullmatch(tree_hash) is None
        or _HEX64.fullmatch(asset_hash) is None
        or tier not in {"simple", "complex"}
        or capability not in {"orientation", "planning", "implementation", "audit"}
        or kind not in {"navigate", "reuse"}
    ):
        return None
    return (
        visibility,
        revision,
        task_id,
        rep,
        pair_index,
        model,
        version,
        max_turns,
        seed,
        tree_hash,
        asset_hash,
        tier,
        capability,
        kind,
    )


def matched_pairs(
    rows: Sequence[Mapping[str, object]],
) -> tuple[tuple[dict, dict], ...]:
    """Return deterministic, structurally complete B/C pairs."""

    groups: dict[_PairKey, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, Mapping) or row.get("condition") not in {"B", "C"}:
            continue
        key = _pair_key(row)
        if key is not None:
            groups[key].append(row)
    result: list[tuple[dict, dict]] = []
    for key in sorted(groups):
        members = groups[key]
        if len(members) != 2:
            continue
        by_condition = {str(member.get("condition")): member for member in members}
        if set(by_condition) != {"B", "C"}:
            continue
        result.append((dict(by_condition["B"]), dict(by_condition["C"])))
    return tuple(result)


def _partition(row: Mapping[str, object]) -> _Partition:
    model = _text(row, "model")
    version = _text(row, "claude_code_version")
    tier = _text(row, "tier")
    capability = _text(row, "capability")
    if None in {model, version, tier, capability}:
        return "legacy", "unclassified", "unclassified", "unclassified"
    assert model is not None
    assert version is not None
    assert tier is not None
    assert capability is not None
    return model, version, tier, capability


def _partition_key(partition: _Partition) -> tuple[int, str, str, str, str]:
    model, version, tier, capability = partition
    legacy = int(not (model == "legacy" and version == "unclassified"))
    return legacy, model, version, tier, capability


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        percentage = 0
    else:
        percentage = int(
            (Decimal(numerator) * 100 / denominator).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
    return f"{numerator}/{denominator} ({percentage}%)"


def _metric(row: Mapping[str, object], key: str) -> Decimal | None:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result >= 0 else None


def _mean(rows: Sequence[Mapping[str, object]], key: str, places: str) -> str:
    values = tuple(value for row in rows if (value := _metric(row, key)) is not None)
    if not values:
        return "—"
    result = (sum(values, Decimal(0)) / len(values)).quantize(
        Decimal(places), rounding=ROUND_HALF_UP
    )
    rendered = format(result, "f")
    if places == "0.01":
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _percentage(count: int, denominator: int) -> str:
    if denominator == 0:
        return "—"
    percentage = int(
        (Decimal(count) * 100 / denominator).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )
    return f"{percentage}%"


def _has_items(value: object) -> bool:
    return type(value) in {list, tuple} and bool(value)


def _public_rows(rows: Sequence[Mapping[str, object]]) -> tuple[dict, ...]:
    result: list[dict] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("visibility") not in {
            None,
            "public",
        }:
            raise ValueError("public report received invalid visibility rows")
        result.append(dict(row))
    return tuple(result)


def public_report(rows: Sequence[Mapping[str, object]]) -> str:
    """Render public evidence without pooling model, tier, or capability."""

    selected = _public_rows(rows)
    if not selected:
        return "no runs recorded\n"
    partitions: dict[_Partition, list[dict]] = defaultdict(list)
    for row in selected:
        partitions[_partition(row)].append(row)
    pairs = matched_pairs(selected)

    lines = ["# Public benchmark report"]
    previous_model_version: tuple[str, str] | None = None
    previous_tier: tuple[str, str, str] | None = None
    for partition in sorted(partitions, key=_partition_key):
        model, version, tier, capability = partition
        model_version = model, version
        tier_key = model, version, tier
        if model_version != previous_model_version:
            lines.extend(("", f"## {model} / {version}"))
            previous_model_version = model_version
            previous_tier = None
        if tier_key != previous_tier:
            lines.extend(("", f"### Tier {tier}"))
            previous_tier = tier_key
        lines.extend(("", f"#### Capability {capability}", ""))

        if capability == "implementation":
            lines.extend(
                (
                    (
                        "| condition | unique tasks | runs | completed | accepted | "
                        "max-turn failures | rubric-score mean | eligible pairs | "
                        "reuse | duplication |"
                    ),
                    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                )
            )
        elif capability in {"orientation", "planning", "audit"}:
            lines.extend(
                (
                    (
                        "| condition | unique tasks | runs | completed | accepted | "
                        "max-turn failures | rubric-score mean | eligible pairs | "
                        "reads | searches | map hits | turns | tokens in | tokens out |"
                    ),
                    (
                        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
                        "---:|---:|---:|---:|"
                    ),
                )
            )
        else:
            lines.extend(
                (
                    (
                        "| condition | unique tasks | runs | completed | accepted | "
                        "max-turn failures | rubric-score mean | eligible pairs |"
                    ),
                    "|---|---:|---:|---:|---:|---:|---:|---:|",
                )
            )

        present_conditions = {
            str(row.get("condition"))
            for row in partitions[partition]
            if type(row.get("condition")) is str
        }
        conditions = tuple(sorted(present_conditions | {"B", "C"}))
        partition_pairs = tuple(
            pair
            for pair in pairs
            if _partition(pair[0]) == partition and _partition(pair[1]) == partition
        )
        eligible_pairs = tuple(
            pair
            for pair in partition_pairs
            if pair[0].get("completed") is True
            and pair[1].get("completed") is True
            and pair[0].get("accepted") is True
            and pair[1].get("accepted") is True
        )
        for condition in conditions:
            condition_rows = tuple(
                row
                for row in partitions[partition]
                if row.get("condition") == condition
            )
            eligible_rows = tuple(
                pair[0] if condition == "B" else pair[1]
                for pair in eligible_pairs
                if condition in {"B", "C"}
            )
            runs = len(condition_rows)
            tasks = len(
                {
                    task_id
                    for row in condition_rows
                    if (task_id := _task_id(row)) is not None
                }
            )
            completed = sum(row.get("completed") is True for row in condition_rows)
            accepted = sum(row.get("accepted") is True for row in condition_rows)
            max_turn_failures = sum(
                row.get("terminal_status") == "error_max_turns"
                for row in condition_rows
            )
            base = (
                f"| {condition} | {tasks} | {runs} | {_ratio(completed, runs)} | "
                f"{_ratio(accepted, runs)} | {max_turn_failures} | "
                f"{_mean(condition_rows, 'rubric_score', '0.01')} | "
                f"{len(eligible_rows) if eligible_rows else '—'}"
            )
            if capability == "implementation":
                reused = sum(_has_items(row.get("reused")) for row in eligible_rows)
                duplicated = sum(
                    _has_items(row.get("duplicated")) for row in eligible_rows
                )
                lines.append(
                    f"{base} | {_percentage(reused, len(eligible_rows))} | "
                    f"{_percentage(duplicated, len(eligible_rows))} |"
                )
            elif capability in {"orientation", "planning", "audit"}:
                metrics = " | ".join(
                    _mean(eligible_rows, key, "0.1")
                    for key in (
                        "reads",
                        "searches",
                        "map_hits",
                        "turns",
                        "tokens_in",
                        "tokens_out",
                    )
                )
                lines.append(f"{base} | {metrics} |")
            else:
                lines.append(f"{base} |")
    return "\n".join(lines) + "\n"


def report(rows: Sequence[Mapping[str, object]]) -> str:
    if not rows:
        return "no runs recorded\n"
    private = sum(
        isinstance(row, Mapping) and row.get("visibility") == "private" for row in rows
    )
    if private:
        if private != len(rows):
            raise ValueError("benchmark report has mixed visibility rows")
        return private_report(rows)
    return public_report(rows)


__all__ = (
    "PRIVATE_GROUP_FIELDS",
    "PRIVATE_NUMERIC_FIELDS",
    "matched_pairs",
    "private_report",
    "public_report",
    "report",
    "require_outside_worktree",
)
