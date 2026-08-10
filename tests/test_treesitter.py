from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from hologram.model import (
    DiagnosticSeverity,
    Language,
    SourceFile,
    SourceRole,
    SymbolKind,
)
from hologram.parsers.api import DEFAULT_REGISTRY, ParserRegistry, extract_file
from hologram.parsers.java import extract as extract_java

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"


def snapshot(relative: str) -> SourceFile:
    path = JAVAMINI / relative
    raw = path.read_bytes()
    return SourceFile(
        path,
        relative,
        Language.JAVA,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


@unittest.skipUnless(
    DEFAULT_REGISTRY.has_parser(Language.JAVA),
    "tree-sitter-java not installed",
)
class TreeSitterJavaTest(unittest.TestCase):
    def _file(self, relative: str):
        source = snapshot(relative)
        return extract_java(
            source,
            DEFAULT_REGISTRY.parser_for(Language.JAVA),
        )

    def test_types_methods_params_returns(self):
        result = self._file("src/engine/PricingEngine.java")
        pricing = next(
            symbol for symbol in result.symbols if symbol.kind is SymbolKind.CLASS
        )
        self.assertEqual(pricing.name, "PricingEngine")
        self.assertEqual(pricing.supers, ("PricePort",))
        evaluate = next(
            symbol for symbol in result.symbols if symbol.name == "evaluate"
        )
        self.assertEqual(evaluate.params, ("OrderId", "List<ItemId>"))
        self.assertEqual(evaluate.returns, "Quote")
        self.assertEqual(evaluate.raises, ("UnknownItemException",))
        self.assertEqual(evaluate.container, "PricingEngine")

    def test_record_components_and_supers(self):
        result = self._file("src/delta/AddOp.java")
        record = next(
            symbol for symbol in result.symbols if symbol.kind is SymbolKind.RECORD
        )
        self.assertEqual(record.params, ("String",))
        self.assertEqual(record.supers, ("DeltaOp",))

    def test_sealed_permits(self):
        result = self._file("src/delta/DeltaOp.java")
        interface = next(
            symbol for symbol in result.symbols if symbol.kind is SymbolKind.INTERFACE
        )
        self.assertEqual(interface.permits, ("AddOp", "RemoveOp"))

    def test_interface_bodyless_methods(self):
        result = self._file("src/engine/PricePort.java")
        methods = {
            symbol.name: symbol
            for symbol in result.symbols
            if symbol.kind is SymbolKind.METHOD
        }
        self.assertEqual(methods["quoteFor"].returns, "Quote")
        self.assertEqual(methods["supports"].returns, "boolean")

    def test_enum_constants(self):
        result = self._file("src/engine/OrderStatus.java")
        enum = next(
            symbol for symbol in result.symbols if symbol.kind is SymbolKind.ENUM
        )
        self.assertEqual(enum.params, ("NEW", "PAID", "SHIPPED"))
        self.assertIn("isTerminal", {symbol.name for symbol in result.symbols})

    def test_calls_with_receivers(self):
        result = self._file("src/App.java")
        main = next(symbol for symbol in result.symbols if symbol.name == "main")
        calls = {
            (f"{call.receiver}.{call.name}" if call.receiver is not None else call.name)
            for call in result.calls
            if call.caller == main.id
        }
        self.assertIn("engine.evaluate", calls)
        self.assertIn("OrderId.of", calls)
        self.assertIn("PricingEngine", calls)

    def test_ctor_extracted(self):
        result = self._file("src/engine/PricingEngine.java")
        constructor = next(
            symbol for symbol in result.symbols if symbol.kind is SymbolKind.CONSTRUCTOR
        )
        self.assertEqual(constructor.params, ("Map<ItemId,Long>",))


class MissingParserErrorTest(unittest.TestCase):
    def test_extract_file_errors_without_parser(self):
        source = snapshot("src/App.java")
        result = extract_file(
            source,
            registry=ParserRegistry(module_loader=lambda name: None),
        )
        self.assertEqual(result.diagnostics[0].code, "missing-parser")
        self.assertEqual(
            result.diagnostics[0].severity,
            DiagnosticSeverity.ERROR,
        )


if __name__ == "__main__":
    unittest.main()
