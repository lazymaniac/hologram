from __future__ import annotations

import hashlib
import json
import tracemalloc
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path, PurePosixPath
from unittest.mock import PropertyMock, patch

import hologram.render as render_module
from hologram.analysis import (
    AnalyzedProject,
    AnalyzedSymbol,
    ReferenceFacts,
    ZeroReference,
)
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
from hologram.render import (
    RenderDecodeError,
    RenderFile,
    RenderIntern,
    RenderIR,
    RenderReexport,
    RenderSymbol,
    decode_render,
    project_render_ir,
    render_project,
)
from hologram.resolve import (
    ResolutionResult,
    ResolutionStatus,
    ResolvedCall,
    ResolvedImport,
    ResolvedReference,
)

STATE = "a" * 64
_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)


def _span(file: str, line: int = 1, column: int = 0) -> SourceSpan:
    return SourceSpan(file, line, column, line, column + 1)


def _source(
    file: str,
    *,
    language: Language = Language.PYTHON,
    role: SourceRole = SourceRole.PRODUCTION,
    root: Path = Path("/repo"),
    raw: bytes = b"frozen\n",
) -> SourceFile:
    return SourceFile(
        root / file,
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
    language: Language = Language.PYTHON,
    kind: SymbolKind = SymbolKind.FUNCTION,
    container: tuple[str, ...] = (),
    signature_key: str | None = None,
    line: int = 1,
    column: int = 0,
    visibility: Visibility = Visibility.PRIVATE,
    signature: str | None = None,
    params: tuple[str, ...] = (),
    returns: str | None = None,
    annotations: tuple[str, ...] = (),
    modifiers: tuple[str, ...] = (),
    components: tuple[str, ...] = (),
    supers: tuple[str, ...] = (),
    permits: tuple[str, ...] = (),
    raises: tuple[str, ...] = (),
    body_lines: int = 0,
) -> Symbol:
    if signature_key is None:
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
        _span(file, line, column),
        visibility,
        signature if signature is not None else name,
        params=params,
        returns=returns,
        annotations=annotations,
        modifiers=modifiers,
        components=components,
        supers=supers,
        permits=permits,
        raises=raises,
        body_lines=body_lines,
    )


def _file(
    file: str,
    *,
    language: Language = Language.PYTHON,
    role: SourceRole = SourceRole.PRODUCTION,
    module: str | None = None,
    symbols: tuple[Symbol, ...] = (),
    imports: tuple[ImportRef, ...] = (),
    root: Path = Path("/repo"),
) -> FileIR:
    return FileIR(
        _source(file, language=language, role=role, root=root),
        module=module,
        symbols=symbols,
        imports=imports,
    )


def _facts(
    *,
    production: tuple[str, ...] = (),
    possible: tuple[str, ...] = (),
    tests: tuple[str, ...] = (),
    generated: tuple[str, ...] = (),
    zero: ZeroReference = ZeroReference.NONE,
) -> ReferenceFacts:
    return ReferenceFacts(
        tuple(PurePosixPath(value) for value in production),
        tuple(PurePosixPath(value) for value in possible),
        tuple(PurePosixPath(value) for value in tests),
        tuple(PurePosixPath(value) for value in generated),
        zero,
    )


def _item(
    symbol: Symbol,
    *,
    references: ReferenceFacts | None = None,
    peers: tuple[SymbolId, ...] = (),
) -> AnalyzedSymbol:
    return AnalyzedSymbol(
        symbol,
        references if references is not None else _facts(),
        None,
        peers,
    )


def _resolution(
    *,
    imports: tuple[ResolvedImport, ...] = (),
    calls: tuple[ResolvedCall, ...] = (),
    references: tuple[ResolvedReference, ...] = (),
) -> ResolutionResult:
    return ResolutionResult(imports, calls, references, ())


def _analyzed(
    files: tuple[FileIR, ...],
    items: tuple[AnalyzedSymbol, ...],
    *,
    resolution: ResolutionResult | None = None,
    root: Path = Path("/repo"),
) -> AnalyzedProject:
    return AnalyzedProject(
        ProjectIR(root, files, (), True),
        resolution if resolution is not None else _resolution(),
        items,
        (),
    )


def _resolved_call(
    caller: Symbol,
    target: Symbol,
    *,
    line: int,
    column: int = 0,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
    display_name: str | None = "stale-display-name",
) -> ResolvedCall:
    fact = CallRef(
        caller.id,
        _span(caller.file, line, column),
        target.name,
        None,
        CallKind.CALL,
        len(target.params),
    )
    return ResolvedCall(
        fact,
        status,
        target.id if status is ResolutionStatus.RESOLVED else None,
        (target.id,),
        display_name,
    )


def _resolved_reference(
    owner: Symbol | None,
    target: Symbol,
    *,
    source_file: str,
    line: int,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
    confidence: ReferenceConfidence = ReferenceConfidence.DEFINITE,
) -> ResolvedReference:
    fact = ReferenceRef(
        owner.id if owner is not None else None,
        _span(source_file, line),
        target.name,
        None,
        ReferenceKind.NAME,
        ReferenceContext.CODE,
        confidence,
    )
    return ResolvedReference(
        fact,
        status,
        target.id if status is ResolutionStatus.RESOLVED else None,
        (target.id,),
    )


