from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import hologram.cli as cli_module
import hologram.diff as diff_module
from hologram.analysis import (
    AnalyzedProject,
    AnalyzedSymbol,
    DuplicateMatch,
    DuplicateScore,
    ReferenceFacts,
    ZeroReference,
)
from hologram.cli import EXIT_INCOMPLETE, EXIT_OK, BuildArtifact, command_diff
from hologram.config import (
    CONFIG_NAME,
    ProjectConfig,
    canonical_config_bytes,
    default_config,
)
from hologram.diff import (
    DependencyChange,
    DiffAdvisory,
    DiffInput,
    DiffReport,
    FileChange,
    FileTopology,
    RevisionError,
    SymbolChange,
    analyze_revision,
    compare_projects,
)
from hologram.model import (
    FileIR,
    Language,
    ProjectIR,
    SourceFile,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)
from hologram.render import RenderFile, RenderIntern, RenderIR, RenderSymbol
from hologram.resolve import ResolutionResult

_STATE_A = "a" * 64
_STATE_B = "b" * 64
_DISCLAIMER = (
    "Static analysis cannot guarantee semantic deadness or authorize deletion; "
    "inspect source and runtime/framework reachability."
)


def _config() -> ProjectConfig:
    return dataclasses.replace(
        default_config(),
        agents=("codex",),
        languages=(Language.PYTHON,),
        include=("**/*.py",),
        exclude=(),
        output=None,
    )


def _span(symbol: Symbol) -> SourceSpan:
    return symbol.span


def _input(
    specs: tuple[tuple[str, str, int, ZeroReference], ...],
    *,
    modules: dict[str, str | None] | None = None,
    dependencies: tuple[str, ...] = (),
    state: str = _STATE_A,
    interns: tuple[RenderIntern, ...] = (),
) -> DiffInput:
    module_by_file = {} if modules is None else modules
    symbols_by_file: dict[str, list[Symbol]] = {}
    analyzed: list[AnalyzedSymbol] = []
    render_by_file: dict[str, list[RenderSymbol]] = {}
    for file, name, line, zero in specs:
        symbol_id = SymbolId(
            Language.PYTHON,
            file,
            (),
            SymbolKind.FUNCTION,
            name,
            "()",
        )
        span = SourceSpan(file, line, 0, line + 1, 0)
        symbol = Symbol(
            symbol_id,
            span,
            Visibility.PRIVATE,
            f"def {name}()",
            body_lines=2,
        )
        symbols_by_file.setdefault(file, []).append(symbol)
        analyzed.append(
            AnalyzedSymbol(
                symbol,
                ReferenceFacts((), (), (), (), zero),
                None,
                (),
            )
        )
        marker = {
            ZeroReference.NONE: (),
            ZeroReference.STRONG: ("×0",),
            ZeroReference.UNCERTAIN: ("×0?",),
        }[zero]
        render_by_file.setdefault(file, []).append(
            RenderSymbol(
                symbol_id,
                line,
                0,
                Visibility.PRIVATE.value,
                symbol.signature,
                (),
                None,
                (),
                (),
                (),
                (),
                (),
                (),
                (),
                (),
                2,
                marker,
            )
        )

    files: list[FileIR] = []
    rendered_files: list[RenderFile] = []
    root = Path("/fixture")
    for file in sorted(symbols_by_file):
        raw = b"# fixture\n"
        source = SourceFile(
            root.joinpath(*Path(file).parts),
            file,
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        files.append(
            FileIR(
                source,
                module_by_file.get(file),
                tuple(symbols_by_file[file]),
            )
        )
        rendered_files.append(
            RenderFile(
                file,
                Language.PYTHON.value,
                SourceRole.PRODUCTION.value,
                module_by_file.get(file),
                (),
                tuple(render_by_file[file]),
            )
        )
    project = ProjectIR(root, tuple(files), (), True)
    resolution = ResolutionResult((), (), (), ())
    analysis = AnalyzedProject(project, resolution, tuple(analyzed), ())
    render = RenderIR(state, interns, dependencies, tuple(rendered_files))
    return DiffInput(analysis, render)


def _replace_render_symbol(
    value: DiffInput,
    symbol_id: SymbolId,
    **changes: Any,
) -> DiffInput:
    files: list[RenderFile] = []
    for file in value.render_ir.files:
        symbols = tuple(
            dataclasses.replace(symbol, **changes)
            if symbol.symbol_id == symbol_id
            else symbol
            for symbol in file.symbols
        )
        files.append(dataclasses.replace(file, symbols=symbols))
    return DiffInput(
        value.analyzed,
        dataclasses.replace(value.render_ir, files=tuple(files)),
    )


def _move_owned_symbol(
    value: DiffInput,
    symbol_id: SymbolId,
    *,
    line: int,
    column: int,
) -> DiffInput:
    span = SourceSpan(symbol_id.file, line, column, line + 1, column)
    replacement: Symbol | None = None
    project_files: list[FileIR] = []
    for file in value.analyzed.project.files:
        symbols: list[Symbol] = []
        for symbol in file.symbols:
            if symbol.id == symbol_id:
                replacement = dataclasses.replace(symbol, span=span)
                symbols.append(replacement)
            else:
                symbols.append(symbol)
        project_files.append(dataclasses.replace(file, symbols=tuple(symbols)))
    if replacement is None:
        raise AssertionError("fixture SymbolId is missing")
    project = dataclasses.replace(
        value.analyzed.project,
        files=tuple(project_files),
    )
    analyzed_symbols = tuple(
        dataclasses.replace(item, symbol=replacement)
        if item.symbol.id == symbol_id
        else item
        for item in value.analyzed.symbols
    )
    analyzed = dataclasses.replace(
        value.analyzed,
        project=project,
        symbols=analyzed_symbols,
    )
    rendered = _replace_render_symbol(
        value,
        symbol_id,
        source_line=line,
        source_column=column,
    ).render_ir
    return DiffInput(analyzed, rendered)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=diff@example.invalid",
        "-c",
        "user.name=Diff Test",
        "commit",
        "-qm",
        message,
    )


