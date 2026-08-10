from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from validation.observe import ObservedFact
from validation.schema import Exclusion, GoldFact

_CATEGORIES = frozenset(
    {
        "declaration",
        "kind",
        "container",
        "visibility",
        "signature",
        "relation",
        "call",
        "call_order",
        "strong_x0",
        "zero_classification",
        "approximate",
    }
)
_DECLARATION_LANGUAGES = ("java", "python", "typescript", "tsx")
_CALL_THRESHOLDS = {
    "java": (0.95, 0.85),
    "python": (0.95, 0.85),
    "typescript": (0.95, 0.85),
    "tsx": (0.95, 0.85),
    "lua": (0.90, 0.70),
}


@dataclass(frozen=True)
class Metric:
    name: str
    numerator: int
    denominator: int
    value: float
    minimum: float
    passed: bool

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("metric name must be nonblank")
        for field_name in ("numerator", "denominator"):
            field_value = getattr(self, field_name)
            if type(field_value) is not int or field_value < 0:
                raise ValueError(f"metric {field_name} must be nonnegative")
        if self.numerator > self.denominator:
            raise ValueError("metric numerator must not exceed denominator")
        for field_name in ("value", "minimum"):
            field_value = getattr(self, field_name)
            if type(field_value) is not float or not math.isfinite(field_value):
                raise TypeError(f"metric {field_name} must be a finite float")
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"metric {field_name} must be between zero and one")
        if type(self.passed) is not bool:
            raise TypeError("metric passed must be a boolean")


@dataclass(frozen=True)
class StaticReport:
    metrics: tuple[Metric, ...]
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.metrics) is not tuple or any(
            type(metric) is not Metric for metric in self.metrics
        ):
            raise TypeError("metrics must be a tuple of Metric records")
        names = tuple(metric.name for metric in self.metrics)
        if len(names) != len(set(names)):
            raise ValueError("metric names must be unique")
        if type(self.failures) is not tuple or any(
            type(failure) is not str or not failure for failure in self.failures
        ):
            raise TypeError("failures must be a tuple of nonblank strings")


@dataclass(frozen=True)
class _Scope:
    exclusion_id: str
    corpus: str
    path: str
    tag: str
    category: str | None = None
    subject: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class _MetricResult:
    metric: Metric
    false_positives: tuple[str, ...]
    false_negatives: tuple[str, ...]


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _subject_json(value: object) -> str:
    if not isinstance(value, list) or len(value) != 6:
        raise ValueError("exclusion SymbolId must be a six-item array")
    if (
        not isinstance(value[0], str)
        or not isinstance(value[1], str)
        or not isinstance(value[2], list)
        or any(not isinstance(part, str) or not part for part in value[2])
        or not isinstance(value[3], str)
        or not isinstance(value[4], str)
        or not value[4]
        or not isinstance(value[5], str)
    ):
        raise ValueError("exclusion SymbolId is malformed")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fact_key(
    fact: GoldFact | ObservedFact,
) -> tuple[str, str, int, str, str, str, str]:
    return (
        fact.corpus,
        fact.path,
        fact.line,
        fact.language,
        fact.category,
        fact.subject,
        _canonical_json(fact.value),
    )


def _declaration_key(
    fact: GoldFact | ObservedFact,
) -> tuple[str, str, int, str, str]:
    return (fact.corpus, fact.path, fact.line, fact.language, fact.subject)


def _observed_id(fact: ObservedFact) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "expected": True,
                "subject": fact.subject,
                "value": fact.value,
            }
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{fact.corpus}:{fact.path}:{fact.line}:{fact.category}:{digest}"


