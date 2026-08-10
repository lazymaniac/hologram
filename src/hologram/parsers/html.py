from __future__ import annotations

from typing import Any

from hologram.model import SourceFile, Symbol, SymbolKind, Visibility

from ._treesitter_common import assemble_file_ir, syntax_diagnostics, walk_all
from .common import symbol_id
from .treesitter import ast_text, node_span


def _id_value(attribute: object) -> Any | None:
    name = None
    value = None
    for node in walk_all(attribute):
        if node is attribute:
            continue
        if node.type == "attribute_name" and name is None:
            name = node
        elif node.type == "attribute_value" and value is None:
            value = node
    if ast_text(name) != "id":
        return None
    return value


def _symbols(source: SourceFile, root: object) -> tuple[Symbol, ...]:
    candidates: list[tuple[int, str, Any]] = []
    for node in walk_all(root):
        if node.type == "tag_name":
            name = ast_text(node)
            if "-" in name:
                candidates.append((node.start_byte, name, node))
        elif node.type == "attribute":
            value = _id_value(node)
            if value is not None and (name := ast_text(value)):
                candidates.append((value.start_byte, f"#{name}", value))

    symbols: list[Symbol] = []
    seen: set[str] = set()
    for _, name, node in sorted(candidates, key=lambda item: item[0]):
        if name in seen:
            continue
        seen.add(name)
        symbols.append(
            Symbol(
                symbol_id(source, (), SymbolKind.FUNCTION, name),
                node_span(source, node),
                Visibility.INTERNAL,
                name,
            )
        )
    return tuple(symbols)


def extract(source: SourceFile, parser: object | None):
    if parser is None or not callable(getattr(parser, "parse", None)):
        raise TypeError("HTML extraction requires a Tree-sitter parser")
    tree = parser.parse(source.raw)  # type: ignore[attr-defined]
    root = tree.root_node
    return assemble_file_ir(
        source,
        module=None,
        symbols=_symbols(source, root),
        diagnostics=syntax_diagnostics(source, root, "HTML"),
    )


__all__ = ["extract"]