def _tree_metadata(root: Path) -> dict[str, tuple[int, int, int, bytes]]:
    result: dict[str, tuple[int, int, int, bytes]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        metadata = path.stat()
        result[path.relative_to(root).as_posix()] = (
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_mtime_ns,
            path.read_bytes(),
        )
    return result


class DiffComparisonTest(unittest.TestCase):
    def test_public_records_are_frozen_slotted_and_owned(self) -> None:
        topology = FileTopology(
            "src/a.py",
            "python",
            "production",
            None,
            [],  # type: ignore[arg-type]
        )
        self.assertEqual(topology.reexports, ())
        self.assertFalse(hasattr(topology, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            topology.path = "changed.py"  # type: ignore[misc]
        self.assertEqual(
            diff_module.__all__,
            [
                "DependencyChange",
                "DiffAdvisory",
                "DiffInput",
                "DiffReport",
                "FileChange",
                "FileTopology",
                "RevisionError",
                "SymbolChange",
                "analyze_revision",
                "compare_projects",
            ],
        )

    def test_record_sides_and_runtime_types_are_strict(self) -> None:
        value = _input((("src/a.py", "a", 1, ZeroReference.NONE),))
        rendered = value.render_ir.files[0].symbols[0]
        topology = FileTopology("src/a.py", "python", "production", None, ())
        with self.assertRaises(TypeError):
            SymbolChange("added", None, "bad")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            FileChange("removed", rendered, None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DiffAdvisory(
                "strong-zero",
                "bad",  # type: ignore[arg-type]
                SourceSpan("src/a.py", 1, 0, 1, 1),
                None,
                None,
                None,
            )
        with self.assertRaises(ValueError):
            FileChange("added", topology, None)
        with self.assertRaises(TypeError):
            DiffReport(("bad",), (), (), (), "")  # type: ignore[arg-type]

    def test_added_changed_removed_symbols_files_and_dependencies(self) -> None:
        before = _input(
            (
                ("src/old.py", "old_api", 3, ZeroReference.NONE),
                ("src/service.py", "run", 10, ZeroReference.NONE),
            ),
            modules={"src/old.py": "old", "src/service.py": "app"},
            dependencies=("app→core", "old→core"),
        )
        after = _input(
            (
                ("src/new.py", "new_api", 4, ZeroReference.NONE),
                ("src/service.py", "run", 10, ZeroReference.NONE),
            ),
            modules={"src/new.py": "new", "src/service.py": "application"},
            dependencies=("application→core",),
        )
        run_id = after.render_ir.files[1].symbols[0].symbol_id
        after = _replace_render_symbol(after, run_id, signature="def run(value)")

        report = compare_projects(before, after)

        self.assertEqual(
            tuple(change.kind for change in report.symbol_changes),
            ("added", "removed", "changed"),
        )
        self.assertEqual(
            tuple(change.kind for change in report.file_changes),
            ("added", "removed", "changed"),
        )
        self.assertEqual(
            report.dependency_changes,
            (
                DependencyChange("added", "application→core"),
                DependencyChange("removed", "app→core"),
                DependencyChange("removed", "old→core"),
            ),
        )
        self.assertIn("+ src/new.py:4 new_api", report.text)
        self.assertIn("- src/old.py:3 old_api", report.text)
        self.assertIn("~ src/service.py:10 run fields=signature", report.text)
        self.assertIn("~ module src/service.py: app→application", report.text)
        self.assertIn("+ dependency application→core", report.text)
        self.assertTrue(report.text.endswith(_DISCLAIMER + "\n"))

    def test_state_and_interns_are_ignored(self) -> None:
        before = _input((("src/a.py", "a", 1, ZeroReference.NONE),))
        after = DiffInput(
            before.analyzed,
            dataclasses.replace(
                before.render_ir,
                state=_STATE_B,
                interns=(RenderIntern("&a", "expanded.value"),),
            ),
        )
        report = compare_projects(before, after)
        self.assertEqual(report.symbol_changes, ())
        self.assertEqual(report.file_changes, ())
        self.assertEqual(report.dependency_changes, ())

    def test_no_added_symbol_skips_broad_duplicate_scoring(self) -> None:
        before = _input((("src/a.py", "a", 1, ZeroReference.NONE),))
        symbol_id = before.render_ir.files[0].symbols[0].symbol_id
        after = _replace_render_symbol(before, symbol_id, signature="def a(value)")
        with mock.patch.object(
            diff_module,
            "find_diff_duplicates",
            side_effect=AssertionError("duplicate scoring ran without additions"),
        ) as scorer:
            report = compare_projects(before, after)
        scorer.assert_not_called()
        self.assertEqual(report.advisories, ())
        self.assertEqual(len(report.symbol_changes), 1)

    def test_report_visibly_escapes_all_externally_derived_inline_text(self) -> None:
        file = 'src/line\nbreak\rcarriage\ttab\x01"quote.py'
        name = 'name\nwith\rcontrols\tand"quote'
        module = 'module\nwith\rcontrols\tand"quote'
        dependency = f'{file}→dep\nwith\rcontrols\tand"quote'
        before = _input(())
        after = _input(
            ((file, name, 4, ZeroReference.STRONG),),
            modules={file: module},
            dependencies=(dependency,),
        )

        report = compare_projects(before, after)

        escaped_file = 'src/line\\nbreak\\rcarriage\\ttab\\u0001\\"quote.py'
        escaped_name = 'name\\nwith\\rcontrols\\tand\\"quote'
        escaped_module = 'module\\nwith\\rcontrols\\tand\\"quote'
        escaped_dependency = escaped_file + '→dep\\nwith\\rcontrols\\tand\\"quote'
        self.assertNotIn(file, report.text)
        self.assertNotIn(name, report.text)
        self.assertNotIn(module, report.text)
        self.assertNotIn(dependency, report.text)
        self.assertIn(f"+ {escaped_file}:4 {escaped_name}", report.text)
        self.assertIn(f"module={escaped_module}", report.text)
        self.assertIn(f"+ dependency {escaped_dependency}", report.text)
        self.assertIn(
            f"new strong ×0: {escaped_file}:4 {escaped_name}",
            report.text,
        )

    def test_every_render_symbol_field_participates_in_change_detection(self) -> None:
        before = _input((("src/a.py", "a", 2, ZeroReference.NONE),))
        symbol = before.render_ir.files[0].symbols[0]
        cases: dict[str, object] = {
            "visibility": "pub",
            "signature": "def a(x)",
            "parameters": ("x",),
            "returns": "int",
            "annotations": ("decorated",),
            "modifiers": ("async",),
            "components": ("component",),
            "supers": ("Base",),
            "permits": ("Child",),
            "ordered_calls": ("call",),
            "throws": ("Error",),
            "behaviors": ("test_a",),
            "body_lines": 3,
            "markers": ("✓",),
        }
        for field, changed in cases.items():
            with self.subTest(field=field):
                after = _replace_render_symbol(
                    before, symbol.symbol_id, **{field: changed}
                )
                report = compare_projects(before, after)
                self.assertEqual(len(report.symbol_changes), 1)
                self.assertEqual(report.symbol_changes[0].kind, "changed")

    def test_source_provenance_is_compared_and_must_match_owned_span(self) -> None:
        before = _input((("src/a.py", "a", 2, ZeroReference.NONE),))
        symbol_id = before.render_ir.files[0].symbols[0].symbol_id
        after = _move_owned_symbol(before, symbol_id, line=3, column=4)
        report = compare_projects(before, after)
        self.assertEqual(len(report.symbol_changes), 1)
        self.assertIn("source_line", report.text)
        self.assertIn("source_column", report.text)

        invalid = _replace_render_symbol(before, symbol_id, source_line=99)
        with self.assertRaisesRegex(ValueError, "source provenance"):
            compare_projects(before, invalid)

    def test_advisories_filter_orient_canonicalize_and_keep_ties(self) -> None:
        before = _input(
            (
                ("src/a.py", "old_a", 1, ZeroReference.NONE),
                ("src/b.py", "old_b", 2, ZeroReference.NONE),
            )
        )
        after = _input(
            (
                ("src/a.py", "old_a", 1, ZeroReference.NONE),
                ("src/b.py", "old_b", 2, ZeroReference.NONE),
                ("src/new.py", "clone", 12, ZeroReference.STRONG),
                ("src/new.py", "clone_two", 20, ZeroReference.UNCERTAIN),
            )
        )
        by_name = {item.symbol.name: item.symbol for item in after.analyzed.symbols}
        score = DuplicateScore(0.92, 1.0, 0.75, 0.5, 0.89, False)
        matches = (
            DuplicateMatch(
                by_name["old_a"].id,
                by_name["old_b"].id,
                _span(by_name["old_a"]),
                _span(by_name["old_b"]),
                score,
            ),
            DuplicateMatch(
                by_name["old_a"].id,
                by_name["clone"].id,
                _span(by_name["old_a"]),
                _span(by_name["clone"]),
                score,
            ),
            DuplicateMatch(
                by_name["old_b"].id,
                by_name["clone"].id,
                _span(by_name["old_b"]),
                _span(by_name["clone"]),
                score,
            ),
            DuplicateMatch(
                by_name["clone_two"].id,
                by_name["clone"].id,
                _span(by_name["clone_two"]),
                _span(by_name["clone"]),
                score,
            ),
        )

        with mock.patch.object(
            diff_module,
            "find_diff_duplicates",
            return_value=matches,
        ):
            report = compare_projects(before, after)

        kinds = tuple(advisory.kind for advisory in report.advisories)
        self.assertEqual(kinds.count("strong-zero"), 1)
        self.assertEqual(kinds.count("uncertain-zero"), 1)
        duplicates = tuple(
            advisory
            for advisory in report.advisories
            if advisory.kind == "duplicate-candidate"
        )
        self.assertEqual(len(duplicates), 3)
        self.assertTrue(
            all(
                item.symbol in {by_name["clone"].id, by_name["clone_two"].id}
                for item in duplicates
            )
        )
        self.assertEqual(
            {item.peer for item in duplicates if item.symbol == by_name["clone"].id},
            {by_name["old_a"].id, by_name["old_b"].id, by_name["clone_two"].id},
        )
        self.assertIn("new strong ×0: src/new.py:12 clone", report.text)
        self.assertIn("new uncertain ×0?: src/new.py:20 clone_two", report.text)
        self.assertIn(
            "ast=0.92 total=0.89 control_flow=1.00 calls=0.75 names=0.50 exact=false",
            report.text,
        )

    def test_duplicate_advisory_rejects_unowned_peer_span(self) -> None:
        before = _input((("src/a.py", "old", 1, ZeroReference.NONE),))
        after = _input(
            (
                ("src/a.py", "old", 1, ZeroReference.NONE),
                ("src/new.py", "new", 2, ZeroReference.NONE),
            )
        )
        old, new = (item.symbol for item in after.analyzed.symbols)
        bad = DuplicateMatch(
            old.id,
            new.id,
            SourceSpan("src/a.py", 99, 0, 99, 1),
            new.span,
            DuplicateScore(1, 1, 1, 1, 1, True),
        )
        with (
            mock.patch.object(
                diff_module,
                "find_diff_duplicates",
                return_value=(bad,),
            ),
            self.assertRaisesRegex(ValueError, "span ownership"),
        ):
            compare_projects(before, after)

    def test_render_analysis_ownership_mismatch_is_rejected(self) -> None:
        value = _input((("src/a.py", "a", 1, ZeroReference.NONE),))
        rendered = value.render_ir.files[0].symbols[0]
        extra_id = dataclasses.replace(rendered.symbol_id, name="other")
        invalid = DiffInput(
            value.analyzed,
            dataclasses.replace(
                value.render_ir,
                files=(
                    dataclasses.replace(
                        value.render_ir.files[0],
                        symbols=(dataclasses.replace(rendered, symbol_id=extra_id),),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "ownership"):
            compare_projects(value, invalid)

    def test_stored_map_duplicate_ids_and_spans_are_owned(self) -> None:
        value = _input(
            (
                ("src/a.py", "a", 1, ZeroReference.NONE),
                ("src/b.py", "b", 2, ZeroReference.NONE),
            )
        )
        left, right = (item.symbol for item in value.analyzed.symbols)
        invalid_match = DuplicateMatch(
            left.id,
            right.id,
            SourceSpan("src/a.py", 99, 0, 99, 1),
            right.span,
            DuplicateScore(1, 1, 1, 1, 1, True),
        )
        invalid = DiffInput(
            dataclasses.replace(
                value.analyzed,
                map_duplicates=(invalid_match,),
            ),
            value.render_ir,
        )
        with self.assertRaisesRegex(ValueError, "map duplicate.*span ownership"):
            compare_projects(value, invalid)

    def test_stored_duplicate_peers_and_pairs_reject_self_and_repetition(self) -> None:
        value = _input(
            (
                ("src/a.py", "a", 1, ZeroReference.NONE),
                ("src/b.py", "b", 2, ZeroReference.NONE),
            )
        )
        first, second = value.analyzed.symbols
        bad_peer_sets = (
            (first.symbol.id,),
            (second.symbol.id, second.symbol.id),
        )
        for peers in bad_peer_sets:
            with self.subTest(peers=peers):
                invalid = DiffInput(
                    dataclasses.replace(
                        value.analyzed,
                        symbols=(
                            dataclasses.replace(first, duplicate_peers=peers),
                            second,
                        ),
                    ),
                    value.render_ir,
                )
                with self.assertRaisesRegex(
                    ValueError, "duplicate peer.*(self|repeated)"
                ):
                    compare_projects(value, invalid)

        score = DuplicateScore(1, 1, 1, 1, 1, True)
        match = DuplicateMatch(
            first.symbol.id,
            second.symbol.id,
            first.symbol.span,
            second.symbol.span,
            score,
        )
        reversed_match = DuplicateMatch(
            second.symbol.id,
            first.symbol.id,
            second.symbol.span,
            first.symbol.span,
            score,
        )
        self_match = DuplicateMatch(
            first.symbol.id,
            first.symbol.id,
            first.symbol.span,
            first.symbol.span,
            score,
        )
        for matches in ((match, reversed_match), (self_match,)):
            with self.subTest(matches=matches):
                invalid = DiffInput(
                    dataclasses.replace(value.analyzed, map_duplicates=matches),
                    value.render_ir,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "map duplicate.*(self|repeated)",
                ):
                    compare_projects(value, invalid)


class RevisionAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def _repo(self, name: str = "repo") -> Path:
        root = self.base / name
        root.mkdir()
        _git(root, "init", "-q")
        return root

    def test_reads_selected_committed_subtree_via_object_database(self) -> None:
        root = self._repo()
        package = root / "pkg"
        package.mkdir()
        (package / "old.py").write_text(
            'REVISION = "$Format:%H$"\n\ndef old():\n    return 1\n',
            encoding="utf-8",
        )
        (root / "outside.py").write_text(
            "def outside():\n    return 2\n",
            encoding="utf-8",
        )
        (root / ".gitattributes").write_text(
            "pkg/old.py export-ignore export-subst\n",
            encoding="utf-8",
        )
        _commit(root, "initial")
        real_run = subprocess.run
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def make_temp(*args: Any, **kwargs: Any) -> str:
            path = Path(real_mkdtemp(*args, **kwargs))
            created.append(path)
            return str(path)

        with (
            mock.patch.object(
                diff_module.subprocess,
                "run",
                wraps=real_run,
            ) as run,
            mock.patch.object(
                diff_module.tempfile,
                "mkdtemp",
                side_effect=make_temp,
            ),
        ):
            result = analyze_revision(package, _config(), "HEAD")

        self.assertEqual(
            tuple(file.source.file for file in result.analyzed.project.files),
            ("old.py",),
        )
        self.assertIn(b"$Format:%H$", result.analyzed.project.files[0].source.raw)
        commands = [tuple(call.args[0]) for call in run.call_args_list]
        self.assertTrue(any("rev-parse" in command for command in commands))
        tree_commands = [command for command in commands if "ls-tree" in command]
        self.assertEqual(len(tree_commands), 1)
        self.assertIn("-rz", tree_commands[0])
        self.assertNotIn("--full-tree", tree_commands[0])
        self.assertEqual(tree_commands[0][-2:], ("--", "."))
        self.assertEqual(
            sum("cat-file" in command and "--batch" in command for command in commands),
            1,
        )
        self.assertFalse(
            any("archive" in command or "worktree" in command for command in commands)
        )
        self.assertTrue(created)
        self.assertTrue(all(not path.exists() for path in created))

    def test_invalid_revision_and_committed_symlink_raise_revision_error(self) -> None:
        root = self._repo("invalid")
        (root / "target.py").write_text(
            "def target():\n    return 1\n", encoding="utf-8"
        )
        (root / "linked.py").symlink_to("target.py")
        _commit(root, "symlink")

        with self.assertRaises(RevisionError):
            analyze_revision(root, _config(), "missing-revision")
        with self.assertRaisesRegex(RevisionError, "unsupported tree entry"):
            analyze_revision(root, _config(), "HEAD")

    def test_malformed_tree_and_blob_protocol_are_rejected(self) -> None:
        config = _config()
        root = self.base / "mock-root"
        root.mkdir()
        oid = b"a" * 40
        valid_rev = subprocess.CompletedProcess([], 0, oid + b"\n", b"")
        malformed_cases = (
            b"100644 blob " + oid + b"\t../escape.py\0",
            b"120000 blob " + oid + b"\tlink.py\0",
            b"100644 blob " + oid + b" no-tab.py\0",
        )
        for tree_output in malformed_cases:
            with self.subTest(tree=tree_output):
                tree = subprocess.CompletedProcess([], 0, tree_output, b"")
                with (
                    mock.patch.object(
                        diff_module.subprocess,
                        "run",
                        side_effect=(valid_rev, tree),
                    ),
                    self.assertRaises(RevisionError),
                ):
                    analyze_revision(root, config, "HEAD")

        tree = subprocess.CompletedProcess(
            [],
            0,
            b"100644 blob " + oid + b"\ta.py\0",
            b"",
        )
        truncated = subprocess.CompletedProcess(
            [],
            0,
            oid + b" blob 10\nshort\n",
            b"",
        )
        with (
            mock.patch.object(
                diff_module.subprocess,
                "run",
                side_effect=(valid_rev, tree, truncated),
            ),
            self.assertRaisesRegex(RevisionError, "batch"),
        ):
            analyze_revision(root, config, "HEAD")

    def test_tree_parser_preserves_tab_filenames_and_rejects_case_collisions(
        self,
    ) -> None:
        oid_a = b"a" * 40
        oid_b = b"b" * 40
        tabbed = b"100644 blob " + oid_a + b"\ttab\tname.py\0"
        self.assertEqual(diff_module._parse_tree(tabbed)[0].path, "tab\tname.py")

        collisions = (
            b"100644 blob "
            + oid_a
            + b"\tA.py\0"
            + b"100644 blob "
            + oid_b
            + b"\ta.py\0",
            b"100644 blob "
            + oid_a
            + b"\tDIR/a.py\0"
            + b"100644 blob "
            + oid_b
            + b"\tdir/b.py\0",
        )
        for tree in collisions:
            with (
                self.subTest(tree=tree),
                self.assertRaisesRegex(
                    RevisionError,
                    "case-insensitive.*collision",
                ),
            ):
                diff_module._parse_tree(tree)

    def test_tree_parser_rejects_non_utf8_paths(self) -> None:
        oid = b"a" * 40
        tree = b"100644 blob " + oid + b"\tbad-\xff.py\0"
        with self.assertRaisesRegex(RevisionError, "UTF-8"):
            diff_module._parse_tree(tree)


class DiffCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def _root(self, name: str) -> tuple[Path, Path, ProjectConfig]:
        root = self.base / name
        root.mkdir()
        config = _config()
        config_path = root / CONFIG_NAME
        config_path.write_bytes(canonical_config_bytes(config))
        return root, config_path, config

    def test_current_artifact_precedes_revision_and_report_is_quietable(self) -> None:
        root, config_path, config = self._root("order")
        before = _input((("src/old.py", "old", 1, ZeroReference.NONE),))
        after = _input((("src/new.py", "new", 2, ZeroReference.NONE),))
        artifact = BuildArtifact(
            config,
            mock.sentinel.snapshot,  # type: ignore[arg-type]
            after.analyzed,
            after.render_ir,
            "rendered",
        )
        report = compare_projects(before, after)
        order: list[str] = []

        def current(*args: object, **kwargs: object) -> BuildArtifact:
            del args, kwargs
            order.append("current")
            return artifact

        def revision(*args: object, **kwargs: object) -> DiffInput:
            del args, kwargs
            order.append("revision")
            return before

        def compare(old: DiffInput, new: DiffInput) -> DiffReport:
            order.append("compare")
            self.assertIs(old, before)
            self.assertIs(new.analyzed, artifact.analyzed)
            self.assertIs(new.render_ir, artifact.render_ir)
            return report

        with (
            mock.patch.object(cli_module, "create_artifact", side_effect=current),
            mock.patch.object(cli_module, "analyze_revision", side_effect=revision),
            mock.patch.object(cli_module, "compare_projects", side_effect=compare),
            mock.patch.object(
                cli_module,
                "commit_writes",
                side_effect=AssertionError("diff wrote delivery files"),
            ),
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    command_diff(root, config_path, "HEAD", quiet=False),
                    EXIT_OK,
                )
            self.assertEqual(stdout.getvalue(), report.text)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    command_diff(root, config_path, "HEAD", quiet=True),
                    EXIT_OK,
                )
            self.assertEqual(stdout.getvalue(), "")

        self.assertEqual(
            order,
            ["current", "revision", "compare", "current", "revision", "compare"],
        )

    def test_incomplete_current_precedes_every_revision_operation(self) -> None:
        root, config_path, _ = self._root("incomplete-current")
        (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        with (
            mock.patch.object(
                cli_module,
                "analyze_revision",
                side_effect=AssertionError("revision ran before current completeness"),
            ) as revision,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                command_diff(root, config_path, "HEAD", quiet=True),
                EXIT_INCOMPLETE,
            )
        revision.assert_not_called()

    def test_real_diff_uses_no_delivery_writes_and_invalid_revision_is_three(
        self,
    ) -> None:
        root, config_path, _ = self._root("real")
        _git(root, "init", "-q")
        source = root / "service.py"
        source.write_text("def old_api():\n    return 1\n", encoding="utf-8")
        _commit(root, "old")
        source.write_text("def new_api():\n    return 1\n", encoding="utf-8")
        before = _tree_metadata(root)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(
                command_diff(root, config_path, "HEAD", quiet=False),
                EXIT_OK,
            )
        self.assertIn("+ service.py:1 new_api", stdout.getvalue())
        self.assertIn("- service.py:1 old_api", stdout.getvalue())
        self.assertEqual(_tree_metadata(root), before)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                command_diff(root, config_path, "missing-rev", quiet=True),
                EXIT_INCOMPLETE,
            )
        self.assertTrue(stderr.getvalue())
        self.assertEqual(_tree_metadata(root), before)


if __name__ == "__main__":
    unittest.main()
