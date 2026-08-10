from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from hologram.model import BodyEvent, BodyEventKind, Language, SourceFile, SourceSpan

from .common import body_lines as _shared_body_lines
from .common import validate_body_events


@dataclass(frozen=True, slots=True)
class GrammarMetadata:
    module: str
    distribution: str
    attribute: str = "language"


@dataclass(frozen=True, slots=True)
class OwnershipContext:
    boundary_keys: frozenset[tuple[int, int, str]]
    include_anonymous: bool = False


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
    except ModuleNotFoundError as error:
        if error.name == name:
            return None
        raise


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
    grammar = factory()
    if not isinstance(grammar, language_type):
        grammar = language_type(grammar)
    try:
        return parser_type(grammar)
    except TypeError:
        parser = parser_type()
        parser.language = grammar
        return parser


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


def ast_field(node: object | None, name: str) -> Any | None:
    if node is None:
        return None
    field = getattr(node, "child_by_field_name", None)
    return field(name) if callable(field) else None


_ast_field = ast_field


def ast_collect(root: object | None, kinds: Iterable[str]) -> list[Any]:
    """Collect matching descendants in byte source order."""
    if root is None:
        return []
    selected = frozenset(kinds)
    stack = [root]
    found: list[Any] = []
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
        "anonymous_object_creation_expression",
        "array_creation_expression",
        "compound_literal_expression",
        "composite_literal",
        "constructor_delegation_call",
        "constructor_invocation",
        "explicit_constructor_invocation",
        "implicit_array_creation_expression",
        "implicit_object_creation_expression",
        "new_expression",
        "object_creation_expression",
        "struct_expression",
        "struct_literal",
        "table_constructor",
    }
)
_TYPESCRIPT_CALLABLE_KINDS = frozenset(
    {
        "arrow_function",
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
        "method_definition",
    }
)
_CALLABLE_KINDS_BY_LANGUAGE: Mapping[Language, frozenset[str]] = MappingProxyType(
    {
        Language.JAVA: frozenset(
            {
                "compact_constructor_declaration",
                "constructor_declaration",
                "lambda_expression",
                "method_declaration",
            }
        ),
        Language.PYTHON: frozenset(),
        Language.TYPESCRIPT: _TYPESCRIPT_CALLABLE_KINDS,
        Language.JAVASCRIPT: _TYPESCRIPT_CALLABLE_KINDS,
        Language.TSX: _TYPESCRIPT_CALLABLE_KINDS,
        Language.VUE: _TYPESCRIPT_CALLABLE_KINDS,
        Language.SVELTE: _TYPESCRIPT_CALLABLE_KINDS,
        Language.KOTLIN: frozenset(
            {
                "anonymous_function",
                "function_declaration",
                "getter",
                "lambda_literal",
                "secondary_constructor",
                "setter",
            }
        ),
        Language.GO: frozenset(
            {"func_literal", "function_declaration", "method_declaration"}
        ),
        Language.RUST: frozenset({"closure_expression", "function_item"}),
        Language.CSHARP: frozenset(
            {
                "accessor_declaration",
                "anonymous_method_expression",
                "constructor_declaration",
                "conversion_operator_declaration",
                "destructor_declaration",
                "indexer_declaration",
                "lambda_expression",
                "local_function_statement",
                "method_declaration",
                "operator_declaration",
            }
        ),
        Language.C: frozenset({"function_definition"}),
        Language.CPP: frozenset({"function_definition", "lambda_expression"}),
        Language.LUA: frozenset({"function_declaration", "function_definition"}),
        Language.HTML: frozenset(),
        Language.HELM: frozenset(),
    }
)
_ANONYMOUS_CALLABLE_KINDS_BY_LANGUAGE: Mapping[
    Language, frozenset[str]
] = MappingProxyType(
    {
        Language.KOTLIN: frozenset({"anonymous_function", "lambda_literal"}),
        Language.GO: frozenset({"func_literal"}),
        Language.RUST: frozenset({"async_block", "closure_expression"}),
        Language.CSHARP: frozenset(
            {"anonymous_method_expression", "lambda_expression"}
        ),
    }
)
_TYPESCRIPT_DECLARATION_BOUNDARIES = frozenset(
    {
        "abstract_class_declaration",
        "class_declaration",
        "enum_declaration",
        "interface_declaration",
        "type_alias_declaration",
    }
)
_OWNED_REGION_BOUNDARIES_BY_LANGUAGE: Mapping[Language, frozenset[str]] = (
    MappingProxyType(
        {
            Language.JAVA: frozenset(
                {
                    "annotation_type_declaration",
                    "class_declaration",
                    "enum_declaration",
                    "interface_declaration",
                    "record_declaration",
                }
            ),
            Language.PYTHON: frozenset(),
            Language.TYPESCRIPT: _TYPESCRIPT_DECLARATION_BOUNDARIES,
            Language.JAVASCRIPT: _TYPESCRIPT_DECLARATION_BOUNDARIES,
            Language.TSX: _TYPESCRIPT_DECLARATION_BOUNDARIES,
            Language.VUE: _TYPESCRIPT_DECLARATION_BOUNDARIES,
            Language.SVELTE: _TYPESCRIPT_DECLARATION_BOUNDARIES,
            Language.KOTLIN: frozenset(
                {"class_declaration", "object_declaration", "type_alias"}
            ),
            Language.GO: frozenset({"type_declaration"}),
            Language.RUST: frozenset(
                {
                    "async_block",
                    "const_item",
                    "enum_item",
                    "impl_item",
                    "mod_item",
                    "static_item",
                    "struct_item",
                    "trait_item",
                    "type_item",
                    "union_item",
                }
            ),
            Language.CSHARP: frozenset(
                {
                    "class_declaration",
                    "delegate_declaration",
                    "enum_declaration",
                    "interface_declaration",
                    "record_declaration",
                    "struct_declaration",
                }
            ),
            Language.C: frozenset(
                {"enum_specifier", "struct_specifier", "union_specifier"}
            ),
            Language.CPP: frozenset(
                {
                    "class_specifier",
                    "enum_specifier",
                    "struct_specifier",
                    "union_specifier",
                }
            ),
            Language.LUA: frozenset(),
            Language.HTML: frozenset(),
            Language.HELM: frozenset(),
        }
    )
)
_OWNED_BOUNDARIES_BY_LANGUAGE: Mapping[Language, frozenset[str]] = MappingProxyType(
    {
        language: _CALLABLE_KINDS_BY_LANGUAGE[language]
        | _OWNED_REGION_BOUNDARIES_BY_LANGUAGE[language]
        for language in Language
    }
)
_COMMON_CONTROL_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "catch_block": "catch",
        "catch_clause": "catch",
        "conditional_expression": "if",
        "enhanced_for_statement": "loop",
        "except_clause": "catch",
        "expression_switch_statement": "match",
        "finally_clause": "finally",
        "for_expression": "loop",
        "for_in_statement": "loop",
        "for_statement": "loop",
        "if_expression": "if",
        "if_statement": "if",
        "loop_expression": "loop",
        "match_expression": "match",
        "match_statement": "match",
        "select_statement": "match",
        "switch_expression": "match",
        "switch_statement": "match",
        "ternary_expression": "if",
        "try_expression": "try",
        "try_statement": "try",
        "type_switch_statement": "match",
        "when_expression": "match",
        "while_expression": "loop",
        "while_statement": "loop",
        "with_statement": "with",
    }
)


