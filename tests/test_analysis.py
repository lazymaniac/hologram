from __future__ import annotations

import hashlib
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
from unittest.mock import PropertyMock, patch

from hologram.analysis import (
    ZeroReference,
    _body_index,
    _resolved_body_targets,
    analyze_project,
    canonical_body,
)
from hologram.model import (
    BodyEvent,
    BodyEventKind,
    BodyIR,
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
from hologram.parsers.api import extract_file
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
    bodies: tuple[BodyIR, ...] = (),
    raw: bytes = b"\n",
    language: Language = Language.PYTHON,
) -> FileIR:
    return FileIR(
        _source(file, role, language=language, raw=raw),
        symbols=symbols,
        calls=calls,
        imports=imports,
        references=references,
        bodies=bodies,
    )


def _event(
    symbol: Symbol,
    line: int,
    kind: BodyEventKind,
    text: str,
) -> BodyEvent:
    return BodyEvent(kind, text, _span(symbol.file, line))


def _body(
    symbol: Symbol,
    events: tuple[BodyEvent, ...],
) -> BodyIR:
    end_line = max((event.span.end_line for event in events), default=1)
    return BodyIR(
        symbol.id,
        SourceSpan(symbol.file, 1, 0, end_line, 1),
        events,
    )


def _body_file(
    symbol: Symbol,
    events: tuple[BodyEvent, ...],
    *,
    role: SourceRole = SourceRole.PRODUCTION,
    raw: bytes = b"frozen body bytes\n",
) -> FileIR:
    return _file(
        symbol.file,
        role=role,
        language=symbol.lang,
        symbols=(symbol,),
        bodies=(_body(symbol, events),),
        raw=raw,
    )