class RenderProjectionTest(unittest.TestCase):
    def test_records_have_exact_frozen_slotted_field_order(self) -> None:
        expected = {
            RenderSymbol: (
                "symbol_id",
                "source_line",
                "source_column",
                "visibility",
                "signature",
                "parameters",
                "returns",
                "annotations",
                "modifiers",
                "components",
                "supers",
                "permits",
                "ordered_calls",
                "throws",
                "behaviors",
                "body_lines",
                "markers",
                "duplicate_peers",
                "call_targets",
            ),
            RenderIntern: ("alias", "value"),
            RenderReexport: ("module", "name", "alias", "wildcard"),
            RenderFile: (
                "path",
                "language",
                "role",
                "module",
                "reexports",
                "symbols",
            ),
            RenderIR: (
                "state",
                "interns",
                "dependencies",
                "files",
            ),
        }
        for record, names in expected.items():
            with self.subTest(record=record.__name__):
                self.assertEqual(tuple(field.name for field in fields(record)), names)
                self.assertNotIn("__dict__", record.__dict__)

    def test_records_own_all_caller_supplied_tuple_fields(self) -> None:
        symbol = _symbol("src/value.py", "value")
        parameters = ["input"]
        calls = ["target"]
        markers = ["✓"]
        peers = [symbol.id]
        targets = [symbol.id]
        rendered = RenderSymbol(
            symbol.id,
            1,
            0,
            "private",
            "value(input)",
            parameters,  # type: ignore[arg-type]
            None,
            [],  # type: ignore[arg-type]
            [],  # type: ignore[arg-type]
            [],  # type: ignore[arg-type]
            [],  # type: ignore[arg-type]
            [],  # type: ignore[arg-type]
            calls,  # type: ignore[arg-type]
            [],  # type: ignore[arg-type]
            [],  # type: ignore[arg-type]
            1,
            markers,  # type: ignore[arg-type]
            peers,  # type: ignore[arg-type]
            targets,  # type: ignore[arg-type]
        )
        parameters.append("mutated")
        calls.append("mutated")
        markers.append("mutated")
        peers.clear()
        targets.clear()
        self.assertEqual(rendered.parameters, ("input",))
        self.assertEqual(rendered.ordered_calls, ("target",))
        self.assertEqual(rendered.markers, ("✓",))
        self.assertEqual(rendered.duplicate_peers, (symbol.id,))
        self.assertEqual(rendered.call_targets, (symbol.id,))

        reexports = [RenderReexport("./api", None, None, True)]
        symbols = [rendered]
        rendered_file = RenderFile(
            "src/value.py",
            "python",
            "production",
            None,
            reexports,  # type: ignore[arg-type]
            symbols,  # type: ignore[arg-type]
        )
        interns = [RenderIntern("alias", "value")]
        dependencies = ["app→core"]
        files = [rendered_file]
        ir = RenderIR(
            STATE,
            interns,  # type: ignore[arg-type]
            dependencies,  # type: ignore[arg-type]
            files,  # type: ignore[arg-type]
        )
        reexports.clear()
        symbols.clear()
        interns.clear()
        dependencies.clear()
        files.clear()
        self.assertEqual(rendered_file.reexports[0].module, "./api")
        self.assertEqual(rendered_file.symbols, (rendered,))
        self.assertEqual(ir.interns, (RenderIntern("alias", "value"),))
        self.assertEqual(ir.dependencies, ("app→core",))
        self.assertEqual(ir.files, (rendered_file,))
        with self.assertRaises(TypeError):
            RenderIR(STATE, (), "app→core", ())  # type: ignore[arg-type]

    def test_projection_preserves_file_ownership_raw_symbol_facts_and_empty_files(
        self,
    ) -> None:
        rich = _symbol(
            "src/a.py",
            "serve",
            line=7,
            column=4,
            visibility=Visibility.PUBLIC,
            signature="serve(value: Input) -> Output",
            params=("value: Input",),
            returns="Output",
            annotations=("Bean",),
            modifiers=("public", "async"),
            components=("id: int",),
            supers=("Base",),
            permits=("Child",),
            raises=("ValueError",),
            body_lines=13,
        )
        other = _symbol("src/b.py", "other", line=3)
        analyzed = _analyzed(
            (
                _file("src/z_empty.py"),
                _file(other.file, symbols=(other,)),
                _file(rich.file, module="app", symbols=(rich,)),
            ),
            (_item(other), _item(rich)),
        )
        ir = project_render_ir(analyzed, state=STATE, hot_threshold=2)
        self.assertEqual((ir.state, ir.interns), (STATE, ()))
        self.assertEqual(
            tuple(file.path for file in ir.files),
            ("src/a.py", "src/b.py", "src/z_empty.py"),
        )
        self.assertEqual(ir.files[2].symbols, ())
        self.assertEqual(
            (ir.files[0].language, ir.files[0].role, ir.files[0].module),
            ("python", "production", "app"),
        )
        rendered = ir.files[0].symbols[0]
        self.assertEqual((rendered.source_line, rendered.source_column), (7, 4))
        self.assertEqual(rendered.symbol_id, rich.id)
        self.assertEqual(rendered.visibility, "pub")
        self.assertEqual(rendered.signature, rich.signature)
        self.assertEqual(rendered.parameters, rich.params)
        self.assertEqual(rendered.returns, rich.returns)
        self.assertEqual(rendered.annotations, rich.annotations)
        self.assertEqual(rendered.modifiers, rich.modifiers)
        self.assertEqual(rendered.components, rich.components)
        self.assertEqual(rendered.supers, rich.supers)
        self.assertEqual(rendered.permits, rich.permits)
        self.assertEqual(rendered.throws, rich.raises)
        self.assertEqual(rendered.body_lines, 13)
        self.assertNotIn("7", rendered.symbol_id.signature_key)
        self.assertFalse(hasattr(rendered, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            rendered.source_line = 9  # type: ignore[misc]

    def test_marker_combinations_follow_frozen_analysis_in_exact_order(self) -> None:
        peer_a = _symbol("src/peers.py", "peer_a", line=20)
        peer_b = _symbol("src/peers.py", "peer_b", line=21)
        hot = _symbol("src/markers.py", "hot")
        dead = _symbol("src/markers.py", "dead", line=2)
        surface = _symbol("src/markers.py", "surface", line=3)
        clone = _symbol("src/markers.py", "clone", line=4)
        quiet = _symbol("src/markers.py", "quiet", line=5)
        symbols = (quiet, clone, surface, dead, hot)
        analyzed = _analyzed(
            (
                _file("src/peers.py", symbols=(peer_b, peer_a)),
                _file("src/markers.py", symbols=symbols),
            ),
            (
                _item(peer_a),
                _item(peer_b),
                _item(
                    hot,
                    references=_facts(
                        production=("a.py", "b.py"),
                        tests=("checks.py",),
                    ),
                    peers=(peer_a.id,),
                ),
                _item(dead, references=_facts(zero=ZeroReference.STRONG)),
                _item(
                    surface,
                    references=_facts(
                        tests=("checks.py",),
                        zero=ZeroReference.UNCERTAIN,
                    ),
                ),
                _item(
                    clone,
                    peers=(peer_a.id, peer_b.id),
                ),
                _item(quiet, references=_facts(production=("one.py",))),
            ),
        )
        ir = project_render_ir(analyzed, state=STATE, hot_threshold=2)
        by_name = {
            symbol.symbol_id.name: symbol
            for file in ir.files
            for symbol in file.symbols
        }
        self.assertEqual(by_name["hot"].markers, ("×2", "✓", "≈1"))
        self.assertEqual(by_name["hot"].duplicate_peers, (peer_a.id,))
        self.assertEqual(by_name["dead"].markers, ("×0",))
        self.assertEqual(by_name["surface"].markers, ("×0?", "✓"))
        self.assertEqual(by_name["clone"].markers, ("≈2",))
        self.assertEqual(
            by_name["clone"].duplicate_peers,
            (peer_a.id, peer_b.id),
        )
        self.assertEqual(by_name["quiet"].markers, ())

    def test_display_ladder_and_ordered_resolved_calls_are_exact(self) -> None:
        caller = _symbol("src/caller.py", "caller")
        unique = _symbol("src/unique.py", "unique")
        left_run = _symbol("src/left.py", "run", container=("Left",))
        right_run = _symbol("src/right.py", "run", container=("Right",))
        pkg_work = _symbol("pkg/left.py", "work", container=("Left",))
        other_work = _symbol("other/left.py", "work", container=("Left",))
        int_overload = _symbol(
            "src/api.py",
            "over",
            kind=SymbolKind.METHOD,
            container=("C",),
            signature_key="(int)",
        )
        str_overload = _symbol(
            "src/api.py",
            "over",
            kind=SymbolKind.METHOD,
            container=("C",),
            signature_key="(str)",
            line=2,
        )
        free_int = _symbol(
            "src/free.py",
            "free",
            signature_key="(int)",
        )
        free_str = _symbol(
            "src/free.py",
            "free",
            signature_key="(str)",
            line=2,
        )
        field = _symbol(
            "src/data.py",
            "value",
            kind=SymbolKind.FIELD,
            container=("C",),
        )
        prop = _symbol(
            "src/data.py",
            "value",
            kind=SymbolKind.PROPERTY,
            container=("C",),
            line=2,
        )
        symbols = (
            caller,
            unique,
            left_run,
            right_run,
            pkg_work,
            other_work,
            int_overload,
            str_overload,
            free_int,
            free_str,
            field,
            prop,
        )
        resolved = (
            _resolved_call(caller, field, line=16),
            _resolved_call(caller, free_int, line=15),
            _resolved_call(caller, int_overload, line=14),
            _resolved_call(caller, int_overload, line=13),
            _resolved_call(caller, pkg_work, line=12),
            _resolved_call(caller, left_run, line=11),
            _resolved_call(caller, unique, line=10),
            _resolved_call(
                caller,
                unique,
                line=7,
                status=ResolutionStatus.UNRESOLVED,
            ),
            _resolved_call(
                caller,
                unique,
                line=8,
                status=ResolutionStatus.EXTERNAL,
            ),
            _resolved_call(
                caller,
                unique,
                line=9,
                status=ResolutionStatus.AMBIGUOUS,
            ),
        )
        files = tuple(
            _file(file, symbols=tuple(item for item in symbols if item.file == file))
            for file in reversed(tuple(dict.fromkeys(item.file for item in symbols)))
        )
        analyzed = _analyzed(
            files,
            tuple(_item(symbol) for symbol in reversed(symbols)),
            resolution=_resolution(calls=resolved),
        )
        ir = project_render_ir(analyzed, state=STATE, hot_threshold=10)
        rendered = {
            symbol.symbol_id: symbol for file in ir.files for symbol in file.symbols
        }
        self.assertEqual(
            rendered[caller.id].ordered_calls,
            (
                "unique",
                "Left.run",
                "pkg/left.py:Left.work",
                "src/api.py:C.over|method|(int)",
                "src/api.py:C.over|method|(int)",
                "src/free.py:free|fn|(int)",
                "src/data.py:C.value|field|",
            ),
        )
        self.assertNotIn("stale-display-name", rendered[caller.id].ordered_calls)
        self.assertEqual(
            rendered[caller.id].call_targets,
            (
                unique.id,
                left_run.id,
                pkg_work.id,
                int_overload.id,
                int_overload.id,
                free_int.id,
                field.id,
            ),
        )
        api = next(file for file in ir.files if file.path == "src/api.py")
        self.assertEqual(
            tuple(symbol.symbol_id.signature_key for symbol in api.symbols),
            ("(int)", "(str)"),
        )

    def test_cross_rung_display_collisions_escalate_until_globally_unique(
        self,
    ) -> None:
        caller = _symbol("caller.py", "caller")
        raw_qualified = _symbol("raw.py", "A.run")
        raw_path = _symbol("path.py", "a.py:A.run")
        container_a = _symbol("src/a.py", "run", container=("A",))
        container_b = _symbol("b.py", "run", container=("B",))
        symbols = (caller, raw_qualified, raw_path, container_a, container_b)
        analyzed = _analyzed(
            tuple(
                _file(
                    file,
                    symbols=tuple(symbol for symbol in symbols if symbol.file == file),
                )
                for file in reversed(tuple(symbol.file for symbol in symbols))
            ),
            tuple(_item(symbol) for symbol in reversed(symbols)),
            resolution=_resolution(
                calls=(
                    _resolved_call(caller, raw_qualified, line=2),
                    _resolved_call(caller, raw_path, line=3),
                    _resolved_call(caller, container_a, line=4),
                    _resolved_call(caller, container_b, line=5),
                )
            ),
        )
        rendered = project_render_ir(analyzed, state=STATE, hot_threshold=10)
        caller_ir = next(
            symbol
            for file in rendered.files
            for symbol in file.symbols
            if symbol.symbol_id == caller.id
        )
        self.assertEqual(
            caller_ir.ordered_calls,
            (
                "A.run",
                "a.py:A.run",
                "src/a.py:A.run",
                "B.run",
            ),
        )

    def test_behaviors_use_only_definite_resolved_test_callable_evidence(
        self,
    ) -> None:
        target = _symbol("tests/api.py", "client")
        orphan = _symbol("tests/api.py", "orphan", line=2)
        first = _symbol(
            "src/runtime.py",
            "checks",
            container=("ATest",),
        )
        second = _symbol(
            "src/runtime.py",
            "checks",
            container=("BTest",),
            line=2,
        )
        possible = _symbol("src/runtime.py", "possible", line=3)
        test_module = _symbol(
            "src/runtime.py",
            "runtime",
            kind=SymbolKind.MODULE,
            line=4,
        )
        generated = _symbol("tests/generated.py", "generated_check")
        production_caller = _symbol("src/production.py", "production_check")
        calls = (
            _resolved_call(first, target, line=9),
            _resolved_call(first, target, line=5),
            _resolved_call(test_module, target, line=6),
            _resolved_call(generated, target, line=7),
            _resolved_call(production_caller, target, line=8),
            _resolved_call(
                second,
                target,
                line=10,
                status=ResolutionStatus.AMBIGUOUS,
            ),
        )
        references = (
            _resolved_reference(
                second,
                target,
                source_file=second.file,
                line=11,
            ),
            _resolved_reference(
                first,
                target,
                source_file=first.file,
                line=12,
            ),
            _resolved_reference(
                possible,
                target,
                source_file=possible.file,
                line=13,
                confidence=ReferenceConfidence.POSSIBLE,
            ),
            _resolved_reference(
                None,
                orphan,
                source_file=first.file,
                line=14,
            ),
        )
        files = (
            _file(
                target.file,
                role=SourceRole.PRODUCTION,
                symbols=(orphan, target),
            ),
            _file(
                first.file,
                role=SourceRole.TEST,
                symbols=(test_module, possible, second, first),
            ),
            _file(
                generated.file,
                role=SourceRole.GENERATED,
                symbols=(generated,),
            ),
            _file(production_caller.file, symbols=(production_caller,)),
        )
        items = tuple(
            _item(
                symbol,
                references=(
                    _facts(tests=(first.file,))
                    if symbol in {target, orphan}
                    else _facts()
                ),
            )
            for symbol in (
                target,
                orphan,
                first,
                second,
                possible,
                test_module,
                generated,
                production_caller,
            )
        )
        analyzed = _analyzed(
            files,
            tuple(reversed(items)),
            resolution=_resolution(
                calls=tuple(reversed(calls)),
                references=tuple(reversed(references)),
            ),
        )
        ir = project_render_ir(analyzed, state=STATE, hot_threshold=10)
        rendered = {
            symbol.symbol_id: symbol for file in ir.files for symbol in file.symbols
        }
        self.assertEqual(
            rendered[target.id].behaviors,
            ("ATest.checks", "BTest.checks"),
        )
        self.assertEqual(rendered[target.id].markers, ("✓",))
        self.assertEqual(rendered[orphan.id].behaviors, ())
        self.assertEqual(rendered[orphan.id].markers, ("✓",))
        self.assertTrue(
            all(
                not symbol.behaviors
                for symbol_id, symbol in rendered.items()
                if symbol_id != target.id
            )
        )

    def test_behavior_names_are_sorted_lexically_not_by_owner_provenance(
        self,
    ) -> None:
        target = _symbol("src/api.py", "target")
        z_owner = _symbol("a_test.py", "check", container=("ZTest",))
        a_owner = _symbol("z_test.py", "check", container=("ATest",))
        analyzed = _analyzed(
            (
                _file(target.file, symbols=(target,)),
                _file(z_owner.file, role=SourceRole.TEST, symbols=(z_owner,)),
                _file(a_owner.file, role=SourceRole.TEST, symbols=(a_owner,)),
            ),
            (_item(target), _item(z_owner), _item(a_owner)),
            resolution=_resolution(
                calls=(
                    _resolved_call(z_owner, target, line=2),
                    _resolved_call(a_owner, target, line=2),
                )
            ),
        )
        rendered = project_render_ir(analyzed, state=STATE, hot_threshold=10)
        target_ir = next(
            symbol
            for file in rendered.files
            for symbol in file.symbols
            if symbol.symbol_id == target.id
        )
        self.assertEqual(target_ir.behaviors, ("ATest.check", "ZTest.check"))

    def test_raw_reexports_and_all_resolved_production_dependencies_are_exact(
        self,
    ) -> None:
        app = _symbol("src/app/main.py", "app")
        app_peer = _symbol("src/app/peer.py", "peer")
        core = _symbol("src/core/api.py", "core")
        blank = _symbol("pkg/blank.py", "blank")
        root_symbol = _symbol("util.py", "root_util")
        test = _symbol("src/checks.py", "check")
        generated = _symbol("gen/client.py", "generated")

        wildcard_first = ImportRef(
            _span(app.file, 1),
            "./wild",
            None,
            None,
            wildcard=True,
            reexport=True,
        )
        named = ImportRef(
            _span(app.file, 2),
            "./api",
            "client",
            "Client",
            reexport=True,
        )
        wildcard_duplicate = ImportRef(
            _span(app.file, 3),
            "./wild",
            None,
            None,
            wildcard=True,
            reexport=True,
        )
        named_duplicate = ImportRef(
            _span(app.file, 4),
            "./api",
            "client",
            "Client",
            reexport=True,
        )
        ordinary = ImportRef(_span(app.file, 5), "core", None, None)
        blank_import = ImportRef(_span(app.file, 6), "blank", None, None)
        ambiguous_import = ImportRef(_span(app.file, 7), "util", None, None)
        test_import = ImportRef(_span(test.file, 1), "core", None, None)
        generated_import = ImportRef(_span(generated.file, 1), "core", None, None)
        imports = (
            ResolvedImport(
                app.file,
                ordinary,
                ResolutionStatus.RESOLVED,
                (core.file,),
                (core.id,),
            ),
            ResolvedImport(
                app.file,
                blank_import,
                ResolutionStatus.RESOLVED,
                (blank.file,),
                (blank.id,),
            ),
            ResolvedImport(
                app.file,
                ambiguous_import,
                ResolutionStatus.AMBIGUOUS,
                (root_symbol.file,),
                (root_symbol.id,),
            ),
            ResolvedImport(
                test.file,
                test_import,
                ResolutionStatus.RESOLVED,
                (core.file,),
                (core.id,),
            ),
            ResolvedImport(
                generated.file,
                generated_import,
                ResolutionStatus.RESOLVED,
                (core.file,),
                (core.id,),
            ),
        )
        calls = (
            _resolved_call(core, app, line=2),
            _resolved_call(app, core, line=8),
            _resolved_call(app, app_peer, line=9),
            _resolved_call(test, core, line=2),
            _resolved_call(generated, core, line=2),
            _resolved_call(
                app,
                root_symbol,
                line=10,
                status=ResolutionStatus.AMBIGUOUS,
            ),
        )
        references = (
            _resolved_reference(
                root_symbol,
                core,
                source_file=root_symbol.file,
                line=2,
                confidence=ReferenceConfidence.POSSIBLE,
            ),
            _resolved_reference(
                blank,
                app,
                source_file=blank.file,
                line=2,
            ),
            _resolved_reference(
                app,
                blank,
                source_file=app.file,
                line=11,
            ),
            _resolved_reference(
                app,
                root_symbol,
                source_file=app.file,
                line=12,
                status=ResolutionStatus.AMBIGUOUS,
            ),
        )
        files = (
            _file(
                app.file,
                module="app",
                symbols=(app,),
                imports=(
                    named_duplicate,
                    wildcard_duplicate,
                    named,
                    wildcard_first,
                ),
            ),
            _file(app_peer.file, module="app", symbols=(app_peer,)),
            _file(core.file, module="core", symbols=(core,)),
            _file(blank.file, module="   ", symbols=(blank,)),
            _file(root_symbol.file, symbols=(root_symbol,)),
            _file(test.file, role=SourceRole.TEST, module="checks", symbols=(test,)),
            _file(
                generated.file,
                role=SourceRole.GENERATED,
                module="gen",
                symbols=(generated,),
            ),
            _file("config/empty.py"),
        )
        symbols = (app, app_peer, core, blank, root_symbol, test, generated)
        analyzed = _analyzed(
            tuple(reversed(files)),
            tuple(_item(symbol) for symbol in reversed(symbols)),
            resolution=_resolution(
                imports=tuple(reversed(imports)),
                calls=tuple(reversed(calls)),
                references=tuple(reversed(references)),
            ),
        )
        ir = project_render_ir(analyzed, state=STATE, hot_threshold=10)
        app_file = next(file for file in ir.files if file.path == app.file)
        self.assertEqual(
            app_file.reexports,
            (
                RenderReexport("./wild", None, None, True),
                RenderReexport("./api", "client", "Client", False),
            ),
        )
        self.assertEqual(
            ir.dependencies,
            (".→core", "app→core", "app→pkg", "core→app", "pkg→app"),
        )
        blank_file = next(file for file in ir.files if file.path == blank.file)
        self.assertEqual(blank_file.module, "   ")
        empty = next(file for file in ir.files if file.path == "config/empty.py")
        self.assertEqual((empty.reexports, empty.symbols), ((), ()))

    def test_projection_is_permutation_root_and_snapshot_read_invariant(self) -> None:
        first = _symbol("src/a.py", "first")
        second = _symbol("src/b.py", "second")
        first_call = _resolved_call(first, second, line=3)
        repeated_call = _resolved_call(first, second, line=4)

        def fixture(root: Path, *, reverse: bool) -> AnalyzedProject:
            files = [
                _file(first.file, symbols=(first,), root=root),
                _file(second.file, symbols=(second,), root=root),
                _file("src/empty.py", root=root),
            ]
            items = [_item(first), _item(second)]
            calls = [first_call, repeated_call]
            if reverse:
                files.reverse()
                items.reverse()
                calls.reverse()
            return _analyzed(
                tuple(files),
                tuple(items),
                resolution=_resolution(calls=tuple(calls)),
                root=root,
            )

        left = fixture(Path("/tmp/alpha"), reverse=False)
        right = fixture(Path("/else/renamed-clone"), reverse=True)
        with (
            patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("disk read"),
            ),
            patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("disk read"),
            ),
            patch.object(
                SourceFile,
                "text",
                new_callable=PropertyMock,
                side_effect=AssertionError("source text read"),
            ),
            patch(
                "hologram.render._project_indexes",
                wraps=render_module._project_indexes,
            ) as indexes,
            patch(
                "hologram.render._display_names",
                wraps=render_module._display_names,
            ) as displays,
        ):
            left_ir = project_render_ir(left, state=STATE, hot_threshold=2)
        indexes.assert_called_once()
        displays.assert_called_once()
        right_ir = project_render_ir(right, state=STATE, hot_threshold=2)
        self.assertEqual(left_ir, right_ir)
        self.assertEqual(render_project(left_ir), render_project(right_ir))
        self.assertEqual(
            next(
                symbol
                for file in left_ir.files
                for symbol in file.symbols
                if symbol.symbol_id == first.id
            ).ordered_calls,
            ("second", "second"),
        )

    def test_large_display_collision_group_indexes_each_relative_path_once(
        self,
    ) -> None:
        size = 400
        symbols = tuple(_symbol(f"pkg{index}/same.py", "same") for index in range(size))
        analyzed = _analyzed(
            tuple(
                _file(symbol.file, symbols=(symbol,)) for symbol in reversed(symbols)
            ),
            tuple(_item(symbol) for symbol in reversed(symbols)),
        )
        with patch(
            "hologram.render.PurePosixPath",
            wraps=PurePosixPath,
        ) as relative_path:
            ir = project_render_ir(analyzed, state=STATE, hot_threshold=10)
        self.assertEqual(relative_path.call_count, size)
        self.assertEqual(len(ir.files), size)
        self.assertEqual(
            tuple(file.path for file in ir.files),
            tuple(sorted(symbol.file for symbol in symbols)),
        )

    def test_projection_rejects_invalid_inputs_and_malformed_ownership(self) -> None:
        symbol = _symbol("src/value.py", "value")
        valid = _analyzed(
            (_file(symbol.file, symbols=(symbol,)),),
            (_item(symbol),),
        )
        for state in ("a" * 63, "A" * 64, "g" * 64, " a" * 63, ""):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "state"):
                project_render_ir(valid, state=state, hot_threshold=1)
        for threshold in (True, False, 0, -1, 1.0, "1", None):
            with (
                self.subTest(threshold=threshold),
                self.assertRaisesRegex((TypeError, ValueError), "hot_threshold"),
            ):
                project_render_ir(
                    valid,
                    state=STATE,
                    hot_threshold=threshold,  # type: ignore[arg-type]
                )

        duplicate_file = _file("src/duplicate.py")
        with self.assertRaisesRegex(ValueError, "duplicate.*file"):
            project_render_ir(
                _analyzed((duplicate_file, duplicate_file), ()),
                state=STATE,
                hot_threshold=1,
            )

        with self.assertRaisesRegex(ValueError, "duplicate.*SymbolId"):
            project_render_ir(
                _analyzed(
                    (_file(symbol.file, symbols=(symbol, symbol)),),
                    (_item(symbol), _item(symbol)),
                ),
                state=STATE,
                hot_threshold=1,
            )

        with self.assertRaisesRegex(ValueError, "ownership"):
            project_render_ir(
                _analyzed((_file(symbol.file),), (_item(symbol),)),
                state=STATE,
                hot_threshold=1,
            )
        with self.assertRaisesRegex(ValueError, "ownership"):
            project_render_ir(
                _analyzed((_file(symbol.file, symbols=(symbol,)),), ()),
                state=STATE,
                hot_threshold=1,
            )
        changed = Symbol(
            symbol.id,
            symbol.span,
            symbol.visibility,
            "changed signature",
        )
        with self.assertRaisesRegex(ValueError, "ownership"):
            project_render_ir(
                _analyzed(
                    (_file(symbol.file, symbols=(symbol,)),),
                    (_item(changed),),
                ),
                state=STATE,
                hot_threshold=1,
            )

        wrong_span = Symbol(
            symbol.id,
            _span("src/other.py"),
            symbol.visibility,
            symbol.signature,
        )
        with self.assertRaisesRegex(ValueError, "ownership"):
            project_render_ir(
                _analyzed(
                    (_file(symbol.file, symbols=(wrong_span,)),),
                    (_item(wrong_span),),
                ),
                state=STATE,
                hot_threshold=1,
            )

        wrong_language_id = SymbolId(
            Language.JAVA,
            symbol.file,
            (),
            SymbolKind.FUNCTION,
            symbol.name,
            "()",
        )
        wrong_language = Symbol(
            wrong_language_id,
            symbol.span,
            symbol.visibility,
            symbol.signature,
        )
        with self.assertRaisesRegex(ValueError, "ownership"):
            project_render_ir(
                _analyzed(
                    (_file(symbol.file, symbols=(wrong_language,)),),
                    (_item(wrong_language),),
                ),
                state=STATE,
                hot_threshold=1,
            )

        first_collision = _symbol(
            "src/collision.py",
            "same",
            container=("A.B",),
            signature_key="(int)",
        )
        second_collision = _symbol(
            "src/collision.py",
            "same",
            container=("A", "B"),
            signature_key="(int)",
            line=2,
        )
        with self.assertRaisesRegex(ValueError, "display.*collision"):
            project_render_ir(
                _analyzed(
                    (
                        _file(
                            first_collision.file,
                            symbols=(first_collision, second_collision),
                        ),
                    ),
                    (_item(first_collision), _item(second_collision)),
                ),
                state=STATE,
                hot_threshold=1,
            )

        missing_target = _symbol("src/missing.py", "missing")
        malformed_call = _resolved_call(symbol, missing_target, line=2)
        with self.assertRaisesRegex(ValueError, "resolved call target"):
            project_render_ir(
                _analyzed(
                    (_file(symbol.file, symbols=(symbol,)),),
                    (_item(symbol),),
                    resolution=_resolution(calls=(malformed_call,)),
                ),
                state=STATE,
                hot_threshold=1,
            )


