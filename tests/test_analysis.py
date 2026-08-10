from __future__ import annotations

import hashlib
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

from hologram.analysis import ZeroReference, analyze_project
from hologram.model import (
    CallKind,
    CallRef,
    FileIR,
    ImportRef,
    Language,
    ProjectIR,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)
from hologram.resolve import (
    ResolutionResult,
    ResolutionStatus,
    ResolvedCall,
    ResolvedImport,
    ResolvedReference,
)

_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)


def _span(file: str, line: int = 1) -> SourceSpan:
    return SourceSpan(file, line, 0, line, 1)


def _source(
    file: str,
    role: SourceRole = SourceRole.PRODUCTION,
    *,
    language: Language = Language.PYTHON,
    raw: bytes = b"\n",
) -> SourceFile:
    return SourceFile(
        Path("/repo") / file,
        file,
        language,
        role,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def _symbol(
    file: str,
    name: str,
    *,
    visibility: Visibility = Visibility.PRIVATE,
    kind: SymbolKind = SymbolKind.FUNCTION,
    container: tuple[str, ...] = (),
    line: int = 1,
    annotations: tuple[str, ...] = (),
    modifiers: tuple[str, ...] = (),
    returns: str | None = None,
    language: Language = Language.PYTHON,
    params: tuple[str, ...] = (),
) -> Symbol:
    signature_key = f"({','.join(params)})" if kind in _CALLABLE_KINDS else ""
    identifier = SymbolId(
        language,
        file,
        container,
        kind,
        name,
        signature_key,
    )
    return Symbol(
        identifier,
        _span(file, line),
        visibility,
        f"{name}({','.join(params)})" if signature_key else name,
        params=params,
        returns=returns,
        annotations=annotations,
        modifiers=modifiers,
    )


def _file(
    file: str,
    *,
    role: SourceRole = SourceRole.PRODUCTION,
    symbols: tuple[Symbol, ...] = (),
    calls: tuple[CallRef, ...] = (),
    imports: tuple[ImportRef, ...] = (),
    references: tuple[ReferenceRef, ...] = (),
    raw: bytes = b"\n",
    language: Language = Language.PYTHON,
) -> FileIR:
    return FileIR(
        _source(file, role, language=language, raw=raw),
        symbols=symbols,
        calls=calls,
        imports=imports,
        references=references,
    )


def _project(*files: FileIR) -> ProjectIR:
    return ProjectIR(Path("/repo"), files, (), True)


def _resolution(
    *,
    imports: tuple[ResolvedImport, ...] = (),
    calls: tuple[ResolvedCall, ...] = (),
    references: tuple[ResolvedReference, ...] = (),
) -> ResolutionResult:
    return ResolutionResult(imports, calls, references, ())


def _reference(
    file: str,
    target: SymbolId,
    *,
    line: int,
    owner: SymbolId | None = None,
    confidence: ReferenceConfidence = ReferenceConfidence.DEFINITE,
    context: ReferenceContext = ReferenceContext.CODE,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
    candidates: tuple[SymbolId, ...] | None = None,
) -> tuple[ReferenceRef, ResolvedReference]:
    fact = ReferenceRef(
        owner,
        _span(file, line),
        target.name,
        None,
        ReferenceKind.NAME,
        context,
        confidence,
    )
    candidate_ids = (target,) if candidates is None else candidates
    resolved_target = target if status is ResolutionStatus.RESOLVED else None
    return fact, ResolvedReference(fact, status, resolved_target, candidate_ids)


def _call(
    caller: SymbolId,
    target: SymbolId,
    *,
    line: int,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
    candidates: tuple[SymbolId, ...] | None = None,
) -> tuple[CallRef, ResolvedCall]:
    fact = CallRef(
        caller, _span(caller.file, line), target.name, None, CallKind.CALL, 0
    )
    candidate_ids = (target,) if candidates is None else candidates
    resolved_target = target if status is ResolutionStatus.RESOLVED else None
    return fact, ResolvedCall(fact, status, resolved_target, candidate_ids, target.name)


def reference_fixture() -> tuple[ProjectIR, ResolutionResult, Mapping[str, SymbolId]]:
    used = _symbol("lib/used.py", "shared")
    shadow = _symbol("lib/shadow.py", "shared")
    caller_a = _symbol("app/a.py", "caller_a")
    caller_b = _symbol("app/b.py", "caller_b")

    a_first, a_first_result = _reference(
        caller_a.file, used.id, line=2, owner=caller_a.id
    )
    a_second, a_second_result = _reference(
        caller_a.file, used.id, line=3, owner=caller_a.id
    )
    b_ref, b_result = _reference(caller_b.file, used.id, line=2, owner=caller_b.id)
    files = (
        _file(used.file, symbols=(used,)),
        _file(shadow.file, symbols=(shadow,)),
        _file(
            caller_a.file,
            symbols=(caller_a,),
            references=(a_first, a_second),
        ),
        _file(caller_b.file, symbols=(caller_b,), references=(b_ref,)),
        _file(
            "app/comment.py",
            raw=b"# shared only appears in this comment\n",
        ),
    )
    resolution = _resolution(references=(b_result, a_second_result, a_first_result))
    ids = MappingProxyType({"used": used.id, "shadow": shadow.id})
    return _project(*files), resolution, ids


def dynamic_fixture() -> tuple[ProjectIR, ResolutionResult, Mapping[str, SymbolId]]:
    public = _symbol("api.py", "surface", visibility=Visibility.PUBLIC)
    callback = _symbol("callbacks.py", "handle")
    possible, possible_result = _reference(
        "config/routes.yaml",
        callback.id,
        line=2,
        confidence=ReferenceConfidence.POSSIBLE,
        context=ReferenceContext.CONFIG,
    )
    project = _project(
        _file(public.file, symbols=(public,)),
        _file(callback.file, symbols=(callback,)),
        _file("config/routes.yaml", references=(possible,)),
    )
    return (
        project,
        _resolution(references=(possible_result,)),
        MappingProxyType({"public": public.id, "callback": callback.id}),
    )


def test_reference_fixture() -> tuple[ProjectIR, ResolutionResult, SymbolId]:
    target = _symbol("app/api.py", "serve")
    test = _symbol("tests/test_api.py", "test_serve")
    fact, result = _call(test.id, target.id, line=2)
    project = _project(
        _file(target.file, symbols=(target,)),
        _file(
            test.file,
            role=SourceRole.TEST,
            symbols=(test,),
            calls=(fact,),
        ),
    )
    return project, _resolution(calls=(result,)), target.id


def nonproduction_declaration_fixture() -> tuple[
    ProjectIR, ResolutionResult, Mapping[str, SymbolId]
]:
    test_helper = _symbol("tests/helpers.py", "helper")
    generated_helper = _symbol("generated/client.py", "helper")
    project = _project(
        _file(
            test_helper.file,
            role=SourceRole.TEST,
            symbols=(test_helper,),
        ),
        _file(
            generated_helper.file,
            role=SourceRole.GENERATED,
            symbols=(generated_helper,),
        ),
    )
    ids = MappingProxyType(
        {
            "test_helper": test_helper.id,
            "generated_helper": generated_helper.id,
        }
    )
    return project, _resolution(), ids


def generated_caller_fixture() -> tuple[ProjectIR, ResolutionResult, SymbolId]:
    target = _symbol("app/api.py", "request")
    generated = _symbol("generated/client.py", "invoke")
    fact, result = _call(generated.id, target.id, line=3)
    project = _project(
        _file(target.file, symbols=(target,)),
        _file(
            generated.file,
            role=SourceRole.GENERATED,
            symbols=(generated,),
            calls=(fact,),
        ),
    )
    return project, _resolution(calls=(result,)), target.id


def intrinsic_reachability_fixture() -> tuple[
    ProjectIR, ResolutionResult, Mapping[str, SymbolId]
]:
    bean = _symbol("app.py", "bean", annotations=("Bean",))
    override = _symbol("app.py", "override", modifiers=("override",), line=2)
    main = _symbol("app.py", "main", annotations=("entrypoint",), line=3)
    project = _project(_file("app.py", symbols=(main, override, bean)))
    ids = MappingProxyType({"bean": bean.id, "override": override.id, "main": main.id})
    return project, _resolution(), ids


def zero_decision_fixture() -> tuple[
    ProjectIR, ResolutionResult, Mapping[str, SymbolId]
]:
    same_file_used = _symbol("app.py", "same_file_used")
    same_file_caller = _symbol("app.py", "caller", line=2)
    protected_surface = _symbol(
        "protected.py", "surface", visibility=Visibility.PROTECTED
    )
    ambiguous_candidate = _symbol("choices.py", "candidate")
    other_candidate = _symbol("other.py", "candidate")
    arbitrary_string_decoy = _symbol("decoy.py", "decoy")
    reexported_private = _symbol("internal.py", "exported")

    same_fact, same_result = _call(same_file_caller.id, same_file_used.id, line=3)
    ambiguous_fact, ambiguous_result = _reference(
        "consumer.py",
        ambiguous_candidate.id,
        line=2,
        status=ResolutionStatus.AMBIGUOUS,
        candidates=(ambiguous_candidate.id, other_candidate.id),
    )
    reexport_fact = ImportRef(
        _span("index.py", 1),
        "internal",
        "exported",
        None,
        reexport=True,
    )
    reexport_result = ResolvedImport(
        "index.py",
        reexport_fact,
        ResolutionStatus.RESOLVED,
        (reexported_private.file,),
        (reexported_private.id,),
    )
    project = _project(
        _file(
            "app.py",
            symbols=(same_file_used, same_file_caller),
            calls=(same_fact,),
        ),
        _file(protected_surface.file, symbols=(protected_surface,)),
        _file(ambiguous_candidate.file, symbols=(ambiguous_candidate,)),
        _file(other_candidate.file, symbols=(other_candidate,)),
        _file(
            arbitrary_string_decoy.file,
            symbols=(arbitrary_string_decoy,),
            raw=b'"decoy"  # decoy\n',
        ),
        _file(reexported_private.file, symbols=(reexported_private,)),
        _file("consumer.py", references=(ambiguous_fact,)),
        _file("index.py", imports=(reexport_fact,)),
    )
    resolution = _resolution(
        imports=(reexport_result,),
        calls=(same_result,),
        references=(ambiguous_result,),
    )
    ids = MappingProxyType(
        {
            "same_file_used": same_file_used.id,
            "protected_surface": protected_surface.id,
            "ambiguous_candidate": ambiguous_candidate.id,
            "arbitrary_string_decoy": arbitrary_string_decoy.id,
            "reexported_private": reexported_private.id,
        }
    )
    return project, resolution, ids


def import_reference_fixture() -> tuple[
    ProjectIR, ResolutionResult, Mapping[str, SymbolId]
]:
    imported = _symbol("lib/used.py", "used")
    ambiguous = _symbol("lib/first.py", "choice")
    other_ambiguous = _symbol("lib/second.py", "choice")
    wildcard_decoy = _symbol("lib/wildcard.py", "wildcard_decoy")
    test_wildcard_decoy = _symbol("lib/test_wildcard.py", "test_wildcard_decoy")
    test_reexported = _symbol("lib/test_support.py", "test_support")
    generated_reexported = _symbol("lib/generated_support.py", "generated_support")
    production_module = _symbol(
        "lib/production_module.py", "production_module", kind=SymbolKind.MODULE
    )
    test_module = _symbol("lib/test_module.py", "test_module", kind=SymbolKind.MODULE)
    generated_module = _symbol(
        "lib/generated_module.py", "generated_module", kind=SymbolKind.MODULE
    )
    named_fact = ImportRef(
        _span("consumer.py", 1),
        "lib.used",
        "used",
        None,
    )
    ambiguous_fact = ImportRef(
        _span("consumer.py", 2),
        "lib.choices",
        "choice",
        None,
    )
    wildcard_fact = ImportRef(
        _span("consumer.py", 3),
        "lib.wildcard",
        None,
        None,
        wildcard=True,
    )
    test_reexport_fact = ImportRef(
        _span("tests/index.py", 1),
        "lib.test_support",
        "test_support",
        None,
        reexport=True,
    )
    generated_reexport_fact = ImportRef(
        _span("generated/index.py", 1),
        "lib.generated_support",
        "generated_support",
        None,
        reexport=True,
    )
    test_wildcard_fact = ImportRef(
        _span("tests/wildcard.py", 1),
        "lib.test_wildcard",
        None,
        None,
        wildcard=True,
        reexport=True,
    )
    production_module_fact = ImportRef(
        _span("module_consumer.py", 1),
        "lib.production_module",
        None,
        "production",
    )
    test_module_fact = ImportRef(
        _span("tests/module_consumer.py", 1),
        "lib.test_module",
        None,
        "tested",
    )
    generated_module_fact = ImportRef(
        _span("generated/module_consumer.py", 1),
        "lib.generated_module",
        None,
        "generated",
    )
    resolution = _resolution(
        imports=(
            ResolvedImport(
                "consumer.py",
                named_fact,
                ResolutionStatus.RESOLVED,
                (imported.file,),
                (imported.id,),
            ),
            ResolvedImport(
                "consumer.py",
                ambiguous_fact,
                ResolutionStatus.AMBIGUOUS,
                (ambiguous.file, other_ambiguous.file),
                (ambiguous.id, other_ambiguous.id),
            ),
            ResolvedImport(
                "consumer.py",
                wildcard_fact,
                ResolutionStatus.RESOLVED,
                (wildcard_decoy.file,),
                (wildcard_decoy.id,),
            ),
            ResolvedImport(
                "tests/index.py",
                test_reexport_fact,
                ResolutionStatus.RESOLVED,
                (test_reexported.file,),
                (test_reexported.id,),
            ),
            ResolvedImport(
                "generated/index.py",
                generated_reexport_fact,
                ResolutionStatus.RESOLVED,
                (generated_reexported.file,),
                (generated_reexported.id,),
            ),
            ResolvedImport(
                "tests/wildcard.py",
                test_wildcard_fact,
                ResolutionStatus.RESOLVED,
                (test_wildcard_decoy.file,),
                (test_wildcard_decoy.id,),
            ),
            ResolvedImport(
                "module_consumer.py",
                production_module_fact,
                ResolutionStatus.RESOLVED,
                (production_module.file,),
                (production_module.id,),
            ),
            ResolvedImport(
                "tests/module_consumer.py",
                test_module_fact,
                ResolutionStatus.RESOLVED,
                (test_module.file,),
                (test_module.id,),
            ),
            ResolvedImport(
                "generated/module_consumer.py",
                generated_module_fact,
                ResolutionStatus.RESOLVED,
                (generated_module.file,),
                (generated_module.id,),
            ),
        )
    )
    project = _project(
        _file(imported.file, symbols=(imported,)),
        _file(ambiguous.file, symbols=(ambiguous,)),
        _file(other_ambiguous.file, symbols=(other_ambiguous,)),
        _file(wildcard_decoy.file, symbols=(wildcard_decoy,)),
        _file(test_wildcard_decoy.file, symbols=(test_wildcard_decoy,)),
        _file(test_reexported.file, symbols=(test_reexported,)),
        _file(generated_reexported.file, symbols=(generated_reexported,)),
        _file(production_module.file, symbols=(production_module,)),
        _file(test_module.file, symbols=(test_module,)),
        _file(generated_module.file, symbols=(generated_module,)),
        _file(
            "consumer.py",
            imports=(named_fact, ambiguous_fact, wildcard_fact),
        ),
        _file(
            "tests/index.py",
            role=SourceRole.TEST,
            imports=(test_reexport_fact,),
        ),
        _file(
            "generated/index.py",
            role=SourceRole.GENERATED,
            imports=(generated_reexport_fact,),
        ),
        _file(
            "tests/wildcard.py",
            role=SourceRole.TEST,
            imports=(test_wildcard_fact,),
        ),
        _file("module_consumer.py", imports=(production_module_fact,)),
        _file(
            "tests/module_consumer.py",
            role=SourceRole.TEST,
            imports=(test_module_fact,),
        ),
        _file(
            "generated/module_consumer.py",
            role=SourceRole.GENERATED,
            imports=(generated_module_fact,),
        ),
    )
    ids = MappingProxyType(
        {
            "imported": imported.id,
            "ambiguous": ambiguous.id,
            "wildcard_decoy": wildcard_decoy.id,
            "test_wildcard_decoy": test_wildcard_decoy.id,
            "test_reexported": test_reexported.id,
            "generated_reexported": generated_reexported.id,
            "production_module": production_module.id,
            "test_module": test_module.id,
            "generated_module": generated_module.id,
        }
    )
    return project, resolution, ids


class ReferenceAnalysisTest(unittest.TestCase):
    def test_distinct_files_and_same_named_symbols_do_not_cross_contaminate(
        self,
    ) -> None:
        project, resolution, ids = reference_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=2)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(
            tuple(map(str, by_id[ids["used"]].production_files)),
            ("app/a.py", "app/b.py"),
        )
        self.assertEqual(by_id[ids["shadow"]].production_files, ())
        self.assertEqual(by_id[ids["shadow"]].possible_files, ())
        self.assertEqual(by_id[ids["shadow"]].zero, ZeroReference.STRONG)

    def test_public_zero_and_dynamic_private_are_uncertain(self) -> None:
        project, resolution, ids = dynamic_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[ids["public"]].zero, ZeroReference.UNCERTAIN)
        self.assertEqual(by_id[ids["callback"]].zero, ZeroReference.UNCERTAIN)
        self.assertEqual(
            tuple(map(str, by_id[ids["callback"]].possible_files)),
            ("config/routes.yaml",),
        )

    def test_test_reference_is_independent_from_production_fan_in(self) -> None:
        project, resolution, symbol_id = test_reference_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        facts = next(
            item.references for item in analyzed.symbols if item.symbol.id == symbol_id
        )
        self.assertEqual(facts.production_files, ())
        self.assertEqual(tuple(map(str, facts.test_files)), ("tests/test_api.py",))
        self.assertEqual(facts.zero, ZeroReference.UNCERTAIN)

    def test_test_and_generated_declarations_never_get_dead_markers(self) -> None:
        project, resolution, ids = nonproduction_declaration_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[ids["test_helper"]].zero, ZeroReference.NONE)
        self.assertEqual(by_id[ids["generated_helper"]].zero, ZeroReference.NONE)

    def test_generated_caller_suppresses_zero_without_test_or_fan_in_marker(
        self,
    ) -> None:
        project, resolution, target = generated_caller_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=1)
        facts = next(
            item.references for item in analyzed.symbols if item.symbol.id == target
        )
        self.assertEqual(facts.production_files, ())
        self.assertEqual(facts.test_files, ())
        self.assertEqual(
            tuple(map(str, facts.generated_files)),
            ("generated/client.py",),
        )
        self.assertEqual(facts.zero, ZeroReference.UNCERTAIN)

    def test_intrinsic_framework_override_and_entrypoint_reachability_is_uncertain(
        self,
    ) -> None:
        project, resolution, ids = intrinsic_reachability_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        for name in ("bean", "override", "main"):
            self.assertEqual(by_id[ids[name]].production_files, ())
            self.assertEqual(by_id[ids[name]].zero, ZeroReference.UNCERTAIN)

    def test_java_entrypoint_requires_public_static_main_with_string_array(
        self,
    ) -> None:
        entrypoint = _symbol(
            "src/Main.java",
            "main",
            language=Language.JAVA,
            kind=SymbolKind.METHOD,
            visibility=Visibility.PUBLIC,
            modifiers=("public", "static"),
            params=("java.lang.String []",),
        )
        private_main = _symbol(
            "src/PrivateMain.java",
            "main",
            language=Language.JAVA,
            kind=SymbolKind.METHOD,
            modifiers=("static",),
            params=("String[]",),
        )
        wrong_parameter = _symbol(
            "src/WrongMain.java",
            "main",
            language=Language.JAVA,
            kind=SymbolKind.METHOD,
            modifiers=("static",),
            params=("int[]",),
        )
        project = _project(
            _file(
                entrypoint.file,
                language=Language.JAVA,
                symbols=(entrypoint,),
            ),
            _file(
                private_main.file,
                language=Language.JAVA,
                symbols=(private_main,),
            ),
            _file(
                wrong_parameter.file,
                language=Language.JAVA,
                symbols=(wrong_parameter,),
            ),
        )
        analyzed = analyze_project(project, _resolution(), hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[entrypoint.id].zero, ZeroReference.UNCERTAIN)
        self.assertEqual(by_id[private_main.id].zero, ZeroReference.STRONG)
        self.assertEqual(by_id[wrong_parameter.id].zero, ZeroReference.STRONG)

    def test_zero_decision_table_covers_same_file_protected_and_ambiguity(
        self,
    ) -> None:
        project, resolution, ids = zero_decision_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(by_id[ids["same_file_used"]].production_files, ())
        self.assertEqual(by_id[ids["same_file_used"]].zero, ZeroReference.NONE)
        self.assertEqual(
            by_id[ids["protected_surface"]].zero,
            ZeroReference.UNCERTAIN,
        )
        self.assertEqual(
            by_id[ids["ambiguous_candidate"]].zero,
            ZeroReference.UNCERTAIN,
        )
        self.assertEqual(
            tuple(map(str, by_id[ids["ambiguous_candidate"]].possible_files)),
            ("consumer.py",),
        )
        self.assertEqual(
            by_id[ids["arbitrary_string_decoy"]].zero,
            ZeroReference.STRONG,
        )
        self.assertEqual(
            by_id[ids["reexported_private"]].zero,
            ZeroReference.UNCERTAIN,
        )

    def test_named_import_edges_are_definite_or_possible_without_wildcard_fanout(
        self,
    ) -> None:
        project, resolution, ids = import_reference_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=10)
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(
            tuple(map(str, by_id[ids["imported"]].production_files)),
            ("consumer.py",),
        )
        self.assertEqual(by_id[ids["imported"]].zero, ZeroReference.NONE)
        self.assertEqual(
            tuple(map(str, by_id[ids["ambiguous"]].possible_files)),
            ("consumer.py",),
        )
        self.assertEqual(by_id[ids["ambiguous"]].zero, ZeroReference.UNCERTAIN)
        self.assertEqual(
            by_id[ids["wildcard_decoy"]].zero,
            ZeroReference.STRONG,
        )
        self.assertEqual(
            tuple(map(str, by_id[ids["test_reexported"]].test_files)),
            ("tests/index.py",),
        )
        self.assertEqual(
            tuple(map(str, by_id[ids["generated_reexported"]].generated_files)),
            ("generated/index.py",),
        )
        self.assertEqual(
            by_id[ids["test_wildcard_decoy"]].zero,
            ZeroReference.STRONG,
        )
        self.assertEqual(
            tuple(map(str, by_id[ids["production_module"]].production_files)),
            ("module_consumer.py",),
        )
        self.assertEqual(
            tuple(map(str, by_id[ids["test_module"]].test_files)),
            ("tests/module_consumer.py",),
        )
        self.assertEqual(
            tuple(map(str, by_id[ids["generated_module"]].generated_files)),
            ("generated/module_consumer.py",),
        )

    def test_results_are_frozen_sorted_and_preserve_phase_inputs(self) -> None:
        project, resolution, _ = intrinsic_reachability_fixture()
        analyzed = analyze_project(project, resolution, hot_threshold=2)
        self.assertIs(analyzed.project, project)
        self.assertIs(analyzed.resolution, resolution)
        self.assertEqual(
            [item.symbol.id.name for item in analyzed.symbols],
            ["bean", "main", "override"],
        )
        self.assertTrue(all(item.body is None for item in analyzed.symbols))
        self.assertTrue(all(item.duplicate_peers == () for item in analyzed.symbols))
        self.assertEqual(analyzed.map_duplicates, ())
        with self.assertRaises(FrozenInstanceError):
            analyzed.symbols = ()  # type: ignore[misc]
        self.assertFalse(hasattr(analyzed, "__dict__"))


if __name__ == "__main__":
    unittest.main()
