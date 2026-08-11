import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import hologram
import hologram.state as state_module
from hologram import (
    Diagnostic,
    DiagnosticSeverity,
    Language,
    ScanEntry,
    ScanResult,
    ScanStatus,
    SourceFile,
    SourceRole,
    default_config,
    detect_language,
)
from hologram.state import (
    StateResult,
    compute_state,
    read_digest_state,
)

_UNSET = object()


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        self.config = default_config()

    def source(self, file: str, raw: bytes) -> SourceFile:
        return SourceFile(
            path=self.root / file,
            file=file,
            language=detect_language(Path(file)) or Language.PYTHON,
            role=SourceRole.PRODUCTION,
            raw=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def scan_with_source(self, file: str, raw: bytes) -> ScanResult:
        snapshot = self.source(file, raw)
        return ScanResult(
            entries=(
                ScanEntry(
                    snapshot.path,
                    snapshot.file,
                    snapshot.language,
                    ScanStatus.INDEXED,
                    None,
                    snapshot,
                ),
            ),
            diagnostics=(),
            complete=True,
        )

    def scan(self, *files: str) -> ScanResult:
        entries = tuple(
            ScanEntry(
                (snapshot := self.source(file, b"x = 1\n")).path,
                snapshot.file,
                snapshot.language,
                ScanStatus.INDEXED,
                None,
                snapshot,
            )
            for file in files
        )
        return ScanResult(entries, (), True)

    def failed_scan(self) -> ScanResult:
        diagnostic = Diagnostic(
            "scan-read-error",
            DiagnosticSeverity.ERROR,
            "bad.py: permission denied",
        )
        entry = ScanEntry(
            self.root / "bad.py",
            "bad.py",
            Language.PYTHON,
            ScanStatus.FAILED,
            "read-error",
            None,
        )
        return ScanResult((entry,), (diagnostic,), False)

    def compute(
        self,
        scan_result: ScanResult | None = None,
        *,
        raw: bytes = b"x = 1\n",
        output: str | None | object = _UNSET,
        extra_unsupported: tuple[str, bytes] | None = None,
        extra_excluded: tuple[str, bytes] | None = None,
    ) -> StateResult:
        config = self.config
        if output is not _UNSET:
            config = dataclasses.replace(config, output=cast(str | None, output))
        if scan_result is None:
            entries = list(self.scan_with_source("main.py", raw).entries)
            if extra_unsupported is not None:
                file, _ignored_raw = extra_unsupported
                entries.append(
                    ScanEntry(
                        self.root / file,
                        file,
                        None,
                        ScanStatus.EXCLUDED,
                        "unsupported-language",
                        None,
                    )
                )
            if extra_excluded is not None:
                file, _ignored_raw = extra_excluded
                entries.append(
                    ScanEntry(
                        self.root / file,
                        file,
                        detect_language(Path(file)),
                        ScanStatus.EXCLUDED,
                        "exclude-pattern",
                        None,
                    )
                )
            scan_result = ScanResult(tuple(entries), (), True)
        return compute_state(self.root, config, scan_result)

    def state(self, **overrides: object) -> str:
        return self.compute(**overrides).value

    def test_state_digest_and_framing_are_stable(self) -> None:
        self.assertEqual(
            self.state(),
            "f930c5f4a522e835268f1fe0e39d91e7779d2060bc202142d783482d773f0db9",
        )

    def test_state_api_is_exported_from_package(self) -> None:
        self.assertIs(hologram.StateResult, StateResult)
        self.assertIs(hologram.compute_state, compute_state)
        self.assertIs(hologram.read_digest_state, read_digest_state)

    def test_state_uses_snapshot_after_disk_changes(self) -> None:
        scan = self.scan_with_source("svc.py", b"before\n")
        first = compute_state(self.root, self.config, scan)
        (self.root / "svc.py").write_bytes(b"after\n")
        second = compute_state(self.root, self.config, scan)
        self.assertEqual(first.value, second.value)

    def test_state_changes_for_every_semantic_input(self) -> None:
        baseline = self.state()
        self.assertNotEqual(baseline, self.state(raw=b"changed\n"))
        self.assertNotEqual(baseline, self.state(output="OTHER.md"))

    def test_entry_order_does_not_change_state(self) -> None:
        left = self.compute(scan_result=self.scan("a.py", "b.py"))
        right = self.compute(scan_result=self.scan("b.py", "a.py"))
        self.assertEqual(left.value, right.value)

    def test_incomplete_scan_produces_incomplete_state(self) -> None:
        scan = self.failed_scan()
        result = self.compute(scan_result=scan)
        self.assertFalse(result.complete)
        self.assertRegex(result.value, r"^[0-9a-f]{64}$")
        self.assertEqual(result.diagnostics, scan.diagnostics)
        self.assertEqual(len(result.diagnostics), 1)
        self.assertIs(result.diagnostics[0], scan.diagnostics[0])

    def test_state_result_owns_list_diagnostics(self) -> None:
        diagnostic = self.failed_scan().diagnostics[0]
        supplied = [diagnostic]
        result = StateResult("0" * 64, supplied, False)
        supplied.clear()
        self.assertEqual(result.diagnostics, (diagnostic,))

    def test_unsupported_files_do_not_change_state(self) -> None:
        baseline = self.state(extra_unsupported=("README.md", b"first\n"))
        changed = self.state(extra_unsupported=("README.md", b"second\n"))
        self.assertEqual(baseline, changed)

    def test_excluded_supported_source_does_not_change_state(self) -> None:
        baseline = self.state(extra_excluded=("generated/Model.java", b"first\n"))
        changed_bytes = self.state(extra_excluded=("generated/Model.java", b"second\n"))
        changed_path = self.state(extra_excluded=("generated/Renamed.java", b"first\n"))
        self.assertEqual(baseline, changed_bytes)
        self.assertEqual(baseline, changed_path)

    def test_moving_an_indexed_source_behind_exclusion_changes_state(self) -> None:
        indexed = self.compute(scan_result=self.scan("main.py", "generated/model.py"))
        excluded = self.compute(extra_excluded=("generated/model.py", b"x = 1\n"))
        self.assertNotEqual(indexed.value, excluded.value)

    def test_failed_language_entry_status_and_reason_change_state(self) -> None:
        snapshot = self.source("bad.py", b"x = 1\n")

        def result(
            file: str,
            status: ScanStatus,
            reason: str | None,
        ) -> ScanResult:
            source = dataclasses.replace(
                snapshot,
                path=self.root / file,
                file=file,
            )
            return ScanResult(
                (
                    ScanEntry(
                        source.path,
                        source.file,
                        Language.PYTHON,
                        status,
                        reason,
                        source,
                    ),
                ),
                (),
                True,
            )

        baseline = self.compute(
            scan_result=result("bad.py", ScanStatus.FAILED, "read-error")
        ).value
        self.assertNotEqual(
            baseline,
            self.compute(
                scan_result=result("renamed.py", ScanStatus.FAILED, "read-error")
            ).value,
        )
        self.assertNotEqual(
            baseline,
            self.compute(
                scan_result=result("bad.py", ScanStatus.INDEXED, "read-error")
            ).value,
        )
        self.assertNotEqual(
            baseline,
            self.compute(
                scan_result=result("bad.py", ScanStatus.FAILED, "invalid-utf8")
            ).value,
        )

    def test_scanner_fatal_git_sentinel_changes_state(self) -> None:
        empty = ScanResult((), (), False)
        fatal = ScanResult(
            (
                ScanEntry(
                    self.root / "<git>",
                    "<git>",
                    None,
                    ScanStatus.FAILED,
                    "git-error",
                    None,
                ),
            ),
            (),
            False,
        )
        changed_reason = dataclasses.replace(
            fatal,
            entries=(dataclasses.replace(fatal.entries[0], reason="git-timeout"),),
        )
        baseline = self.compute(scan_result=empty).value
        fatal_value = self.compute(scan_result=fatal).value
        self.assertNotEqual(baseline, fatal_value)
        self.assertNotEqual(
            fatal_value,
            self.compute(scan_result=changed_reason).value,
        )

    def test_every_language_none_failure_changes_state(self) -> None:
        empty = self.compute(scan_result=ScanResult((), (), True)).value

        def state_for(
            file: str,
            status: ScanStatus,
            reason: str,
        ) -> str:
            return self.compute(
                scan_result=ScanResult(
                    (
                        ScanEntry(
                            self.root / file,
                            file,
                            None,
                            status,
                            reason,
                            None,
                        ),
                    ),
                    (),
                    False,
                )
            ).value

        root_failure = state_for(
            "<filesystem>",
            ScanStatus.FAILED,
            "root-open-failed",
        )
        walk_failure = state_for(
            "blocked/private",
            ScanStatus.FAILED,
            "walk-error",
        )
        changed_path = state_for(
            "blocked/other",
            ScanStatus.FAILED,
            "walk-error",
        )
        changed_reason = state_for(
            "blocked/private",
            ScanStatus.FAILED,
            "directory-stat-error",
        )
        excluded = state_for(
            "blocked/private",
            ScanStatus.EXCLUDED,
            "walk-error",
        )

        self.assertNotEqual(empty, root_failure)
        self.assertNotEqual(empty, walk_failure)
        self.assertNotEqual(walk_failure, changed_path)
        self.assertNotEqual(walk_failure, changed_reason)
        self.assertNotEqual(walk_failure, excluded)
        self.assertEqual(empty, excluded)

    def test_entry_language_assignment_is_hashed_per_path(self) -> None:
        def entry(file: str, language: Language) -> ScanEntry:
            raw = b"same source\n"
            snapshot = SourceFile(
                self.root / file,
                file,
                language,
                SourceRole.PRODUCTION,
                raw,
                hashlib.sha256(raw).hexdigest(),
            )
            return ScanEntry(
                snapshot.path,
                snapshot.file,
                language,
                ScanStatus.INDEXED,
                None,
                snapshot,
            )

        left = ScanResult(
            (
                entry("first.code", Language.PYTHON),
                entry("second.code", Language.JAVA),
            ),
            (),
            True,
        )
        right = ScanResult(
            (
                entry("first.code", Language.JAVA),
                entry("second.code", Language.PYTHON),
            ),
            (),
            True,
        )
        first = self.compute(scan_result=left)
        second = self.compute(scan_result=right)
        self.assertNotEqual(first.value, second.value)

    def test_source_role_is_hashed(self) -> None:
        baseline = self.scan_with_source("main.py", b"x = 1\n")
        states = set()
        for role in SourceRole:
            source = dataclasses.replace(baseline.sources[0], role=role)
            scan_result = dataclasses.replace(
                baseline,
                entries=(
                    dataclasses.replace(
                        baseline.entries[0],
                        source=source,
                    ),
                ),
            )
            states.add(self.compute(scan_result=scan_result).value)
        self.assertEqual(len(states), len(SourceRole))

    def test_duplicate_entry_files_are_rejected_before_hashing(self) -> None:
        indexed = self.scan_with_source("duplicate.py", b"first\n").entries[0]
        excluded = ScanEntry(
            self.root / "duplicate.py",
            "duplicate.py",
            Language.PYTHON,
            ScanStatus.EXCLUDED,
            "exclude-pattern",
            None,
        )
        for entries in ((indexed, excluded), (excluded, indexed)):
            with (
                self.subTest(order=tuple(entry.status for entry in entries)),
                self.assertRaisesRegex(
                    ValueError,
                    r"duplicate ScanEntry\.file 'duplicate\.py'",
                ),
            ):
                self.compute(scan_result=ScanResult(entries, (), True))

    def test_indexed_helm_source_bytes_change_state(self) -> None:
        first = self.compute(
            scan_result=self.scan_with_source("chart/templates/app.yaml", b"one\n"),
        )
        second = self.compute(
            scan_result=self.scan_with_source("chart/templates/app.yaml", b"two\n"),
        )
        self.assertNotEqual(first.value, second.value)

    def test_root_path_does_not_change_portable_state(self) -> None:
        other_root = self.root.parent / "elsewhere"
        left = self.scan_with_source("svc.py", b"same\n")
        source = dataclasses.replace(
            left.sources[0],
            path=other_root / "svc.py",
        )
        right = ScanResult(
            (
                dataclasses.replace(
                    left.entries[0],
                    path=source.path,
                    source=source,
                ),
            ),
            (),
            True,
        )
        first = compute_state(self.root, self.config, left)
        second = compute_state(other_root, self.config, right)
        self.assertEqual(first.value, second.value)

    def test_compute_state_never_reads_or_resolves_paths(self) -> None:
        scan = self.scan_with_source("svc.py", b"snapshot\n")
        with (
            mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("must not read source paths"),
            ),
            mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("must not read source paths"),
            ),
            mock.patch.object(
                Path,
                "resolve",
                side_effect=AssertionError("must not resolve source paths"),
            ),
        ):
            result = self.compute(scan_result=scan)
        self.assertRegex(result.value, r"^[0-9a-f]{64}$")


class ReadDigestStateTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "PROJECT_DIGEST.md"
        self.digest = "a" * 64

    def test_accepts_exact_state_field_with_valid_boundaries(self) -> None:
        accepted = (
            f"state={self.digest}",
            f"# project state={self.digest} regen",
            f"# project · state={self.digest} · regen: hologram build\nbody\n",
            f"# project·state={self.digest}·regen",
        )
        for content in accepted:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(read_digest_state(self.path), self.digest)

    def test_rejects_noncanonical_or_embedded_state_fields(self) -> None:
        rejected = (
            "# no stamp",
            "state=" + "a" * 12,
            "state=" + "A" * 64,
            "state =" + self.digest,
            "state= " + self.digest,
            "state = " + self.digest,
            "mystate=" + self.digest,
            "state=" + self.digest + "x",
            "state=" + "a" * 63,
            "state=" + "a" * 65,
            "# header\nbody state=" + self.digest,
        )
        for content in rejected:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertIsNone(read_digest_state(self.path))

    def test_missing_artifact_returns_none(self) -> None:
        self.assertIsNone(read_digest_state(self.path))

    def test_nonmissing_read_errors_are_not_masked(self) -> None:
        for error in (PermissionError("denied"), OSError("I/O failure")):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(
                    Path,
                    "open",
                    side_effect=error,
                ),
                self.assertRaises(type(error)),
            ):
                read_digest_state(self.path)

    def test_invalid_utf8_is_treated_as_missing_state(self) -> None:
        self.path.write_bytes(b"\xff")
        self.assertIsNone(read_digest_state(self.path))

    def test_invalid_or_huge_body_is_not_read_or_decoded(self) -> None:
        header = f"# project · state={self.digest} · regen: x\n".encode()
        self.path.write_bytes(header + b"\xff" * (1024 * 1024))
        self.assertEqual(read_digest_state(self.path), self.digest)

    def test_header_read_is_binary_and_bounded(self) -> None:
        sizes: list[int] = []
        header = f"state={self.digest}\n".encode("ascii")

        class Artifact:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def readline(self, size: int) -> bytes:
                sizes.append(size)
                return header

            def read(self, *args):
                raise AssertionError("artifact body must not be read")

        with mock.patch.object(Path, "open", return_value=Artifact()) as opened:
            self.assertEqual(read_digest_state(self.path), self.digest)
        opened.assert_called_once_with("rb")
        self.assertEqual(
            sizes,
            [state_module._STATE_HEADER_MAX_BYTES + 1],
        )

    def test_overlong_header_is_rejected(self) -> None:
        cap = state_module._STATE_HEADER_MAX_BYTES
        self.path.write_bytes(
            f"state={self.digest} ".encode("ascii") + b"x" * cap + b"\n"
        )
        self.assertIsNone(read_digest_state(self.path))


if __name__ == "__main__":
    unittest.main()
