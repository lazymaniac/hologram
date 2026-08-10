from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from hologram import pipeline
from hologram.config import ProjectConfig, default_config

from . import observe
from .corpus import build_census, load_registry, resolve_checkout, select_gold_sample
from .metrics import Metric, StaticReport, evaluate_static
from .schema import CensusRecord, Exclusion, GoldFact, GoldSample, load_jsonl

_VALIDATION_ROOT = Path(__file__).resolve().parent
_REPRODUCIBLE_ENVIRONMENT = {
    "LC_ALL": "C",
    "TZ": "UTC",
    "SOURCE_DATE_EPOCH": "1786233600",
}


@dataclass(frozen=True)
class _Capture:
    facts: tuple[observe.ObservedFact, ...]
    facts_bytes: bytes
    rendered_bytes: bytes
    file_count: int


@dataclass(frozen=True)
class _ValidationResult:
    report: StaticReport
    runs: int
    census: int
    sample: int
    synthetic_files: int
    byte_equal: bool = True


class _UsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise _UsageError(message)


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


def _fact_key(fact: observe.ObservedFact) -> tuple[str, str, int, str, str, str]:
    return (
        fact.corpus,
        fact.path,
        fact.line,
        fact.category,
        fact.subject,
        _canonical_json(fact.value),
    )


def _fact_bytes(facts: Sequence[observe.ObservedFact]) -> bytes:
    ordered = tuple(sorted(facts, key=_fact_key))
    rows = (
        _canonical_json(
            {
                "category": fact.category,
                "subject": fact.subject,
                "value": fact.value,
                "corpus": fact.corpus,
                "path": fact.path,
                "line": fact.line,
                "language": fact.language,
            }
        )
        for fact in ordered
    )
    text = "\n".join(rows)
    return (text + "\n").encode("utf-8") if text else b""


def _foundation_config() -> ProjectConfig:
    return dataclasses.replace(
        default_config(),
        agents=(),
        output="PROJECT_DIGEST.md",
    )


@contextmanager
def _reproducible_environment() -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in _REPRODUCIBLE_ENVIRONMENT}
    os.environ.update(_REPRODUCIBLE_ENVIRONMENT)
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if hasattr(time, "tzset"):
            time.tzset()


def _capture_once(
    root: Path,
    config: ProjectConfig,
    *,
    corpus: str,
) -> _Capture:
    facts, rendered, file_count = observe._observe_project_artifact(
        corpus=corpus,
        root=root,
        config=config,
    )
    return _Capture(
        facts,
        _fact_bytes(facts),
        rendered.encode("utf-8"),
        file_count,
    )


def _validate_runs(runs: int) -> None:
    if type(runs) is not int or runs < 2:
        raise ValueError("runs must be an integer of at least two")


def _first_difference(left: bytes, right: bytes) -> int:
    for index, (left_byte, right_byte) in enumerate(zip(left, right, strict=False)):
        if left_byte != right_byte:
            return index
    return min(len(left), len(right))


def _require_equal(label: str, baseline: bytes, candidate: bytes, run: int) -> None:
    if baseline == candidate:
        return
    offset = _first_difference(baseline, candidate)
    left_hash = hashlib.sha256(baseline).hexdigest()
    right_hash = hashlib.sha256(candidate).hexdigest()
    raise ValueError(
        f"{label} nondeterministic at run {run}: first differing byte {offset}; "
        f"sha256 {left_hash} != {right_hash}"
    )