def _parse_scope(exclusion: Exclusion) -> _Scope:
    if exclusion.scope == "file":
        if exclusion.line is not None:
            raise ValueError("file exclusions must not have a source line")
        return _Scope(exclusion.id, exclusion.corpus, exclusion.path, "file")
    if exclusion.line is None:
        raise ValueError("structured exclusions require a source line")
    try:
        decoded = json.loads(exclusion.scope)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid exclusion scope {exclusion.id!r}") from error
    if _canonical_json(decoded) != exclusion.scope:
        raise ValueError(f"exclusion scope {exclusion.id!r} is not canonical")
    if not isinstance(decoded, list) or not decoded:
        raise ValueError(f"invalid exclusion scope {exclusion.id!r}")

    tag = decoded[0]
    if tag == "fact" and len(decoded) == 4:
        category = decoded[1]
        if category not in _CATEGORIES or not isinstance(decoded[3], dict):
            raise ValueError(f"invalid fact exclusion {exclusion.id!r}")
        return _Scope(
            exclusion.id,
            exclusion.corpus,
            exclusion.path,
            "fact",
            cast(str, category),
            _subject_json(decoded[2]),
            _canonical_json(decoded[3]),
        )
    if tag == "category" and len(decoded) == 3:
        category = decoded[1]
        if category not in _CATEGORIES:
            raise ValueError(f"invalid category exclusion {exclusion.id!r}")
        return _Scope(
            exclusion.id,
            exclusion.corpus,
            exclusion.path,
            "category",
            cast(str, category),
            _subject_json(decoded[2]),
        )
    if tag == "source_call" and len(decoded) == 4:
        _subject_json(decoded[1])
        if (
            type(decoded[2]) is not int
            or decoded[2] < 0
            or not isinstance(decoded[3], str)
            or not decoded[3]
        ):
            raise ValueError(f"invalid source-call exclusion {exclusion.id!r}")
        return _Scope(
            exclusion.id,
            exclusion.corpus,
            exclusion.path,
            "source_call",
        )
    if tag == "candidate" and len(decoded) == 3:
        if any(not isinstance(item, str) or not item for item in decoded[1:]):
            raise ValueError(f"invalid candidate exclusion {exclusion.id!r}")
        return _Scope(
            exclusion.id,
            exclusion.corpus,
            exclusion.path,
            "candidate",
        )
    raise ValueError(f"unknown exclusion scope {exclusion.id!r}")


def _scope_matches_fact(
    scope: _Scope,
    fact: GoldFact | ObservedFact,
) -> bool:
    if (scope.corpus, scope.path) != (fact.corpus, fact.path):
        return False
    if scope.tag == "file":
        return True
    if scope.tag == "category":
        return scope.category == fact.category and scope.subject == fact.subject
    if scope.tag == "fact":
        return (
            scope.category == fact.category
            and scope.subject == fact.subject
            and scope.value == _canonical_json(fact.value)
        )
    return False


def _validated_inputs(
    gold: Sequence[GoldFact],
    exclusions: Sequence[Exclusion],
    observed: Sequence[ObservedFact],
) -> tuple[tuple[GoldFact, ...], tuple[_Scope, ...], tuple[ObservedFact, ...]]:
    gold_rows = tuple(gold)
    exclusion_rows = tuple(exclusions)
    observed_rows = tuple(observed)
    if any(type(fact) is not GoldFact for fact in gold_rows):
        raise TypeError("gold must contain only GoldFact records")
    if any(type(exclusion) is not Exclusion for exclusion in exclusion_rows):
        raise TypeError("exclusions must contain only Exclusion records")
    if any(type(fact) is not ObservedFact for fact in observed_rows):
        raise TypeError("observed must contain only ObservedFact records")

    gold_ids = tuple(fact.id for fact in gold_rows)
    if len(gold_ids) != len(set(gold_ids)):
        raise ValueError("gold fact IDs must be unique")
    exclusion_ids = tuple(exclusion.id for exclusion in exclusion_rows)
    if len(exclusion_ids) != len(set(exclusion_ids)):
        raise ValueError("exclusion IDs must be unique")
    gold_keys = tuple(_fact_key(fact) for fact in gold_rows)
    if len(gold_keys) != len(set(gold_keys)):
        raise ValueError("gold fact identities must be unique")
    observed_keys = tuple(_fact_key(fact) for fact in observed_rows)
    if len(observed_keys) != len(set(observed_keys)):
        raise ValueError("observed fact identities must be unique")
    if any(fact.category not in _CATEGORIES for fact in observed_rows):
        raise ValueError("observed fact category is not canonical")

    scopes = tuple(_parse_scope(exclusion) for exclusion in exclusion_rows)
    for scope in scopes:
        if scope.tag not in {"file", "fact", "category"}:
            continue
        overlap = next(
            (fact for fact in gold_rows if _scope_matches_fact(scope, fact)),
            None,
        )
        if overlap is not None:
            raise ValueError(
                f"exclusion {scope.exclusion_id!r} overlaps explicit gold fact "
                f"{overlap.id!r}"
            )

    reviewed_paths = {(fact.corpus, fact.path) for fact in gold_rows} | {
        (exclusion.corpus, exclusion.path) for exclusion in exclusion_rows
    }
    scored_observed = tuple(
        fact
        for fact in observed_rows
        if (fact.corpus, fact.path) in reviewed_paths
        and not any(_scope_matches_fact(scope, fact) for scope in scopes)
    )
    return gold_rows, scopes, scored_observed


def _ratio(numerator: int, denominator: int, *, nonvacuous: bool = False) -> float:
    if denominator:
        return numerator / denominator
    return 0.0 if nonvacuous else 1.0


