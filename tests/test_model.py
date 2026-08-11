import dataclasses
import hashlib
import unittest
from pathlib import Path

import hologram
from hologram import model as canonical_model
from hologram.model import (
    Binding,
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    Diagnostic,
    DiagnosticSeverity,
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


class ModelTests(unittest.TestCase):
    def test_root_exports_the_canonical_symbol(self) -> None:
        self.assertIs(canonical_model.Symbol, hologram.Symbol)

    def test_source_span_rejects_non_normalized_file_paths(self) -> None:
        for file in (".", "./a.py", "a//b.py", "a/./b.py"):
            with self.subTest(file=file), self.assertRaises(ValueError):
                SourceSpan(file, 1, 0, 1, 0)

    def test_source_span_documents_coordinate_semantics(self) -> None:
        self.assertEqual(
            "One-based lines, zero-based UTF-8 byte columns, and end-exclusive "
            "endpoints.",
            SourceSpan.__doc__,
        )

    def test_symbol_id_is_line_independent(self) -> None:
        symbol_id = SymbolId(
            Language.JAVA,
            "src/shop/Price.java",
            ("shop", "Price"),
            SymbolKind.METHOD,
            "quote",
            "(OrderId)",
        )
        symbol = Symbol(
            symbol_id,
            SourceSpan("src/shop/Price.java", 8, 4, 10, 5),
            Visibility.PUBLIC,
            "quote(OrderId)",
        )

        moved_symbol = dataclasses.replace(
            symbol,
            span=SourceSpan("src/shop/Price.java", 108, 4, 110, 5),
        )

        self.assertEqual(symbol.id, moved_symbol.id)

    def test_symbol_id_owns_immutable_container_path(self) -> None:
        container_path = ["shop"]
        symbol_id = SymbolId(
            Language.JAVA,
            "src/shop/Price.java",
            container_path,
            SymbolKind.METHOD,
            "quote",
            "(OrderId)",
        )

        container_path.append("Price")

        self.assertEqual(("shop",), symbol_id.container_path)
        self.assertIsInstance(hash(symbol_id), int)

    def test_source_snapshot_owns_immutable_bytes(self) -> None:
        raw = bytearray(b"price = 10\n")
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )

        raw[:] = b"price = 20\n"

        self.assertEqual(b"price = 10\n", source.raw)
        self.assertEqual("price = 10\n", source.text)
        self.assertIsInstance(hash(source), int)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            source.raw = b"price = 20\n"

    def test_source_snapshot_owns_memoryview_bytes(self) -> None:
        backing = bytearray(b"price = 10\n")
        raw = memoryview(backing)
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )

        backing[:] = b"price = 20\n"

        self.assertEqual(b"price = 10\n", source.raw)
        self.assertIsInstance(hash(source), int)

    def test_source_snapshot_rejects_ambiguous_raw_inputs(self) -> None:
        cases = (
            ("integer", 3, b"\0\0\0"),
            ("string", "raw", b"raw"),
            ("list", [65, 66], b"AB"),
        )

        for kind, raw, coerced in cases:
            with self.subTest(kind=kind), self.assertRaisesRegex(
                TypeError,
                "^raw must be bytes, bytearray, or memoryview$",
            ):
                SourceFile(
                    Path("/repo/f.py"),
                    "f.py",
                    Language.PYTHON,
                    SourceRole.PRODUCTION,
                    raw,
                    hashlib.sha256(coerced).hexdigest(),
                )

    def test_source_snapshot_text_strictly_decodes_utf8(self) -> None:
        raw = b"\xff"
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )

        with self.assertRaises(UnicodeDecodeError):
            _ = source.text

    def test_source_snapshot_rejects_invalid_sha256_format(self) -> None:
        raw = b"price = 10\n"

        with self.assertRaisesRegex(
            ValueError,
            "^sha256 must be exactly 64 lowercase hexadecimal digits$",
        ):
            SourceFile(
                Path("/repo/f.py"),
                "f.py",
                Language.PYTHON,
                SourceRole.PRODUCTION,
                raw,
                "A" * 64,
            )

    def test_source_snapshot_rejects_mismatched_sha256(self) -> None:
        raw = b"price = 10\n"

        with self.assertRaisesRegex(
            ValueError,
            "^sha256 must match raw source bytes$",
        ):
            SourceFile(
                Path("/repo/f.py"),
                "f.py",
                Language.PYTHON,
                SourceRole.PRODUCTION,
                raw,
                "0" * 64,
            )

    def test_body_span_retains_source_for_later_analysis(self) -> None:
        owner = SymbolId(
            Language.PYTHON,
            "f.py",
            (),
            SymbolKind.FUNCTION,
            "price",
            "()",
        )
        body = BodyIR(
            owner,
            SourceSpan("f.py", 1, 0, 2, 0),
            (
                BodyEvent(
                    BodyEventKind.LITERAL,
                    "<number>",
                    SourceSpan("f.py", 1, 8, 1, 10),
                ),
            ),
        )
        raw = b"price = 10\n"
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        file_ir = FileIR(source, module="f", bodies=(body,))
        project = ProjectIR(Path("/repo"), (file_ir,), (), True)

        self.assertIs(raw, project.files[0].source.raw)
        self.assertEqual(body.span, project.files[0].bodies[0].span)

    def test_body_ir_owns_immutable_events(self) -> None:
        owner = SymbolId(
            Language.PYTHON,
            "f.py",
            (),
            SymbolKind.FUNCTION,
            "price",
            "()",
        )
        span = SourceSpan("f.py", 1, 0, 1, 10)
        event = BodyEvent(BodyEventKind.LITERAL, "<number>", span)
        events = [event]
        body = BodyIR(owner, span, events)

        events.clear()

        self.assertEqual((event,), body.events)
        self.assertIsInstance(hash(body), int)

    def test_raw_call_is_not_capped_or_resolved_in_ir(self) -> None:
        caller = SymbolId(
            Language.PYTHON,
            "f.py",
            (),
            SymbolKind.FUNCTION,
            "run",
            "()",
        )
        calls = tuple(
            CallRef(
                caller,
                SourceSpan("f.py", line, 0, line, 7),
                f"call_{line}",
                None,
                CallKind.CALL,
                0,
            )
            for line in range(1, 15)
        )
        raw = b""
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        file_ir = FileIR(source, calls=calls)

        self.assertEqual(calls, file_ir.calls)
        self.assertEqual(14, len(file_ir.calls))
        self.assertEqual("call_14", file_ir.calls[-1].name)

    def test_symbol_owns_immutable_tuple_fields(self) -> None:
        symbol_id = SymbolId(
            Language.JAVA,
            "src/shop/Price.java",
            ("shop", "Price"),
            SymbolKind.METHOD,
            "quote",
            "(OrderId)",
        )
        mutable_fields = {
            "params": ["OrderId"],
            "supers": ["BasePrice"],
            "permits": ["RetailPrice"],
            "raises": ["PricingError"],
            "bindings": [Binding("order_id", "OrderId")],
            "components": ["amount"],
            "annotations": ["cached"],
            "modifiers": ["final"],
        }
        expected = {
            name: tuple(values) for name, values in mutable_fields.items()
        }
        symbol = Symbol(
            symbol_id,
            SourceSpan("src/shop/Price.java", 8, 4, 10, 5),
            Visibility.PUBLIC,
            "quote(OrderId)",
            **mutable_fields,
        )

        for values in mutable_fields.values():
            values.clear()

        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertEqual(value, getattr(symbol, name))
        self.assertIsInstance(hash(symbol), int)

    def test_file_ir_owns_immutable_tuple_fields(self) -> None:
        raw = b"pass\n"
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        owner = SymbolId(
            Language.PYTHON,
            "f.py",
            (),
            SymbolKind.FUNCTION,
            "run",
            "()",
        )
        span = SourceSpan("f.py", 1, 0, 1, 4)
        symbol = Symbol(owner, span, Visibility.PUBLIC, "run()")
        call = CallRef(owner, span, "work", None, CallKind.CALL, 0)
        imported = ImportRef(span, "work", None, None)
        reference = ReferenceRef(
            owner,
            span,
            "work",
            None,
            ReferenceKind.NAME,
            ReferenceContext.CODE,
            ReferenceConfidence.DEFINITE,
        )
        body = BodyIR(owner, span, ())
        diagnostic = Diagnostic(
            "parse-warning",
            DiagnosticSeverity.WARNING,
            "partial parse",
            span,
        )
        mutable_fields = {
            "symbols": [symbol],
            "calls": [call],
            "imports": [imported],
            "references": [reference],
            "bodies": [body],
            "diagnostics": [diagnostic],
        }
        expected = {
            name: tuple(values) for name, values in mutable_fields.items()
        }
        file_ir = FileIR(source, module="f", **mutable_fields)

        for values in mutable_fields.values():
            values.clear()

        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertEqual(value, getattr(file_ir, name))
        self.assertIsInstance(hash(file_ir), int)

    def test_project_ir_owns_immutable_tuple_fields(self) -> None:
        raw = b"pass\n"
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        file_ir = FileIR(source)
        diagnostic = Diagnostic(
            "project-warning",
            DiagnosticSeverity.WARNING,
            "partial project",
        )
        files = [file_ir]
        diagnostics = [diagnostic]
        project = ProjectIR(Path("/repo"), files, diagnostics, False)

        files.clear()
        diagnostics.clear()

        self.assertEqual((file_ir,), project.files)
        self.assertEqual((diagnostic,), project.diagnostics)
        self.assertIsInstance(hash(project), int)

    def test_tuple_fields_reject_ambiguous_values(self) -> None:
        raw = b"pass\n"
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
        symbol_id = SymbolId(
            Language.PYTHON,
            "f.py",
            (),
            SymbolKind.FUNCTION,
            "run",
            "()",
        )
        span = SourceSpan("f.py", 1, 0, 1, 4)
        body = BodyIR(symbol_id, span, ())
        symbol = Symbol(symbol_id, span, Visibility.PUBLIC, "run()")
        file_ir = FileIR(source)
        project = ProjectIR(Path("/repo"), (), (), True)
        tuple_fields = (
            (symbol_id, "container_path"),
            (body, "events"),
            (symbol, "params"),
            (symbol, "supers"),
            (symbol, "permits"),
            (symbol, "raises"),
            (symbol, "bindings"),
            (symbol, "components"),
            (symbol, "annotations"),
            (symbol, "modifiers"),
            (file_ir, "symbols"),
            (file_ir, "calls"),
            (file_ir, "imports"),
            (file_ir, "references"),
            (file_ir, "bodies"),
            (file_ir, "diagnostics"),
            (project, "files"),
            (project, "diagnostics"),
        )
        invalid_factories = (
            ("string", lambda: "scope"),
            ("bytes", lambda: b"scope"),
            ("set", lambda: {"scope"}),
            ("mapping", lambda: {"scope": "value"}),
            ("generator", lambda: (value for value in ("scope",))),
            ("integer", lambda: 1),
        )
        violations = []

        for record, field in tuple_fields:
            for kind, factory in invalid_factories:
                try:
                    dataclasses.replace(record, **{field: factory()})
                except TypeError as error:
                    expected = f"{field} must be a tuple or list"
                    if str(error) != expected:
                        violations.append(
                            f"{type(record).__name__}.{field}/{kind}: {error}"
                        )
                else:
                    violations.append(
                        f"{type(record).__name__}.{field}/{kind}: accepted"
                    )

        self.assertEqual([], violations)

    def test_dynamic_reference_keeps_context_and_confidence(self) -> None:
        owner = SymbolId(
            Language.JAVA,
            "src/App.java",
            ("App",),
            SymbolKind.METHOD,
            "register",
            "()",
        )
        reference = ReferenceRef(
            owner,
            SourceSpan("src/App.java", 4, 1, 4, 10),
            "onRefresh",
            None,
            ReferenceKind.NAME,
            ReferenceContext.ANNOTATION,
            ReferenceConfidence.POSSIBLE,
        )

        self.assertEqual(ReferenceContext.ANNOTATION, reference.context)
        self.assertEqual(ReferenceConfidence.POSSIBLE, reference.confidence)


if __name__ == "__main__":
    unittest.main()
