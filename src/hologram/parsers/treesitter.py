from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from hologram.model import BodyEvent, BodyEventKind, Language, SourceFile, SourceSpan

from .common import body_lines as _shared_body_lines
from .common import validate_body_events


@dataclass(frozen=True, slots=True)
class GrammarMetadata:
    module: str
    distribution: str
    attribute: str = "language"


GRAMMAR_METADATA: Mapping[Language, GrammarMetadata] = MappingProxyType(
    {
        Language.JAVA: GrammarMetadata("tree_sitter_java", "tree-sitter-java"),
        Language.TYPESCRIPT: GrammarMetadata(
            "tree_sitter_typescript",
            "tree-sitter-typescript",
            "language_typescript",
        ),
        Language.JAVASCRIPT: GrammarMetadata(
            "tree_sitter_typescript",
            "tree-sitter-typescript",
            "language_typescript",
        ),
        Language.TSX: GrammarMetadata(
            "tree_sitter_typescript",
            "tree-sitter-typescript",
            "language_tsx",
        ),
        Language.VUE: GrammarMetadata(
            "tree_sitter_typescript",
            "tree-sitter-typescript",
            "language_typescript",
        ),
        Language.SVELTE: GrammarMetadata(
            "tree_sitter_typescript",
            "tree-sitter-typescript",
            "language_typescript",
        ),
        Language.KOTLIN: GrammarMetadata(
            "tree_sitter_kotlin",
            "tree-sitter-kotlin",
        ),
        Language.GO: GrammarMetadata("tree_sitter_go", "tree-sitter-go"),
        Language.RUST: GrammarMetadata("tree_sitter_rust", "tree-sitter-rust"),
        Language.CSHARP: GrammarMetadata(
            "tree_sitter_c_sharp",
            "tree-sitter-c-sharp",
        ),
        Language.C: GrammarMetadata("tree_sitter_c", "tree-sitter-c"),
        Language.CPP: GrammarMetadata("tree_sitter_cpp", "tree-sitter-cpp"),
        Language.LUA: GrammarMetadata("tree_sitter_lua", "tree-sitter-lua"),
        Language.HTML: GrammarMetadata("tree_sitter_html", "tree-sitter-html"),
    }
)


_GRAMMAR_MODULES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        language.value: (metadata.module, metadata.distribution)
        for language, metadata in GRAMMAR_METADATA.items()
    }
)


def _optional_module(
    loader: Callable[[str], object | None],
    name: str,
) -> object | None:
    try:
        return loader(name)
    except ImportError:
        return None


def load_parser(
    language: Language,
    module_loader: Callable[[str], object | None],
) -> object | None:
    """Construct one parser from installed modules without installing anything."""
    metadata = GRAMMAR_METADATA.get(language)
    if metadata is None:
        return None
    grammar_module = _optional_module(module_loader, metadata.module)
    if grammar_module is None:
        return None
    tree_sitter = _optional_module(module_loader, "tree_sitter")
    if tree_sitter is None:
        return None
    factory = getattr(grammar_module, metadata.attribute, None)
    language_type = getattr(tree_sitter, "Language", None)
    parser_type = getattr(tree_sitter, "Parser", None)
    if not callable(factory) or language_type is None or parser_type is None:
        return None
    try:
        grammar = factory()
        if not isinstance(grammar, language_type):
            grammar = language_type(grammar)
        try:
            return parser_type(grammar)
        except TypeError:
            parser = parser_type()
            parser.language = grammar
            return parser
    except Exception:  # noqa: BLE001 - optional parser failures mean unavailable
        return None


_load_parser = load_parser


def grammar_version(language: Language) -> str:
    metadata = GRAMMAR_METADATA.get(language)
    if metadata is None:
        return "missing"
    try:
        runtime = importlib.metadata.version("tree-sitter")
        grammar = importlib.metadata.version(metadata.distribution)
    except importlib.metadata.PackageNotFoundError:
        return "missing"
    return f"{runtime}/{grammar}"


def parser_versions() -> Mapping[str, str]:
    values = {
        language.value: grammar_version(language) for language in GRAMMAR_METADATA
    }
    return MappingProxyType(dict(sorted(values.items())))


def ast_text(node: object | None) -> str:
    if node is None:
        return ""
    raw = getattr(node, "text", b"")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


_ast_text = ast_text


def ast_field(node: object | None, name: str) -> object | None:
    if node is None:
        return None
    field = getattr(node, "child_by_field_name", None)
    return field(name) if callable(field) else None


_ast_field = ast_field


def ast_collect(root: object | None, kinds: Iterable[str]) -> list[object]:
    """Collect matching descendants in byte source order."""
    if root is None:
        return []
    selected = frozenset(kinds)
    stack = [root]
    found: list[object] = []
    while stack:
        node = stack.pop()
        if getattr(node, "type", None) in selected:
            found.append(node)
        stack.extend(getattr(node, "children", ()))
    found.sort(
        key=lambda node: (
            getattr(node, "start_byte", 0),
            getattr(node, "end_byte", 0),
        )
    )
    return found