def _result(
    name: str,
    numerator: int,
    denominator: int,
    minimum: float,
    *,
    false_positives: Sequence[str] = (),
    false_negatives: Sequence[str] = (),
    nonvacuous: bool = False,
) -> _MetricResult:
    value = _ratio(numerator, denominator, nonvacuous=nonvacuous)
    passed = denominator > 0 and value >= minimum if nonvacuous else value >= minimum
    return _MetricResult(
        Metric(name, numerator, denominator, value, minimum, passed),
        tuple(sorted(false_positives)),
        tuple(sorted(false_negatives)),
    )


def _exact_set_result(
    name: str,
    minimum: float,
    positive: Sequence[GoldFact],
    observed: Sequence[ObservedFact],
    *,
    union_denominator: bool,
    nonvacuous: bool = False,
) -> _MetricResult:
    positive_by_key = {_fact_key(fact): fact for fact in positive}
    observed_by_key = {_fact_key(fact): fact for fact in observed}
    matched = positive_by_key.keys() & observed_by_key.keys()
    false_positive_keys = observed_by_key.keys() - positive_by_key.keys()
    false_negative_keys = positive_by_key.keys() - observed_by_key.keys()
    denominator = (
        len(matched) + len(false_positive_keys) + len(false_negative_keys)
        if union_denominator
        else len(positive_by_key)
    )
    return _result(
        name,
        len(matched),
        denominator,
        minimum,
        false_positives=tuple(
            _observed_id(observed_by_key[key]) for key in false_positive_keys
        ),
        false_negatives=tuple(positive_by_key[key].id for key in false_negative_keys),
        nonvacuous=nonvacuous,
    )


def _declaration_results(
    gold: tuple[GoldFact, ...],
    observed: tuple[ObservedFact, ...],
) -> tuple[list[_MetricResult], set[tuple[str, str, int, str, str]]]:
    positive = tuple(
        fact for fact in gold if fact.category == "declaration" and fact.expected
    )
    actual = tuple(fact for fact in observed if fact.category == "declaration")
    positive_by_key = {_fact_key(fact): fact for fact in positive}
    actual_by_key = {_fact_key(fact): fact for fact in actual}
    matched_keys = positive_by_key.keys() & actual_by_key.keys()
    matched_declarations = {
        _declaration_key(positive_by_key[key]) for key in matched_keys
    }

    def precision_recall(
        language: str | None,
        precision_name: str,
        recall_name: str,
        precision_minimum: float,
        recall_minimum: float,
    ) -> tuple[_MetricResult, _MetricResult]:
        language_positive = {
            key: fact
            for key, fact in positive_by_key.items()
            if language is None or fact.language == language
        }
        language_actual = {
            key: fact
            for key, fact in actual_by_key.items()
            if language is None or fact.language == language
        }
        matched = language_positive.keys() & language_actual.keys()
        false_positive_keys = language_actual.keys() - language_positive.keys()
        false_negative_keys = language_positive.keys() - language_actual.keys()
        precision = _result(
            precision_name,
            len(matched),
            len(language_actual),
            precision_minimum,
            false_positives=tuple(
                _observed_id(language_actual[key]) for key in false_positive_keys
            ),
        )
        recall = _result(
            recall_name,
            len(matched),
            len(language_positive),
            recall_minimum,
            false_negatives=tuple(
                language_positive[key].id for key in false_negative_keys
            ),
        )
        return precision, recall

    results = list(
        precision_recall(
            None,
            "declaration_micro_precision",
            "declaration_micro_recall",
            0.99,
            0.97,
        )
    )
    for language in _DECLARATION_LANGUAGES:
        results.extend(
            precision_recall(
                language,
                f"declaration_precision_{language}",
                f"declaration_recall_{language}",
                0.97,
                0.95,
            )
        )
    return results, matched_declarations


def _attribute_result(
    category: str,
    name: str,
    minimum: float,
    gold: tuple[GoldFact, ...],
    observed: tuple[ObservedFact, ...],
    matched_declarations: set[tuple[str, str, int, str, str]],
    *,
    language: str | None = None,
) -> _MetricResult:
    positive = tuple(
        fact
        for fact in gold
        if fact.category == category
        and fact.expected
        and _declaration_key(fact) in matched_declarations
        and (language is None or fact.language == language)
    )
    actual = tuple(
        fact
        for fact in observed
        if fact.category == category
        and _declaration_key(fact) in matched_declarations
        and (language is None or fact.language == language)
    )
    return _exact_set_result(
        name,
        minimum,
        positive,
        actual,
        union_denominator=False,
    )