def _control_kinds(**overrides: str) -> Mapping[str, str]:
    return MappingProxyType({**_COMMON_CONTROL_KINDS, **overrides})


_TYPESCRIPT_CONTROL_KINDS = _control_kinds(do_statement="loop")
_CONTROL_KINDS_BY_LANGUAGE: Mapping[Language, Mapping[str, str]] = MappingProxyType(
    {
        Language.JAVA: _control_kinds(
            do_statement="loop",
            try_with_resources_statement="try",
        ),
        Language.PYTHON: _COMMON_CONTROL_KINDS,
        Language.TYPESCRIPT: _TYPESCRIPT_CONTROL_KINDS,
        Language.JAVASCRIPT: _TYPESCRIPT_CONTROL_KINDS,
        Language.TSX: _TYPESCRIPT_CONTROL_KINDS,
        Language.VUE: _TYPESCRIPT_CONTROL_KINDS,
        Language.SVELTE: _TYPESCRIPT_CONTROL_KINDS,
        Language.KOTLIN: _control_kinds(
            do_while_statement="loop",
            finally_block="finally",
        ),
        Language.GO: _COMMON_CONTROL_KINDS,
        Language.RUST: _COMMON_CONTROL_KINDS,
        Language.CSHARP: _control_kinds(
            do_statement="loop",
            foreach_statement="loop",
            lock_statement="with",
            using_statement="with",
        ),
        Language.C: _control_kinds(do_statement="loop"),
        Language.CPP: _control_kinds(
            do_statement="loop",
            for_range_loop="loop",
        ),
        Language.LUA: _control_kinds(repeat_statement="loop"),
        Language.HTML: _COMMON_CONTROL_KINDS,
        Language.HELM: _COMMON_CONTROL_KINDS,
    }
)
_TYPE_KINDS = frozenset(
    {
        "array_type",
        "catch_type",
        "function_type",
        "generic_type",
        "implicit_type",
        "integral_type",
        "nullable_type",
        "nested_type_identifier",
        "placeholder_type_specifier",
        "predefined_type",
        "primitive_type",
        "qualified_name",
        "scoped_type_identifier",
        "simple_type",
        "slice_type",
        "template_argument_list",
        "template_type",
        "type_annotation",
        "type_argument_list",
        "type_arguments",
        "type_descriptor",
        "type_identifier",
        "user_type",
        "void_type",
    }
)
_TYPE_NAME_LEAF_KINDS = frozenset({"type_identifier"})
# C++ and Rust grammars preserve syntactically ambiguous generic arguments as
# type-shaped nodes. Keep a NAME fact alongside TYPE for exact value-reference joins.
_GENERIC_ARGUMENT_KINDS_BY_LANGUAGE: Mapping[Language, frozenset[str]] = (
    MappingProxyType(
        {
            Language.CPP: frozenset({"template_argument_list"}),
            Language.RUST: frozenset({"type_arguments"}),
        }
    )
)
_TYPE_DELIMITER_CONTAINERS = frozenset(
    {"template_argument_list", "type_argument_list", "type_arguments"}
)
_IDENTIFIER_KINDS = frozenset(
    {
        "field_identifier",
        "identifier",
        "property_identifier",
        "shorthand_property_identifier",
    }
)
_BINDING_LEAF_KINDS = _IDENTIFIER_KINDS | frozenset(
    {
        "implicit_parameter",
        "self",
        "shorthand_field_identifier",
        "shorthand_property_identifier_pattern",
        "this",
    }
)
_NAME_KINDS = _BINDING_LEAF_KINDS
_PARAMETER_CONTAINER_KINDS = frozenset(
    {
        "class_parameters",
        "formal_parameters",
        "function_value_parameters",
        "inferred_parameters",
        "lambda_parameters",
        "parameter_list",
        "parameters",
        "closure_parameters",
    }
)
_DEFAULT_PARAMETER_FIELDS = ("parameters", "parameter", "value_parameters")
_PARAMETER_FIELDS_BY_LANGUAGE: Mapping[Language, tuple[str, ...]] = MappingProxyType(
    {Language.GO: ("receiver", "parameters", "result")}
)
_BODY_KINDS = frozenset(
    {
        "arrow_expression_clause",
        "block",
        "compound_statement",
        "constructor_body",
        "function_body",
        "statement_block",
    }
)
_ADDITIONAL_BODY_CHILD_KINDS: Mapping[Language, Mapping[str, frozenset[str]]] = (
    MappingProxyType(
        {
            Language.CPP: MappingProxyType(
                {"function_definition": frozenset({"field_initializer_list"})}
            ),
            Language.CSHARP: MappingProxyType(
                {"constructor_declaration": frozenset({"constructor_initializer"})}
            ),
            Language.KOTLIN: MappingProxyType(
                {"secondary_constructor": frozenset({"constructor_delegation_call"})}
            ),
        }
    )
)

_BindingSelector = Literal[
    "identifier",
    "declarator",
    "parameters",
    "pattern",
    "lua-assignment",
]


@dataclass(frozen=True, slots=True)
class _BindingRule:
    fields: tuple[str, ...] = ()
    child_kinds: tuple[str, ...] = ()
    selector: _BindingSelector = "identifier"
    required_tokens: tuple[str, ...] = ()


def _rules(values: Mapping[str, _BindingRule]) -> Mapping[str, _BindingRule]:
    return MappingProxyType(dict(values))