_ast_collect = ast_collect


def _point(point: object) -> tuple[int, int]:
    if hasattr(point, "row") and hasattr(point, "column"):
        return int(point.row), int(point.column)
    return int(point[0]), int(point[1])  # type: ignore[index]


def node_span(source: SourceFile, node: object) -> SourceSpan:
    """Convert Tree-sitter zero-based byte points to canonical coordinates."""
    typed_node = cast(Any, node)
    start_row, start_column = _point(typed_node.start_point)
    end_row, end_column = _point(typed_node.end_point)
    return SourceSpan(
        source.file,
        start_row + 1,
        start_column,
        end_row + 1,
        end_column,
    )


def body_lines(body: object | None) -> int:
    return _shared_body_lines(body)


_body_lines = body_lines


_CALL_KINDS = frozenset(
    {
        "call",
        "call_expression",
        "function_call",
        "function_call_expression",
        "invocation_expression",
        "method_invocation",
    }
)
_CONSTRUCT_KINDS = frozenset(
    {
        "constructor_invocation",
        "new_expression",
        "object_creation_expression",
        "struct_expression",
        "struct_literal",
    }
)
_CALLABLE_KINDS = frozenset(
    {
        "anonymous_function",
        "arrow_function",
        "constructor_declaration",
        "function_declaration",
        "function_definition",
        "function_expression",
        "function_item",
        "function_literal",
        "lambda_expression",
        "method_declaration",
        "method_definition",
    }
)
_CONTROL_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "if_statement": "if",
        "if_expression": "if",
        "conditional_expression": "if",
        "for_statement": "loop",
        "for_expression": "loop",
        "for_in_statement": "loop",
        "enhanced_for_statement": "loop",
        "while_statement": "loop",
        "while_expression": "loop",
        "do_statement": "loop",
        "loop_expression": "loop",
        "try_statement": "try",
        "try_expression": "try",
        "catch_clause": "catch",
        "except_clause": "catch",
        "finally_clause": "finally",
        "match_expression": "match",
        "match_statement": "match",
        "switch_expression": "match",
        "switch_statement": "match",
        "when_expression": "match",
        "with_statement": "with",
    }
)
_TYPE_KINDS = frozenset(
    {
        "array_type",
        "generic_type",
        "integral_type",
        "primitive_type",
        "scoped_type_identifier",
        "simple_type",
        "type_identifier",
        "user_type",
    }
)
_IDENTIFIER_KINDS = frozenset(
    {
        "field_identifier",
        "identifier",
        "property_identifier",
        "shorthand_property_identifier",
    }
)
_LOCAL_PARENT_KINDS = frozenset(
    {
        "const_declaration",
        "declaration",
        "init_declarator",
        "lexical_declaration",
        "local_declaration",
        "local_variable_declaration",
        "short_var_declaration",
        "variable_declaration",
        "variable_declarator",
    }
)
_PARAMETER_KINDS = frozenset(
    {
        "formal_parameter",
        "optional_parameter",
        "parameter",
        "parameter_declaration",
        "required_parameter",
        "rest_parameter",
        "spread_parameter",
        "variadic_parameter",
    }
)
_MEMBER_PARENT_KINDS = frozenset(
    {
        "attribute",
        "field_access",
        "member_access_expression",
        "member_expression",
        "navigation_expression",
        "selector_expression",
    }
)
_KEYWORDS = frozenset(
    {
        "await",
        "break",
        "case",
        "continue",
        "defer",
        "delete",
        "else",
        "finally",
        "goto",
        "if",
        "import",
        "match",
        "new",
        "raise",
        "return",
        "switch",
        "throw",
        "try",
        "when",
        "while",
        "yield",
    }
)
_OPERATORS = frozenset(
    {
        "+",
        "-",
        "*",
        "/",
        "%",
        "**",
        "=",
        ":=",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "&&",
        "||",
        "!",
        "&",
        "|",
        "^",
        "~",
        "<<",
        ">>",
        "?",
        "??",
        "?.",
        "=>",
        "in",
        "is",
        "as",
    }
)


def _literal_text(node: object) -> str | None:
    kind = str(getattr(node, "type", "")).casefold()
    if kind in {"true", "false", "boolean", "boolean_literal"}:
        return "<bool>"
    if kind in {"null", "nil", "none", "null_literal"}:
        return "<null>"
    if "string" in kind or kind in {"char_literal", "character_literal"}:
        return "<string>"
    if (
        "number" in kind
        or "integer" in kind
        or "float" in kind
        or kind in {"decimal_literal", "real_literal"}
    ):
        return "<number>"
    return None