def _call_occurrence_key(
    fact: GoldFact | ObservedFact,
) -> tuple[str, str, int, str, str, str]:
    target = fact.value.get("target")
    if target is None:
        raise ValueError("call facts must contain a target")
    ordinal = fact.value.get("ordinal")
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("call facts must contain a nonnegative ordinal")
    return (
        fact.corpus,
        fact.path,
        fact.line,
        fact.language,
        fact.subject,
        _canonical_json(target),
    )


def _call_results(
    gold: tuple[GoldFact, ...],
    observed: tuple[ObservedFact, ...],
) -> list[_MetricResult]:
    results: list[_MetricResult] = []
    for language, (precision_minimum, recall_minimum) in _CALL_THRESHOLDS.items():
        positive = tuple(
            fact
            for fact in gold
            if fact.category == "call" and fact.expected and fact.language == language
        )
        actual = tuple(
            fact
            for fact in observed
            if fact.category == "call" and fact.language == language
        )
        expected_groups: dict[tuple[str, str, int, str, str, str], list[GoldFact]] = (
            defaultdict(list)
        )
        actual_groups: dict[tuple[str, str, int, str, str, str], list[ObservedFact]] = (
            defaultdict(list)
        )
        for expected_fact in positive:
            expected_groups[_call_occurrence_key(expected_fact)].append(expected_fact)
        for actual_fact in actual:
            actual_groups[_call_occurrence_key(actual_fact)].append(actual_fact)

        true_positives = 0
        false_positives: list[str] = []
        false_negatives: list[str] = []
        for key in sorted(expected_groups.keys() | actual_groups.keys()):
            expected_rows = sorted(expected_groups[key], key=lambda fact: fact.id)
            actual_rows = sorted(actual_groups[key], key=_observed_id)
            matched = min(len(expected_rows), len(actual_rows))
            true_positives += matched
            false_negatives.extend(fact.id for fact in expected_rows[matched:])
            false_positives.extend(_observed_id(fact) for fact in actual_rows[matched:])
        results.append(
            _result(
                f"call_precision_{language}",
                true_positives,
                len(actual),
                precision_minimum,
                false_positives=false_positives,
            )
        )
        results.append(
            _result(
                f"call_recall_{language}",
                true_positives,
                len(positive),
                recall_minimum,
                false_negatives=false_negatives,
            )
        )
    return results


def _call_order_result(
    gold: tuple[GoldFact, ...],
    observed: tuple[ObservedFact, ...],
) -> _MetricResult:
    reviewed: list[GoldFact] = []
    for fact in gold:
        if fact.category != "call_order" or not fact.expected:
            continue
        targets = fact.value.get("targets")
        if not isinstance(targets, tuple):
            raise TypeError("call_order targets must be an array")
        if len(targets) >= 2:
            reviewed.append(fact)
    reviewed_subjects = {_declaration_key(fact) for fact in reviewed}
    actual = tuple(
        fact
        for fact in observed
        if fact.category == "call_order" and _declaration_key(fact) in reviewed_subjects
    )
    return _exact_set_result(
        "call_order_accuracy",
        0.85,
        reviewed,
        actual,
        union_denominator=False,
    )


def _failure(result: _MetricResult) -> str:
    metric = result.metric
    false_positives = tuple(sorted(set(result.false_positives)))[:10]
    false_negatives = tuple(sorted(set(result.false_negatives)))[:10]
    fp_text = ", ".join(false_positives) if false_positives else "-"
    fn_text = ", ".join(false_negatives) if false_negatives else "-"
    return (
        f"{metric.name}: {metric.numerator}/{metric.denominator} = "
        f"{metric.value:.2%}, requires >= {metric.minimum:.2%}; "
        f"false positives: {fp_text}; false negatives: {fn_text}"
    )


