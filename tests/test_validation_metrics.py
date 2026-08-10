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

from hologram import analysis, pipeline, render
from hologram.config import ProjectConfig, default_config
from hologram.resolve import ResolutionStatus
from validation.observe import ObservedFact, observe_project, observe_rendered_map

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_ROOT = PROJECT_ROOT / "validation" / "fixtures" / "advertised"
STATE = "a" * 64


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


if __name__ == "__main__":
    unittest.main()
