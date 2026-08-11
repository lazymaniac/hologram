from __future__ import annotations

import dataclasses
import inspect
import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest import mock

import hologram
from hologram import analysis, pipeline, render
from hologram.config import ProjectConfig, default_config
from hologram.parsers.api import DEFAULT_REGISTRY
from hologram.resolve import ResolutionStatus
from validation.metrics import (
    Metric,
    StaticReport,
    evaluate_static,
    require_thresholds,
)
from validation.observe import ObservedFact, observe_project, observe_rendered_map
from validation.schema import Exclusion, GoldFact

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_ROOT = PROJECT_ROOT / "validation" / "fixtures" / "advertised"
STATE = "a" * 64
REVISION = "1" * 40


def _config() -> ProjectConfig:
    return replace(default_config(), agents=(), output="PROJECT_DIGEST.md")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _name(fact: ObservedFact) -> str:
    value = json.loads(fact.subject)
    assert isinstance(value, list)
    return str(value[4])


def _subject(
    name: str,
    *,
    language: str = "python",
    path: str = "src/app.py",
    kind: str = "fn",
    signature_key: str = "()",
) -> str:
    return json.dumps(
        [language, path, [], kind, name, signature_key],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _gold(
    fact_id: str,
    category: str,
    subject: str,
    value: Mapping[str, object],
    *,
    expected: bool = True,
    corpus: str = "sample",
    line: int = 1,
) -> GoldFact:
    decoded = json.loads(subject)
    return GoldFact(
        fact_id,
        corpus,
        REVISION,
        decoded[1],
        line,
        decoded[0],
        category,  # type: ignore[arg-type]
        subject,
        value,
        expected,
    )


def _observed(
    fact: GoldFact,
    *,
    value: Mapping[str, object] | None = None,
    category: str | None = None,
) -> ObservedFact:
    return ObservedFact(
        category or fact.category,
        fact.subject,
        value or fact.value,
        fact.corpus,
        fact.path,
        fact.line,
        fact.language,
    )


def _metric(report: StaticReport, name: str) -> Metric:
    return next(metric for metric in report.metrics if metric.name == name)


def _render_snapshot(
    snapshot: pipeline.BuildSnapshot,
    config: ProjectConfig,
) -> str:
    analyzed = analysis.analyze_project(
        snapshot.project,
        snapshot.resolution,
        hot_threshold=config.hot_threshold,
    )
    ir = render.project_render_ir(
        analyzed,
        state=STATE,
        hot_threshold=config.hot_threshold,
    )
    return render.render_project(ir)


class ObservedFactTest(unittest.TestCase):
    def test_record_and_public_api_are_exact_and_deeply_frozen(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(ObservedFact)),
            ("category", "subject", "value", "corpus", "path", "line", "language"),
        )
        self.assertEqual(
            tuple(inspect.signature(observe_project).parameters),
            ("corpus", "root", "config"),
        )
        self.assertEqual(
            tuple(inspect.signature(observe_rendered_map).parameters),
            ("corpus", "rendered"),
        )

        nested = {"targets": [["python", "a.py", [], "fn", "run", "()"]]}
        fact = ObservedFact(
            "call_order",
            '["python","a.py",[],"fn","run","()"]',
            nested,
            "fixture",
            "a.py",
            1,
            "python",
        )
        nested["targets"].clear()
        self.assertEqual(
            _thaw(fact.value),
            {"targets": [["python", "a.py", [], "fn", "run", "()"]]},
        )
        with self.assertRaises(TypeError):
            fact.value["targets"] = ()  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            fact.line = 2  # type: ignore[misc]
        for path, language, subject in (
            (" ", "python", _subject("run", path=" ")),
            ("a.py", "typescript", _subject("run")),
            ("other.py", "python", _subject("run")),
        ):
            with (
                self.subTest(path=path, language=language),
                self.assertRaises(ValueError),
            ):
                ObservedFact(
                    "declaration",
                    subject,
                    {"name": "run"},
                    "fixture",
                    path,
                    1,
                    language,
                )

    def test_incomplete_snapshot_stops_before_analysis_or_fact_emission(self) -> None:
        incomplete = mock.Mock(spec=pipeline.BuildSnapshot)
        incomplete.scan.diagnostics = ()
        incomplete.state.diagnostics = ()
        incomplete.project.diagnostics = ()
        incomplete.resolution.diagnostics = ()
        error = pipeline.IncompleteBuildError(incomplete)
        incomplete.require_complete.side_effect = error
        config = _config()

        with (
            mock.patch(
                "validation.observe.pipeline.build_project", return_value=incomplete
            ) as build,
            mock.patch("validation.observe.analysis.analyze_project") as analyze,
            mock.patch("validation.observe.render.project_render_ir") as project,
            self.assertRaises(pipeline.IncompleteBuildError) as raised,
        ):
            observe_project(corpus="broken", root=Path("fixture"), config=config)

        self.assertIs(raised.exception, error)
        build.assert_called_once_with(Path("fixture"), config)
        analyze.assert_not_called()
        project.assert_not_called()

    def test_project_and_render_observation_preserve_canonical_provenance(self) -> None:
        config = _config()
        snapshot = pipeline.build_project(SYNTHETIC_ROOT, config)
        snapshot.require_complete()
        rendered = _render_snapshot(snapshot, config)

        def no_snapshot_read(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("observation reread a captured source path")

        with (
            mock.patch(
                "validation.observe.pipeline.build_project",
                return_value=snapshot,
            ) as build,
            mock.patch.object(Path, "read_bytes", side_effect=no_snapshot_read),
            mock.patch.object(Path, "read_text", side_effect=no_snapshot_read),
        ):
            project_facts = observe_project(
                corpus="synthetic",
                root=SYNTHETIC_ROOT,
                config=config,
            )
        build.assert_called_once_with(SYNTHETIC_ROOT, config)
        map_facts = observe_rendered_map(corpus="synthetic", rendered=rendered)

        self.assertEqual(
            project_facts,
            tuple(
                sorted(
                    project_facts,
                    key=lambda fact: (
                        fact.corpus,
                        fact.path,
                        fact.line,
                        fact.category,
                        fact.subject,
                        json.dumps(
                            _thaw(fact.value),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            ),
        )
        self.assertEqual(
            sum(fact.category == "declaration" for fact in project_facts),
            132,
        )

        comparable = {
            "declaration",
            "kind",
            "container",
            "visibility",
            "signature",
            "call",
            "call_order",
            "strong_x0",
            "zero_classification",
            "approximate",
        }
        self.assertEqual(
            tuple(fact for fact in project_facts if fact.category in comparable),
            tuple(fact for fact in map_facts if fact.category in comparable),
        )

        ordered = next(
            fact
            for fact in project_facts
            if fact.category == "call_order"
            and fact.language == "typescript"
            and _name(fact) == "goldOrderedCaller"
        )
        self.assertEqual(
            [target[4] for target in _thaw(ordered.value)["targets"]],  # type: ignore[index]
            ["goldFirst", "goldSecond", "goldFirst"],
        )
        self.assertEqual(ordered.line, 3)

        clone = next(
            fact
            for fact in map_facts
            if fact.category == "approximate" and _name(fact) == "goldExactCloneA"
        )
        self.assertEqual(_thaw(clone.value)["peer"][4], "goldExactCloneB")  # type: ignore[index]
        unused = {
            fact.category: _thaw(fact.value)
            for fact in project_facts
            if _name(fact) == "GoldUnusedStrong"
            and fact.category in {"strong_x0", "zero_classification"}
        }
        self.assertEqual(unused["strong_x0"], {"classification": "strong"})
        self.assertEqual(
            unused["zero_classification"],
            {"classification": "strong"},
        )

        super_fact = next(
            fact
            for fact in project_facts
            if fact.category == "relation" and _name(fact) == "GoldTypeScriptDerived"
        )
        self.assertEqual(
            _thaw(super_fact.value)["target"]["symbol"][4],  # type: ignore[index]
            "GoldTypeScriptBase",
        )

    @unittest.skipUnless(
        DEFAULT_REGISTRY.has_parser(hologram.Language.TYPESCRIPT),
        "tree-sitter-typescript not installed",
    )
    def test_typescript_signature_facts_are_structurally_canonical(self) -> None:
        raw = """\
export type Pair = { left: string; right: number };
export interface Props { value: Map < string, number >; }
const internal = 1;
export function run(value: Pair): void {}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "api.ts").write_text(raw, encoding="utf-8")
            facts = observe_project(corpus="fixture", root=root, config=_config())

        signatures = {
            (_name(fact), json.loads(fact.subject)[3]): _thaw(fact.value)
            for fact in facts
            if fact.category == "signature"
        }
        self.assertEqual(
            signatures[("Pair", "type")],
            {
                "text": "type Pair={left:string;right:number}",
                "params": [],
                "returns": None,
                "raises": [],
            },
        )
        self.assertEqual(
            signatures[("value", "property")],
            {
                "text": "value:Map<string,number>",
                "params": [],
                "returns": None,
                "raises": [],
            },
        )
        self.assertEqual(
            signatures[("internal", "constant")],
            {
                "text": "internal",
                "params": [],
                "returns": None,
                "raises": [],
            },
        )
        self.assertEqual(
            signatures[("run", "fn")],
            {
                "text": "run(Pair):void",
                "params": ["Pair"],
                "returns": "void",
                "raises": [],
            },
        )
        component = next(
            fact
            for fact in facts
            if fact.category == "relation"
            and _name(fact) == "Props"
            and _thaw(fact.value)["kind"] == "component"  # type: ignore[index]
        )
        self.assertEqual(
            _thaw(component.value)["target"]["symbol"][4],  # type: ignore[index]
            "value",
        )

    def test_ambiguous_and_external_calls_are_omitted_without_reordering(self) -> None:
        config = _config()
        snapshot = pipeline.build_project(SYNTHETIC_ROOT, config)
        calls = list(snapshot.resolution.calls)
        first_index = next(
            index
            for index, item in enumerate(calls)
            if item.fact.caller is not None
            and item.fact.caller.file == "typescript/calls.ts"
        )
        original = calls[first_index]
        self.assertIsNotNone(original.target)
        calls[first_index] = replace(
            original,
            status=ResolutionStatus.AMBIGUOUS,
            target=None,
        )
        changed = replace(
            snapshot,
            resolution=replace(snapshot.resolution, calls=tuple(calls)),
        )
        with mock.patch(
            "validation.observe.pipeline.build_project",
            return_value=changed,
        ):
            facts = observe_project(
                corpus="synthetic",
                root=SYNTHETIC_ROOT,
                config=config,
            )
        order = next(
            fact
            for fact in facts
            if fact.category == "call_order"
            and fact.language == "typescript"
            and _name(fact) == "goldOrderedCaller"
        )
        self.assertEqual(
            [target[4] for target in _thaw(order.value)["targets"]],  # type: ignore[index]
            ["goldSecond", "goldFirst"],
        )
        calls_for_owner = [
            fact
            for fact in facts
            if fact.category == "call" and fact.subject == order.subject
        ]
        self.assertEqual(
            [_thaw(fact.value)["ordinal"] for fact in calls_for_owner],  # type: ignore[index]
            [0, 1],
        )

    def test_external_dependencies_are_model_facts_without_path_rereads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.ts").write_text(
                'import express from "express";\n'
                'import { missing } from "./missing";\n'
                "declare namespace Local {}\n"
                "export function run(): number { return 1; }\n",
                encoding="utf-8",
            )
            facts = observe_project(corpus="external", root=root, config=_config())

        dependency = next(
            fact
            for fact in facts
            if fact.category == "relation" and _thaw(fact.value)["kind"] == "dependency"  # type: ignore[index]
        )
        self.assertEqual(_name(dependency), "app")
        self.assertEqual(
            _thaw(dependency.value),
            {"kind": "dependency", "target": {"external": "express"}},
        )
        self.assertEqual(
            sum(
                fact.category == "relation"
                and _thaw(fact.value)["kind"] == "dependency"  # type: ignore[index]
                for fact in facts
            ),
            1,
        )

    def test_dynamic_language_imports_are_not_static_dependency_relations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text(
                "import external_package\n\ndef run():\n    return 1\n",
                encoding="utf-8",
            )
            facts = observe_project(corpus="external", root=root, config=_config())

        self.assertFalse(
            any(
                fact.category == "relation"
                and _thaw(fact.value)["kind"] == "dependency"  # type: ignore[index]
                for fact in facts
            )
        )


class StaticMetricTest(unittest.TestCase):
    def test_public_records_api_and_threshold_boundary_are_exact(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(Metric)),
            ("name", "numerator", "denominator", "value", "minimum", "passed"),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(StaticReport)),
            ("metrics", "failures"),
        )
        self.assertEqual(
            tuple(inspect.signature(evaluate_static).parameters),
            ("gold", "exclusions", "observed"),
        )
        self.assertEqual(
            tuple(inspect.signature(require_thresholds).parameters),
            ("report",),
        )

        gold: list[GoldFact] = []
        observed: list[ObservedFact] = []
        for index in range(100):
            subject = _subject(f"item_{index}")
            fact = _gold(
                f"declaration-{index}",
                "declaration",
                subject,
                {"name": f"item_{index}"},
            )
            gold.append(fact)
            if index < 97:
                observed.append(_observed(fact))
        signature = _gold(
            "signature-0",
            "signature",
            gold[0].subject,
            {"text": "item_0()", "params": [], "returns": None, "raises": []},
        )
        gold.append(signature)
        observed.append(_observed(signature))
        report = evaluate_static(gold, (), observed)
        recall = _metric(report, "declaration_recall_python")
        self.assertEqual(
            (recall.numerator, recall.denominator, recall.value, recall.minimum),
            (97, 100, 0.97, 0.95),
        )
        self.assertTrue(recall.passed)
        minimums = {metric.name: metric.minimum for metric in report.metrics}
        self.assertEqual(
            minimums,
            {
                "declaration_micro_precision": 0.99,
                "declaration_micro_recall": 0.97,
                "declaration_precision_java": 0.97,
                "declaration_recall_java": 0.95,
                "declaration_precision_python": 0.97,
                "declaration_recall_python": 0.95,
                "declaration_precision_typescript": 0.97,
                "declaration_recall_typescript": 0.95,
                "declaration_precision_tsx": 0.97,
                "declaration_recall_tsx": 0.95,
                "kind_accuracy": 0.99,
                "container_accuracy": 0.99,
                "visibility_accuracy": 0.99,
                "signature_accuracy": 0.95,
                "signature_accuracy_python": 0.90,
                "relation_exact_accuracy": 0.97,
                "call_precision_java": 0.95,
                "call_recall_java": 0.85,
                "call_precision_python": 0.95,
                "call_recall_python": 0.85,
                "call_precision_typescript": 0.95,
                "call_recall_typescript": 0.85,
                "call_precision_tsx": 0.95,
                "call_recall_tsx": 0.85,
                "call_precision_lua": 0.90,
                "call_recall_lua": 0.70,
                "call_order_accuracy": 0.85,
                "strong_x0_precision": 1.0,
                "strong_x0_recall": 1.0,
                "zero_classification_accuracy": 1.0,
                "approximate_precision": 1.0,
                "approximate_recall": 0.80,
            },
        )

        passing = StaticReport((Metric("passing", 1, 1, 1.0, 1.0, True),), ())
        require_thresholds(passing)
        failing = StaticReport(
            (Metric("failing", 0, 1, 0.0, 1.0, False),),
            ("failing: 0/1",),
        )
        with self.assertRaisesRegex(ValueError, "failing: 0/1"):
            require_thresholds(failing)

    def test_declaration_micro_precision_recall_and_negative_decoys(self) -> None:
        a = _gold("a", "declaration", _subject("a"), {"name": "a"})
        b = _gold("b", "declaration", _subject("b"), {"name": "b"})
        decoy = _gold(
            "decoy",
            "declaration",
            _subject("decoy"),
            {"name": "decoy"},
            expected=False,
        )
        extra = _gold(
            "extra",
            "declaration",
            _subject("extra"),
            {"name": "extra"},
        )
        report = evaluate_static(
            (a, b, decoy),
            (),
            (_observed(a), _observed(decoy), _observed(extra)),
        )

        precision = _metric(report, "declaration_micro_precision")
        recall = _metric(report, "declaration_micro_recall")
        language_precision = _metric(report, "declaration_precision_python")
        language_recall = _metric(report, "declaration_recall_python")
        self.assertEqual(
            (precision.numerator, precision.denominator, precision.value),
            (1, 3, 1 / 3),
        )
        self.assertEqual(
            (recall.numerator, recall.denominator, recall.value),
            (1, 2, 0.5),
        )
        self.assertEqual(language_precision.value, precision.value)
        self.assertEqual(language_recall.value, recall.value)
        failure = next(
            item
            for item in report.failures
            if item.startswith("declaration_micro_precision:")
        )
        self.assertIn("1/3 = 33.33%", failure)
        self.assertIn("requires >= 99.00%", failure)
        self.assertIn("false positives:", failure)

    def test_attribute_accuracy_uses_only_matched_declarations(self) -> None:
        a = _gold("a-decl", "declaration", _subject("a"), {"name": "a"})
        b = _gold("b-decl", "declaration", _subject("b"), {"name": "b"})
        a_kind = _gold("a-kind", "kind", a.subject, {"kind": "fn"})
        b_kind = _gold("b-kind", "kind", b.subject, {"kind": "fn"})
        a_container = _gold("a-container", "container", a.subject, {"container": []})
        b_container = _gold("b-container", "container", b.subject, {"container": []})
        a_visibility = _gold(
            "a-visibility", "visibility", a.subject, {"visibility": "private"}
        )
        b_visibility = _gold(
            "b-visibility", "visibility", b.subject, {"visibility": "private"}
        )
        signature: dict[str, object] = {
            "text": "a()",
            "params": [],
            "returns": None,
            "raises": [],
        }
        a_signature = _gold("a-signature", "signature", a.subject, signature)
        b_signature = _gold("b-signature", "signature", b.subject, signature)

        report = evaluate_static(
            (
                a,
                b,
                a_kind,
                b_kind,
                a_container,
                b_container,
                a_visibility,
                b_visibility,
                a_signature,
                b_signature,
            ),
            (),
            (
                _observed(a),
                _observed(a_kind),
                _observed(a_container),
                _observed(a_visibility, value={"visibility": "pub"}),
                _observed(a_signature),
            ),
        )

        self.assertEqual(
            (
                _metric(report, "kind_accuracy").numerator,
                _metric(report, "kind_accuracy").denominator,
            ),
            (1, 1),
        )
        self.assertEqual(
            (
                _metric(report, "container_accuracy").numerator,
                _metric(report, "container_accuracy").denominator,
            ),
            (1, 1),
        )
        visibility = _metric(report, "visibility_accuracy")
        self.assertEqual((visibility.numerator, visibility.denominator), (0, 1))
        signature_metric = _metric(report, "signature_accuracy_python")
        self.assertEqual(
            (signature_metric.numerator, signature_metric.denominator),
            (1, 1),
        )

    def test_relation_set_and_call_occurrence_and_order_formulas(self) -> None:
        owner = _subject("owner")
        x = json.loads(_subject("x"))
        y = json.loads(_subject("y"))
        z = json.loads(_subject("z"))
        relation_x = _gold(
            "relation-x",
            "relation",
            owner,
            {"kind": "component", "target": {"symbol": x}},
        )
        relation_y = _gold(
            "relation-y",
            "relation",
            owner,
            {"kind": "component", "target": {"symbol": y}},
        )
        relation_z = _gold(
            "relation-z",
            "relation",
            owner,
            {"kind": "component", "target": {"symbol": z}},
        )
        calls = (
            _gold("call-x-0", "call", owner, {"target": x, "ordinal": 0}),
            _gold("call-y-1", "call", owner, {"target": y, "ordinal": 1}),
            _gold("call-x-2", "call", owner, {"target": x, "ordinal": 2}),
        )
        order = _gold(
            "order",
            "call_order",
            owner,
            {"targets": [x, y, x]},
        )
        observed_calls = (
            _observed(calls[0], value={"target": x, "ordinal": 7}),
            _observed(calls[2], value={"target": x, "ordinal": 8}),
            _observed(calls[1], value={"target": z, "ordinal": 9}),
        )
        report = evaluate_static(
            (relation_x, relation_y, *calls, order),
            (),
            (
                _observed(relation_x),
                _observed(relation_z),
                *observed_calls,
                _observed(order, value={"targets": [x, x, y]}),
            ),
        )

        relation = _metric(report, "relation_exact_accuracy")
        self.assertEqual(
            (relation.numerator, relation.denominator, relation.value),
            (1, 3, 1 / 3),
        )
        call_precision = _metric(report, "call_precision_python")
        call_recall = _metric(report, "call_recall_python")
        self.assertEqual(
            (call_precision.numerator, call_precision.denominator),
            (2, 3),
        )
        self.assertEqual((call_recall.numerator, call_recall.denominator), (2, 3))
        call_order = _metric(report, "call_order_accuracy")
        self.assertEqual((call_order.numerator, call_order.denominator), (0, 1))

    def test_only_scoring_exclusions_suppress_their_exact_scope(self) -> None:
        positive = _gold(
            "positive", "declaration", _subject("positive"), {"name": "positive"}
        )
        negative = _gold(
            "negative",
            "declaration",
            _subject("negative"),
            {"name": "negative"},
            expected=False,
        )

        def exclusion(scope: object, *, name: str, line: int | None = 1) -> Exclusion:
            return Exclusion(
                name,
                "sample",
                REVISION,
                "src/app.py",
                line,
                "python",
                scope
                if isinstance(scope, str)
                else json.dumps(scope, sort_keys=True, separators=(",", ":")),
                "test_scope",
            )

        with self.assertRaisesRegex(ValueError, "overlaps explicit gold fact"):
            evaluate_static(
                (positive,),
                (
                    exclusion(
                        [
                            "fact",
                            "declaration",
                            json.loads(positive.subject),
                            {"name": "positive"},
                        ],
                        name="positive-overlap",
                    ),
                ),
                (),
            )
        with self.assertRaisesRegex(ValueError, "overlaps explicit gold fact"):
            evaluate_static(
                (negative,),
                (
                    exclusion(
                        ["category", "declaration", json.loads(negative.subject)],
                        name="negative-overlap",
                    ),
                ),
                (),
            )

        exact = _gold("exact", "declaration", _subject("exact"), {"name": "exact"})
        category = _gold(
            "category", "declaration", _subject("category"), {"name": "category"}
        )
        documented = _gold(
            "documented",
            "declaration",
            _subject("documented"),
            {"name": "documented"},
        )
        file_fact = _gold(
            "file",
            "declaration",
            _subject("file", path="ignored.py"),
            {"name": "file"},
        )
        report = evaluate_static(
            (positive,),
            (
                exclusion(
                    [
                        "fact",
                        "declaration",
                        json.loads(exact.subject),
                        {"name": "exact"},
                    ],
                    name="exact-exclusion",
                ),
                exclusion(
                    ["category", "declaration", json.loads(category.subject)],
                    name="category-exclusion",
                ),
                exclusion(
                    ["candidate", "function", "documented"],
                    name="candidate-documentation",
                ),
                Exclusion(
                    "file-exclusion",
                    "sample",
                    REVISION,
                    "ignored.py",
                    None,
                    "python",
                    "file",
                    "unsupported",
                ),
            ),
            (
                _observed(positive),
                _observed(exact),
                _observed(category),
                _observed(documented),
                _observed(file_fact),
            ),
        )
        precision = _metric(report, "declaration_micro_precision")
        self.assertEqual((precision.numerator, precision.denominator), (1, 2))

    def test_advisory_metrics_are_nonvacuous_and_closed_world(self) -> None:
        strong_true = _gold(
            "strong-true",
            "strong_x0",
            _subject("unused", path="synthetic.py"),
            {"classification": "strong"},
            corpus="synthetic",
        )
        strong_false = _gold(
            "strong-false",
            "strong_x0",
            _subject("used", path="synthetic.py"),
            {"classification": "strong"},
            expected=False,
            corpus="synthetic",
        )
        empty = evaluate_static((strong_true, strong_false), (), ())
        empty_precision = _metric(empty, "strong_x0_precision")
        self.assertEqual(
            (empty_precision.numerator, empty_precision.denominator), (0, 0)
        )
        self.assertFalse(empty_precision.passed)

        zero_facts = tuple(
            _gold(
                f"zero-{name}",
                "zero_classification",
                _subject(name, path="synthetic.py"),
                {"classification": classification},
                corpus="synthetic",
            )
            for name, classification in (
                ("none", "none"),
                ("strong", "strong"),
                ("uncertain", "uncertain"),
            )
        )
        left = _subject("clone_a", path="synthetic.py")
        right = json.loads(_subject("clone_b", path="synthetic.py"))
        decoy = json.loads(_subject("not_clone", path="synthetic.py"))
        approximate_true = _gold(
            "approximate-true",
            "approximate",
            left,
            {"peer": right},
            corpus="synthetic",
        )
        approximate_false = _gold(
            "approximate-false",
            "approximate",
            left,
            {"peer": decoy},
            expected=False,
            corpus="synthetic",
        )
        report = evaluate_static(
            (
                strong_true,
                strong_false,
                *zero_facts,
                approximate_true,
                approximate_false,
            ),
            (),
            (
                _observed(strong_false),
                _observed(zero_facts[0]),
                _observed(zero_facts[1], value={"classification": "none"}),
                _observed(approximate_true),
                _observed(approximate_false),
            ),
        )
        strong_precision = _metric(report, "strong_x0_precision")
        strong_recall = _metric(report, "strong_x0_recall")
        self.assertEqual(
            (strong_precision.numerator, strong_precision.denominator), (0, 1)
        )
        self.assertEqual((strong_recall.numerator, strong_recall.denominator), (0, 1))
        zero = _metric(report, "zero_classification_accuracy")
        self.assertEqual((zero.numerator, zero.denominator), (1, 3))
        approximate_precision = _metric(report, "approximate_precision")
        approximate_recall = _metric(report, "approximate_recall")
        self.assertEqual(
            (approximate_precision.numerator, approximate_precision.denominator),
            (1, 2),
        )
        self.assertEqual(
            (approximate_recall.numerator, approximate_recall.denominator),
            (1, 1),
        )

    def test_failure_output_caps_stable_ids_and_never_prints_absolute_paths(
        self,
    ) -> None:
        gold = tuple(
            _gold(
                f"missing-{index}",
                "declaration",
                _subject(f"missing_{index}"),
                {"name": f"missing_{index}"},
            )
            for index in range(25)
        )
        observed = tuple(
            _observed(
                _gold(
                    f"extra-{index}",
                    "declaration",
                    _subject(f"extra_{index}"),
                    {"name": f"extra_{index}"},
                )
            )
            for index in range(25)
        )
        report = evaluate_static(gold, (), observed)
        failure = next(
            item
            for item in report.failures
            if item.startswith("declaration_micro_precision:")
        )
        self.assertNotIn(str(PROJECT_ROOT), failure)
        ids = failure.split("false positives: ", 1)[1].split("; false negatives: ")
        self.assertLessEqual(sum(part.count(",") + 1 for part in ids), 20)


if __name__ == "__main__":
    unittest.main()