def _capture_runs(
    root: Path,
    config: ProjectConfig,
    *,
    corpus: str,
    runs: int,
) -> _Capture:
    _validate_runs(runs)
    baseline: _Capture | None = None
    for run in range(1, runs + 1):
        with tempfile.TemporaryDirectory(prefix="hologram-static-validation-") as tmp:
            with _reproducible_environment():
                artifact = _capture_once(root, config, corpus=corpus)
            temporary = Path(tmp)
            (temporary / "facts.jsonl").write_bytes(artifact.facts_bytes)
            (temporary / "PROJECT_DIGEST.md").write_bytes(artifact.rendered_bytes)
        if baseline is None:
            baseline = artifact
            continue
        _require_equal("project facts", baseline.facts_bytes, artifact.facts_bytes, run)
        _require_equal(
            "rendered map",
            baseline.rendered_bytes,
            artifact.rendered_bytes,
            run,
        )
        if baseline.file_count != artifact.file_count:
            raise ValueError(
                f"project file count nondeterministic at run {run}: "
                f"{baseline.file_count} != {artifact.file_count}"
            )
    assert baseline is not None
    return baseline


def assert_byte_determinism(
    root: Path,
    config: ProjectConfig,
    *,
    runs: int = 3,
) -> None:
    """Require identical canonical facts and rendered bytes across fresh runs."""

    if type(config) is not ProjectConfig:
        raise TypeError("config must be a ProjectConfig")
    _capture_runs(Path(root), config, corpus="determinism", runs=runs)


def _load_gold(
    facts_dir: Path,
    exclusions_dir: Path,
    corpora: Sequence[str],
) -> tuple[tuple[GoldFact, ...], tuple[Exclusion, ...]]:
    facts: list[GoldFact] = []
    exclusions: list[Exclusion] = []
    for corpus in corpora:
        facts.extend(load_jsonl(facts_dir / f"{corpus}.jsonl", GoldFact))
        exclusions.extend(load_jsonl(exclusions_dir / f"{corpus}.jsonl", Exclusion))
    return tuple(facts), tuple(exclusions)


def _mismatch(label: str, expected: Sequence[object], actual: Sequence[object]) -> None:
    if tuple(expected) == tuple(actual):
        return
    limit = min(len(expected), len(actual))
    index = next(
        (
            position
            for position in range(limit)
            if expected[position] != actual[position]
        ),
        limit,
    )
    raise ValueError(
        f"{label} drift at row {index + 1}: stored={len(expected)} "
        f"regenerated={len(actual)}"
    )


def _validate_synthetic(root: Path, *, runs: int) -> _ValidationResult:
    config = _foundation_config()
    artifact = _capture_runs(Path(root), config, corpus="synthetic", runs=runs)
    facts, exclusions = _load_gold(
        _VALIDATION_ROOT / "gold" / "facts",
        _VALIDATION_ROOT / "gold" / "exclusions",
        ("synthetic",),
    )
    report = evaluate_static(facts, exclusions, artifact.facts)
    return _ValidationResult(report, runs, 0, 0, artifact.file_count)


def _validate_public(
    registry_path: Path,
    *,
    census_path: Path,
    sample_path: Path,
    facts_dir: Path,
    exclusions_dir: Path,
    synthetic_root: Path,
    environ: Mapping[str, str],
    runs: int,
) -> _ValidationResult:
    _validate_runs(runs)
    registry = load_registry(registry_path)
    roots = {spec.name: resolve_checkout(spec, environ) for spec in registry.corpora}
    regenerated_census = build_census(registry, roots)
    stored_census = load_jsonl(census_path, CensusRecord)
    _mismatch("census", stored_census, regenerated_census)
    regenerated_sample = select_gold_sample(regenerated_census, registry)
    stored_sample = load_jsonl(sample_path, GoldSample)
    _mismatch("sample", stored_sample, regenerated_sample)

    config = _foundation_config()
    observed: list[observe.ObservedFact] = []
    for spec in registry.corpora:
        artifact = _capture_runs(
            roots[spec.name],
            config,
            corpus=spec.name,
            runs=runs,
        )
        observed.extend(artifact.facts)
    synthetic = _capture_runs(
        synthetic_root,
        config,
        corpus="synthetic",
        runs=runs,
    )
    observed.extend(synthetic.facts)

    corpus_names = tuple(spec.name for spec in registry.corpora) + ("synthetic",)
    facts, exclusions = _load_gold(facts_dir, exclusions_dir, corpus_names)
    report = evaluate_static(facts, exclusions, observed)
    return _ValidationResult(
        report,
        runs,
        len(stored_census),
        len(stored_sample),
        synthetic.file_count,
    )