_TYPESCRIPT_PARAMETER_RULES = _rules(
    {
        "optional_parameter": _BindingRule(("pattern",), selector="pattern"),
        "required_parameter": _BindingRule(("pattern",), selector="pattern"),
    }
)
_C_PARAMETER_RULES = _rules(
    {"parameter_declaration": _BindingRule(("declarator",), selector="declarator")}
)
_CPP_PARAMETER_RULES = _rules(
    {
        **_C_PARAMETER_RULES,
        "optional_parameter_declaration": _BindingRule(
            ("declarator",), selector="declarator"
        ),
        "variadic_parameter_declaration": _BindingRule(
            ("declarator",), selector="declarator"
        ),
    }
)
_PARAMETER_BINDING_RULES: Mapping[Language, Mapping[str, _BindingRule]] = (
    MappingProxyType(
        {
            Language.JAVA: _rules(
                {
                    "formal_parameter": _BindingRule(("name",)),
                    "inferred_parameters": _BindingRule(child_kinds=("identifier",)),
                    "receiver_parameter": _BindingRule(child_kinds=("this",)),
                    "spread_parameter": _BindingRule(
                        child_kinds=("variable_declarator",),
                        selector="declarator",
                    ),
                    "variable_declarator": _BindingRule(("name",)),
                }
            ),
            Language.TYPESCRIPT: _TYPESCRIPT_PARAMETER_RULES,
            Language.JAVASCRIPT: _TYPESCRIPT_PARAMETER_RULES,
            Language.TSX: _TYPESCRIPT_PARAMETER_RULES,
            Language.VUE: _TYPESCRIPT_PARAMETER_RULES,
            Language.SVELTE: _TYPESCRIPT_PARAMETER_RULES,
            Language.KOTLIN: _rules(
                {
                    "class_parameter": _BindingRule(child_kinds=("identifier",)),
                    "parameter": _BindingRule(child_kinds=("identifier",)),
                    "variable_declaration": _BindingRule(child_kinds=("identifier",)),
                }
            ),
            Language.GO: _rules(
                {
                    "parameter_declaration": _BindingRule(("name",)),
                    "variadic_parameter_declaration": _BindingRule(("name",)),
                }
            ),
            Language.RUST: _rules(
                {
                    "closure_parameters": _BindingRule(selector="pattern"),
                    "parameter": _BindingRule(("pattern",), selector="pattern"),
                    "self_parameter": _BindingRule(child_kinds=("self",)),
                }
            ),
            Language.CSHARP: _rules(
                {
                    "parameter": _BindingRule(("name",)),
                    "parameter_list": _BindingRule(("name",)),
                }
            ),
            Language.C: _C_PARAMETER_RULES,
            Language.CPP: _CPP_PARAMETER_RULES,
            Language.LUA: _rules({"parameters": _BindingRule(("name",))}),
        }
    )
)

_TYPESCRIPT_LOCAL_RULES = _rules(
    {
        "catch_clause": _BindingRule(("parameter",), selector="pattern"),
        "for_in_statement": _BindingRule(
            ("left",),
            selector="pattern",
            required_tokens=("const", "let", "var"),
        ),
        "variable_declarator": _BindingRule(("name",), selector="pattern"),
    }
)
_C_LOCAL_RULES = _rules(
    {"declaration": _BindingRule(("declarator",), selector="declarator")}
)
_CPP_LOCAL_RULES = _rules(
    {
        **_C_LOCAL_RULES,
        "catch_clause": _BindingRule(("parameters",), selector="parameters"),
        "for_range_loop": _BindingRule(("declarator",), selector="declarator"),
    }
)
_LOCAL_BINDING_RULES: Mapping[Language, Mapping[str, _BindingRule]] = MappingProxyType(
    {
        Language.JAVA: _rules(
            {
                "catch_formal_parameter": _BindingRule(("name",)),
                "enhanced_for_statement": _BindingRule(("name",)),
                "instanceof_expression": _BindingRule(
                    ("name", "pattern"), selector="pattern"
                ),
                "pattern": _BindingRule(selector="pattern"),
                "resource": _BindingRule(("name",)),
                "type_pattern": _BindingRule(selector="pattern"),
                "variable_declarator": _BindingRule(("name",)),
            }
        ),
        Language.TYPESCRIPT: _TYPESCRIPT_LOCAL_RULES,
        Language.JAVASCRIPT: _TYPESCRIPT_LOCAL_RULES,
        Language.TSX: _TYPESCRIPT_LOCAL_RULES,
        Language.VUE: _TYPESCRIPT_LOCAL_RULES,
        Language.SVELTE: _TYPESCRIPT_LOCAL_RULES,
        Language.KOTLIN: _rules(
            {
                "catch_block": _BindingRule(child_kinds=("identifier",)),
                "variable_declaration": _BindingRule(child_kinds=("identifier",)),
            }
        ),
        Language.GO: _rules(
            {
                "range_clause": _BindingRule(("left",), required_tokens=(":=",)),
                "short_var_declaration": _BindingRule(("left",)),
                "var_spec": _BindingRule(("name",)),
            }
        ),
        Language.RUST: _rules(
            {
                "for_expression": _BindingRule(("pattern",), selector="pattern"),
                "let_condition": _BindingRule(("pattern",), selector="pattern"),
                "let_declaration": _BindingRule(("pattern",), selector="pattern"),
                "match_arm": _BindingRule(("pattern",), selector="pattern"),
            }
        ),
        Language.CSHARP: _rules(
            {
                "catch_declaration": _BindingRule(("name",)),
                "declaration_expression": _BindingRule(("name",)),
                "declaration_pattern": _BindingRule(("name",)),
                "foreach_statement": _BindingRule(("left",)),
                "variable_declarator": _BindingRule(("name",)),
            }
        ),
        Language.C: _C_LOCAL_RULES,
        Language.CPP: _CPP_LOCAL_RULES,
        Language.LUA: _rules(
            {
                "for_generic_clause": _BindingRule(child_kinds=("variable_list",)),
                "for_numeric_clause": _BindingRule(("name",)),
                "variable_declaration": _BindingRule(
                    child_kinds=("assignment_statement",),
                    selector="lua-assignment",
                ),
            }
        ),
    }
)


@dataclass(frozen=True, slots=True)
class _MemberRule:
    fields: tuple[str, ...] = ()
    child_kinds: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    last_named: bool = False
    continuation_only: bool = False


