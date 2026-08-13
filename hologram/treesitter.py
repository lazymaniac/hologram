"""Tree-sitter runtime: grammar registry, parser cache, AST helpers."""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tree-sitter core (required for Java and TypeScript/JavaScript)
# ---------------------------------------------------------------------------

try:
    from tree_sitter import Language as _TSLanguage
    from tree_sitter import Parser as _TSParser
except ImportError:
    _TSLanguage = _TSParser = None  # type: ignore[assignment,misc]


def _load_parser(module: str, attr: str = "language"):
    if _TSParser is None:
        return None
    try:
        mod = __import__(module)
    except ImportError:
        return None
    return _TSParser(_TSLanguage(getattr(mod, attr)()))


# lang -> (importable grammar module, pip package)
_GRAMMAR_MODULES = {
    "java": ("tree_sitter_java", "tree-sitter-java"),
    "typescript": ("tree_sitter_typescript", "tree-sitter-typescript"),
    "javascript": ("tree_sitter_typescript", "tree-sitter-typescript"),
    "tsx": ("tree_sitter_typescript", "tree-sitter-typescript"),
    "vue": ("tree_sitter_typescript", "tree-sitter-typescript"),
    "svelte": ("tree_sitter_typescript", "tree-sitter-typescript"),
    "go": ("tree_sitter_go", "tree-sitter-go"),
    "rust": ("tree_sitter_rust", "tree-sitter-rust"),
    "csharp": ("tree_sitter_c_sharp", "tree-sitter-c-sharp"),
    "kotlin": ("tree_sitter_kotlin", "tree-sitter-kotlin"),
    "c": ("tree_sitter_c", "tree-sitter-c"),
    "cpp": ("tree_sitter_cpp", "tree-sitter-cpp"),
    "lua": ("tree_sitter_lua", "tree-sitter-lua"),
    "html": ("tree_sitter_html", "tree-sitter-html"),
    "bash": ("tree_sitter_bash", "tree-sitter-bash"),
    "css": ("tree_sitter_css", "tree-sitter-css"),
}

_PARSERS = {
    "java": _load_parser("tree_sitter_java"),
    "typescript": _load_parser("tree_sitter_typescript", "language_typescript"),
    "tsx": _load_parser("tree_sitter_typescript", "language_tsx"),
    "go": _load_parser("tree_sitter_go"),
    "rust": _load_parser("tree_sitter_rust"),
    "csharp": _load_parser("tree_sitter_c_sharp"),
    "kotlin": _load_parser("tree_sitter_kotlin"),
    "c": _load_parser("tree_sitter_c"),
    "cpp": _load_parser("tree_sitter_cpp"),
    "lua": _load_parser("tree_sitter_lua"),
    "html": _load_parser("tree_sitter_html"),
    "bash": _load_parser("tree_sitter_bash"),
    "css": _load_parser("tree_sitter_css"),
}
_PARSERS["javascript"] = _PARSERS["typescript"]
_PARSERS["vue"] = _PARSERS["svelte"] = _PARSERS["typescript"]

USING_TREESITTER = _PARSERS["java"] is not None  # kept for callers/tests


def has_parser(lang: str) -> bool:
    return _PARSERS.get(lang) is not None


def _grammar_pkgs(langs) -> list[str]:
    return ["tree-sitter"] + sorted({_GRAMMAR_MODULES[l][1] for l in langs})


def _ast_text(node) -> str:
    return node.text.decode(errors="replace") if node is not None else ""


def _ast_field(node, name):
    return node.child_by_field_name(name)


def _ast_collect(root, kinds) -> list:
    """All descendant nodes of the given types, in source order."""
    stack, found = [root], []
    while stack:
        n = stack.pop()
        if n.type in kinds:
            found.append(n)
        stack.extend(n.children)
    found.sort(key=lambda n: n.start_byte)
    return found


def _ast_calls(body, own_name: str, call_kinds, entry_fn) -> list[str]:
    """Called names in source order, receiver-qualified, and deduplicated."""
    if body is None:
        return []
    seen: list[str] = []
    for n in _ast_collect(body, call_kinds):
        name, entry = entry_fn(n)
        if not name or name == own_name or entry in seen:
            continue
        seen.append(entry)
    return seen



def _body_lines(body) -> int:
    return body.end_point[0] - body.start_point[0] + 1 if body is not None else 0