def _direct_symbol(
    symbol_id: SymbolId,
    *,
    line: int = 1,
    column: int = 0,
    visibility: str = "pub",
    signature: str | None = None,
    parameters: tuple[str, ...] = (),
    returns: str | None = None,
    annotations: tuple[str, ...] = (),
    modifiers: tuple[str, ...] = (),
    components: tuple[str, ...] = (),
    supers: tuple[str, ...] = (),
    permits: tuple[str, ...] = (),
    ordered_calls: tuple[str, ...] = (),
    throws: tuple[str, ...] = (),
    behaviors: tuple[str, ...] = (),
    body_lines: int = 0,
    markers: tuple[str, ...] = (),
    duplicate_peers: tuple[SymbolId, ...] = (),
    call_targets: tuple[SymbolId, ...] = (),
) -> RenderSymbol:
    return RenderSymbol(
        symbol_id,
        line,
        column,
        visibility,
        signature if signature is not None else symbol_id.name,
        parameters,
        returns,
        annotations,
        modifiers,
        components,
        supers,
        permits,
        ordered_calls,
        throws,
        behaviors,
        body_lines,
        markers,
        duplicate_peers,
        call_targets,
    )


def _all_fields_render_ir() -> RenderIR:
    production_path = "src/α ids/Item.java"
    production_id = SymbolId(
        Language.JAVA,
        production_path,
        ("Outer", "Inner"),
        SymbolKind.METHOD,
        "build",
        "(List<A,B>)",
    )
    overload_id = SymbolId(
        Language.JAVA,
        production_path,
        ("Outer", "Inner"),
        SymbolKind.METHOD,
        "build",
        "(String)",
    )
    test_path = "tests/item test.py"
    test_id = SymbolId(
        Language.PYTHON,
        test_path,
        (),
        SymbolKind.FUNCTION,
        "check",
        "()",
    )
    return RenderIR(
        STATE,
        (),
        ("app→core", "pkg→α"),
        (
            RenderFile(
                "gen/generated.py",
                "python",
                "generated",
                None,
                (),
                (),
            ),
            RenderFile(
                "src/index.ts",
                "typescript",
                "production",
                None,
                (RenderReexport("./api kit", None, None, True),),
                (),
            ),
            RenderFile(
                production_path,
                "java",
                "production",
                "com.acme.ids",
                (RenderReexport("./named", "Thing", "Alias", False),),
                (
                    _direct_symbol(
                        production_id,
                        line=7,
                        column=4,
                        signature="build(List<A,B> value) → Résult",
                        parameters=("value: List<A,B>",),
                        returns="Résult",
                        annotations=('@Bean("x")',),
                        modifiers=("public", "async"),
                        components=("id: int",),
                        supers=("Base<T,U>",),
                        permits=("Child One",),
                        ordered_calls=("Target.run", "Target.run"),
                        call_targets=(overload_id, overload_id),
                        throws=("ProblemException",),
                        behaviors=("ItemTest.creates item",),
                        body_lines=21,
                        markers=("×3", "✓", "≈2"),
                        duplicate_peers=(overload_id, test_id),
                    ),
                    _direct_symbol(
                        overload_id,
                        line=30,
                        column=2,
                        signature="build(String value)",
                        parameters=("value: String",),
                        returns="Résult2",
                    ),
                ),
            ),
            RenderFile(
                test_path,
                "python",
                "test",
                None,
                (),
                (
                    _direct_symbol(
                        test_id,
                        line=2,
                        column=1,
                        visibility="private",
                        signature="check()",
                        markers=("×0?",),
                    ),
                ),
            ),
        ),
    )