def evaluate_static(
    gold: Sequence[GoldFact],
    exclusions: Sequence[Exclusion],
    observed: Sequence[ObservedFact],
) -> StaticReport:
    """Evaluate frozen static-accuracy gates over the reviewed sample surface."""

    gold_rows, _scopes, observed_rows = _validated_inputs(
        gold,
        exclusions,
        observed,
    )
    results, matched_declarations = _declaration_results(gold_rows, observed_rows)
    for category, name in (
        ("kind", "kind_accuracy"),
        ("container", "container_accuracy"),
        ("visibility", "visibility_accuracy"),
    ):
        results.append(
            _attribute_result(
                category,
                name,
                0.99,
                gold_rows,
                observed_rows,
                matched_declarations,
            )
        )
    results.append(
        _attribute_result(
            "signature",
            "signature_accuracy",
            0.95,
            gold_rows,
            observed_rows,
            matched_declarations,
        )
    )
    signature_languages = sorted(
        {
            fact.language
            for fact in gold_rows
            if fact.category == "signature" and fact.expected
        }
    )
    for language in signature_languages:
        results.append(
            _attribute_result(
                "signature",
                f"signature_accuracy_{language}",
                0.90,
                gold_rows,
                observed_rows,
                matched_declarations,
                language=language,
            )
        )

    results.append(
        _exact_set_result(
            "relation_exact_accuracy",
            0.97,
            tuple(
                fact
                for fact in gold_rows
                if fact.category == "relation" and fact.expected
            ),
            tuple(fact for fact in observed_rows if fact.category == "relation"),
            union_denominator=True,
        )
    )
    results.extend(_call_results(gold_rows, observed_rows))
    results.append(_call_order_result(gold_rows, observed_rows))

    strong_positive = tuple(
        fact for fact in gold_rows if fact.category == "strong_x0" and fact.expected
    )
    strong_observed = tuple(
        fact for fact in observed_rows if fact.category == "strong_x0"
    )
    strong_positive_keys = {_fact_key(fact) for fact in strong_positive}
    strong_observed_keys = {_fact_key(fact) for fact in strong_observed}
    strong_match = strong_positive_keys & strong_observed_keys
    strong_fp = tuple(
        fact for fact in strong_observed if _fact_key(fact) not in strong_positive_keys
    )
    strong_fn = tuple(
        fact for fact in strong_positive if _fact_key(fact) not in strong_observed_keys
    )
    results.append(
        _result(
            "strong_x0_precision",
            len(strong_match),
            len(strong_observed),
            1.0,
            false_positives=tuple(_observed_id(fact) for fact in strong_fp),
            nonvacuous=True,
        )
    )
    results.append(
        _result(
            "strong_x0_recall",
            len(strong_match),
            len(strong_positive),
            1.0,
            false_negatives=tuple(fact.id for fact in strong_fn),
        )
    )

    synthetic_zero = tuple(
        fact
        for fact in gold_rows
        if fact.corpus == "synthetic"
        and fact.category == "zero_classification"
        and fact.expected
    )
    synthetic_zero_subjects = {_declaration_key(fact) for fact in synthetic_zero}
    observed_zero = tuple(
        fact
        for fact in observed_rows
        if fact.corpus == "synthetic"
        and fact.category == "zero_classification"
        and _declaration_key(fact) in synthetic_zero_subjects
    )
    results.append(
        _exact_set_result(
            "zero_classification_accuracy",
            1.0,
            synthetic_zero,
            observed_zero,
            union_denominator=False,
        )
    )

    approximate_positive = tuple(
        fact
        for fact in gold_rows
        if fact.corpus == "synthetic"
        and fact.category == "approximate"
        and fact.expected
    )
    approximate_observed = tuple(
        fact
        for fact in observed_rows
        if fact.corpus == "synthetic" and fact.category == "approximate"
    )
    approximate_keys = {_fact_key(fact) for fact in approximate_positive}
    approximate_observed_keys = {_fact_key(fact) for fact in approximate_observed}
    approximate_match = approximate_keys & approximate_observed_keys
    results.append(
        _result(
            "approximate_precision",
            len(approximate_match),
            len(approximate_observed),
            1.0,
            false_positives=tuple(
                _observed_id(fact)
                for fact in approximate_observed
                if _fact_key(fact) not in approximate_keys
            ),
        )
    )
    results.append(
        _result(
            "approximate_recall",
            len(approximate_match),
            len(approximate_positive),
            0.80,
            false_negatives=tuple(
                fact.id
                for fact in approximate_positive
                if _fact_key(fact) not in approximate_observed_keys
            ),
        )
    )

    metrics = tuple(result.metric for result in results)
    failures = tuple(_failure(result) for result in results if not result.metric.passed)
    return StaticReport(metrics, failures)


def require_thresholds(report: StaticReport) -> None:
    if type(report) is not StaticReport:
        raise TypeError("report must be a StaticReport")
    failures = report.failures
    if not failures:
        failures = tuple(
            f"{metric.name}: {metric.numerator}/{metric.denominator} = "
            f"{metric.value:.2%}, requires >= {metric.minimum:.2%}"
            for metric in report.metrics
            if not metric.passed
        )
    if failures:
        raise ValueError("static validation thresholds failed:\n" + "\n".join(failures))


__all__ = (
    "Metric",
    "StaticReport",
    "evaluate_static",
    "require_thresholds",
)