def _member_rules(values: Mapping[str, _MemberRule]) -> Mapping[str, _MemberRule]:
    return MappingProxyType(dict(values))


_TYPESCRIPT_MEMBER_RULES = _member_rules(
    {"member_expression": _MemberRule(("property",))}
)
_MEMBER_RULES_BY_LANGUAGE: Mapping[Language, Mapping[str, _MemberRule]] = (
    MappingProxyType(
        {
            Language.JAVA: _member_rules(
                {
                    "field_access": _MemberRule(("field",)),
                    "method_invocation": _MemberRule(
                        ("name",),
                        required_fields=("object",),
                    ),
                }
            ),
            Language.TYPESCRIPT: _TYPESCRIPT_MEMBER_RULES,
            Language.JAVASCRIPT: _TYPESCRIPT_MEMBER_RULES,
            Language.TSX: _TYPESCRIPT_MEMBER_RULES,
            Language.VUE: _TYPESCRIPT_MEMBER_RULES,
            Language.SVELTE: _TYPESCRIPT_MEMBER_RULES,
            Language.KOTLIN: _member_rules(
                {"navigation_expression": _MemberRule(last_named=True)}
            ),
            Language.GO: _member_rules(
                {"selector_expression": _MemberRule(("field",))}
            ),
            Language.RUST: _member_rules(
                {
                    "call_expression": _MemberRule(
                        child_kinds=("generic_function", "scoped_identifier")
                    ),
                    "field_expression": _MemberRule(("field",)),
                    "generic_function": _MemberRule(
                        child_kinds=("scoped_identifier",),
                        continuation_only=True,
                    ),
                    "scoped_identifier": _MemberRule(("name",), continuation_only=True),
                }
            ),
            Language.CSHARP: _member_rules(
                {
                    "generic_name": _MemberRule(
                        child_kinds=("identifier",), continuation_only=True
                    ),
                    "member_access_expression": _MemberRule(("name",)),
                    "member_binding_expression": _MemberRule(("name",)),
                }
            ),
            Language.C: _member_rules({"field_expression": _MemberRule(("field",))}),
            Language.CPP: _member_rules(
                {
                    "call_expression": _MemberRule(
                        child_kinds=("qualified_identifier",)
                    ),
                    "dependent_name": _MemberRule(
                        child_kinds=("template_method",), continuation_only=True
                    ),
                    "field_expression": _MemberRule(("field",)),
                    "qualified_identifier": _MemberRule(
                        ("name",), continuation_only=True
                    ),
                    "template_function": _MemberRule(("name",), continuation_only=True),
                    "template_method": _MemberRule(("name",), continuation_only=True),
                }
            ),
            Language.LUA: _member_rules(
                {
                    "dot_index_expression": _MemberRule(("field",)),
                    "method_index_expression": _MemberRule(("method",)),
                }
            ),
        }
    )
)
_KEYWORDS = frozenset(
    {
        "await",
        "break",
        "case",
        "catch",
        "const",
        "continue",
        "defer",
        "delete",
        "do",
        "else",
        "fallthrough",
        "finally",
        "for",
        "foreach",
        "func",
        "go",
        "if",
        "let",
        "lock",
        "loop",
        "goto",
        "import",
        "match",
        "new",
        "raise",
        "range",
        "return",
        "select",
        "self",
        "super",
        "switch",
        "synchronized",
        "this",
        "throw",
        "try",
        "unsafe",
        "using",
        "val",
        "var",
        "when",
        "while",
        "with",
        "yield",
    }
)


def _literal_kinds(
    *,
    strings: Iterable[str] = (),
    numbers: Iterable[str] = (),
    booleans: Iterable[str] = (),
    nulls: Iterable[str] = (),
) -> Mapping[str, str]:
    return MappingProxyType(
        {
            **dict.fromkeys(strings, "<string>"),
            **dict.fromkeys(numbers, "<number>"),
            **dict.fromkeys(booleans, "<bool>"),
            **dict.fromkeys(nulls, "<null>"),
        }
    )