def _projected_repeated_values(
    values: tuple[str, ...],
    *,
    occurrences: int = 3,
) -> RenderIR:
    if not 1 <= occurrences <= 10:
        raise ValueError("test helper supports one through ten occurrences")
    files: list[FileIR] = []
    items: list[AnalyzedSymbol] = []
    for index, value in enumerate(values):
        path = f"src/value{index}.py"
        optional = [value if occurrences > offset else None for offset in range(1, 10)]
        symbol = _symbol(
            path,
            f"value{index}",
            signature=value,
            params=((value,) if optional[1] is not None else ()),
            returns=optional[2],
            annotations=((value,) if optional[3] is not None else ()),
            modifiers=((value,) if optional[4] is not None else ()),
            components=((value,) if optional[5] is not None else ()),
            supers=((value,) if optional[6] is not None else ()),
            permits=((value,) if optional[7] is not None else ()),
            raises=((value,) if optional[8] is not None else ()),
        )
        files.append(_file(path, module=optional[0], symbols=(symbol,)))
        items.append(_item(symbol))
    return project_render_ir(
        _analyzed(tuple(reversed(files)), tuple(reversed(items))),
        state=STATE,
        hot_threshold=10,
    )


def _json_token(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _exact_intern_savings(value: str, alias: str, occurrences: int) -> int:
    literal = _json_token(f"&{value}" if value.startswith("&") else value)
    alias_token = _json_token(alias)
    declaration = f"· intern {alias_token} {_json_token(value)}\n"
    return (
        occurrences * len(literal.encode())
        - occurrences * len(alias_token.encode())
        - len(declaration.encode())
    )


def _small_render_ir(*, two_symbols: bool = False) -> RenderIR:
    path = "src/a.py"
    first_id = SymbolId(
        Language.PYTHON,
        path,
        (),
        SymbolKind.FUNCTION,
        "a",
        "()",
    )
    symbols = [_direct_symbol(first_id, signature="a()")]
    if two_symbols:
        second_id = SymbolId(
            Language.PYTHON,
            path,
            (),
            SymbolKind.FUNCTION,
            "b",
            "()",
        )
        symbols.append(_direct_symbol(second_id, line=2, signature="b()"))
    return RenderIR(
        STATE,
        (),
        (),
        (
            RenderFile(
                path,
                "python",
                "production",
                None,
                (),
                tuple(symbols),
            ),
        ),
    )


class RenderRoundTripTest(unittest.TestCase):
    def test_all_fields_have_exact_canonical_unicode_grammar_and_round_trip(
        self,
    ) -> None:
        ir = _all_fields_render_ir()
        expected = (
            f"# hologram state={STATE} · regen: hologram build\n"
            '· deps ["app→core","pkg→α"]\n'
            '@ "gen/generated.py" "python" "generated" null\n'
            '@ "src/index.ts" "typescript" "production" null\n'
            '  reexport [["./api kit",null,null,true]]\n'
            '@ "src/α ids/Item.java" "java" "production" "com.acme.ids"\n'
            '  reexport [["./named","Thing","Alias",false]]\n'
            '  :7:4 [["Outer","Inner"],"method","build",'
            '"(List<A,B>)"] "pub"\n'
            '    signature "build(List<A,B> value) → Résult"\n'
            '    param ["value: List<A,B>"]\n'
            '    return "Résult"\n'
            '    annotation ["@Bean(\\"x\\")"]\n'
            '    modifier ["public","async"]\n'
            '    component ["id: int"]\n'
            '    super ["Base<T,U>"]\n'
            '    permit ["Child One"]\n'
            '    call ["Target.run","Target.run"]\n'
            '    call-target [["java","src/α ids/Item.java",'
            '["Outer","Inner"],"method","build","(String)"],'
            '["java","src/α ids/Item.java",["Outer","Inner"],'
            '"method","build","(String)"]]\n'
            '    throw ["ProblemException"]\n'
            '    behavior ["ItemTest.creates item"]\n'
            '    duplicate [["java","src/α ids/Item.java",'
            '["Outer","Inner"],"method","build","(String)"],'
            '["python","tests/item test.py",[],"fn","check","()"]]\n'
            "    body 21\n"
            '    mark ["×3","✓","≈2"]\n'
            '  :30:2 [["Outer","Inner"],"method","build",'
            '"(String)"] "pub"\n'
            '    signature "build(String value)"\n'
            '    param ["value: String"]\n'
            '    return "Résult2"\n'
            '@ "tests/item test.py" "python" "test" null\n'
            '  :2:1 [[] ,"fn","check","()"] "private"\n'
            '    signature "check()"\n'
            "    return null\n"
            '    mark ["×0?"]\n'
        ).replace('[[] ,"fn"', '[[],"fn"')
        text = render_project(ir)
        self.assertEqual(text, expected)
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))
        self.assertEqual(decode_render(text), ir)
        self.assertEqual(render_project(decode_render(text)), text)

    def test_file_leaves_keep_same_shape_and_every_method_owned(self) -> None:
        files: list[RenderFile] = []
        for path, owner in (
            ("src/ids/ItemId.java", "ItemId"),
            ("src/ids/OrderId.java", "OrderId"),
        ):
            type_id = SymbolId(
                Language.JAVA,
                path,
                (),
                SymbolKind.RECORD,
                owner,
                "",
            )
            method_id = SymbolId(
                Language.JAVA,
                path,
                (owner,),
                SymbolKind.METHOD,
                "of",
                "(String)",
            )
            files.append(
                RenderFile(
                    path,
                    "java",
                    "production",
                    "ids",
                    (),
                    (
                        _direct_symbol(type_id, signature=f"{owner}(String)"),
                        _direct_symbol(
                            method_id,
                            line=2,
                            signature=f"of(String):{owner}",
                            parameters=("String",),
                            returns=owner,
                        ),
                    ),
                )
            )
        text = render_project(RenderIR(STATE, (), (), tuple(files)))
        self.assertIn('@ "src/ids/ItemId.java"', text)
        self.assertIn('@ "src/ids/OrderId.java"', text)
        self.assertEqual(text.count('"of(String):'), 2)
        self.assertNotIn("ItemId,OrderId", text)
        self.assertNotIn("⟨X⟩", text)

    def test_projection_plans_raw_interns_and_renderer_validates_exact_table(
        self,
    ) -> None:
        value = "company.deep.namespace.service.target.run"
        ir = _projected_repeated_values((value,))
        self.assertEqual(ir.interns, (RenderIntern("&run", value),))
        rendered_file = ir.files[0]
        self.assertEqual(rendered_file.module, value)
        self.assertEqual(rendered_file.symbols[0].signature, value)
        self.assertEqual(rendered_file.symbols[0].parameters, (value,))

        text = render_project(ir)
        self.assertIn(
            f"· intern {_json_token('&run')} {_json_token(value)}\n",
            text,
        )
        self.assertEqual(text.count(_json_token("&run")), 4)
        self.assertEqual(text.count(_json_token(value)), 1)
        self.assertEqual(decode_render(text), ir)
        with self.assertRaisesRegex(ValueError, "intern"):
            render_project(replace(ir, interns=()))
        with self.assertRaisesRegex(ValueError, "intern"):
            render_project(replace(ir, interns=(RenderIntern("&wrong", value),)))
        with self.assertRaisesRegex(ValueError, "intern"):
            render_project(
                replace(
                    ir,
                    interns=(
                        RenderIntern("&run", value),
                        RenderIntern("&spare", "unused.long.value"),
                    ),
                )
            )

    def test_interning_uses_three_occurrences_and_strict_positive_utf8_savings(
        self,
    ) -> None:
        only_two = "company.deep.namespace.service.target.run"
        self.assertEqual(
            _projected_repeated_values((only_two,), occurrences=2).interns,
            (),
        )
        self.assertEqual(
            _projected_repeated_values(("x/y",), occurrences=6).interns,
            (),
        )

        zero = f"{'x' * 15}/Stable"
        positive = f"{'x' * 16}/Stable"
        self.assertEqual(_exact_intern_savings(zero, "&Stable", 3), 0)
        self.assertEqual(_exact_intern_savings(positive, "&Stable", 3), 2)
        self.assertEqual(_projected_repeated_values((zero,)).interns, ())
        self.assertEqual(
            _projected_repeated_values((positive,)).interns,
            (RenderIntern("&Stable", positive),),
        )

    def test_aliases_are_suffix_unique_before_profitability_and_sorted(
        self,
    ) -> None:
        left = "company.deep.namespace.Left.run"
        right = "company.deep.namespace.Right.run"
        ir = _projected_repeated_values((right, left))
        self.assertEqual(
            ir.interns,
            (
                RenderIntern("&Left.run", left),
                RenderIntern("&Right.run", right),
            ),
        )

        unprofitable = "x/Left/run"
        profitable = "company.really.long.and.descriptive.package.namespace.Left.run"
        disambiguated = _projected_repeated_values((profitable, unprofitable))
        self.assertEqual(
            disambiguated.interns,
            (RenderIntern("&namespace.Left.run", profitable),),
        )

        same_segments = _projected_repeated_values(
            ("company/pkg/Left/run", "company-pkg-Left-run"),
            occurrences=6,
        )
        self.assertEqual(same_segments.interns, ())
        self.assertEqual(
            _projected_repeated_values(("1234567890---",), occurrences=10).interns,
            (),
        )

    def test_alias_planning_uses_linear_memory_for_deep_segment_values(self) -> None:
        value = ".".join(("segment",) * 3_000)
        tracemalloc.start()
        try:
            ir = _projected_repeated_values((value,))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(ir.interns, (RenderIntern("&segment", value),))
        self.assertLess(peak, 12 * 1024 * 1024)

    def test_aliases_round_trip_through_every_eligible_field(self) -> None:
        value = "company.deep.namespace.service.target.run"
        symbol_id = SymbolId(
            Language.PYTHON,
            "src/a.py",
            (),
            SymbolKind.FUNCTION,
            "a",
            "()",
        )
        symbol = _direct_symbol(
            symbol_id,
            signature=value,
            parameters=(value,),
            returns=value,
            annotations=(value,),
            modifiers=(value,),
            components=(value,),
            supers=(value,),
            permits=(value,),
            ordered_calls=(value,),
            call_targets=(symbol_id,),
            throws=(value,),
            behaviors=(value,),
        )
        ir = RenderIR(
            STATE,
            (RenderIntern("&run", value),),
            (value,),
            (
                RenderFile(
                    "src/a.py",
                    "python",
                    "production",
                    value,
                    (RenderReexport(value, value, value, False),),
                    (symbol,),
                ),
            ),
        )

        text = render_project(ir)
        self.assertEqual(text.count(_json_token(value)), 1)
        self.assertEqual(text.count(_json_token("&run")), 17)
        self.assertEqual(decode_render(text), ir)
        self.assertEqual(render_project(decode_render(text)), text)

    def test_utf8_json_escaping_and_literal_ampersands_are_reversible(self) -> None:
        for value in (
            "company/really/long/quoted\\line\nrun",
            "公司/very/long/descriptive/路径/run",
            "&company.deep.namespace.service.target.run",
        ):
            with self.subTest(value=value):
                ir = _projected_repeated_values((value,))
                self.assertTrue(ir.interns)
                text = render_project(ir)
                self.assertIn(_json_token(value), text)
                self.assertEqual(decode_render(text), ir)

        literal = _projected_repeated_values(("&x",), occurrences=2)
        self.assertEqual(literal.interns, ())
        literal_text = render_project(literal)
        self.assertEqual(literal_text.count(_json_token("&&x")), 2)
        self.assertEqual(decode_render(literal_text), literal)

    def test_renderer_validates_canonical_structure_without_reordering(self) -> None:
        ir = _all_fields_render_ir()
        production = next(file for file in ir.files if file.symbols)
        symbol = production.symbols[0]
        invalid = (
            replace(ir, state="A" * 64),
            replace(ir, dependencies=tuple(reversed(ir.dependencies))),
            replace(ir, dependencies=("app→core", "app→core")),
            replace(ir, files=tuple(reversed(ir.files))),
            replace(
                ir,
                files=tuple(
                    replace(
                        production,
                        reexports=(
                            production.reexports[0],
                            production.reexports[0],
                        ),
                    )
                    if file is production
                    else file
                    for file in ir.files
                ),
            ),
            replace(
                ir,
                files=tuple(
                    replace(
                        production,
                        symbols=(
                            replace(symbol, markers=("✓", "×0")),
                            *production.symbols[1:],
                        ),
                    )
                    if file is production
                    else file
                    for file in ir.files
                ),
            ),
            replace(
                ir,
                files=tuple(
                    replace(production, path="/absolute.java")
                    if file is production
                    else file
                    for file in ir.files
                ),
            ),
        )
        for malformed in invalid:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                render_project(malformed)

        two = _small_render_ir(two_symbols=True)
        with self.assertRaises(ValueError):
            render_project(
                replace(
                    two,
                    files=(
                        replace(
                            two.files[0],
                            symbols=tuple(reversed(two.files[0].symbols)),
                        ),
                    ),
                )
            )

    def test_renderer_and_decoder_require_lossless_target_provenance(self) -> None:
        ir = _all_fields_render_ir()
        production = next(file for file in ir.files if file.path.startswith("src/α"))
        owner = production.symbols[0]

        def with_owner(symbol: RenderSymbol) -> RenderIR:
            return replace(
                ir,
                files=tuple(
                    replace(file, symbols=(symbol, *file.symbols[1:]))
                    if file is production
                    else file
                    for file in ir.files
                ),
            )

        missing = SymbolId(
            Language.JAVA,
            "missing.java",
            (),
            SymbolKind.FUNCTION,
            "missing",
            "()",
        )
        malformed = (
            replace(owner, duplicate_peers=()),
            replace(owner, duplicate_peers=tuple(reversed(owner.duplicate_peers))),
            replace(owner, duplicate_peers=(owner.symbol_id,)),
            replace(owner, duplicate_peers=(missing,)),
            replace(owner, call_targets=()),
            replace(owner, call_targets=(missing, missing)),
        )
        for symbol in malformed:
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                render_project(with_owner(symbol))

        text = render_project(ir)
        without_call_targets = (
            "\n".join(
                line
                for line in text.splitlines()
                if not line.startswith("    call-target ")
            )
            + "\n"
        )
        without_duplicate_peers = (
            "\n".join(
                line
                for line in text.splitlines()
                if not line.startswith("    duplicate ")
            )
            + "\n"
        )
        for malformed_text in (without_call_targets, without_duplicate_peers):
            with self.assertRaises(RenderDecodeError):
                decode_render(malformed_text)

    def test_decoder_rejects_header_whitespace_and_json_variants(self) -> None:
        text = render_project(_all_fields_render_ir())
        mutations = {
            "header": text.replace("# hologram", "# hologram map", 1),
            "state": text.replace(f"state={STATE}", f"state={'A' * 64}", 1),
            "regen": text.replace("hologram build", "hologram check", 1),
            "crlf": text.replace("\n", "\r\n"),
            "missing-final-lf": text[:-1],
            "extra-final-lf": f"{text}\n",
            "trailing-space": text.replace("· deps ", "· deps  ", 1),
            "escaped-unicode": text.replace("α", "\\u03b1", 1),
            "json-spacing": text.replace(
                '["app→core","pkg→α"]',
                '["app→core", "pkg→α"]',
                1,
            ),
            "unknown-top-level": text.replace(
                '· deps ["app→core","pkg→α"]\n',
                '· deps ["app→core","pkg→α"]\n· unknown []\n',
                1,
            ),
        }
        for name, malformed in mutations.items():
            self.assertNotEqual(malformed, text)
            with self.subTest(name=name), self.assertRaises(RenderDecodeError):
                decode_render(malformed)
        with self.assertRaises(RenderDecodeError):
            decode_render(None)  # type: ignore[arg-type]

    def test_decoder_rejects_file_and_symbol_structure_mutations(self) -> None:
        empty = RenderIR(
            STATE,
            (),
            (),
            (
                RenderFile("a.py", "python", "production", None, (), ()),
                RenderFile("b.py", "python", "test", None, (), ()),
            ),
        )
        empty_text = render_project(empty)
        empty_lines = empty_text.splitlines()
        file_mutations = {
            "missing-deps": "\n".join((empty_lines[0], *empty_lines[2:])) + "\n",
            "duplicate-deps": "\n".join(
                (empty_lines[0], empty_lines[1], empty_lines[1], *empty_lines[2:])
            )
            + "\n",
            "duplicate-file": "\n".join((*empty_lines, empty_lines[2])) + "\n",
            "unsorted-files": "\n".join(
                (*empty_lines[:2], empty_lines[3], empty_lines[2])
            )
            + "\n",
            "invalid-language": empty_text.replace('"python"', '"ruby"', 1),
            "invalid-role": empty_text.replace('"production"', '"fixture"', 1),
            "invalid-path": empty_text.replace('"a.py"', '"/a.py"', 1),
            "invalid-module-type": empty_text.replace(
                '"production" null',
                '"production" 1',
                1,
            ),
        }
        for name, malformed in file_mutations.items():
            with self.subTest(name=name), self.assertRaises(RenderDecodeError):
                decode_render(malformed)

    def test_decoder_rejects_noncanonical_intern_declarations(self) -> None:
        value = "company.deep.namespace.service.target.run"
        canonical = render_project(_projected_repeated_values((value,)))
        declaration = f'· intern "&run" {_json_token(value)}\n'
        self.assertIn(declaration, canonical)

        left = "company.deep.namespace.Left.run"
        right = "company.deep.namespace.Right.run"
        pair_text = render_project(_projected_repeated_values((right, left)))
        left_declaration = f'· intern "&Left.run" {_json_token(left)}\n'
        right_declaration = f'· intern "&Right.run" {_json_token(right)}\n'
        self.assertIn(f"{left_declaration}{right_declaration}", pair_text)

        short_ir = _projected_repeated_values(("x/y",), occurrences=6)
        short_text = render_project(short_ir)
        aliased_short = short_text.replace(_json_token("x/y"), '"&y"')
        aliased_short = aliased_short.replace(
            f"# hologram state={STATE} · regen: hologram build\n",
            f'# hologram state={STATE} · regen: hologram build\n· intern "&y" "x/y"\n',
            1,
        )
        under_three_text = render_project(
            _projected_repeated_values((value,), occurrences=2)
        ).replace(_json_token(value), '"&run"')
        under_three_text = under_three_text.replace(
            f"# hologram state={STATE} · regen: hologram build\n",
            f"# hologram state={STATE} · regen: hologram build\n{declaration}",
            1,
        )

        mutations = {
            "missing": canonical.replace(declaration, "", 1),
            "duplicate-alias": canonical.replace(declaration, declaration * 2, 1),
            "malformed-alias": canonical.replace('"&run"', '"&1"'),
            "wrong-derived": canonical.replace('"&run"', '"&target.run"'),
            "wrong-value": canonical.replace(
                _json_token(value),
                _json_token(f"{value}.wrong"),
                1,
            ),
            "unsorted": pair_text.replace(
                f"{left_declaration}{right_declaration}",
                f"{right_declaration}{left_declaration}",
                1,
            ),
            "duplicate-expanded-value": canonical.replace(
                declaration,
                f'{declaration}· intern "&other" {_json_token(value)}\n',
                1,
            ),
            "under-three": under_three_text,
            "nonpositive": aliased_short,
        }
        for name, malformed in mutations.items():
            self.assertNotEqual(malformed, canonical)
            with self.subTest(name=name), self.assertRaises(RenderDecodeError):
                decode_render(malformed)

        text = render_project(_small_render_ir())
        signature = '    signature "a()"\n'
        returns = "    return null\n"
        symbol_mutations = {
            "missing-signature": text.replace(signature, "", 1),
            "missing-return": text.replace(returns, "", 1),
            "duplicate-signature": text.replace(signature, signature * 2, 1),
            "wrong-child-order": text.replace(
                f"{signature}{returns}",
                f"{returns}{signature}",
                1,
            ),
            "empty-optional": text.replace(
                signature,
                f"{signature}    param []\n",
                1,
            ),
            "zero-body": text.replace(returns, f"{returns}    body 0\n", 1),
            "negative-body": text.replace(returns, f"{returns}    body -1\n", 1),
            "unknown-child": text.replace(returns, f"{returns}    mystery []\n", 1),
            "zero-line": text.replace("  :1:0 ", "  :0:0 ", 1),
            "negative-column": text.replace("  :1:0 ", "  :1:-1 ", 1),
            "bad-local-id": text.replace(
                '[[],"fn","a","()"]',
                '[[],"fn","a"]',
                1,
            ),
            "bad-container": text.replace(
                '[[],"fn","a","()"]',
                '["C","fn","a","()"]',
                1,
            ),
            "bad-kind": text.replace(
                '[[],"fn","a","()"]',
                '[[],"bogus","a","()"]',
                1,
            ),
            "bad-visibility": text.replace(' "pub"\n', ' "package"\n', 1),
            "undeclared-alias": text.replace('"a()"', '"&missing"', 1),
        }
        for name, malformed in symbol_mutations.items():
            self.assertNotEqual(malformed, text)
            with self.subTest(name=name), self.assertRaises(RenderDecodeError):
                decode_render(malformed)


if __name__ == "__main__":
    unittest.main()
