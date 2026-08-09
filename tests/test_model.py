import dataclasses
import hashlib
import unittest
from pathlib import Path

from hologram.model import (
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    FileIR,
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

    def test_source_snapshot_owns_immutable_bytes(self) -> None:
        raw = "price = 10\n".encode()
        source = SourceFile(
            Path("/repo/f.py"),
            "f.py",
            Language.PYTHON,
            SourceRole.PRODUCTION,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )

        self.assertEqual("price = 10\n", source.text)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            source.raw = b"price = 20\n"

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

        self.assertEqual(14, len(calls))
        self.assertEqual("call_14", calls[-1].name)

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