def _substantive_events(
    symbol: Symbol,
    *,
    parameter: str = "input_value",
    local: str = "running_total",
    member: str = "amount",
    operator: str = "+",
    literal: str = "<number>",
    control: str = "if",
    call_name: str = "normalize",
) -> tuple[BodyEvent, ...]:
    values = (
        (BodyEventKind.PARAM, parameter),
        (BodyEventKind.LOCAL, local),
        (BodyEventKind.NAME, parameter),
        (BodyEventKind.OPERATOR, "="),
        (BodyEventKind.LITERAL, literal),
        (BodyEventKind.CONTROL_ENTER, control),
        (BodyEventKind.NAME, local),
        (BodyEventKind.OPERATOR, operator),
        (BodyEventKind.LITERAL, "<number>"),
        (BodyEventKind.CALL, call_name),
        (BodyEventKind.NAME, local),
        (BodyEventKind.MEMBER, member),
        (BodyEventKind.KEYWORD, "return"),
        (BodyEventKind.NAME, local),
        (BodyEventKind.CONTROL_EXIT, control),
    )
    return tuple(
        _event(symbol, index + 2, kind, text)
        for index, (kind, text) in enumerate(values)
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

    def test_source_roles_override_test_like_and_production_like_paths(self) -> None:
        tested = _symbol("tests/production_api.py", "tested")
        produced = _symbol("src/production_api.py", "produced")
        test_caller = _symbol("src/checks.py", "test_call")
        production_caller = _symbol("tests/runtime.py", "runtime_call")
        test_declaration = _symbol("src/support.py", "test_support")
        tested_call, tested_result = _call(test_caller.id, tested.id, line=2)
        produced_call, produced_result = _call(
            production_caller.id,
            produced.id,
            line=2,
        )
        project = _project(
            _file(tested.file, symbols=(tested,)),
            _file(produced.file, symbols=(produced,)),
            _file(
                test_caller.file,
                role=SourceRole.TEST,
                symbols=(test_caller,),
                calls=(tested_call,),
            ),
            _file(
                production_caller.file,
                role=SourceRole.PRODUCTION,
                symbols=(production_caller,),
                calls=(produced_call,),
            ),
            _file(
                test_declaration.file,
                role=SourceRole.TEST,
                symbols=(test_declaration,),
            ),
        )
        analyzed = analyze_project(
            project,
            _resolution(calls=(produced_result, tested_result)),
            hot_threshold=1,
        )
        by_id = {item.symbol.id: item.references for item in analyzed.symbols}
        self.assertEqual(
            tuple(map(str, by_id[tested.id].test_files)),
            ("src/checks.py",),
        )
        self.assertEqual(by_id[tested.id].production_files, ())
        self.assertEqual(
            tuple(map(str, by_id[produced.id].production_files)),
            ("tests/runtime.py",),
        )
        self.assertEqual(by_id[test_declaration.id].zero, ZeroReference.NONE)

    def test_fact_permutation_and_overload_identity_are_deterministic(self) -> None:
        selected = _symbol("lib/api.py", "convert", params=("int",))
        unused = _symbol("lib/api.py", "convert", params=("str",), line=2)
        first = _symbol("z.py", "first")
        second = _symbol("a.py", "second")
        first_call, first_result = _call(first.id, selected.id, line=2)
        second_call, second_result = _call(second.id, selected.id, line=2)
        files = (
            _file(
                selected.file,
                symbols=(unused, selected),
            ),
            _file(first.file, symbols=(first,), calls=(first_call,)),
            _file(second.file, symbols=(second,), calls=(second_call,)),
        )
        forward = analyze_project(
            _project(*files),
            _resolution(calls=(first_result, second_result)),
            hot_threshold=2,
        )
        reversed_ = analyze_project(
            _project(*reversed(files)),
            _resolution(calls=(second_result, first_result)),
            hot_threshold=2,
        )
        forward_by_id = {item.symbol.id: item.references for item in forward.symbols}
        reverse_by_id = {item.symbol.id: item.references for item in reversed_.symbols}
        self.assertEqual(forward_by_id, reverse_by_id)
        self.assertEqual(
            tuple(map(str, forward_by_id[selected.id].production_files)),
            ("a.py", "z.py"),
        )
        self.assertEqual(forward_by_id[selected.id].zero, ZeroReference.NONE)
        self.assertEqual(forward_by_id[unused.id].zero, ZeroReference.STRONG)

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


class BodyProfileTest(unittest.TestCase):
    @staticmethod
    def _call_target_map(
        events: tuple[BodyEvent, ...],
        target: SymbolId,
    ) -> Mapping[tuple[BodyEventKind, SourceSpan], SymbolId]:
        call = next(event for event in events if event.kind is BodyEventKind.CALL)
        return MappingProxyType({(BodyEventKind.CALL, call.span): target})

    def test_comments_formatting_and_local_names_share_exact_profile(self) -> None:
        target = _symbol("lib/normalize.py", "normalize", params=("int",))
        left = _symbol(
            "left.py",
            "transform_value",
            params=("Input",),
            returns="list < int >",
        )
        right = _symbol(
            "right.py",
            "transform_value",
            params=("Input",),
            returns="list<int>",
        )
        left_events = _substantive_events(
            left,
            parameter="incoming_value",
            local="running_total",
        )
        right_events = _substantive_events(
            right,
            parameter="source",
            local="sum_value",
        )
        left_file = _body_file(
            left,
            left_events,
            raw=b"# comments and formatting are frozen but irrelevant\n",
        )
        right_file = _body_file(right, right_events, raw=b"different bytes\n")
        self.assertEqual(
            canonical_body(
                left,
                left_file,
                self._call_target_map(left_events, target.id),
            ),
            canonical_body(
                right,
                right_file,
                self._call_target_map(right_events, target.id),
            ),
        )

    def test_operator_member_literal_control_and_resolved_call_are_preserved(
        self,
    ) -> None:
        target = _symbol("lib/normalize.py", "normalize")
        other_target = _symbol("lib/convert.py", "convert")
        base_symbol = _symbol("base.py", "transform")
        base_events = _substantive_events(base_symbol)
        base = canonical_body(
            base_symbol,
            _body_file(base_symbol, base_events),
            self._call_target_map(base_events, target.id),
        )
        variants: tuple[tuple[str, dict[str, str], SymbolId], ...] = (
            ("operator", {"operator": "-"}, target.id),
            ("member", {"member": "balance"}, target.id),
            ("literal", {"literal": "<string>"}, target.id),
            ("control", {"control": "loop"}, target.id),
            ("call", {}, other_target.id),
        )
        for index, (name, changes, resolved_target) in enumerate(variants, start=1):
            changed = _symbol(f"changed-{index}.py", "transform")
            changed_events = _substantive_events(changed, **changes)
            profile = canonical_body(
                changed,
                _body_file(changed, changed_events),
                self._call_target_map(changed_events, resolved_target),
            )
            with self.subTest(change=name):
                self.assertNotEqual(base.semantic_tokens, profile.semantic_tokens)

    def test_literal_categories_ref_serialization_names_and_control_paths_are_exact(
        self,
    ) -> None:
        symbol = _symbol(
            "src/profile.py",
            "HTTP_response_value",
            params=("value",),
            returns=" list < int > ",
        )
        target = _symbol(
            "lib/target.py",
            "normalize",
            container=("Outer",),
            params=("int",),
        )
        constructed = _symbol(
            "lib/widget.py",
            "Widget",
            kind=SymbolKind.CLASS,
        )
        values = (
            (BodyEventKind.PARAM, "value"),
            (BodyEventKind.LITERAL, "<string>"),
            (BodyEventKind.LITERAL, "<number>"),
            (BodyEventKind.LITERAL, "<bool>"),
            (BodyEventKind.LITERAL, "<null>"),
            (BodyEventKind.LITERAL, "secret-value"),
            (BodyEventKind.CONTROL_ENTER, "loop"),
            (BodyEventKind.CONTROL_ENTER, "if"),
            (BodyEventKind.NAME, "value"),
            (BodyEventKind.CONTROL_EXIT, "if"),
            (BodyEventKind.CONTROL_ENTER, "if"),
            (BodyEventKind.MEMBER, "HTTPServerURL"),
            (BodyEventKind.CALL, "normalize"),
            (BodyEventKind.CONSTRUCT, "Widget"),
            (BodyEventKind.CONTROL_EXIT, "if"),
            (BodyEventKind.CONTROL_EXIT, "loop"),
            (BodyEventKind.CONTROL_ENTER, "try"),
            (BodyEventKind.KEYWORD, "return"),
            (BodyEventKind.CONTROL_EXIT, "try"),
        )
        events = tuple(
            _event(symbol, index + 2, kind, text)
            for index, (kind, text) in enumerate(values)
        )
        call = next(event for event in events if event.kind is BodyEventKind.CALL)
        construct = next(
            event for event in events if event.kind is BodyEventKind.CONSTRUCT
        )
        profile = canonical_body(
            symbol,
            _body_file(symbol, events),
            {
                (BodyEventKind.CALL, call.span): target.id,
                (BodyEventKind.CONSTRUCT, construct.span): constructed.id,
            },
        )
        for category in ("STR", "NUM", "BOOL", "NULL", "OTHER"):
            self.assertIn(f"LITERAL:{category}", profile.semantic_tokens)
        self.assertFalse(
            any("secret-value" in token for token in profile.semantic_tokens)
        )
        self.assertIn(
            'REF:["python","lib/target.py",["Outer"],"fn","normalize","(int)"]',
            profile.semantic_tokens,
        )
        self.assertIn(
            'REF:["python","lib/widget.py",[],"class","Widget",""]',
            profile.semantic_tokens,
        )
        self.assertEqual(
            profile.control_flow,
            ("loop:0", "loop:0/if:0", "loop:0/if:1", "try:1"),
        )
        self.assertEqual(
            profile.resolved_calls,
            frozenset({target.id, constructed.id}),
        )
        self.assertEqual(profile.return_key, "list<int>")
        self.assertEqual(profile.arity, 1)
        self.assertEqual(profile.semantic_size, len(profile.semantic_tokens))
        self.assertEqual(
            profile.name_tokens,
            frozenset({"http", "response", "value"}),
        )
        self.assertEqual(
            profile.ast_shingles,
            frozenset(
                zip(
                    profile.semantic_tokens,
                    profile.semantic_tokens[1:],
                    profile.semantic_tokens[2:],
                    profile.semantic_tokens[3:],
                    profile.semantic_tokens[4:],
                )
            ),
        )

    def test_only_exact_frozen_literal_tags_define_categories(self) -> None:
        symbol = _symbol("src/literals.py", "classify")
        expected = {
            "<string>": "STR",
            "<number>": "NUM",
            "<bool>": "BOOL",
            "<null>": "NULL",
            "string": "OTHER",
            "int": "OTHER",
            "true": "OTHER",
            "null": "OTHER",
            "<STRING>": "OTHER",
            " <number>": "OTHER",
        }
        for payload, category in expected.items():
            event = _event(symbol, 2, BodyEventKind.LITERAL, payload)
            profile = canonical_body(symbol, _body_file(symbol, (event,)), {})
            with self.subTest(payload=payload):
                self.assertEqual(
                    profile.semantic_tokens,
                    (f"LITERAL:{category}",),
                )

    def test_unresolved_real_java_generic_types_ignore_source_spacing(self) -> None:
        compact_raw = b"""\
class C {
    List<String> transform(List<String> value) {
        List<String> copy = value;
        return copy;
    }
}
"""
        spaced_raw = b"""\
class C {
    List < String > transform(List < String > value) {
        List < String > copy = value;
        return copy;
    }
}
"""
        profiles = []
        for file, raw in (("Compact.java", compact_raw), ("Spaced.java", spaced_raw)):
            file_ir = extract_file(_source(file, language=Language.JAVA, raw=raw))
            self.assertFalse(file_ir.diagnostics)
            symbol = next(
                item
                for item in file_ir.symbols
                if item.name == "transform" and item.kind is SymbolKind.METHOD
            )
            profiles.append(canonical_body(symbol, file_ir, {}))
        self.assertEqual(profiles[0], profiles[1])

    def test_ineligible_bodies_record_exact_precedence_and_source_roles(self) -> None:
        target = _symbol("lib/target.py", "target")
        cases: list[tuple[Symbol, FileIR, str]] = []

        test_symbol = _symbol("src/test_named.py", "test helper")
        test_events = _substantive_events(test_symbol)
        cases.append(
            (
                test_symbol,
                _body_file(test_symbol, test_events, role=SourceRole.TEST),
                "test",
            )
        )

        generated = _symbol("src/client.py", "generated helper")
        generated_events = _substantive_events(generated)
        cases.append(
            (
                generated,
                _body_file(generated, generated_events, role=SourceRole.GENERATED),
                "generated",
            )
        )

        constructor = _symbol(
            "src/model.py",
            "Model",
            kind=SymbolKind.CONSTRUCTOR,
        )
        constructor_events = _substantive_events(constructor)
        cases.append(
            (
                constructor,
                _body_file(constructor, constructor_events),
                "constructor",
            )
        )

        accessor = _symbol(
            "src/model.py",
            "value",
            kind=SymbolKind.PROPERTY,
            line=20,
        )
        accessor_events = _substantive_events(accessor)
        cases.append((accessor, _body_file(accessor, accessor_events), "accessor"))

        getter = _symbol(
            "src/model.py",
            "get",
            kind=SymbolKind.METHOD,
            modifiers=("get",),
            line=40,
        )
        getter_events = _substantive_events(getter)
        cases.append((getter, _body_file(getter, getter_events), "accessor"))

        delegate = _symbol("src/delegate.py", "delegate")
        delegate_events = tuple(
            _event(delegate, index + 2, kind, text)
            for index, (kind, text) in enumerate(
                (
                    (BodyEventKind.KEYWORD, "const"),
                    (BodyEventKind.LOCAL, "delegate"),
                    (BodyEventKind.OPERATOR, "="),
                    (BodyEventKind.PARAM, "value"),
                    (BodyEventKind.KEYWORD, "return"),
                    (BodyEventKind.CALL, "target"),
                    (BodyEventKind.NAME, "value"),
                )
            )
        )
        cases.append(
            (delegate, _body_file(delegate, delegate_events), "trivial-delegate")
        )

        tiny = _symbol("src/tiny.py", "tiny")
        tiny_events = (
            _event(tiny, 2, BodyEventKind.KEYWORD, "return"),
            _event(tiny, 3, BodyEventKind.LITERAL, "<number>"),
        )
        cases.append(
            (tiny, _body_file(tiny, tiny_events), "fewer-than-12-semantic-tokens")
        )

        for candidate, file_ir, expected in cases:
            with self.subTest(symbol=candidate.name):
                self.assertEqual(
                    canonical_body(candidate, file_ir, {}).excluded_reason,
                    expected,
                )

        production = _symbol("tests/production.py", "production helper")
        production_events = _substantive_events(production)
        production_profile = canonical_body(
            production,
            _body_file(
                production,
                production_events,
                role=SourceRole.PRODUCTION,
            ),
            self._call_target_map(production_events, target.id),
        )
        self.assertIsNone(production_profile.excluded_reason)

    def test_real_language_forwarders_are_trivial_despite_declaration_artifacts(
        self,
    ) -> None:
        samples = (
            (
                Language.PYTHON,
                "forward.py",
                "forward",
                b"def forward(value):\n    return target(value)\n",
            ),
            (
                Language.TYPESCRIPT,
                "forward.ts",
                "forward",
                b"const forward = (value: number): number => target(value);\n",
            ),
            (
                Language.CSHARP,
                "Forward.cs",
                "Forward",
                b"class C { int Forward(int value) => target(value); }\n",
            ),
            (
                Language.KOTLIN,
                "Forward.kt",
                "forward",
                b"fun forward(value: Int): Int = target(value)\n",
            ),
            (
                Language.JAVA,
                "Forward.java",
                "forward",
                b"class C { static int forward(int value) { return target(value); } }\n",
            ),
            (
                Language.GO,
                "forward.go",
                "forward",
                b"package p\nfunc forward(value int) int { return target(value) }\n",
            ),
            (
                Language.RUST,
                "forward.rs",
                "forward",
                b"fn forward(value: i32) -> i32 { target(value) }\n",
            ),
            (
                Language.RUST,
                "async_forward.rs",
                "forward",
                b"async fn forward(value: i32) -> i32 { target(value).await }\n",
            ),
        )
        for language, file, name, raw in samples:
            file_ir = extract_file(_source(file, language=language, raw=raw))
            self.assertFalse(file_ir.diagnostics)
            body_owners = {body.owner for body in file_ir.bodies}
            symbol = next(
                item
                for item in file_ir.symbols
                if item.name == name
                and item.kind in _CALLABLE_KINDS
                and item.id in body_owners
            )
            with self.subTest(language=language.value, file=file):
                self.assertEqual(
                    canonical_body(symbol, file_ir, {}).excluded_reason,
                    "trivial-delegate",
                )

        kotlin_raw = b"""\
fun transform(value: Int): Int {
    val result = target(value)
    return result
}
"""
        kotlin_ir = extract_file(
            _source("Transform.kt", language=Language.KOTLIN, raw=kotlin_raw)
        )
        transform = next(item for item in kotlin_ir.symbols if item.name == "transform")
        self.assertNotEqual(
            canonical_body(transform, kotlin_ir, {}).excluded_reason,
            "trivial-delegate",
        )

        side_effect_raw = b"""\
def forward(value):
    target(value)
    return value
"""
        side_effect_ir = extract_file(
            _source(
                "side_effect.py",
                language=Language.PYTHON,
                raw=side_effect_raw,
            )
        )
        side_effect = next(
            item
            for item in side_effect_ir.symbols
            if item.name == "forward" and item.kind is SymbolKind.FUNCTION
        )
        self.assertNotEqual(
            canonical_body(side_effect, side_effect_ir, {}).excluded_reason,
            "trivial-delegate",
        )

        separate_await_raw = b"""\
async fn forward(value: i32) -> i32 {
    target(value);
    other.await
}
"""
        separate_await_ir = extract_file(
            _source(
                "separate_await.rs",
                language=Language.RUST,
                raw=separate_await_raw,
            )
        )
        separate_await = next(
            item
            for item in separate_await_ir.symbols
            if item.name == "forward" and item.kind is SymbolKind.FUNCTION
        )
        self.assertNotEqual(
            canonical_body(
                separate_await,
                separate_await_ir,
                {},
            ).excluded_reason,
            "trivial-delegate",
        )

        rejected_artifacts = {
            "other-local": ((BodyEventKind.LOCAL, "result"),),
            "literal": ((BodyEventKind.LITERAL, "<number>"),),
            "control": (
                (BodyEventKind.CONTROL_ENTER, "if"),
                (BodyEventKind.CONTROL_EXIT, "if"),
            ),
            "semantic-operator": ((BodyEventKind.OPERATOR, "+"),),
            "assignment-after-name": (
                (BodyEventKind.NAME, "field"),
                (BodyEventKind.OPERATOR, "="),
            ),
        }
        negative = _symbol("negative.py", "forward")
        for case, artifacts in rejected_artifacts.items():
            values = (
                (BodyEventKind.PARAM, "value"),
                *artifacts,
                (BodyEventKind.CALL, "target"),
                (BodyEventKind.NAME, "value"),
            )
            events = tuple(
                _event(negative, index + 2, kind, text)
                for index, (kind, text) in enumerate(values)
            )
            with self.subTest(rejected=case):
                self.assertNotEqual(
                    canonical_body(
                        negative, _body_file(negative, events), {}
                    ).excluded_reason,
                    "trivial-delegate",
                )

        for keyword in ("return", "yield"):
            values = (
                (BodyEventKind.PARAM, "value"),
                (BodyEventKind.CALL, "target"),
                (BodyEventKind.NAME, "value"),
                (BodyEventKind.KEYWORD, keyword),
                (BodyEventKind.NAME, "value"),
            )
            events = tuple(
                _event(negative, index + 2, kind, text)
                for index, (kind, text) in enumerate(values)
            )
            with self.subTest(post_call_keyword=keyword):
                self.assertNotEqual(
                    canonical_body(
                        negative,
                        _body_file(negative, events),
                        {},
                    ).excluded_reason,
                    "trivial-delegate",
                )

    def test_missing_body_is_none_in_analysis_but_direct_body_count_is_strict(
        self,
    ) -> None:
        symbol = _symbol("src/bodyless.py", "bodyless")
        bodyless = _file(symbol.file, symbols=(symbol,))
        analyzed = analyze_project(
            _project(bodyless),
            _resolution(),
            hot_threshold=2,
        )
        self.assertIsNone(analyzed.symbols[0].body)
        with self.assertRaisesRegex(ValueError, "expected exactly one body"):
            canonical_body(symbol, bodyless, {})

        events = _substantive_events(symbol)
        body = _body(symbol, events)
        duplicated = _file(
            symbol.file,
            symbols=(symbol,),
            bodies=(body, body),
        )
        with self.assertRaisesRegex(ValueError, "expected exactly one body"):
            canonical_body(symbol, duplicated, {})
        with self.assertRaisesRegex(ValueError, "expected exactly one body"):
            analyze_project(
                _project(duplicated),
                _resolution(),
                hot_threshold=2,
            )

    def test_resolved_body_targets_keep_unique_facts_and_reject_conflicts(self) -> None:
        caller = _symbol("src/caller.py", "caller")
        left = _symbol("src/left.py", "work")
        right = _symbol("src/right.py", "work")
        constructed = _symbol(
            "src/widget.py",
            "Widget",
            kind=SymbolKind.CLASS,
        )
        left_fact, left_result = _call(caller.id, left.id, line=3)
        duplicate_result = ResolvedCall(
            left_fact,
            ResolutionStatus.RESOLVED,
            left.id,
            (left.id,),
            left.name,
        )
        mapping = _resolved_body_targets(
            _resolution(calls=(duplicate_result, left_result))
        )
        self.assertEqual(
            mapping[(BodyEventKind.CALL, left_fact.span)],
            left.id,
        )

        construct_fact = CallRef(
            caller.id,
            _span(caller.file, 4),
            constructed.name,
            None,
            CallKind.CONSTRUCT,
            0,
        )
        construct_result = ResolvedCall(
            construct_fact,
            ResolutionStatus.RESOLVED,
            constructed.id,
            (constructed.id,),
            constructed.name,
        )
        name_fact = ReferenceRef(
            caller.id,
            _span(caller.file, 5),
            left.name,
            None,
            ReferenceKind.NAME,
            ReferenceContext.CODE,
            ReferenceConfidence.DEFINITE,
        )
        name_result = ResolvedReference(
            name_fact,
            ResolutionStatus.RESOLVED,
            left.id,
            (left.id,),
        )
        type_fact = ReferenceRef(
            caller.id,
            _span(caller.file, 6),
            constructed.name,
            None,
            ReferenceKind.TYPE,
            ReferenceContext.REFLECTION,
            ReferenceConfidence.POSSIBLE,
        )
        type_result = ResolvedReference(
            type_fact,
            ResolutionStatus.RESOLVED,
            constructed.id,
            (constructed.id,),
        )
        joined = _resolved_body_targets(
            _resolution(
                calls=(construct_result, left_result),
                references=(type_result, name_result),
            )
        )
        self.assertEqual(
            joined[(BodyEventKind.CONSTRUCT, construct_fact.span)],
            constructed.id,
        )
        self.assertEqual(joined[(BodyEventKind.NAME, name_fact.span)], left.id)
        self.assertEqual(
            joined[(BodyEventKind.TYPE, type_fact.span)],
            constructed.id,
        )

        right_fact = CallRef(
            caller.id,
            left_fact.span,
            right.name,
            None,
            CallKind.CALL,
            0,
        )
        right_result = ResolvedCall(
            right_fact,
            ResolutionStatus.RESOLVED,
            right.id,
            (right.id,),
            right.name,
        )
        with self.assertRaisesRegex(ValueError, "conflicting resolved body targets"):
            _resolved_body_targets(_resolution(calls=(left_result, right_result)))

        conflicting_name_fact = ReferenceRef(
            caller.id,
            name_fact.span,
            right.name,
            None,
            ReferenceKind.NAME,
            ReferenceContext.CODE,
            ReferenceConfidence.DEFINITE,
        )
        conflicting_name = ResolvedReference(
            conflicting_name_fact,
            ResolutionStatus.RESOLVED,
            right.id,
            (right.id,),
        )
        with self.assertRaisesRegex(ValueError, "conflicting resolved body targets"):
            _resolved_body_targets(
                _resolution(references=(name_result, conflicting_name))
            )

        ambiguous = ResolvedCall(
            right_fact,
            ResolutionStatus.AMBIGUOUS,
            None,
            (left.id, right.id),
            None,
        )
        self.assertEqual(_resolved_body_targets(_resolution(calls=(ambiguous,))), {})

        ambiguous_reference = ResolvedReference(
            name_fact,
            ResolutionStatus.AMBIGUOUS,
            None,
            (left.id, right.id),
        )
        self.assertEqual(
            _resolved_body_targets(_resolution(references=(ambiguous_reference,))),
            {},
        )

    def test_malformed_control_streams_raise_local_invariant_errors(self) -> None:
        symbol = _symbol("src/broken.py", "broken")
        malformed = {
            "underflow": ((BodyEventKind.CONTROL_EXIT, "if"),),
            "mismatch": (
                (BodyEventKind.CONTROL_ENTER, "if"),
                (BodyEventKind.CONTROL_EXIT, "loop"),
            ),
            "unclosed": ((BodyEventKind.CONTROL_ENTER, "if"),),
        }
        for name, values in malformed.items():
            events = tuple(
                _event(symbol, index + 2, kind, text)
                for index, (kind, text) in enumerate(values)
            )
            with self.subTest(case=name), self.assertRaises(ValueError):
                canonical_body(symbol, _body_file(symbol, events), {})

        excluded = (
            (
                _symbol("tests/broken.py", "broken_test"),
                SourceRole.TEST,
            ),
            (
                _symbol("generated/broken.py", "broken_generated"),
                SourceRole.GENERATED,
            ),
            (
                _symbol(
                    "src/broken_constructor.py",
                    "Broken",
                    kind=SymbolKind.CONSTRUCTOR,
                ),
                SourceRole.PRODUCTION,
            ),
            (
                _symbol(
                    "src/broken_accessor.py",
                    "value",
                    kind=SymbolKind.PROPERTY,
                ),
                SourceRole.PRODUCTION,
            ),
        )
        for candidate, role in excluded:
            events = (_event(candidate, 2, BodyEventKind.CONTROL_EXIT, "if"),)
            with (
                self.subTest(excluded=candidate.name),
                self.assertRaisesRegex(ValueError, "control stack underflow"),
            ):
                canonical_body(
                    candidate,
                    _body_file(candidate, events, role=role),
                    {},
                )

    def test_analyze_builds_join_once_and_populates_only_owned_bodies(self) -> None:
        target = _symbol("lib/target.py", "normalize")
        first = _symbol("src/first.py", "first")
        second = _symbol("src/second.py", "second")
        first_events = _substantive_events(first)
        second_events = _substantive_events(second)
        first_call_event = next(
            event for event in first_events if event.kind is BodyEventKind.CALL
        )
        second_call_event = next(
            event for event in second_events if event.kind is BodyEventKind.CALL
        )
        first_fact = CallRef(
            first.id,
            first_call_event.span,
            target.name,
            None,
            CallKind.CALL,
            0,
        )
        second_fact = CallRef(
            second.id,
            second_call_event.span,
            target.name,
            None,
            CallKind.CALL,
            0,
        )
        resolution = _resolution(
            calls=(
                ResolvedCall(
                    second_fact,
                    ResolutionStatus.RESOLVED,
                    target.id,
                    (target.id,),
                    target.name,
                ),
                ResolvedCall(
                    first_fact,
                    ResolutionStatus.RESOLVED,
                    target.id,
                    (target.id,),
                    target.name,
                ),
            )
        )
        project = _project(
            _body_file(second, second_events),
            _file(target.file, symbols=(target,)),
            _body_file(first, first_events),
        )
        with patch(
            "hologram.analysis._resolved_body_targets",
            wraps=_resolved_body_targets,
        ) as joined:
            analyzed = analyze_project(project, resolution, hot_threshold=2)
        joined.assert_called_once_with(resolution)
        by_id = {item.symbol.id: item for item in analyzed.symbols}
        self.assertIsNone(by_id[target.id].body)
        first_body = by_id[first.id].body
        second_body = by_id[second.id].body
        self.assertIsNotNone(first_body)
        self.assertIsNotNone(second_body)
        assert first_body is not None
        assert second_body is not None
        self.assertEqual(first_body.resolved_calls, frozenset({target.id}))
        self.assertEqual(second_body.resolved_calls, frozenset({target.id}))

    def test_analyze_indexes_many_one_file_bodies_once_without_public_scans(
        self,
    ) -> None:
        symbols = tuple(
            _symbol("src/many.py", f"worker_{index:03d}", line=index + 1)
            for index in range(128)
        )
        bodies = tuple(
            _body(
                symbol,
                (
                    _event(
                        symbol, symbol.span.start_line, BodyEventKind.KEYWORD, "return"
                    ),
                    _event(
                        symbol,
                        symbol.span.start_line,
                        BodyEventKind.LITERAL,
                        "<number>",
                    ),
                ),
            )
            for symbol in symbols
        )
        project = _project(
            _file("src/many.py", symbols=tuple(reversed(symbols)), bodies=bodies)
        )
        with (
            patch("hologram.analysis._body_index", wraps=_body_index) as indexed,
            patch(
                "hologram.analysis.canonical_body",
                side_effect=AssertionError("public body scan"),
            ) as public_scan,
        ):
            analyzed = analyze_project(project, _resolution(), hot_threshold=2)
        indexed.assert_called_once_with(project)
        public_scan.assert_not_called()
        self.assertEqual(
            tuple(item.symbol.id for item in analyzed.symbols),
            tuple(symbol.id for symbol in symbols),
        )
        self.assertTrue(all(item.body is not None for item in analyzed.symbols))

    def test_frozen_body_analysis_survives_disk_mutation_without_any_reread(
        self,
    ) -> None:
        raw = b"""\
def compute(value: int) -> int:
    total = value + 1
    if total > 2:
        total = total * 3
    return total
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compute.py"
            path.write_bytes(raw)
            source = SourceFile(
                path,
                "compute.py",
                Language.PYTHON,
                SourceRole.PRODUCTION,
                raw,
                hashlib.sha256(raw).hexdigest(),
            )
            file_ir = extract_file(source)
            project = ProjectIR(Path(directory), (file_ir,), (), True)
            before = analyze_project(project, _resolution(), hot_threshold=2)
            path.write_text("def compute(value):\n    return 999\n", encoding="utf-8")
            with (
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("disk reread"),
                ),
                patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError("disk reread"),
                ),
                patch.object(
                    SourceFile,
                    "text",
                    new_callable=PropertyMock,
                    side_effect=AssertionError("source text access"),
                ),
            ):
                after = analyze_project(project, _resolution(), hot_threshold=2)
        before_profile = next(
            item.body for item in before.symbols if item.symbol.name == "compute"
        )
        after_profile = next(
            item.body for item in after.symbols if item.symbol.name == "compute"
        )
        self.assertEqual(before_profile, after_profile)
        self.assertIsNotNone(after_profile)
        assert after_profile is not None
        self.assertIsNone(after_profile.excluded_reason)


if __name__ == "__main__":
    unittest.main()