def validate_corpora(
    registry: Path,
    *,
    environ: Mapping[str, str],
    runs: int = 3,
) -> StaticReport:
    """Validate all frozen public corpora plus the advertised-language fixture."""

    selected = Path(registry)
    result = _validate_public(
        selected,
        census_path=selected.parent / "gold" / "census.jsonl",
        sample_path=selected.parent / "gold" / "sample.jsonl",
        facts_dir=selected.parent / "gold" / "facts",
        exclusions_dir=selected.parent / "gold" / "exclusions",
        synthetic_root=selected.parent / "fixtures" / "advertised",
        environ=environ,
        runs=runs,
    )
    return result.report


def _metric_payload(metric: Metric) -> dict[str, object]:
    return {
        "name": metric.name,
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "value": metric.value,
        "minimum": metric.minimum,
        "passed": metric.passed,
    }


def _result_bytes(result: _ValidationResult) -> bytes:
    passed = not result.report.failures and all(
        metric.passed for metric in result.report.metrics
    )
    payload = {
        "byte_equal": result.byte_equal,
        "census": result.census,
        "failures": list(result.report.failures),
        "metrics": [_metric_payload(metric) for metric in result.report.metrics],
        "passed": passed,
        "runs": result.runs,
        "sample": result.sample,
        "synthetic_files": result.synthetic_files,
    }
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _output_path(path: Path) -> Path:
    selected = Path(path)
    try:
        resolved = selected.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise _UsageError(f"invalid output path: {selected}") from error
    if resolved == _VALIDATION_ROOT or resolved.is_relative_to(_VALIDATION_ROOT):
        raise _UsageError("generated output must not be written beneath validation/")
    return resolved


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="hologram validation", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--registry", type=Path)
    mode.add_argument("--synthetic", type=Path)
    parser.add_argument("--census", type=Path)
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--facts", type=Path)
    parser.add_argument("--exclusions", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        try:
            arguments = _parser().parse_args(argv)
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else 1
        try:
            _validate_runs(arguments.runs)
        except ValueError as error:
            raise _UsageError(str(error)) from error
        public_options = (
            arguments.census,
            arguments.sample,
            arguments.facts,
            arguments.exclusions,
        )
        if arguments.synthetic is not None and any(
            option is not None for option in public_options
        ):
            raise _UsageError(
                "--census, --sample, --facts, and --exclusions require --registry"
            )
        output = (
            _output_path(arguments.output) if arguments.output is not None else None
        )

        if arguments.synthetic is not None:
            result = _validate_synthetic(arguments.synthetic, runs=arguments.runs)
        else:
            registry = arguments.registry
            assert registry is not None
            base = registry.parent / "gold"
            result = _validate_public(
                registry,
                census_path=arguments.census or base / "census.jsonl",
                sample_path=arguments.sample or base / "sample.jsonl",
                facts_dir=arguments.facts or base / "facts",
                exclusions_dir=arguments.exclusions or base / "exclusions",
                synthetic_root=registry.parent / "fixtures" / "advertised",
                environ=os.environ,
                runs=arguments.runs,
            )
        raw = _result_bytes(result)
        if output is None:
            sys.stdout.write(raw.decode("utf-8"))
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
        return 0 if not result.report.failures else 1
    except _UsageError as error:
        sys.stderr.write(f"hologram validation: {error}\n")
        return 2
    except pipeline.IncompleteBuildError as error:
        sys.stderr.write(f"hologram validation: {error}\n")
        return 3
    except (OSError, TypeError, ValueError) as error:
        sys.stderr.write(f"hologram validation: {error}\n")
        return 1


__all__ = ("assert_byte_determinism", "main", "validate_corpora")


if __name__ == "__main__":
    raise SystemExit(main())