def _same_node(left: object | None, right: object) -> bool:
    if left is None:
        return False
    return (
        getattr(left, "start_byte", None) == getattr(right, "start_byte", None)
        and getattr(left, "end_byte", None) == getattr(right, "end_byte", None)
        and getattr(left, "type", None) == getattr(right, "type", None)
    )


def _is_named_field(parent: object | None, node: object, names: Iterable[str]) -> bool:
    return any(_same_node(ast_field(parent, name), node) for name in names)


def _call_text(node: object, *, construct: bool) -> str:
    field_names = ("type", "constructor") if construct else ("name", "function")
    for field_name in field_names:
        field = ast_field(node, field_name)
        if field is not None:
            return ast_text(field)
    return ast_text(node)


class _TreeSitterBodyEventWalker:
    def __init__(self, source: SourceFile, callable_node: object) -> None:
        self.source = source
        self.callable_node = callable_node
        self.events: list[BodyEvent] = []

    def event(self, kind: BodyEventKind, text: str, node: object) -> None:
        self.events.append(BodyEvent(kind, text, node_span(self.source, node)))

    def walk(self) -> tuple[BodyEvent, ...]:
        parameters = ast_field(self.callable_node, "parameters")
        if parameters is not None:
            self.visit(parameters, None, in_parameters=True)
        body = ast_field(self.callable_node, "body")
        if body is None:
            body = self.callable_node
        self.visit(body, self.callable_node, in_parameters=False, is_root=True)
        result = tuple(self.events)
        validate_body_events(result)
        return result

    def visit(
        self,
        node: object,
        parent: object | None,
        *,
        in_parameters: bool,
        is_root: bool = False,
    ) -> None:
        kind = str(getattr(node, "type", ""))
        if not is_root and kind in _CALLABLE_KINDS:
            return

        control = _CONTROL_KINDS.get(kind)
        if control is not None:
            self.event(BodyEventKind.CONTROL_ENTER, control, node)

        if kind in _CONSTRUCT_KINDS:
            self.event(
                BodyEventKind.CONSTRUCT,
                _call_text(node, construct=True),
                node,
            )
        elif kind in _CALL_KINDS:
            self.event(BodyEventKind.CALL, _call_text(node, construct=False), node)

        literal = _literal_text(node)
        if literal is not None:
            self.event(BodyEventKind.LITERAL, literal, node)
            if control is not None:
                self.event(BodyEventKind.CONTROL_EXIT, control, node)
            return

        if kind in _TYPE_KINDS:
            self.event(BodyEventKind.TYPE, ast_text(node), node)
            if kind != "type_identifier":
                if control is not None:
                    self.event(BodyEventKind.CONTROL_EXIT, control, node)
                return
        elif kind in _IDENTIFIER_KINDS:
            parent_kind = str(getattr(parent, "type", ""))
            if in_parameters and (
                parent_kind in _PARAMETER_KINDS
                and _is_named_field(parent, node, ("name", "pattern"))
            ):
                event_kind = BodyEventKind.PARAM
            elif parent_kind in _LOCAL_PARENT_KINDS and _is_named_field(
                parent,
                node,
                ("declarator", "left", "name", "pattern"),
            ):
                event_kind = BodyEventKind.LOCAL
            elif parent_kind in _MEMBER_PARENT_KINDS and _is_named_field(
                parent,
                node,
                ("field", "member", "name", "property"),
            ):
                event_kind = BodyEventKind.MEMBER
            elif _is_named_field(parent, node, ("type",)):
                event_kind = BodyEventKind.TYPE
            else:
                event_kind = BodyEventKind.NAME
            self.event(event_kind, ast_text(node), node)
        elif not bool(getattr(node, "is_named", True)):
            text = ast_text(node)
            if text in _KEYWORDS:
                self.event(BodyEventKind.KEYWORD, text, node)
            elif text in _OPERATORS or re.fullmatch(r"[+*/%<>=!&|^~?-]+", text):
                self.event(BodyEventKind.OPERATOR, text, node)

        for child in getattr(node, "children", ()):
            self.visit(child, node, in_parameters=in_parameters)

        if control is not None:
            self.event(BodyEventKind.CONTROL_EXIT, control, node)


def body_events(source: SourceFile, callable_node: object) -> tuple[BodyEvent, ...]:
    """Emit facts from one Tree-sitter callable without entering nested callables."""
    return _TreeSitterBodyEventWalker(source, callable_node).walk()


tree_sitter_body_events = body_events


__all__ = [
    "GRAMMAR_METADATA",
    "GrammarMetadata",
    "ast_collect",
    "ast_field",
    "ast_text",
    "body_events",
    "body_lines",
    "grammar_version",
    "load_parser",
    "node_span",
    "parser_versions",
    "tree_sitter_body_events",
]