_TYPESCRIPT_LITERAL_KINDS = _literal_kinds(
    strings=("string", "template_string"),
    numbers=("number",),
    booleans=("false", "true"),
    nulls=("null",),
)
_LITERAL_KINDS_BY_LANGUAGE: Mapping[Language, Mapping[str, str]] = MappingProxyType(
    {
        Language.JAVA: _literal_kinds(
            strings=(
                "character_literal",
                "string_literal",
                "template_expression",
            ),
            numbers=(
                "binary_integer_literal",
                "decimal_floating_point_literal",
                "decimal_integer_literal",
                "hex_floating_point_literal",
                "hex_integer_literal",
                "octal_integer_literal",
            ),
            booleans=("false", "true"),
            nulls=("null_literal",),
        ),
        Language.TYPESCRIPT: _TYPESCRIPT_LITERAL_KINDS,
        Language.JAVASCRIPT: _TYPESCRIPT_LITERAL_KINDS,
        Language.TSX: _TYPESCRIPT_LITERAL_KINDS,
        Language.VUE: _TYPESCRIPT_LITERAL_KINDS,
        Language.SVELTE: _TYPESCRIPT_LITERAL_KINDS,
        Language.KOTLIN: _literal_kinds(
            strings=(
                "character_literal",
                "multiline_string_literal",
                "string_literal",
            ),
            numbers=("float_literal", "number_literal"),
        ),
        Language.GO: _literal_kinds(
            strings=(
                "interpreted_string_literal",
                "raw_string_literal",
                "rune_literal",
            ),
            numbers=("float_literal", "imaginary_literal", "int_literal"),
            booleans=("false", "true"),
            nulls=("nil",),
        ),
        Language.RUST: _literal_kinds(
            strings=("char_literal", "raw_string_literal", "string_literal"),
            numbers=("float_literal", "integer_literal", "negative_literal"),
            booleans=("boolean_literal",),
        ),
        Language.CSHARP: _literal_kinds(
            strings=(
                "character_literal",
                "interpolated_string_expression",
                "raw_string_literal",
                "string_literal",
                "verbatim_string_literal",
            ),
            numbers=("integer_literal", "real_literal"),
            booleans=("boolean_literal",),
            nulls=("null_literal",),
        ),
        Language.C: _literal_kinds(
            strings=("char_literal", "concatenated_string", "string_literal"),
            numbers=("number_literal",),
            booleans=("false", "true"),
            nulls=("null",),
        ),
        Language.CPP: _literal_kinds(
            strings=(
                "char_literal",
                "concatenated_string",
                "raw_string_literal",
                "string_literal",
            ),
            numbers=("number_literal",),
            booleans=("false", "true"),
            nulls=("null", "nullptr"),
        ),
        Language.LUA: _literal_kinds(
            strings=("string",),
            numbers=("number",),
            booleans=("false", "true"),
            nulls=("nil",),
        ),
    }
)
_TEXT_LITERALS_BY_LANGUAGE: Mapping[Language, Mapping[str, str]] = MappingProxyType(
    {
        Language.KOTLIN: MappingProxyType(
            {"false": "<bool>", "null": "<null>", "true": "<bool>"}
        )
    }
)
_INTERPOLATED_STRING_KINDS_BY_LANGUAGE: Mapping[Language, frozenset[str]] = (
    MappingProxyType(
        {
            Language.JAVA: frozenset({"template_expression"}),
            Language.TYPESCRIPT: frozenset({"template_string"}),
            Language.JAVASCRIPT: frozenset({"template_string"}),
            Language.TSX: frozenset({"template_string"}),
            Language.VUE: frozenset({"template_string"}),
            Language.SVELTE: frozenset({"template_string"}),
            Language.KOTLIN: frozenset({"multiline_string_literal", "string_literal"}),
            Language.CSHARP: frozenset({"interpolated_string_expression"}),
        }
    )
)
_INTERPOLATION_KINDS = frozenset(
    {
        "interpolation",
        "interpolation_expression",
        "string_interpolation",
        "template_substitution",
    }
)
_ARGUMENT_KINDS = frozenset(
    {
        "argument_list",
        "arguments",
        "value_arguments",
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


def _literal_text(language: Language, node: object) -> str | None:
    if not bool(getattr(node, "is_named", True)):
        return None
    kind = str(getattr(node, "type", ""))
    classified = _LITERAL_KINDS_BY_LANGUAGE.get(language, {}).get(kind)
    if classified is not None:
        return classified
    return _TEXT_LITERALS_BY_LANGUAGE.get(language, {}).get(ast_text(node))


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
    field_names = (
        ("type", "constructor", "name") if construct else ("name", "function", "callee")
    )
    for field_name in field_names:
        field = ast_field(node, field_name)
        if field is not None:
            return ast_text(field)
    for child in getattr(node, "children", ()):
        if (
            bool(getattr(child, "is_named", False))
            and getattr(child, "type", "") not in _ARGUMENT_KINDS
        ):
            return ast_text(child)
    return ast_text(node)


def _node_key(node: object) -> tuple[int, int, str]:
    return (
        int(getattr(node, "start_byte", 0)),
        int(getattr(node, "end_byte", 0)),
        str(getattr(node, "type", "")),
    )


def ownership_context(
    owned_boundaries: Iterable[object] = (),
    *,
    include_anonymous: bool = False,
) -> OwnershipContext:
    """Precompute ownership boundaries for repeated walks in one extraction."""
    return OwnershipContext(
        frozenset(_node_key(node) for node in owned_boundaries),
        include_anonymous,
    )


def _resolve_ownership(
    ownership: OwnershipContext | None,
    owned_boundaries: Iterable[object],
    include_anonymous: bool,
) -> OwnershipContext:
    return ownership or ownership_context(
        owned_boundaries,
        include_anonymous=include_anonymous,
    )


def _field_roots(node: object, names: Iterable[str]) -> tuple[object, ...]:
    roots: list[object] = []
    for name in names:
        many = getattr(node, "children_by_field_name", None)
        fields = tuple(many(name)) if callable(many) else ()
        if not fields:
            field = ast_field(node, name)
            fields = (field,) if field is not None else ()
        for field in fields:
            if all(not _same_node(field, item) for item in roots):
                roots.append(field)
    return tuple(roots)


def _child_field(node: object, index: int) -> str | None:
    field_name = getattr(node, "field_name_for_child", None)
    if not callable(field_name):
        return None
    value = field_name(index)
    return str(value) if value is not None else None


def _child_roots(node: object, kinds: Iterable[str]) -> tuple[object, ...]:
    selected = frozenset(kinds)
    return tuple(
        child
        for child in getattr(node, "children", ())
        if str(getattr(child, "type", "")) in selected
    )


def _walk_owned(root: object, boundary_kinds: frozenset[str]) -> Iterable[object]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        children = getattr(node, "children", ())
        for child in reversed(children):
            if str(getattr(child, "type", "")) not in boundary_kinds:
                stack.append(child)


def _parameter_parts(source: SourceFile, callable_node: object) -> tuple[object, ...]:
    roots = _field_roots(
        callable_node,
        _PARAMETER_FIELDS_BY_LANGUAGE.get(
            source.language,
            _DEFAULT_PARAMETER_FIELDS,
        ),
    )
    if roots:
        return roots
    direct = _child_roots(callable_node, _PARAMETER_CONTAINER_KINDS)
    if direct:
        return direct
    pending = list(_field_roots(callable_node, ("declarator",)))
    found: list[object] = []
    while pending:
        declarator = pending.pop(0)
        found.extend(
            _field_roots(
                declarator,
                _DEFAULT_PARAMETER_FIELDS,
            )
        )
        pending.extend(_field_roots(declarator, ("declarator",)))
    if found:
        return tuple(found)
    if (
        source.language is Language.KOTLIN
        and str(getattr(callable_node, "type", "")) == "setter"
    ):
        return _child_roots(callable_node, _IDENTIFIER_KINDS)[:1]
    return ()


def _body_parts(source: SourceFile, callable_node: object) -> tuple[object, ...]:
    roots = list(_field_roots(callable_node, ("body",)))
    if not roots:
        roots.extend(_child_roots(callable_node, _BODY_KINDS))
    callable_kind = str(getattr(callable_node, "type", ""))
    if callable_kind == "program":
        return (callable_node,)
    extra_kinds = _ADDITIONAL_BODY_CHILD_KINDS.get(source.language, {}).get(
        callable_kind,
        frozenset(),
    )
    roots.extend(_child_roots(callable_node, extra_kinds))
    if roots:
        return tuple(
            {_node_key(root): root for root in sorted(roots, key=_node_key)}.values()
        )
    if source.language is Language.KOTLIN and callable_kind == "lambda_literal":
        return tuple(
            child
            for child in getattr(callable_node, "children", ())
            if bool(getattr(child, "is_named", False))
            and str(getattr(child, "type", "")) != "lambda_parameters"
        )
    return ()


def _direct_binding_nodes(root: object) -> tuple[object, ...]:
    if str(getattr(root, "type", "")) in _BINDING_LEAF_KINDS:
        return (root,)
    return _child_roots(root, _BINDING_LEAF_KINDS)


_UNFIELDED_DECLARATOR_WRAPPERS = frozenset(
    {
        "abstract_parenthesized_declarator",
        "attributed_declarator",
        "parenthesized_declarator",
        "reference_declarator",
    }
)


def _declarator_binding_nodes(root: object) -> tuple[object, ...]:
    direct = _direct_binding_nodes(root)
    if str(getattr(root, "type", "")) in _BINDING_LEAF_KINDS:
        return direct
    declarators = _field_roots(root, ("declarator",))
    if declarators:
        return tuple(
            binder
            for declarator in declarators
            for binder in _declarator_binding_nodes(declarator)
        )
    kind = str(getattr(root, "type", ""))
    if kind == "structured_binding_declarator":
        return direct
    if kind in _UNFIELDED_DECLARATOR_WRAPPERS:
        return tuple(
            binder
            for child in getattr(root, "children", ())
            if bool(getattr(child, "is_named", False))
            for binder in _declarator_binding_nodes(child)
        )
    return direct


_PATTERN_CONTAINERS = frozenset(
    {
        "array_pattern",
        "captured_pattern",
        "closure_parameters",
        "generic_pattern",
        "match_pattern",
        "mut_pattern",
        "object_pattern",
        "or_pattern",
        "parenthesized_pattern",
        "pattern",
        "record_pattern_body",
        "record_pattern_component",
        "ref_pattern",
        "reference_pattern",
        "rest_pattern",
        "slice_pattern",
        "struct_pattern",
        "tuple_pattern",
        "tuple_struct_pattern",
    }
)


def _pattern_binding_nodes(root: object) -> tuple[object, ...]:
    kind = str(getattr(root, "type", ""))
    if kind in _BINDING_LEAF_KINDS:
        return (root,)
    if kind in {"assignment_pattern", "object_assignment_pattern"}:
        return tuple(
            binder
            for child in _field_roots(root, ("left",))
            for binder in _pattern_binding_nodes(child)
        )
    if kind == "pair_pattern":
        return tuple(
            binder
            for child in _field_roots(root, ("value",))
            for binder in _pattern_binding_nodes(child)
        )
    if kind == "field_pattern":
        patterns = _field_roots(root, ("pattern",))
        if patterns:
            return tuple(
                binder for child in patterns for binder in _pattern_binding_nodes(child)
            )
        return tuple(
            child
            for child in _field_roots(root, ("name",))
            if str(getattr(child, "type", "")) == "shorthand_field_identifier"
        )
    if kind == "record_pattern":
        bodies = _child_roots(root, ("record_pattern_body",))
        return tuple(
            binder for child in bodies for binder in _pattern_binding_nodes(child)
        )
    if kind not in _PATTERN_CONTAINERS:
        return ()
    found: list[object] = []
    for index, child in enumerate(getattr(root, "children", ())):
        if not bool(getattr(child, "is_named", False)):
            continue
        field = _child_field(root, index)
        child_kind = str(getattr(child, "type", ""))
        if field in {"key", "name", "right", "type"} or child_kind in _TYPE_KINDS:
            continue
        if (
            child_kind in _BINDING_LEAF_KINDS
            or child_kind in _PATTERN_CONTAINERS
            or child_kind
            in {
                "assignment_pattern",
                "field_pattern",
                "object_assignment_pattern",
                "pair_pattern",
                "record_pattern",
            }
        ):
            found.extend(_pattern_binding_nodes(child))
    return tuple(found)


def _lua_assignment_binding_nodes(root: object) -> tuple[object, ...]:
    variables = _child_roots(root, ("variable_list",))
    return tuple(binder for item in variables for binder in _direct_binding_nodes(item))


_C_PARAMETER_DECLARATION_KINDS = frozenset(
    {
        "optional_parameter_declaration",
        "parameter_declaration",
        "variadic_parameter_declaration",
    }
)


def _parameter_list_binding_nodes(root: object) -> tuple[object, ...]:
    declarations = (
        (root,)
        if str(getattr(root, "type", "")) in _C_PARAMETER_DECLARATION_KINDS
        else _child_roots(root, _C_PARAMETER_DECLARATION_KINDS)
    )
    return tuple(
        binder
        for declaration in declarations
        for declarator in _field_roots(declaration, ("declarator",))
        for binder in _declarator_binding_nodes(declarator)
    )


def _rule_binding_nodes(node: object, rule: _BindingRule) -> tuple[object, ...]:
    if rule.required_tokens and not any(
        ast_text(child) in rule.required_tokens
        for child in getattr(node, "children", ())
    ):
        return ()
    roots = (*_field_roots(node, rule.fields), *_child_roots(node, rule.child_kinds))
    if not roots and not rule.fields and not rule.child_kinds:
        roots = (node,)
    if rule.selector == "declarator":
        select = _declarator_binding_nodes
    elif rule.selector == "parameters":
        select = _parameter_list_binding_nodes
    elif rule.selector == "pattern":
        select = _pattern_binding_nodes
    elif rule.selector == "lua-assignment":
        select = _lua_assignment_binding_nodes
    else:
        select = _direct_binding_nodes
    return tuple(binder for root in roots for binder in select(root))


def _binding_keys(
    source: SourceFile,
    roots: Iterable[object],
    rules: Mapping[str, _BindingRule],
    *,
    root_leaves: bool = False,
    nested_boundaries: frozenset[str] = frozenset(),
) -> frozenset[tuple[int, int, str]]:
    names: list[object] = []
    boundary_kinds = _OWNED_BOUNDARIES_BY_LANGUAGE[source.language] | nested_boundaries
    for root in roots:
        if root_leaves and str(getattr(root, "type", "")) in _BINDING_LEAF_KINDS:
            names.append(root)
        for node in _walk_owned(root, boundary_kinds):
            rule = rules.get(str(getattr(node, "type", "")))
            if rule is not None:
                names.extend(_rule_binding_nodes(node, rule))
    return frozenset(_node_key(node) for node in names)


def _parameter_keys(
    source: SourceFile,
    parameters: Iterable[object],
) -> frozenset[tuple[int, int, str]]:
    return _binding_keys(
        source,
        parameters,
        _PARAMETER_BINDING_RULES.get(source.language, {}),
        root_leaves=True,
        nested_boundaries=_PARAMETER_CONTAINER_KINDS,
    )


def _local_keys(
    source: SourceFile,
    bodies: Iterable[object],
) -> frozenset[tuple[int, int, str]]:
    return _binding_keys(
        source,
        bodies,
        _LOCAL_BINDING_RULES.get(source.language, {}),
    )


def _anonymous_callable_kinds(language: Language) -> frozenset[str]:
    if language in {
        Language.TYPESCRIPT,
        Language.JAVASCRIPT,
        Language.TSX,
        Language.VUE,
        Language.SVELTE,
    }:
        return _CALLABLE_KINDS_BY_LANGUAGE[language]
    return _ANONYMOUS_CALLABLE_KINDS_BY_LANGUAGE.get(language, frozenset())


def _anonymous_callables(
    source: SourceFile,
    bodies: Iterable[object],
    owned_boundaries: frozenset[tuple[int, int, str]],
) -> tuple[object, ...]:
    anonymous_kinds = _anonymous_callable_kinds(source.language)
    if not anonymous_kinds:
        return ()
    boundaries = _OWNED_BOUNDARIES_BY_LANGUAGE[source.language] - anonymous_kinds
    found: list[object] = []
    for body in bodies:
        stack = [body]
        while stack:
            node = stack.pop()
            key = _node_key(node)
            if key in owned_boundaries and node is not body:
                continue
            if key[2] in anonymous_kinds:
                found.append(node)
            for child in reversed(tuple(getattr(node, "children", ()))):
                child_key = _node_key(child)
                if child_key in owned_boundaries or child_key[2] in boundaries:
                    continue
                stack.append(child)
    return tuple(found)


def _is_member_child(
    parent: object | None,
    node: object,
    language: Language,
    inherited: bool,
) -> bool:
    if parent is None:
        return False
    parent_kind = str(getattr(parent, "type", ""))
    rule = _MEMBER_RULES_BY_LANGUAGE.get(language, {}).get(parent_kind)
    if rule is None:
        return False
    if rule.continuation_only and not inherited:
        return False
    if rule.required_fields and not all(
        _field_roots(parent, (field,)) for field in rule.required_fields
    ):
        return False
    if rule.fields:
        return any(
            _same_node(field, node) for field in _field_roots(parent, rule.fields)
        )
    if rule.child_kinds:
        return any(
            _same_node(child, node) for child in _child_roots(parent, rule.child_kinds)
        )
    if not rule.last_named:
        return False
    named = tuple(
        child
        for child in getattr(parent, "children", ())
        if bool(getattr(child, "is_named", False))
    )
    return bool(named) and _same_node(named[-1], node)


def _member_context_for_child(
    parent: object,
    child: object,
    language: Language,
    inherited: bool,
) -> bool:
    return _is_member_child(parent, child, language, inherited)


def _kotlin_constructor(node: object) -> bool:
    if str(getattr(node, "type", "")) != "call_expression":
        return False
    callee: object | None = None
    for child in getattr(node, "children", ()):
        if (
            bool(getattr(child, "is_named", False))
            and str(getattr(child, "type", "")) not in _ARGUMENT_KINDS
        ):
            callee = child
            break
    if callee is None:
        return False
    name = ast_text(callee).rsplit(".", 1)[-1]
    return bool(name) and name[0].isupper()


def _type_context_for_child(
    parent: object,
    child: object,
    index: int,
    inherited: bool,
    language: Language,
) -> bool:
    parent_kind = str(getattr(parent, "type", ""))
    child_kind = str(getattr(child, "type", ""))
    field = _child_field(parent, index)
    if parent_kind == "array_type" and field in {"length", "size"}:
        return False
    if parent_kind in _GENERIC_ARGUMENT_KINDS_BY_LANGUAGE.get(language, frozenset()):
        return bool(getattr(child, "is_named", False)) and (child_kind in _TYPE_KINDS)
    if parent_kind in _TYPE_KINDS:
        return bool(getattr(child, "is_named", False))
    if field in {"returns", "type"}:
        return True
    if parent_kind == "record_pattern":
        named = tuple(
            item
            for item in getattr(parent, "children", ())
            if bool(getattr(item, "is_named", False))
        )
        if named and _same_node(named[0], child):
            return True
    return inherited


def _generic_argument_context_for_child(
    parent: object,
    child: object,
    language: Language,
    inherited: bool,
) -> bool:
    parent_kind = str(getattr(parent, "type", ""))
    if parent_kind in _GENERIC_ARGUMENT_KINDS_BY_LANGUAGE.get(language, frozenset()):
        return bool(getattr(child, "is_named", False))
    return inherited


class _TreeSitterBodyEventWalker:
    def __init__(
        self,
        source: SourceFile,
        callable_node: object,
        *,
        owned_boundaries: Iterable[object] = (),
        include_anonymous: bool = False,
        ownership: OwnershipContext | None = None,
    ) -> None:
        self.source = source
        self.callable_node = callable_node
        self.events: list[BodyEvent] = []
        self._events_seen: set[BodyEvent] = set()
        self.parameter_keys: frozenset[tuple[int, int, str]] = frozenset()
        self.local_keys: frozenset[tuple[int, int, str]] = frozenset()
        context = _resolve_ownership(
            ownership,
            owned_boundaries,
            include_anonymous,
        )
        self.owned_boundary_keys = context.boundary_keys
        self.include_anonymous = context.include_anonymous

    def event(self, kind: BodyEventKind, text: str, node: object) -> None:
        event = BodyEvent(kind, text, node_span(self.source, node))
        if event not in self._events_seen:
            self._events_seen.add(event)
            self.events.append(event)

    def walk(self) -> tuple[BodyEvent, ...]:
        parameters = _parameter_parts(self.source, self.callable_node)
        bodies = _body_parts(self.source, self.callable_node)
        anonymous = (
            _anonymous_callables(self.source, bodies, self.owned_boundary_keys)
            if self.include_anonymous
            else ()
        )
        parameters = (
            *parameters,
            *(
                parameter
                for callable_node in anonymous
                for parameter in _parameter_parts(self.source, callable_node)
            ),
        )
        local_bodies = (
            *bodies,
            *(
                body
                for callable_node in anonymous
                for body in _body_parts(self.source, callable_node)
            ),
        )
        self.parameter_keys = _parameter_keys(self.source, parameters)
        self.local_keys = _local_keys(self.source, local_bodies)
        for parameter in parameters:
            self.visit(parameter, self.callable_node)
        for body in bodies:
            self.visit(body, self.callable_node, is_root=True)
        result = tuple(self.events)
        validate_body_events(result)
        return result

    def visit(
        self,
        node: object,
        parent: object | None,
        *,
        is_root: bool = False,
        type_context: bool = False,
        member_context: bool = False,
        generic_argument_context: bool = False,
    ) -> None:
        kind = str(getattr(node, "type", ""))
        if not is_root and _owned_boundary(
            self.source,
            node,
            self.owned_boundary_keys,
            include_anonymous=self.include_anonymous,
        ):
            return

        control = _CONTROL_KINDS_BY_LANGUAGE[self.source.language].get(kind)
        if control is not None:
            self.event(BodyEventKind.CONTROL_ENTER, control, node)

        is_construct = kind in _CONSTRUCT_KINDS or (
            self.source.language is Language.KOTLIN and _kotlin_constructor(node)
        )
        if is_construct:
            self.event(
                BodyEventKind.CONSTRUCT,
                _call_text(node, construct=True),
                node,
            )
        elif kind in _CALL_KINDS:
            self.event(BodyEventKind.CALL, _call_text(node, construct=False), node)

        literal = _literal_text(self.source.language, node)
        if literal is not None:
            self.event(BodyEventKind.LITERAL, literal, node)
            interpolated = _INTERPOLATED_STRING_KINDS_BY_LANGUAGE.get(
                self.source.language,
                frozenset(),
            )
            if kind in interpolated:
                for child in getattr(node, "children", ()):
                    if str(getattr(child, "type", "")) in _INTERPOLATION_KINDS:
                        self.visit(child, node)
            if control is not None:
                self.event(BodyEventKind.CONTROL_EXIT, control, node)
            return

        if kind in _TYPE_KINDS and not member_context:
            self.event(BodyEventKind.TYPE, ast_text(node), node)
            if generic_argument_context and kind in _TYPE_NAME_LEAF_KINDS:
                self.event(BodyEventKind.NAME, ast_text(node), node)
            type_context = True
        if kind in _NAME_KINDS:
            key = _node_key(node)
            if key in self.parameter_keys:
                event_kind = BodyEventKind.PARAM
            elif key in self.local_keys:
                event_kind = BodyEventKind.LOCAL
            elif member_context:
                self.event(BodyEventKind.MEMBER, ast_text(node), node)
                event_kind = BodyEventKind.NAME
            elif type_context or _is_named_field(parent, node, ("type",)):
                event_kind = BodyEventKind.TYPE
            else:
                event_kind = BodyEventKind.NAME
            self.event(event_kind, ast_text(node), node)
            if generic_argument_context and event_kind is BodyEventKind.TYPE:
                self.event(BodyEventKind.NAME, ast_text(node), node)
        elif kind not in _TYPE_KINDS:
            text = ast_text(node)
            if text in _KEYWORDS:
                self.event(BodyEventKind.KEYWORD, text, node)
            elif (
                not type_context
                and str(getattr(parent, "type", "")) not in _TYPE_DELIMITER_CONTAINERS
                and not bool(getattr(node, "is_named", True))
                and (text in _OPERATORS or re.fullmatch(r"[+*/%<>=!&|^~?-]+", text))
            ):
                self.event(BodyEventKind.OPERATOR, text, node)

        for index, child in enumerate(getattr(node, "children", ())):
            self.visit(
                child,
                node,
                type_context=_type_context_for_child(
                    node,
                    child,
                    index,
                    type_context,
                    self.source.language,
                ),
                member_context=_member_context_for_child(
                    node,
                    child,
                    self.source.language,
                    member_context,
                ),
                generic_argument_context=_generic_argument_context_for_child(
                    node,
                    child,
                    self.source.language,
                    generic_argument_context,
                ),
            )

        if control is not None:
            self.event(BodyEventKind.CONTROL_EXIT, control, node)


def _owned_boundary(
    source: SourceFile,
    node: object,
    explicit: frozenset[tuple[int, int, str]],
    *,
    include_anonymous: bool,
) -> bool:
    key = _node_key(node)
    if key in explicit:
        return True
    kind = key[2]
    if kind not in _OWNED_BOUNDARIES_BY_LANGUAGE[source.language]:
        return False
    if not include_anonymous:
        return True
    return kind not in _anonymous_callable_kinds(source.language)


def owned_nodes(
    source: SourceFile,
    callable_node: object,
    *,
    owned_boundaries: Iterable[object] = (),
    include_anonymous: bool = False,
    ownership: OwnershipContext | None = None,
) -> tuple[Any, ...]:
    """Return parameter/body nodes owned by one named or module declaration."""
    context = _resolve_ownership(ownership, owned_boundaries, include_anonymous)
    roots = (*_parameter_parts(source, callable_node), *_body_parts(source, callable_node))
    found: list[Any] = []
    stack = list(reversed(roots))
    root_keys = frozenset(_node_key(root) for root in roots)
    while stack:
        node = stack.pop()
        if _node_key(node) not in root_keys and _owned_boundary(
            source,
            node,
            context.boundary_keys,
            include_anonymous=context.include_anonymous,
        ):
            continue
        found.append(node)
        stack.extend(reversed(tuple(getattr(node, "children", ()))))
    return tuple(found)


def body_events(
    source: SourceFile,
    callable_node: object,
    *,
    owned_boundaries: Iterable[object] = (),
    include_anonymous: bool = False,
    ownership: OwnershipContext | None = None,
) -> tuple[BodyEvent, ...]:
    """Emit facts from one Tree-sitter callable without entering nested callables."""
    return _TreeSitterBodyEventWalker(
        source,
        callable_node,
        owned_boundaries=owned_boundaries,
        include_anonymous=include_anonymous,
        ownership=ownership,
    ).walk()


tree_sitter_body_events = body_events


__all__ = [
    "GRAMMAR_METADATA",
    "GrammarMetadata",
    "OwnershipContext",
    "ast_collect",
    "ast_field",
    "ast_text",
    "body_events",
    "body_lines",
    "grammar_version",
    "load_parser",
    "node_span",
    "owned_nodes",
    "ownership_context",
    "parser_versions",
    "tree_sitter_body_events",
]
