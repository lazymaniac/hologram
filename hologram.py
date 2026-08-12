#!/usr/bin/env python3
"""hologram: compress a codebase into a compact markdown map for LLM sessions.

Deterministic. One layout: named type fields, public name-based signatures,
project-internal calls, factored private names, and test locations.

Extraction uses tree-sitter for supported languages, stdlib `ast` for Python,
and a narrow template scanner for Helm.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

LANG_EXTENSIONS = {
    ".java": "java",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "tsx",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".lua": "lua",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".yaml": "helm",
    ".yml": "helm",
    ".tpl": "helm",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
}

DENYLIST_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "out", "bin", "obj",
    "vendor", "generated", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    "fixtures", "testdata", "resources",
}

TYPE_KINDS = ("class", "interface", "record", "enum", "type")


@dataclass
class Symbol:
    name: str
    kind: str
    file: str
    line: int
    signature: str = ""
    params: list[str] = field(default_factory=list)
    param_names: list[str] = field(default_factory=list)
    returns: str | None = None
    visibility: str = "pub"
    container: str | None = None
    lang: str = ""
    fields: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    supers: list[str] = field(default_factory=list)
    permits: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)  # var/param/field name -> declared type
    size: int = 0  # body line count (0 = bodyless/unknown)


def detect_language(path: Path) -> str | None:
    return LANG_EXTENSIONS.get(path.suffix)


def scan_files(root: Path) -> list[Path]:
    """Source files under root: git-tracked only when root is a git repo (so .gitignore
    excludes vendored/data trees), else a pruned filesystem walk. Deterministic order."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                 capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                results = []
                for rel in out.stdout.split("\0"):
                    if not rel or detect_language(Path(rel)) is None:
                        continue
                    if any(part in DENYLIST_DIRS or part.startswith(".")
                           for part in Path(rel).parts[:-1]):
                        continue
                    p = root / rel
                    if p.is_file():
                        results.append(p)
                return sorted(results)
        except (OSError, subprocess.TimeoutExpired):
            pass
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in DENYLIST_DIRS and not d.startswith("."))
        for fn in filenames:
            p = Path(dirpath) / fn
            if detect_language(p) is not None:
                results.append(p)
    return sorted(results)


# ---------------------------------------------------------------------------
# Shared text utilities
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(
    r'"""(?:\\.|(?!""").)*"""'
    r"|'''(?:\\.|(?!''').)*'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'",
    re.S,
)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*|#[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def strip_comments_and_strings(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub('"s"', text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


def _parse_throws(clause: str | None) -> list[str]:
    if not clause:
        return []
    return [t.strip().split(".")[-1] for t in clause.split(",") if t.strip()]


def _split_top_commas(raw: str, opens: str, closes: str) -> list[str]:
    """Split on commas that sit outside any bracket nesting."""
    parts, depth, cur = [], 0, ""
    for c in raw:
        if c in opens:
            depth += 1
        elif c in closes:
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    return parts


def split_params(raw: str) -> list[str]:
    """Split a Java parameter list on top-level commas, return declared types only."""
    types = []
    for p in _split_top_commas(raw, "<([", ">)]"):
        p = re.sub(r"@\w+(\([^)]*\))?", "", p).strip()
        p = re.sub(r"^final\s+", "", p)
        tokens = p.rsplit(None, 1)
        if tokens:
            types.append(tight_type(tokens[0].strip()))
    return types


def tight_type(t: str) -> str:
    """Collapse interior whitespace in a type expression: Map<K, V> -> Map<K,V>."""
    return re.sub(r",\s+", ",", t)


def _base_type(t: str) -> str:
    """Bare type name: Map<K,V> -> Map, list[X] -> list, String[] -> String."""
    return re.sub(r"[<\[(].*", "", t).strip()


def _heritage(segment: str) -> tuple[list[str], list[str]]:
    """(supers, permits) from the text between a type's name and its body
    (Java and TS share the extends/implements keywords)."""
    def names(kw: str) -> list[str]:
        m = re.search(rf"\b{kw}\s+([\w.<>, \t\n]+?)(?=\bextends\b|\bimplements\b|\bpermits\b|$)",
                      segment)
        if not m:
            return []
        return [re.sub(r"<.*", "", n.strip()).split(".")[-1]
                for n in m.group(1).split(",") if n.strip()]
    supers = names("extends") + names("implements")
    return supers, names("permits")


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


# ---------------------------------------------------------------------------
# Java extraction
# ---------------------------------------------------------------------------

_JAVA_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


def _ast_modifiers(node) -> str:
    for c in node.children:
        if c.type == "modifiers":
            return _ast_text(c)
    return ""


def _ast_param_types(node) -> list[str]:
    """Declared parameter types from a formal_parameters node."""
    raw = _ast_text(node)
    return split_params(raw[1:-1]) if raw.startswith("(") else split_params(raw)


def _java_param_names(node) -> list[str]:
    names: list[str] = []
    for p in (node.children if node is not None else ()):
        if p.type not in ("formal_parameter", "spread_parameter"):
            continue
        name = _ast_field(p, "name")
        names.append(_ast_text(name) if name is not None else "")
    return names


def _ast_vis(mods: str) -> str:
    return "priv" if any(v in mods for v in ("private", "protected")) else "pub"


def _java_call_entry(n) -> tuple[str, str]:
    if n.type == "object_creation_expression":
        entry = re.sub(r"<.*", "", _ast_text(_ast_field(n, "type")))
        return entry, entry
    name = _ast_text(_ast_field(n, "name"))
    obj = _ast_field(n, "object")
    entry = (f"{_ast_text(obj)}.{name}"
             if obj is not None and obj.type == "identifier" else name)
    return name, entry


def _java_calls(body, own_name: str) -> list[str]:
    return _ast_calls(body, own_name,
                      ("method_invocation", "object_creation_expression"),
                      _java_call_entry)


def _java_param_bindings(params_node) -> dict[str, str]:
    binds: dict[str, str] = {}
    if params_node is None:
        return binds
    for p in params_node.children:
        if p.type == "formal_parameter":
            t, n = _ast_field(p, "type"), _ast_field(p, "name")
            if t is not None and n is not None:
                binds[_ast_text(n)] = _base_type(_ast_text(t))
    return binds


def _java_class_bindings(tn) -> dict[str, str]:
    """Field and record-component types visible to every method of the type."""
    binds = _java_param_bindings(_ast_field(tn, "parameters"))  # record components
    body = _ast_field(tn, "body")
    for f in (body.children if body is not None else ()):
        if f.type == "field_declaration":
            t = _ast_text(_ast_field(f, "type"))
            for dec in f.children:
                if dec.type == "variable_declarator":
                    binds[_ast_text(_ast_field(dec, "name"))] = _base_type(t)
    return binds


def _java_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for d in _ast_collect(body, ("local_variable_declaration", "enhanced_for_statement")):
        t = _ast_text(_ast_field(d, "type"))
        if d.type == "enhanced_for_statement":
            n = _ast_field(d, "name")
            if n is not None and t != "var":
                binds[_ast_text(n)] = _base_type(t)
            continue
        for dec in d.children:
            if dec.type != "variable_declarator":
                continue
            name = _ast_text(_ast_field(dec, "name"))
            if t == "var":
                val = _ast_field(dec, "value")
                if val is not None and val.type == "object_creation_expression":
                    binds[name] = _base_type(_ast_text(_ast_field(val, "type")))
            else:
                binds[name] = _base_type(t)
    return binds


def _java_method_symbol(m, type_name: str, rel: str, class_binds: dict[str, str]) -> Symbol:
    name = _ast_text(_ast_field(m, "name"))
    params = _ast_param_types(_ast_field(m, "parameters"))
    mods = _ast_modifiers(m)
    body = _ast_field(m, "body")
    binds = {**class_binds,
             **_java_param_bindings(_ast_field(m, "parameters")),
             **_java_local_bindings(body)}
    throws = []
    for c in m.children:
        if c.type == "throws":
            throws = _parse_throws(_ast_text(c).removeprefix("throws"))
    if m.type == "constructor_declaration":
        return Symbol(
            name=name, kind="ctor", file=rel, line=m.start_point[0] + 1,
            signature=f"{name}({','.join(params)})", params=params,
            param_names=_java_param_names(_ast_field(m, "parameters")), returns=name,
            visibility=_ast_vis(mods),
            container=type_name, lang="java", raises=throws,
            calls=_java_calls(body, name), bindings=binds, size=_body_lines(body),
        )
    returns = _ast_text(_ast_field(m, "type"))
    ret_suffix = f":{returns}" if returns != "void" else ""
    return Symbol(
        name=name, kind="method", file=rel, line=m.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=_java_param_names(_ast_field(m, "parameters")),
        returns=returns,
        visibility=_ast_vis(mods),
        container=type_name, lang="java",
        calls=_java_calls(body, name), raises=throws, bindings=binds,
        size=_body_lines(body),
    )


def _extract_java(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["java"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, _JAVA_TYPE_NODE_KINDS):
        kind = _JAVA_TYPE_NODE_KINDS[tn.type]
        name = _ast_text(_ast_field(tn, "name"))
        body = _ast_field(tn, "body")
        header_end = body.start_byte if body is not None else tn.end_byte
        header = text.encode()[tn.start_byte:header_end].decode(errors="replace")
        supers, permits = _heritage(re.sub(r"\(.*?\)", "", header, flags=re.S))
        mods = _ast_modifiers(tn)
        params: list[str] = []
        if kind == "record":
            pnode = _ast_field(tn, "parameters")
            params = _ast_param_types(pnode) if pnode is not None else []
        elif kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(c, "name"))
                      for c in body.children if c.type == "enum_constant"]
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=(f"sealed {kind} {name}" if "sealed" in mods else f"{kind} {name}"),
            params=params,
            fields=(list(_java_class_bindings(tn)) if kind != "enum" else []),
            visibility=_ast_vis(mods), lang="java",
            supers=supers, permits=permits,
        ))
        if body is None:
            continue
        class_binds = _java_class_bindings(tn)
        containers = list(body.children)
        if kind == "enum":
            containers = [c for c in body.children if c.type == "enum_body_declarations"]
            containers = [gc for c in containers for gc in c.children] or list(body.children)
        for c in containers:
            if c.type in ("method_declaration", "constructor_declaration"):
                symbols.append(_java_method_symbol(c, name, rel, class_binds))
    return symbols


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction
# ---------------------------------------------------------------------------

_TS_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}


def _ts_exported(n) -> bool:
    return n.parent is not None and n.parent.type == "export_statement"


def _ts_params(node) -> list[str]:
    """Declared parameter types from a formal_parameters node; `?` when untyped."""
    raw = _ast_text(node)
    if raw.startswith("("):
        raw = raw[1:-1]
    types = []
    for p in _split_top_commas(raw, "<([{", ">)]}"):
        p = re.sub(r"^(private|public|protected|readonly)\s+", "", p.strip())
        p = p.split("=")[0]
        types.append(tight_type(p.split(":", 1)[1].strip()) if ":" in p else "?")
    return types


def _ts_param_names(node) -> list[str]:
    raw = _ast_text(node)
    if raw.startswith("("):
        raw = raw[1:-1]
    names: list[str] = []
    for p in _split_top_commas(raw, "<([{", ">)]}"):
        p = re.sub(r"^(private|public|protected|readonly)\s+", "", p.strip())
        p = p.split("=", 1)[0].strip().removeprefix("...")
        name = p.split(":", 1)[0].strip().rstrip("?")
        names.append(name if _IDENT_RE.fullmatch(name) else "")
    return names


def _ts_return(node) -> str | None:
    rt = _ast_field(node, "return_type")
    return tight_type(_ast_text(rt).lstrip(":").strip()) if rt is not None else None


def _ts_call_entry(n) -> tuple[str, str]:
    if n.type == "new_expression":
        entry = re.sub(r"<.*", "", _ast_text(_ast_field(n, "constructor")))
        return entry, entry
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "member_expression":
        name = _ast_text(_ast_field(fn, "property"))
        obj = _ast_field(fn, "object")
        entry = (f"{_ast_text(obj)}.{name}"
                 if obj is not None and obj.type == "identifier" else name)
        return name, entry
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    return "", ""


def _ts_calls(body, own_name: str) -> list[str]:
    return _ast_calls(body, own_name, ("call_expression", "new_expression"),
                      _ts_call_entry)


def _ts_param_bindings(params_node) -> dict[str, str]:
    binds: dict[str, str] = {}
    if params_node is None:
        return binds
    for p in params_node.children:
        if p.type in ("required_parameter", "optional_parameter"):
            pat, t = _ast_field(p, "pattern"), _ast_field(p, "type")
            if pat is not None and pat.type == "identifier" and t is not None:
                binds[_ast_text(pat)] = _base_type(_ast_text(t).lstrip(":").strip())
    return binds


def _ts_class_bindings(body) -> dict[str, str]:
    """Typed fields, including constructor parameter properties (private x: T)."""
    binds: dict[str, str] = {}
    for c in (body.children if body is not None else ()):
        if c.type == "public_field_definition":
            n, t = _ast_field(c, "name"), _ast_field(c, "type")
            if n is not None and t is not None:
                binds[_ast_text(n)] = _base_type(_ast_text(t).lstrip(":").strip())
        if c.type == "method_definition" and _ast_text(_ast_field(c, "name")) == "constructor":
            for p in (_ast_field(c, "parameters") or c).children:
                if p.type == "required_parameter" and any(
                        ch.type == "accessibility_modifier" for ch in p.children):
                    binds.update(_ts_param_bindings_one(p))
    return binds


def _ts_param_bindings_one(p) -> dict[str, str]:
    pat, t = _ast_field(p, "pattern"), _ast_field(p, "type")
    if pat is not None and pat.type == "identifier" and t is not None:
        return {_ast_text(pat): _base_type(_ast_text(t).lstrip(":").strip())}
    return {}


def _ts_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for dec in _ast_collect(body, ("variable_declarator",)):
        n, t, val = _ast_field(dec, "name"), _ast_field(dec, "type"), _ast_field(dec, "value")
        if n is None or n.type != "identifier":
            continue
        if t is not None:
            binds[_ast_text(n)] = _base_type(_ast_text(t).lstrip(":").strip())
        elif val is not None and val.type == "new_expression":
            binds[_ast_text(n)] = _base_type(_ast_text(_ast_field(val, "constructor")))
    return binds


def _ts_fn_symbol(node, rel: str, container: str | None, visibility: str,
                  class_binds: dict[str, str] | None = None,
                  name: str | None = None, fn_node=None) -> Symbol:
    """Function/method Symbol. `fn_node` carries params/body when the name lives on a
    different node (arrow assigned to a const or a class field)."""
    fn = fn_node if fn_node is not None else node
    name = name if name is not None else _ast_text(_ast_field(node, "name"))
    params = _ts_params(_ast_field(fn, "parameters"))
    returns = _ts_return(fn)
    body = _ast_field(fn, "body")
    ret_suffix = f":{returns}" if returns and returns != "void" else ""
    return Symbol(
        name=name, kind="method" if container else "fn", file=rel,
        line=node.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=_ts_param_names(_ast_field(fn, "parameters")),
        returns=returns,
        visibility=visibility, container=container, lang="typescript",
        calls=_ts_calls(body, name), size=_body_lines(body),
        bindings={**(class_binds or {}),
                  **_ts_param_bindings(_ast_field(fn, "parameters")),
                  **_ts_local_bindings(body)},
    )


_TS_FN_VALUES = ("arrow_function", "function_expression")


def _ts_top_level_arrows(root_node, rel: str) -> list[Symbol]:
    """Module-scope `const f = (…) => …` plus object-literal APIs
    (`export const api = { get(){}, … }`). Nested closures are deliberately
    excluded — only declarations at program/export level count."""
    symbols = []
    for top in root_node.children:
        exported = top.type == "export_statement"
        decls = top.children if exported else [top]
        for decl in decls:
            if decl.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for d in decl.children:
                if d.type != "variable_declarator":
                    continue
                name = _ast_text(_ast_field(d, "name"))
                val = _ast_field(d, "value")
                if val is None:
                    continue
                if val.type in _TS_FN_VALUES:
                    symbols.append(_ts_fn_symbol(
                        d, rel, None, "pub" if exported else "priv",
                        name=name, fn_node=val))
                elif val.type == "object":
                    fns = []
                    for c in val.children:
                        if c.type == "method_definition":
                            fns.append((_ast_text(_ast_field(c, "name")), c, c))
                        elif c.type == "pair":
                            v = _ast_field(c, "value")
                            if v is not None and v.type in _TS_FN_VALUES:
                                fns.append((_ast_text(_ast_field(c, "key")), c, v))
                    if not fns:
                        continue  # plain config object, not an API
                    symbols.append(Symbol(
                        name=name, kind="class", file=rel, line=d.start_point[0] + 1,
                        signature=f"const {name}",
                        visibility="pub" if exported else "priv", lang="typescript"))
                    for mname, node, fn_node in fns:
                        symbols.append(_ts_fn_symbol(
                            node, rel, name, "pub", name=mname, fn_node=fn_node))
    return symbols


def _ts_aliases_and_reexports(root_node, rel: str) -> list[Symbol]:
    symbols = []
    for al in _ast_collect(root_node, ("type_alias_declaration",)):
        target = tight_type(_ast_text(_ast_field(al, "value")))[:40]
        value = _ast_field(al, "value")
        fields = [_ast_text(_ast_field(p, "name"))
                  for p in (value.children if value is not None else ())
                  if p.type == "property_signature" and _ast_field(p, "name") is not None]
        symbols.append(Symbol(
            name=_ast_text(_ast_field(al, "name")), kind="type", file=rel,
            line=al.start_point[0] + 1,
            signature=f"type {_ast_text(_ast_field(al, 'name'))}",
            params=[target] if target else [], fields=fields,
            visibility="pub" if _ts_exported(al) else "priv", lang="typescript"))
    for ex in root_node.children:
        if ex.type != "export_statement" or _ast_field(ex, "source") is None:
            continue  # only `export … from './x'` barrels
        for spec in _ast_collect(ex, ("export_specifier",)):
            nm = _ast_field(spec, "alias") or _ast_field(spec, "name")
            if nm is not None:
                symbols.append(Symbol(
                    name=_ast_text(nm), kind="reexport", file=rel,
                    line=ex.start_point[0] + 1, signature=_ast_text(nm),
                    visibility="pub", lang="typescript"))
    return symbols


def _extract_ts(text: str, rel: str, lang: str = "typescript") -> list[Symbol]:
    tree = _PARSERS[lang].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, _TS_TYPE_NODE_KINDS):
        kind = _TS_TYPE_NODE_KINDS[tn.type]
        name = _ast_text(_ast_field(tn, "name"))
        body = _ast_field(tn, "body")
        header_end = body.start_byte if body is not None else tn.end_byte
        header = text.encode()[tn.start_byte:header_end].decode(errors="replace")
        supers, _ = _heritage(header)
        params: list[str] = []
        if kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(c, "name") or c)
                      for c in body.children
                      if c.type in ("enum_assignment", "property_identifier")]
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=params, supers=supers,
            fields=(list(_ts_class_bindings(body)) if kind == "class"
                    else [_ast_text(_ast_field(p, "name"))
                          for p in (body.children if body is not None else ())
                          if p.type == "property_signature"
                          and _ast_field(p, "name") is not None]),
            visibility="pub" if _ts_exported(tn) else "priv", lang="typescript",
        ))
        if kind == "class" and body is not None:
            class_binds = _ts_class_bindings(body)
            for c in body.children:
                if c.type == "public_field_definition":
                    val = _ast_field(c, "value")
                    if val is not None and val.type in _TS_FN_VALUES:
                        vis = "priv" if any(ch.type == "accessibility_modifier"
                                            and _ast_text(ch) == "private"
                                            for ch in c.children) else "pub"
                        symbols.append(_ts_fn_symbol(
                            c, rel, name, vis, class_binds,
                            name=_ast_text(_ast_field(c, "name")), fn_node=val))
                    continue
                if c.type != "method_definition":
                    continue
                mname = _ast_text(_ast_field(c, "name"))
                if mname == "constructor":
                    symbols.append(Symbol(
                        name=name, kind="ctor", file=rel, line=c.start_point[0] + 1,
                        signature=f"{name}({','.join(_ts_params(_ast_field(c, 'parameters')))})",
                        params=_ts_params(_ast_field(c, "parameters")),
                        param_names=_ts_param_names(_ast_field(c, "parameters")),
                        returns=name,
                        container=name, lang="typescript",
                    ))
                    continue
                vis = "priv" if any(ch.type == "accessibility_modifier"
                                    and _ast_text(ch) == "private"
                                    for ch in c.children) else "pub"
                symbols.append(_ts_fn_symbol(c, rel, name, vis, class_binds))
    for fn in _ast_collect(tree.root_node, ("function_declaration",)):
        symbols.append(_ts_fn_symbol(
            fn, rel, None, "pub" if _ts_exported(fn) else "priv"))
    symbols.extend(_ts_top_level_arrows(tree.root_node, rel))
    symbols.extend(_ts_aliases_and_reexports(tree.root_node, rel))
    return symbols


def _extract_tsx(text: str, rel: str) -> list[Symbol]:
    return _extract_ts(text, rel, "tsx")


_SFC_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)


def _extract_sfc(text: str, rel: str) -> list[Symbol]:
    """Vue/Svelte single-file components: the component itself plus everything the
    TS extractor finds inside its <script> blocks (line numbers preserved)."""
    stem = Path(rel).stem
    symbols = [Symbol(name=stem, kind="class", file=rel, line=1,
                      signature=f"component {stem}", visibility="pub",
                      lang=Path(rel).suffix.lstrip("."))]
    for m in _SFC_SCRIPT_RE.finditer(text):
        offset = text.count("\n", 0, m.start(1))
        for s in _extract_ts(m.group(1), rel):
            s.line += offset
            symbols.append(s)
    return symbols


# ---------------------------------------------------------------------------
# Go extraction
# ---------------------------------------------------------------------------

def _go_vis(name: str) -> str:
    return "pub" if name[:1].isupper() else "priv"


def _go_type_text(node) -> str:
    return tight_type(_ast_text(node).lstrip("*&"))


def _go_params(plist) -> tuple[list[str], dict[str, str]]:
    """(declared types in order, name->type bindings) from a parameter_list."""
    types: list[str] = []
    binds: dict[str, str] = {}
    if plist is None:
        return types, binds
    for p in plist.children:
        if p.type not in ("parameter_declaration", "variadic_parameter_declaration"):
            continue
        t = _go_type_text(_ast_field(p, "type"))
        if p.type == "variadic_parameter_declaration":
            t = "..." + t
        names = [c for c in p.children if c.type == "identifier"]
        for n in names or [None]:
            types.append(t)
            if n is not None:
                binds[_ast_text(n)] = _base_type(t)
    return types, binds


def _go_param_names(plist) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for p in plist.children:
        if p.type not in ("parameter_declaration", "variadic_parameter_declaration"):
            continue
        found = [c for c in p.children if c.type == "identifier"]
        names.extend(_ast_text(n) for n in found)
        if not found:
            names.append("")
    return names


def _go_result(node) -> str | None:
    res = _ast_field(node, "result")
    if res is None:
        return None
    if res.type == "parameter_list":
        types, _ = _go_params(res)
        return f"({','.join(types)})" if len(types) > 1 else (types[0] if types else None)
    return _go_type_text(res)


def _go_call_entry(n) -> tuple[str, str]:
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "selector_expression":
        name = _ast_text(_ast_field(fn, "field"))
        op = _ast_field(fn, "operand")
        entry = (f"{_ast_text(op)}.{name}"
                 if op is not None and op.type == "identifier" else name)
        return name, entry
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    return "", ""


def _go_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for d in _ast_collect(body, ("var_spec", "short_var_declaration")):
        if d.type == "var_spec":
            t = _ast_field(d, "type")
            n = _ast_field(d, "name")
            if t is not None and n is not None:
                binds[_ast_text(n)] = _base_type(_go_type_text(t))
            continue
        left, right = _ast_field(d, "left"), _ast_field(d, "right")
        if left is None or right is None:
            continue
        lids = [c for c in left.children if c.type == "identifier"]
        lits = _ast_collect(right, ("composite_literal",))
        if len(lids) == 1 and len(lits) == 1:
            binds[_ast_text(lids[0])] = _base_type(
                _go_type_text(_ast_field(lits[0], "type")))
    return binds


def _extract_go(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["go"].parse(text.encode())
    symbols: list[Symbol] = []
    struct_fields: dict[str, dict[str, str]] = {}
    for ts_node in _ast_collect(tree.root_node, ("type_spec",)):
        name = _ast_text(_ast_field(ts_node, "name"))
        tnode = _ast_field(ts_node, "type")
        if tnode is None:
            continue
        if tnode.type == "struct_type":
            components: list[str] = []
            supers: list[str] = []
            fields: dict[str, str] = {}
            for f in _ast_collect(tnode, ("field_declaration",)):
                ftype = _ast_field(f, "type")
                fnames = [c for c in f.children if c.type == "field_identifier"]
                if not fnames:  # embedded type = composition/promotion
                    supers.append(_base_type(_go_type_text(ftype)))
                    continue
                for fn in fnames:
                    components.append(_go_type_text(ftype))
                    fields[_ast_text(fn)] = _base_type(_go_type_text(ftype))
            struct_fields[name] = fields
            symbols.append(Symbol(
                name=name, kind="class", file=rel, line=ts_node.start_point[0] + 1,
                signature=f"struct {name}", params=components, fields=list(fields),
                supers=supers,
                visibility=_go_vis(name), lang="go",
            ))
        elif tnode.type == "interface_type":
            symbols.append(Symbol(
                name=name, kind="interface", file=rel,
                line=ts_node.start_point[0] + 1,
                signature=f"interface {name}",
                visibility=_go_vis(name), lang="go",
            ))
            for m in _ast_collect(tnode, ("method_elem", "method_spec")):
                mname = _ast_text(_ast_field(m, "name"))
                params, _ = _go_params(_ast_field(m, "parameters"))
                returns = _go_result(m)
                ret_suffix = f":{returns}" if returns else ""
                symbols.append(Symbol(
                    name=mname, kind="method", file=rel, line=m.start_point[0] + 1,
                    signature=f"{mname}({','.join(params)}){ret_suffix}",
                    params=params,
                    param_names=_go_param_names(_ast_field(m, "parameters")),
                    returns=returns,
                    visibility=_go_vis(mname), container=name, lang="go",
                ))
    for fn in _ast_collect(tree.root_node,
                           ("function_declaration", "method_declaration")):
        name = _ast_text(_ast_field(fn, "name"))
        container = None
        binds: dict[str, str] = {}
        if fn.type == "method_declaration":
            rtypes, rbinds = _go_params(_ast_field(fn, "receiver"))
            container = _base_type(rtypes[0]) if rtypes else None
            binds.update(rbinds)
            if container is not None:
                binds.update(struct_fields.get(container, {}))
        params, pbinds = _go_params(_ast_field(fn, "parameters"))
        binds.update(pbinds)
        body = _ast_field(fn, "body")
        binds.update(_go_local_bindings(body))
        returns = _go_result(fn)
        ret_suffix = f":{returns}" if returns else ""
        symbols.append(Symbol(
            name=name, kind="method" if container else "fn", file=rel,
            line=fn.start_point[0] + 1,
            signature=f"{name}({','.join(params)}){ret_suffix}",
            params=params, param_names=_go_param_names(_ast_field(fn, "parameters")),
            returns=returns,
            visibility=_go_vis(name), container=container, lang="go",
            calls=_ast_calls(body, name, ("call_expression",), _go_call_entry),
            size=_body_lines(body),
            bindings=binds,
        ))
    return symbols


# ---------------------------------------------------------------------------
# Rust extraction
# ---------------------------------------------------------------------------

def _rs_vis(node) -> str:
    return ("pub" if any(c.type == "visibility_modifier" for c in node.children)
            else "priv")


def _rs_params(params_node) -> tuple[list[str], dict[str, str]]:
    types: list[str] = []
    binds: dict[str, str] = {}
    if params_node is None:
        return types, binds
    for p in params_node.children:
        if p.type != "parameter":
            continue
        t = tight_type(_ast_text(_ast_field(p, "type")))
        types.append(t)
        pat = _ast_field(p, "pattern")
        if pat is not None and pat.type == "identifier":
            binds[_ast_text(pat)] = _base_type(t.lstrip("&"))
    return types, binds


def _rs_param_names(params_node) -> list[str]:
    names: list[str] = []
    for p in (params_node.children if params_node is not None else ()):
        if p.type != "parameter":
            continue
        pat = _ast_field(p, "pattern")
        names.append(_ast_text(pat) if pat is not None and pat.type == "identifier" else "")
    return names


def _rs_call_entry(n) -> tuple[str, str]:
    if n.type == "struct_expression":
        name = _base_type(_ast_text(_ast_field(n, "name")))
        return name, name
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "field_expression":
        name = _ast_text(_ast_field(fn, "field"))
        val = _ast_field(fn, "value")
        entry = (f"{_ast_text(val)}.{name}"
                 if val is not None and val.type == "identifier" else name)
        return name, entry
    if fn.type == "scoped_identifier":
        name = _ast_text(_ast_field(fn, "name"))
        path = _ast_field(fn, "path")
        prefix = _base_type(_ast_text(path).split("::")[-1]) if path is not None else ""
        return name, (f"{prefix}.{name}" if prefix else name)
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    return "", ""


def _rs_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for d in _ast_collect(body, ("let_declaration",)):
        pat = _ast_field(d, "pattern")
        if pat is None or pat.type != "identifier":
            continue
        t = _ast_field(d, "type")
        val = _ast_field(d, "value")
        if t is not None:
            binds[_ast_text(pat)] = _base_type(tight_type(_ast_text(t)).lstrip("&"))
        elif val is not None and val.type == "struct_expression":
            binds[_ast_text(pat)] = _base_type(_ast_text(_ast_field(val, "name")))
    return binds


def _rs_fn_symbol(fn, rel: str, container: str | None, vis: str,
                  extra_binds: dict[str, str] | None = None) -> Symbol:
    name = _ast_text(_ast_field(fn, "name"))
    params, binds = _rs_params(_ast_field(fn, "parameters"))
    body = _ast_field(fn, "body")
    binds = {**(extra_binds or {}), **binds, **_rs_local_bindings(body)}
    rt = _ast_field(fn, "return_type")
    returns = tight_type(_ast_text(rt)) if rt is not None else None
    ret_suffix = f":{returns}" if returns else ""
    return Symbol(
        name=name, kind="method" if container else "fn", file=rel,
        line=fn.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=_rs_param_names(_ast_field(fn, "parameters")),
        returns=returns,
        visibility=vis, container=container, lang="rust",
        calls=_ast_calls(body, name,
                         ("call_expression", "struct_expression"), _rs_call_entry),
        size=_body_lines(body),
        bindings=binds,
    )


def _extract_rust(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["rust"].parse(text.encode())
    symbols: list[Symbol] = []
    type_syms: dict[str, Symbol] = {}
    for tn in _ast_collect(tree.root_node,
                           ("struct_item", "enum_item", "trait_item")):
        name = _ast_text(_ast_field(tn, "name"))
        vis = _rs_vis(tn)
        line = tn.start_point[0] + 1
        body = _ast_field(tn, "body")
        if tn.type == "struct_item":
            field_nodes = (_ast_collect(body, ("field_declaration",))
                           if body is not None else [])
            components = [tight_type(_ast_text(_ast_field(f, "type")))
                          for f in field_nodes]
            fields = [_ast_text(_ast_field(f, "name"))
                      for f in field_nodes if _ast_field(f, "name") is not None]
            sym = Symbol(name=name, kind="class", file=rel, line=line,
                         signature=f"struct {name}", params=components,
                         fields=fields,
                         visibility=vis, lang="rust")
        elif tn.type == "enum_item":
            variants = [_ast_text(_ast_field(v, "name"))
                        for v in _ast_collect(body, ("enum_variant",))
                        ] if body is not None else []
            sym = Symbol(name=name, kind="enum", file=rel, line=line,
                         signature=f"enum {name}", params=variants,
                         visibility=vis, lang="rust")
        else:
            sym = Symbol(name=name, kind="interface", file=rel, line=line,
                         signature=f"trait {name}", visibility=vis, lang="rust")
            for m in _ast_collect(body, ("function_signature_item", "function_item")
                                  ) if body is not None else []:
                symbols.append(_rs_fn_symbol(m, rel, name, vis))
        symbols.append(sym)
        type_syms[name] = sym
    for imp in _ast_collect(tree.root_node, ("impl_item",)):
        container = _base_type(_ast_text(_ast_field(imp, "type")))
        trait = _ast_field(imp, "trait")
        if trait is not None and container in type_syms:
            type_syms[container].supers.append(_base_type(_ast_text(trait)))
        body = _ast_field(imp, "body")
        self_bind = {"self": container}
        for m in (_ast_collect(body, ("function_item",)) if body is not None else []):
            if m.parent is not None and m.parent.type != "declaration_list":
                continue  # skip fns nested inside method bodies
            symbols.append(_rs_fn_symbol(m, rel, container, _rs_vis(m), self_bind))
    for fn in tree.root_node.children:
        if fn.type == "function_item":
            symbols.append(_rs_fn_symbol(fn, rel, None, _rs_vis(fn)))
    return symbols


# ---------------------------------------------------------------------------
# C# extraction
# ---------------------------------------------------------------------------

_CS_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "struct_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


def _cs_vis(node) -> str:
    mods = [_ast_text(c) for c in node.children if c.type == "modifier"]
    return "priv" if any(m in ("private", "protected") for m in mods) else "pub"


def _cs_params(plist) -> tuple[list[str], dict[str, str]]:
    types: list[str] = []
    binds: dict[str, str] = {}
    if plist is None:
        return types, binds
    for p in plist.children:
        if p.type != "parameter":
            continue
        t = tight_type(_ast_text(_ast_field(p, "type")))
        types.append(t)
        n = _ast_field(p, "name")
        if n is not None:
            binds[_ast_text(n)] = _base_type(t)
    return types, binds


def _cs_param_names(plist) -> list[str]:
    names: list[str] = []
    for p in (plist.children if plist is not None else ()):
        if p.type != "parameter":
            continue
        name = _ast_field(p, "name")
        names.append(_ast_text(name) if name is not None else "")
    return names


def _cs_call_entry(n) -> tuple[str, str]:
    if n.type == "object_creation_expression":
        entry = _base_type(_ast_text(_ast_field(n, "type")))
        return entry, entry
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "member_access_expression":
        name = _ast_text(_ast_field(fn, "name"))
        expr = _ast_field(fn, "expression")
        entry = (f"{_ast_text(expr)}.{name}"
                 if expr is not None and expr.type == "identifier" else name)
        return name, entry
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    return "", ""


def _cs_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for d in _ast_collect(body, ("variable_declaration",)):
        t = _ast_text(_ast_field(d, "type"))
        for dec in _ast_collect(d, ("variable_declarator",)):
            n = _ast_field(dec, "name")
            if n is None:
                continue
            if t == "var":
                lits = _ast_collect(dec, ("object_creation_expression",))
                if len(lits) == 1:
                    binds[_ast_text(n)] = _base_type(
                        _ast_text(_ast_field(lits[0], "type")))
            else:
                binds[_ast_text(n)] = _base_type(t)
    return binds


def _extract_cs(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["csharp"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, _CS_TYPE_NODE_KINDS):
        kind = _CS_TYPE_NODE_KINDS[tn.type]
        name = _ast_text(_ast_field(tn, "name"))
        body = _ast_field(tn, "body")
        supers = []
        for c in tn.children:
            if c.type == "base_list":
                supers = [_base_type(_ast_text(b)) for b in c.children
                          if b.type in ("identifier", "generic_name",
                                        "qualified_name")]
        params: list[str] = []
        type_fields: list[str] = []
        if kind == "record":
            for c in tn.children:
                if c.type == "parameter_list":
                    params, record_binds = _cs_params(c)
                    type_fields.extend(record_binds)
        elif kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(m, "name"))
                      for m in _ast_collect(body, ("enum_member_declaration",))]
        for member in (body.children if body is not None else ()):
            if member.type == "field_declaration":
                type_fields.extend(_cs_local_bindings(member))
            elif member.type == "property_declaration":
                field_name = _ast_field(member, "name")
                if field_name is not None:
                    type_fields.append(_ast_text(field_name))
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=params, fields=list(dict.fromkeys(type_fields)),
            supers=supers,
            visibility=_cs_vis(tn), lang="csharp",
        ))
        if body is None:
            continue
        class_binds: dict[str, str] = {}
        for f in body.children:
            if f.type == "field_declaration":
                class_binds.update(_cs_local_bindings(f))
        for m in body.children:
            if m.type not in ("method_declaration", "constructor_declaration"):
                continue
            mname = _ast_text(_ast_field(m, "name"))
            mparams, pbinds = _cs_params(_ast_field(m, "parameters"))
            mbody = _ast_field(m, "body")
            binds = {**class_binds, **pbinds, **_cs_local_bindings(mbody)}
            calls = _ast_calls(mbody, mname,
                               ("invocation_expression",
                                "object_creation_expression"), _cs_call_entry)
            if m.type == "constructor_declaration":
                symbols.append(Symbol(
                    name=mname, kind="ctor", file=rel, line=m.start_point[0] + 1,
                    signature=f"{mname}({','.join(mparams)})",
                    params=mparams,
                    param_names=_cs_param_names(_ast_field(m, "parameters")),
                    returns=mname,
                    visibility=_cs_vis(m), container=name, lang="csharp",
                    calls=calls, bindings=binds,
                ))
                continue
            returns = tight_type(_ast_text(_ast_field(m, "returns")))
            ret_suffix = f":{returns}" if returns and returns != "void" else ""
            symbols.append(Symbol(
                name=mname, kind="method", file=rel, line=m.start_point[0] + 1,
                signature=f"{mname}({','.join(mparams)}){ret_suffix}",
                params=mparams,
                param_names=_cs_param_names(_ast_field(m, "parameters")),
                returns=returns,
                visibility=_cs_vis(m), container=name, lang="csharp",
                calls=calls, bindings=binds, size=_body_lines(mbody),
            ))
    return symbols


# ---------------------------------------------------------------------------
# Kotlin extraction
# ---------------------------------------------------------------------------

def _kt_vis(node) -> str:
    for m in node.children:
        if m.type == "modifiers" and any(
                _ast_text(c) in ("private", "protected") for c in m.children):
            return "priv"
    return "pub"


def _kt_params(pnode) -> tuple[list[str], dict[str, str]]:
    """Types + name bindings from function_value_parameters / class_parameters."""
    types: list[str] = []
    binds: dict[str, str] = {}
    if pnode is None:
        return types, binds
    for p in pnode.children:
        if p.type not in ("parameter", "class_parameter"):
            continue
        idents = [c for c in p.children if c.type == "identifier"]
        tnodes = [c for c in p.children
                  if c.type in ("user_type", "nullable_type", "function_type")]
        if not idents or not tnodes:
            continue
        t = tight_type(_ast_text(tnodes[-1]))
        types.append(t)
        binds[_ast_text(idents[0])] = _base_type(t.rstrip("?"))
    return types, binds


def _kt_param_names(pnode) -> list[str]:
    names: list[str] = []
    for p in (pnode.children if pnode is not None else ()):
        if p.type not in ("parameter", "class_parameter"):
            continue
        ident = next((c for c in p.children if c.type == "identifier"), None)
        names.append(_ast_text(ident) if ident is not None else "")
    return names


def _kt_return(fn) -> str | None:
    """Return type: the user_type sibling between the parameter list and the body."""
    seen_params = False
    for c in fn.children:
        if c.type == "function_value_parameters":
            seen_params = True
        elif seen_params and c.type in ("user_type", "nullable_type"):
            return tight_type(_ast_text(c))
        elif c.type == "function_body":
            break
    return None


def _kt_call_entry(n) -> tuple[str, str]:
    head = n.children[0] if n.children else None
    if head is None:
        return "", ""
    if head.type in ("identifier", "simple_identifier"):
        name = _ast_text(head)
        return name, name
    if head.type == "navigation_expression":
        parts = _ast_text(head).split(".")
        name = parts[-1]
        if len(parts) == 2 and _IDENT_RE.fullmatch(parts[0]):
            return name, f"{parts[0]}.{name}"
        return name, name
    return "", ""


def _kt_fn_symbol(fn, rel: str, container: str | None, vis: str,
                  class_binds: dict[str, str]) -> Symbol:
    name = _ast_text(_ast_field(fn, "name"))
    pnode = next((c for c in fn.children if c.type == "function_value_parameters"), None)
    params, binds = _kt_params(pnode)
    body = next((c for c in fn.children if c.type == "function_body"), None)
    returns = _kt_return(fn)
    ret_suffix = f":{returns}" if returns and returns != "Unit" else ""
    return Symbol(
        name=name, kind="method" if container else "fn", file=rel,
        line=fn.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=_kt_param_names(pnode), returns=returns,
        visibility=vis, container=container, lang="kotlin",
        calls=_ast_calls(body, name, ("call_expression",), _kt_call_entry),
        bindings={**class_binds, **binds},
        size=(body.end_point[0] - body.start_point[0] + 1) if body is not None else 0,
    )


def _extract_kotlin(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["kotlin"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node,
                           ("class_declaration", "object_declaration")):
        name = _ast_text(_ast_field(tn, "name"))
        if not name:
            continue
        is_iface = any(c.type == "interface" for c in tn.children)
        is_enum = any(_ast_text(m) == "enum"
                      for m in _ast_collect(tn, ("class_modifier",)))
        is_data = any(_ast_text(m) == "data"
                      for m in _ast_collect(tn, ("class_modifier",)))
        ctor = next((c for c in tn.children if c.type == "primary_constructor"), None)
        cparams: list[str] = []
        class_binds: dict[str, str] = {}
        pl = None
        if ctor is not None:
            pl = next((c for c in ctor.children if c.type == "class_parameters"), None)
            cparams, class_binds = _kt_params(pl)
        class_fields = []
        if ctor is not None and pl is not None:
            class_fields = [_ast_text(next(c for c in p.children if c.type == "identifier"))
                            for p in pl.children
                            if p.type == "class_parameter"
                            and re.search(r"\b(?:val|var)\b", _ast_text(p))
                            and any(c.type == "identifier" for c in p.children)]
        supers = []
        for ds in tn.children:
            if ds.type == "delegation_specifiers":
                supers = [_base_type(re.sub(r"\(.*", "", _ast_text(d)))
                          for d in ds.children if d.type == "delegation_specifier"]
        body = next((c for c in tn.children
                     if c.type in ("class_body", "enum_class_body")), None)
        for prop in (body.children if body is not None else ()):
            if prop.type == "property_declaration":
                ident = next((c for c in prop.children if c.type == "variable_declaration"), None)
                if ident is not None:
                    name_node = next((c for c in ident.children if c.type == "identifier"), None)
                    if name_node is not None:
                        class_fields.append(_ast_text(name_node))
        if is_enum and body is not None:
            cparams = [_ast_text(e.children[0])
                       for e in _ast_collect(body, ("enum_entry",)) if e.children]
        kind = ("enum" if is_enum else "interface" if is_iface
                else "record" if is_data else "class")
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=cparams,
            fields=[] if is_enum else list(dict.fromkeys(class_fields)), supers=supers,
            visibility=_kt_vis(tn), lang="kotlin",
        ))
        if body is not None:
            for fn in body.children:
                if fn.type == "function_declaration":
                    vis = _kt_vis(fn) if not is_iface else _kt_vis(tn)
                    symbols.append(_kt_fn_symbol(fn, rel, name, vis, class_binds))
    for fn in tree.root_node.children:
        if fn.type == "function_declaration":
            symbols.append(_kt_fn_symbol(fn, rel, None, _kt_vis(fn), {}))
    return symbols


# ---------------------------------------------------------------------------
# C / C++ extraction (shared declarator machinery)
# ---------------------------------------------------------------------------

def _c_fn_declarator(node):
    """(function_declarator, name_node) beneath a definition/declaration, peeling
    pointer/reference wrappers. name_node may be identifier/field_identifier/
    qualified_identifier."""
    fd = None
    n = _ast_field(node, "declarator")
    while n is not None:
        if n.type == "function_declarator":
            fd = n
            n = _ast_field(n, "declarator")
        elif n.type in ("pointer_declarator", "reference_declarator"):
            n = _ast_field(n, "declarator")
        else:
            break
    return fd, n


def _c_params(plist) -> tuple[list[str], dict[str, str]]:
    types: list[str] = []
    binds: dict[str, str] = {}
    if plist is None:
        return types, binds
    for p in plist.children:
        if p.type != "parameter_declaration":
            continue
        base = tight_type(_ast_text(_ast_field(p, "type")))
        d = _ast_field(p, "declarator")
        stars = _ast_text(d).count("*") if d is not None else 0
        types.append(base + "*" * stars)
        while d is not None and d.type in ("pointer_declarator", "reference_declarator"):
            d = _ast_field(d, "declarator")
        if d is not None and d.type == "identifier":
            binds[_ast_text(d)] = _base_type(base)
    return types, binds


def _c_param_names(plist) -> list[str]:
    names: list[str] = []
    for p in (plist.children if plist is not None else ()):
        if p.type != "parameter_declaration":
            continue
        d = _ast_field(p, "declarator")
        while d is not None and d.type in ("pointer_declarator", "reference_declarator",
                                            "array_declarator"):
            d = _ast_field(d, "declarator")
        names.append(_ast_text(d) if d is not None
                     and d.type in ("identifier", "field_identifier") else "")
    return names


def _c_field_names(body) -> list[str]:
    names: list[str] = []
    for declaration in (body.children if body is not None else ()):
        if declaration.type != "field_declaration":
            continue
        fd, _ = _c_fn_declarator(declaration)
        if fd is not None:
            continue
        for node in _ast_collect(declaration, ("field_identifier", "identifier")):
            name = _ast_text(node)
            if name not in names:
                names.append(name)
    return names


def _c_call_entry(n) -> tuple[str, str]:
    if n.type == "new_expression":
        entry = _base_type(_ast_text(_ast_field(n, "type")))
        return entry, entry
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    if fn.type == "field_expression":
        name = _ast_text(_ast_field(fn, "field"))
        obj = _ast_field(fn, "argument")
        entry = (f"{_ast_text(obj)}.{name}"
                 if obj is not None and obj.type == "identifier" else name)
        return name, entry
    if fn.type == "qualified_identifier":
        name = _ast_text(_ast_field(fn, "name"))
        scope = _ast_field(fn, "scope")
        return name, (f"{_base_type(_ast_text(scope))}.{name}"
                      if scope is not None else name)
    return "", ""


def _c_static(node) -> bool:
    return any(c.type == "storage_class_specifier" and _ast_text(c) == "static"
               for c in node.children)


def _c_enum_symbol(tn, name: str, rel: str, lang: str) -> Symbol:
    body = _ast_field(tn, "body")
    values = [_ast_text(_ast_field(e, "name"))
              for e in (_ast_collect(body, ("enumerator",)) if body is not None else [])]
    return Symbol(name=name, kind="enum", file=rel, line=tn.start_point[0] + 1,
                  signature=f"enum {name}", params=values, visibility="pub", lang=lang)


def _extract_c(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["c"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, ("struct_specifier", "enum_specifier",
                                            "type_definition")):
        if tn.type == "type_definition":
            inner = _ast_field(tn, "type")
            alias = _ast_text(_ast_field(tn, "declarator"))
            if inner is None or _ast_field(inner, "body") is None or not alias:
                continue
            if inner.type == "enum_specifier":
                symbols.append(_c_enum_symbol(inner, alias, rel, "c"))
            elif inner.type == "struct_specifier":
                comps = [tight_type(_ast_text(_ast_field(f, "type")))
                         for f in _ast_collect(inner, ("field_declaration",))]
                symbols.append(Symbol(
                    name=alias, kind="class", file=rel, line=tn.start_point[0] + 1,
                    signature=f"struct {alias}", params=comps,
                    fields=_c_field_names(_ast_field(inner, "body")),
                    visibility="pub", lang="c"))
            continue
        name_node = _ast_field(tn, "name")
        if name_node is None or _ast_field(tn, "body") is None:
            continue
        name = _ast_text(name_node)
        if tn.type == "enum_specifier":
            symbols.append(_c_enum_symbol(tn, name, rel, "c"))
        else:
            comps = [tight_type(_ast_text(_ast_field(f, "type")))
                     for f in _ast_collect(_ast_field(tn, "body"),
                                           ("field_declaration",))]
            symbols.append(Symbol(
                name=name, kind="class", file=rel, line=tn.start_point[0] + 1,
                signature=f"struct {name}", params=comps,
                fields=_c_field_names(_ast_field(tn, "body")),
                visibility="pub", lang="c"))
    defined_fns: set[str] = set()
    for fn in _ast_collect(tree.root_node, ("function_definition",)):
        fd, name_node = _c_fn_declarator(fn)
        if fd is None or name_node is None or name_node.type != "identifier":
            continue
        name = _ast_text(name_node)
        defined_fns.add(name)
        params, binds = _c_params(_ast_field(fd, "parameters"))
        returns = tight_type(_ast_text(_ast_field(fn, "type")))
        body = _ast_field(fn, "body")
        symbols.append(Symbol(
            name=name, kind="fn", file=rel, line=fn.start_point[0] + 1,
            signature=f"{name}({','.join(params)})"
                      + (f":{returns}" if returns != "void" else ""),
            params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
            returns=returns,
            visibility="priv" if _c_static(fn) else "pub", lang="c",
            calls=_ast_calls(body, name, ("call_expression",), _c_call_entry),
            size=_body_lines(body),
            bindings=binds,
        ))
    for decl in tree.root_node.children:  # top-level prototypes (headers)
        if decl.type != "declaration":
            continue
        fd, name_node = _c_fn_declarator(decl)
        if fd is None or name_node is None or name_node.type != "identifier":
            continue
        name = _ast_text(name_node)
        if name in defined_fns:
            continue
        params, _ = _c_params(_ast_field(fd, "parameters"))
        returns = tight_type(_ast_text(_ast_field(decl, "type")))
        symbols.append(Symbol(
            name=name, kind="fn", file=rel, line=decl.start_point[0] + 1,
            signature=f"{name}({','.join(params)})"
                      + (f":{returns}" if returns != "void" else ""),
            params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
            returns=returns,
            visibility="priv" if _c_static(decl) else "pub", lang="c",
        ))
    return symbols


def _extract_cpp(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["cpp"].parse(text.encode())
    symbols: list[Symbol] = []
    members: dict[tuple[str, str], Symbol] = {}
    for tn in _ast_collect(tree.root_node, ("class_specifier", "struct_specifier",
                                            "enum_specifier")):
        name_node = _ast_field(tn, "name")
        body = _ast_field(tn, "body")
        if name_node is None or body is None:
            continue
        cname = _ast_text(name_node)
        if tn.type == "enum_specifier":
            symbols.append(_c_enum_symbol(tn, cname, rel, "cpp"))
            continue
        comps: list[str] = []
        supers = []
        for c in tn.children:
            if c.type == "base_class_clause":
                supers = [_base_type(_ast_text(b)) for b in c.children
                          if b.type in ("type_identifier", "qualified_identifier")]
        access = "private" if tn.type == "class_specifier" else "public"
        type_sym = Symbol(
            name=cname, kind="class", file=rel, line=tn.start_point[0] + 1,
            signature=f"class {cname}", supers=supers, visibility="pub", lang="cpp")
        symbols.append(type_sym)
        for m in body.children:
            if m.type == "access_specifier":
                access = _ast_text(m).rstrip(":")
                continue
            fd, name_node = _c_fn_declarator(m)
            if m.type in ("function_definition", "declaration", "field_declaration") \
                    and fd is not None and name_node is not None:
                mname = _ast_text(name_node)
                params, binds = _c_params(_ast_field(fd, "parameters"))
                rtype = _ast_field(m, "type")
                returns = tight_type(_ast_text(rtype)) if rtype is not None else None
                mbody = _ast_field(m, "body")
                kind = "ctor" if mname == cname else "method"
                ret_suffix = (f":{returns}"
                              if kind == "method" and returns and returns != "void"
                              else "")
                sym = Symbol(
                    name=mname, kind=kind, file=rel, line=m.start_point[0] + 1,
                    signature=f"{mname}({','.join(params)}){ret_suffix}",
                    params=params,
                    param_names=_c_param_names(_ast_field(fd, "parameters")),
                    returns=returns or (cname if kind == "ctor" else None),
                    visibility="pub" if access == "public" else "priv",
                    container=cname, lang="cpp",
                    calls=_ast_calls(mbody, mname,
                                     ("call_expression", "new_expression"),
                                     _c_call_entry),
                    bindings=binds, size=_body_lines(mbody),
                )
                symbols.append(sym)
                members[(cname, mname)] = sym
            elif m.type == "field_declaration" and fd is None:
                t = _ast_field(m, "type")
                if t is not None:
                    comps.append(tight_type(_ast_text(t)))
        type_sym.params = comps
        type_sym.fields = _c_field_names(body)
    for fn in _ast_collect(tree.root_node, ("function_definition",)):
        fd, name_node = _c_fn_declarator(fn)
        if fd is None or name_node is None:
            continue
        body = _ast_field(fn, "body")
        if name_node.type == "qualified_identifier":  # out-of-line member def
            container = _base_type(_ast_text(_ast_field(name_node, "scope")))
            mname = _ast_text(_ast_field(name_node, "name"))
            calls = _ast_calls(body, mname, ("call_expression", "new_expression"),
                               _c_call_entry)
            existing = members.get((container, mname))
            if existing is not None:
                if not existing.calls:
                    existing.calls = calls
                continue
            params, binds = _c_params(_ast_field(fd, "parameters"))
            rtype = _ast_field(fn, "type")
            returns = tight_type(_ast_text(rtype)) if rtype is not None else None
            symbols.append(Symbol(
                name=mname, kind="method", file=rel, line=fn.start_point[0] + 1,
                signature=f"{mname}({','.join(params)})"
                          + (f":{returns}" if returns and returns != "void" else ""),
                params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
                returns=returns,
                visibility="pub", container=container, lang="cpp",
                calls=calls, bindings=binds,
            ))
        elif name_node.type == "identifier" and fn.parent is not None \
                and fn.parent.type in ("translation_unit", "namespace_definition",
                                       "declaration_list"):
            name = _ast_text(name_node)
            params, binds = _c_params(_ast_field(fd, "parameters"))
            returns = tight_type(_ast_text(_ast_field(fn, "type")))
            symbols.append(Symbol(
                name=name, kind="fn", file=rel, line=fn.start_point[0] + 1,
                signature=f"{name}({','.join(params)})"
                          + (f":{returns}" if returns != "void" else ""),
                params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
                returns=returns,
                visibility="priv" if _c_static(fn) else "pub", lang="cpp",
                calls=_ast_calls(body, name, ("call_expression", "new_expression"),
                                 _c_call_entry),
                bindings=binds,
            ))
    return symbols


# ---------------------------------------------------------------------------
# Lua extraction
# ---------------------------------------------------------------------------

def _lua_call_entry(n) -> tuple[str, str]:
    fn = _ast_field(n, "name")
    if fn is None:
        return "", ""
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    if fn.type in ("dot_index_expression", "method_index_expression"):
        field = _ast_field(fn, "field") or _ast_field(fn, "method")
        table = _ast_field(fn, "table")
        name = _ast_text(field)
        entry = (f"{_ast_text(table)}.{name}"
                 if table is not None and table.type == "identifier" else name)
        return name, entry
    return "", ""


def _extract_lua(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["lua"].parse(text.encode())
    symbols: list[Symbol] = []
    for fn in _ast_collect(tree.root_node, ("function_declaration",)):
        name_node = _ast_field(fn, "name")
        if name_node is None:
            continue
        container = None
        is_local = any(c.type == "local" for c in fn.children)
        if name_node.type == "identifier":
            name = _ast_text(name_node)
        elif name_node.type in ("dot_index_expression", "method_index_expression"):
            field = _ast_field(name_node, "field") or _ast_field(name_node, "method")
            name = _ast_text(field)
            container = _ast_text(_ast_field(name_node, "table"))
        else:
            continue
        pnode = _ast_field(fn, "parameters")
        params = [_ast_text(c) for c in (pnode.children if pnode is not None else [])
                  if c.type == "identifier"]
        symbols.append(Symbol(
            name=name, kind="method" if container else "fn", file=rel,
            line=fn.start_point[0] + 1,
            signature=f"{name}({','.join(params)})", params=params,
            param_names=params,
            visibility="priv" if is_local or name.startswith("_") else "pub",
            container=container, lang="lua",
            calls=_ast_calls(_ast_field(fn, "body"), name,
                             ("function_call",), _lua_call_entry),
            size=_body_lines(_ast_field(fn, "body")),
        ))
    return symbols


# ---------------------------------------------------------------------------
# Bash extraction (functions with command-call chains; params are positional)
# ---------------------------------------------------------------------------

def _bash_call_entry(node) -> tuple[str, str]:
    name = _ast_text(_ast_field(node, "name"))
    return name, name


def _extract_bash(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["bash"].parse(text.encode())
    symbols: list[Symbol] = []
    for fn in _ast_collect(tree.root_node, ("function_definition",)):
        name = _ast_text(_ast_field(fn, "name"))
        if not name:
            continue
        body = _ast_field(fn, "body")
        symbols.append(Symbol(
            name=name, kind="fn", file=rel,
            line=fn.start_point[0] + 1, signature=f"{name}()",
            visibility="priv" if name.startswith("_") else "pub",
            lang="bash",
            calls=_ast_calls(body, name, ("command",), _bash_call_entry),
            size=_body_lines(body),
        ))
    return symbols


# ---------------------------------------------------------------------------
# CSS extraction (selectors, custom properties, keyframes — names only)
# ---------------------------------------------------------------------------

def _css_symbols(text: str, rel: str, offset: int = 0) -> list[Symbol]:
    tree = _PARSERS["css"].parse(text.encode())
    symbols: list[Symbol] = []
    seen: set[str] = set()

    def add(name: str, node):
        if name in seen:
            return
        seen.add(name)
        symbols.append(Symbol(
            name=name, kind="fn", file=rel,
            line=node.start_point[0] + 1 + offset, signature=name,
            visibility="priv", lang="css"))

    for sel in _ast_collect(tree.root_node, ("class_selector", "id_selector")):
        if sel.type == "class_selector":
            names = [c for c in sel.children if c.type == "class_name"]
            if names:
                add(f".{_ast_text(names[-1])}", sel)
        else:
            add(f"#{_ast_text(_ast_field(sel, 'name') or sel.children[-1])}", sel)
    for decl in _ast_collect(tree.root_node, ("declaration",)):
        prop = next((c for c in decl.children if c.type == "property_name"), None)
        if prop is not None and (p := _ast_text(prop)).startswith("--"):
            add(p, decl)
    for kf in _ast_collect(tree.root_node, ("keyframes_statement",)):
        name = next((c for c in kf.children if c.type == "keyframes_name"), None)
        if name is not None:
            add(f"@{_ast_text(name)}", kf)
    return symbols


def _extract_css(text: str, rel: str) -> list[Symbol]:
    return _css_symbols(text, rel)


# ---------------------------------------------------------------------------
# HTML extraction (ids, custom elements, and nested <script>/<style> blocks)
# ---------------------------------------------------------------------------

def _extract_html(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["html"].parse(text.encode())
    symbols: list[Symbol] = []
    seen: set[str] = set()
    for attr in _ast_collect(tree.root_node, ("attribute",)):
        parts = [c for c in attr.children]
        if not parts or _ast_text(parts[0]) != "id":
            continue
        vals = _ast_collect(attr, ("attribute_value",))
        if vals and (v := _ast_text(vals[0])) and f"#{v}" not in seen:
            seen.add(f"#{v}")
            symbols.append(Symbol(
                name=f"#{v}", kind="fn", file=rel,
                line=attr.start_point[0] + 1, signature=f"#{v}",
                visibility="priv", lang="html"))
    for tag in _ast_collect(tree.root_node, ("tag_name",)):
        t = _ast_text(tag)
        if "-" in t and t not in seen:  # custom element
            seen.add(t)
            symbols.append(Symbol(
                name=t, kind="fn", file=rel, line=tag.start_point[0] + 1,
                signature=t, visibility="priv", lang="html"))
    # Nested code blocks are best-effort: extracted only when that grammar is
    # installed, so a missing optional parser degrades the map, never the build.
    for el in _ast_collect(tree.root_node, ("script_element", "style_element")):
        raw = next((c for c in el.children if c.type == "raw_text"), None)
        if raw is None:
            continue
        nested_lang = "typescript" if el.type == "script_element" else "css"
        if not has_parser(nested_lang):
            continue
        offset = raw.start_point[0]
        if nested_lang == "css":
            nested = _css_symbols(_ast_text(raw), rel, offset)
        else:
            nested = _extract_ts(_ast_text(raw), rel)
            for s in nested:
                s.line += offset
        symbols.extend(n for n in nested if n.name not in seen)
        seen.update(n.name for n in nested)
    return symbols


# ---------------------------------------------------------------------------
# Helm extraction (no grammar: define names, values keys, chart name)
# ---------------------------------------------------------------------------

_HELM_DEFINE_RE = re.compile(r'\{\{-?\s*define\s+"([^"]+)"')


def _extract_helm(text: str, rel: str) -> list[Symbol]:
    """Only fires inside a chart layout (templates/, Chart.yaml, values.yaml) so
    ordinary YAML (CI configs, k8s manifests) stays out of the digest."""
    parts = Path(rel).parts
    base = Path(rel).name
    in_chart = "templates" in parts or base in ("Chart.yaml", "values.yaml")
    if not in_chart:
        return []
    symbols: list[Symbol] = []
    if base == "Chart.yaml":
        m = re.search(r"(?m)^name:\s*(\S+)", text)
        if m:
            symbols.append(Symbol(
                name=m.group(1), kind="class", file=rel,
                line=text.count("\n", 0, m.start()) + 1,
                signature=f"chart {m.group(1)}", visibility="pub", lang="helm"))
    elif base == "values.yaml":
        for m in re.finditer(r"(?m)^([A-Za-z_][\w-]*):", text):
            symbols.append(Symbol(
                name=m.group(1), kind="fn", file=rel,
                line=text.count("\n", 0, m.start()) + 1,
                signature=m.group(1), visibility="priv", lang="helm"))
    for m in _HELM_DEFINE_RE.finditer(text):
        symbols.append(Symbol(
            name=m.group(1), kind="fn", file=rel,
            line=text.count("\n", 0, m.start()) + 1,
            signature=f'define "{m.group(1)}"', visibility="pub", lang="helm"))
    return symbols


# ---------------------------------------------------------------------------
# Python extraction (stdlib ast — precise and dependency-free)
# ---------------------------------------------------------------------------

def _py_param_facts(node: ast.FunctionDef | ast.AsyncFunctionDef
                    ) -> tuple[list[str], list[str]]:
    types: list[str] = []
    names: list[str] = []

    def add(arg: ast.arg | None, prefix: str = "") -> None:
        if arg is None or arg.arg in ("self", "cls"):
            return
        names.append(prefix + arg.arg)
        types.append(tight_type(ast.unparse(arg.annotation)) if arg.annotation else "?")

    for arg in node.args.posonlyargs + node.args.args:
        add(arg)
    add(node.args.vararg, "*")
    for arg in node.args.kwonlyargs:
        add(arg)
    add(node.args.kwarg, "**")
    return types, names


def _py_calls(node) -> list[str]:
    seen: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                entry = fn.id
            elif isinstance(fn, ast.Attribute):
                base = fn.value
                entry = (f"{base.id}.{fn.attr}"
                         if isinstance(base, ast.Name) else fn.attr)
            else:
                continue
            if entry not in seen:
                seen.append(entry)
    return seen


def _py_raises(node) -> list[str]:
    seen: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            target = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
            if name and name not in seen:
                seen.append(name)
    return seen


def _py_bindings(node) -> dict[str, str]:
    """Annotated params plus `x = Ctor(...)` locals (Ctor = capitalized name)."""
    binds: dict[str, str] = {}
    for arg in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])):
        if arg.annotation is not None and arg.arg not in ("self", "cls"):
            binds[arg.arg] = _base_type(ast.unparse(arg.annotation))
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id[:1].isupper()):
            binds[sub.targets[0].id] = sub.value.func.id
        elif (isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name)):
            binds[sub.target.id] = _base_type(ast.unparse(sub.annotation))
    return binds


def _py_fn_symbol(node, rel: str, container: str | None) -> Symbol:
    returns = tight_type(ast.unparse(node.returns)) if node.returns else None
    params, param_names = _py_param_facts(node)
    ret_suffix = f":{returns}" if returns and returns != "None" else ""
    return Symbol(
        name=node.name, kind="method" if container else "fn", file=rel,
        line=node.lineno,
        signature=f"{node.name}({','.join(params)}){ret_suffix}",
        params=params, param_names=param_names, returns=returns,
        visibility="priv" if node.name.startswith("_") else "pub",
        container=container, lang="python",
        calls=[c for c in _py_calls(node) if c != node.name],
        raises=_py_raises(node),
        bindings=_py_bindings(node),
        size=(getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
    )


def _extract_python(text: str, rel: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            base_names = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                          for b in node.bases}
            is_enum = base_names & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
            members = [t.targets[0].id for t in node.body
                       if isinstance(t, ast.Assign) and len(t.targets) == 1
                       and isinstance(t.targets[0], ast.Name)] if is_enum else []
            field_types = [ast.unparse(t.annotation) for t in node.body
                           if isinstance(t, ast.AnnAssign)]
            field_names = [t.target.id for t in node.body
                           if isinstance(t, ast.AnnAssign)
                           and isinstance(t.target, ast.Name)]
            field_names.extend(
                t.targets[0].id for t in node.body
                if isinstance(t, ast.Assign) and len(t.targets) == 1
                and isinstance(t.targets[0], ast.Name) and not is_enum
            )
            supers = [] if is_enum else [
                re.sub(r"\[.*", "", ast.unparse(b)).split(".")[-1] for b in node.bases]
            symbols.append(Symbol(
                name=node.name, kind="enum" if is_enum else "class", file=rel,
                line=node.lineno,
                signature=f"class {node.name}",
                params=members if is_enum else field_types,
                fields=[] if is_enum else list(dict.fromkeys(field_names)),
                supers=supers,
                visibility="priv" if node.name.startswith("_") else "pub",
                lang="python",
            ))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_py_fn_symbol(sub, rel, node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_py_fn_symbol(node, rel, None))
    return symbols


EXTRACTORS = {
    "java": _extract_java,
    "python": _extract_python,
    "typescript": _extract_ts,
    "javascript": _extract_ts,
    "tsx": _extract_tsx,
    "vue": _extract_sfc,
    "svelte": _extract_sfc,
    "kotlin": _extract_kotlin,
    "go": _extract_go,
    "rust": _extract_rust,
    "csharp": _extract_cs,
    "c": _extract_c,
    "cpp": _extract_cpp,
    "lua": _extract_lua,
    "html": _extract_html,
    "helm": _extract_helm,
    "bash": _extract_bash,
    "css": _extract_css,
}


def extract_file(path: Path, root: Path, text: str | None = None) -> list[Symbol]:
    lang = detect_language(path)
    if lang is None:
        return []
    extractor = EXTRACTORS.get(lang)
    if extractor is None:
        return []
    if lang in _GRAMMAR_MODULES and not has_parser(lang):
        raise SystemExit(f"{lang} extraction requires tree-sitter: "
                         f"pip install {' '.join(_grammar_pkgs([lang]))}")
    if text is None:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return []
    return extractor(text, str(path.relative_to(root)))


# ---------------------------------------------------------------------------
# Gather + fan-in
# ---------------------------------------------------------------------------

def _generator_fingerprint() -> bytes:
    """Tool bytes make old rendering/extraction logic stale for every target repo."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).digest()
    except OSError:
        return b"hologram"


def _new_state_hash():
    state = hashlib.md5()
    state.update(_generator_fingerprint())
    return state


def _gather(root: Path, langs: set[str] | None = None):
    """Extract symbols, identifier-token sets per file, and the corpus state hash.
    `langs` restricts to those languages (e.g. {"java"}); None means all."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    symbols: list[Symbol] = []
    file_tokens: dict[str, set[str]] = {}
    usage_tokens: Counter[str] = Counter()
    state = _new_state_hash()
    for f in files:
        rel = str(f.relative_to(root))
        raw = f.read_bytes()
        state.update(rel.encode())
        state.update(hashlib.md5(raw).digest())
        text = raw.decode(errors="replace")
        symbols.extend(extract_file(f, root, text))
        identifiers = _IDENT_RE.findall(strip_comments_and_strings(text))
        file_tokens[rel] = set(identifiers)
        usage_tokens.update(identifiers)
        # The string stripper necessarily removes f-string expressions.  Restore
        # Python identifier/attribute reads from the AST without counting comments,
        # ordinary string contents, or declaration names.
        if detect_language(f) == "python":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                pass
            else:
                usage_tokens.update(
                    node.id for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                )
                usage_tokens.update(
                    node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                )
    return files, symbols, file_tokens, usage_tokens, state.hexdigest()[:12]


def _state_hash(root: Path, langs: set[str] | None = None) -> str:
    """The corpus hash `_gather` would produce, without parsing anything — cheap
    freshness probe for `check` / `--if-stale`."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    state = _new_state_hash()
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        state.update(str(f.relative_to(root)).encode())
        state.update(hashlib.md5(raw).digest())
    return state.hexdigest()[:12]


def _digest_state(digest: str) -> str | None:
    """The `state` stamp recorded in a digest's header line, if any."""
    m = re.search(r"· state (\w{12})", digest.split("\n", 1)[0])
    return m.group(1) if m else None


def _zero_usage_names(symbols: list[Symbol], usage_tokens: Counter[str]) -> set[str]:
    """Code functions/classes with no statically observed project reference."""
    declarations = Counter(s.name for s in symbols if s.kind != "reexport")
    return {
        s.name for s in symbols
        if s.kind in ("fn", "method", "class")
        and s.lang not in ("html", "helm")
        and not (s.name.startswith("__") and s.name.endswith("__"))
        and usage_tokens[s.name] <= declarations[s.name]
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F",
               "type": "T"}


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _is_test_path(rel: str) -> bool:
    path = Path(rel)
    parts = [p.casefold() for p in path.parts[:-1]]
    raw_stem = path.stem
    stem = raw_stem.casefold()
    return (any(p in ("test", "tests", "__tests__") for p in parts)
            or stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))
            or raw_stem.endswith(("Test", "Tests", "Spec", "IT")))


def _tree_lines(payload_by_dir: dict[str, list[str]]) -> list[str]:
    """Render dir paths as a path-compressed trie: shared prefixes stated once.
    Payload lines carry their own relative indent; the trie adds depth indent."""
    tree: dict = {}
    for d in sorted(payload_by_dir):
        node = tree
        for part in Path(d).parts:
            node = node.setdefault(part, {})
        node.setdefault("\0", []).extend(payload_by_dir[d])

    out: list[str] = []

    def emit(node: dict, label: str | None, depth: int) -> None:
        children = {k: v for k, v in node.items() if k != "\0"}
        payload = node.get("\0", [])
        while label is not None and len(children) == 1 and not payload:
            (k, child), = children.items()
            label = f"{label}/{k}"
            payload = child.get("\0", [])
            children = {kk: vv for kk, vv in child.items() if kk != "\0"}
        base = depth
        if label is not None:
            out.append(" " * depth + label)
            base = depth + 1
        for ln in payload:
            out.append(" " * base + ln)
        for k in sorted(children):
            emit(children[k], k, base)

    emit(tree, None, 0)
    return out


def _strip_exc(name: str) -> str:
    return name.removesuffix("Exception") or name


_BOILERPLATE_PARTS = ("src", "main", "java", "kotlin", "test", "tests", "lib")


def _dep_lines(symbols: list[Symbol], file_tokens: dict[str, set[str]],
               min_refs: int = 2) -> list[str]:
    """Module dependency edges (`a→b` = code in a references types defined in b),
    from data already in hand. Modules are top path segments after boilerplate
    and the corpus-wide shared prefix."""
    type_dir: dict[str, str] = {}
    for s in symbols:
        if s.kind in TYPE_KINDS and not _is_test_path(s.file):
            type_dir.setdefault(s.name, str(Path(s.file).parent))
    dirs = {str(Path(rel).parent) for rel in file_tokens} | set(type_dir.values())
    stripped = {d: [p for p in Path(d).parts if p not in _BOILERPLATE_PARTS]
                for d in dirs}
    common: list[str] = []
    lists = [p for p in stripped.values() if p]
    while lists and all(len(p) > len(common) + 1 for p in lists) \
            and len({p[len(common)] for p in lists}) == 1:
        common.append(lists[0][len(common)])

    def label(d: str) -> str:
        parts = stripped[d]
        if common and parts[:len(common)] == common and len(parts) > len(common):
            parts = parts[len(common):]
        return parts[0] if parts else "."

    counts: dict[tuple[str, str], int] = {}
    for rel, toks in file_tokens.items():
        if _is_test_path(rel):
            continue
        m_from = label(str(Path(rel).parent))
        for t in toks & set(type_dir):
            m_to = label(type_dir[t])
            if m_from != m_to:
                counts[(m_from, m_to)] = counts.get((m_from, m_to), 0) + 1
    by_src: dict[str, list[str]] = {}
    for (a, b), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= min_refs:
            by_src.setdefault(a, []).append(b)
    cells = [f"{a}→{','.join(bs)}" for a, bs in sorted(by_src.items())]
    lines, cur = [], ""
    for c in cells:
        if cur and len(cur) + len(c) + 3 > 110:
            lines.append(f"· deps {cur}")
            cur = c
        else:
            cur = f"{cur} | {c}" if cur else c
    if cur:
        lines.append(f"· deps {cur}")
    return lines


def _total_loc(files: list[Path]) -> int:
    loc = 0
    for f in files:
        try:
            loc += len(f.read_text(errors="replace").splitlines())
        except OSError:
            pass
    return loc


def _symbol_identity(symbol: Symbol) -> tuple[str, str, str, str, int]:
    return (symbol.file, symbol.lang, symbol.container or "", symbol.name, symbol.line)


def _target_descriptions(targets: list[Symbol]) -> dict[int, str]:
    """Shortest stable project-wide name that identifies each call target."""
    by_name = Counter(s.name for s in targets)
    qualified = {id(s): (f"{s.container}.{s.name}" if s.container else s.name)
                 for s in targets}
    by_qualified = Counter(qualified.values())
    stemmed = {id(s): f"{Path(s.file).stem}.{qualified[id(s)]}" for s in targets}
    by_stemmed = Counter(stemmed.values())
    out: dict[int, str] = {}
    for symbol in targets:
        if by_name[symbol.name] == 1:
            out[id(symbol)] = symbol.name
        elif by_qualified[qualified[id(symbol)]] == 1:
            out[id(symbol)] = qualified[id(symbol)]
        elif by_stemmed[stemmed[id(symbol)]] == 1:
            out[id(symbol)] = stemmed[id(symbol)]
        else:
            out[id(symbol)] = f"{symbol.file}:{qualified[id(symbol)]}"
    return out


def _resolved_project_calls(symbols: list[Symbol]
                            ) -> tuple[dict[int, list[str]], set[int]]:
    """Resolve raw calls to project symbols; omit external and ambiguous targets.

    Returns display calls by caller identity plus production targets called by tests.
    """
    production = [s for s in symbols if not _is_test_path(s.file)]
    targets = [s for s in production if s.kind in TYPE_KINDS + ("fn", "method")]
    types = [s for s in targets if s.kind in TYPE_KINDS]
    callables = [s for s in targets if s.kind in ("fn", "method")]

    type_index: dict[tuple[str, str], list[Symbol]] = {}
    method_index: dict[tuple[str, str, str], list[Symbol]] = {}
    file_top_index: dict[tuple[str, str, str], list[Symbol]] = {}
    module_top_index: dict[tuple[str, str, str], list[Symbol]] = {}
    lang_name_index: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in types:
        type_index.setdefault((symbol.lang, symbol.name), []).append(symbol)
    for symbol in callables:
        lang_name_index.setdefault((symbol.lang, symbol.name), []).append(symbol)
        if symbol.container:
            method_index.setdefault(
                (symbol.lang, symbol.container, symbol.name), []).append(symbol)
        else:
            file_top_index.setdefault(
                (symbol.lang, symbol.file, symbol.name), []).append(symbol)
            module_top_index.setdefault(
                (symbol.lang, Path(symbol.file).stem, symbol.name), []).append(symbol)

    def one(items: list[Symbol] | None) -> Symbol | None:
        return items[0] if items is not None and len(items) == 1 else None

    same_container_languages = {
        "java", "typescript", "javascript", "tsx", "vue", "svelte",
        "csharp", "kotlin", "cpp", "rust",
    }

    def resolve(caller: Symbol, raw: str) -> Symbol | None:
        receiver, dot, name = raw.rpartition(".")
        if not dot:
            name = raw
            target_type = one(type_index.get((caller.lang, name)))
            if target_type is not None:
                return target_type
            if caller.container and caller.lang in same_container_languages:
                target = one(method_index.get((caller.lang, caller.container, name)))
                if target is not None:
                    return target
            target = one(file_top_index.get((caller.lang, caller.file, name)))
            if target is not None:
                return target
            return one(lang_name_index.get((caller.lang, name)))

        if receiver in ("self", "cls", "this") and caller.container:
            return one(method_index.get((caller.lang, caller.container, name)))
        if receiver in caller.bindings:
            owner = _base_type(caller.bindings[receiver])
            return one(method_index.get((caller.lang, owner, name)))
        owner = receiver.rsplit(".", 1)[-1]
        if (caller.lang, owner) in type_index:
            return one(method_index.get((caller.lang, owner, name)))
        module = receiver.rsplit(".", 1)[-1]
        return one(module_top_index.get((caller.lang, module, name)))

    raw_targets: dict[int, list[Symbol]] = {}
    for caller in symbols:
        found: list[Symbol] = []
        seen: set[tuple[str, str, str, str, int]] = set()
        for raw in caller.calls:
            target = resolve(caller, raw)
            if target is None:
                continue
            key = _symbol_identity(target)
            if key not in seen:
                seen.add(key)
                found.append(target)
        raw_targets[id(caller)] = found

    public_callers = {
        _symbol_identity(s): s for s in production
        if s.kind in ("fn", "method") and s.visibility == "pub"
    }
    adjacency = {
        key: [_symbol_identity(t) for t in raw_targets[id(caller)]
              if t.kind in ("fn", "method")]
        for key, caller in public_callers.items()
    }

    def reaches(source: tuple[str, str, str, str, int],
                target: tuple[str, str, str, str, int],
                seen: set[tuple[str, str, str, str, int]] | None = None) -> bool:
        if source not in public_callers:
            return False
        seen = set() if seen is None else seen
        if source in seen:
            return False
        seen.add(source)
        for child in adjacency.get(source, ()):
            if child == target or reaches(child, target, seen):
                return True
        return False

    descriptions = _target_descriptions(targets)
    displayed: dict[int, list[str]] = {}
    for caller in symbols:
        found = raw_targets[id(caller)]
        if caller.visibility == "pub" and not _is_test_path(caller.file):
            reduced: list[Symbol] = []
            for target in found:
                target_key = _symbol_identity(target)
                implied = any(
                    other is not target
                    and reaches(_symbol_identity(other), target_key)
                    and not reaches(target_key, _symbol_identity(other))
                    for other in found
                )
                if not implied:
                    reduced.append(target)
            found = reduced
        displayed[id(caller)] = [descriptions[id(target)] for target in found]

    tested = {
        id(target)
        for caller in symbols if _is_test_path(caller.file)
        for target in raw_targets[id(caller)]
    }
    return displayed, tested


_PRIVATE_SEPARATORS = "_./-"


def _factored_name_tokens(names: list[str]) -> list[str]:
    """Losslessly factor repeated identifier prefixes when bytes strictly shrink."""
    ordered = list(dict.fromkeys(names))
    remaining = set(range(len(ordered)))
    groups: list[tuple[int, str, list[int]]] = []
    while True:
        candidates: dict[str, list[int]] = {}
        for index in remaining:
            base = ordered[index].removesuffix("×0")
            for pos, char in enumerate(base):
                prefix = base[:pos + 1]
                if (char in _PRIVATE_SEPARATORS
                        and any(c not in _PRIVATE_SEPARATORS for c in prefix)):
                    candidates.setdefault(prefix, []).append(index)
        choices: list[tuple[int, int, str, list[int]]] = []
        for prefix, indexes in candidates.items():
            indexes = sorted(set(indexes))
            if len(indexes) < 3:
                continue
            plain = ",".join(ordered[i] for i in indexes)
            compact = prefix + "{" + ",".join(
                ordered[i][len(prefix):] for i in indexes) + "}"
            saving = len(plain.encode()) - len(compact.encode())
            if saving > 0:
                choices.append((saving, len(prefix), prefix, indexes))
        if not choices:
            break
        saving, _, prefix, indexes = min(
            choices, key=lambda item: (-item[0], -item[1], item[2]))
        del saving
        groups.append((min(indexes), prefix, indexes))
        remaining.difference_update(indexes)
    tokens = [(index, ordered[index]) for index in sorted(remaining)]
    for first, prefix, indexes in groups:
        tokens.append((first, prefix + "{" + ",".join(
            ordered[index][len(prefix):] for index in indexes) + "}"))
    return [value for _, value in sorted(tokens)]


def _factored_names(names: list[str]) -> str:
    return ",".join(_factored_name_tokens(names))


def _private_lines(prefix: str, names: list[str], width: int = 120) -> list[str]:
    """Wrap factored names only between independently reconstructable tokens."""
    lines: list[str] = []
    continuation = " " * len(prefix)
    current = prefix
    for token in _factored_name_tokens(names):
        candidate = current + ("," if current.strip() != prefix.strip() else "") + token
        if len(candidate) > width and current != prefix:
            lines.append(current + ",")
            current = continuation + token
        else:
            current = candidate
    if current != prefix:
        lines.append(current)
    return lines


def _braced_lines(label: str, names: list[str], width: int = 120) -> list[str]:
    if not names:
        return [label]
    prefix = label + "{"
    continuation = " " * len(prefix)
    lines: list[str] = []
    current = prefix
    for name in names:
        candidate = current + ("," if current != prefix else "") + name
        if len(candidate) + 1 > width and current != prefix:
            lines.append(current + ",")
            current = continuation + name
        else:
            current = candidate
    lines.append(current + "}")
    return lines


def _test_index_lines(files: list[Path], symbols: list[Symbol], root: Path) -> list[str]:
    test_paths = sorted(str(path.relative_to(root)) for path in files
                        if _is_test_path(str(path.relative_to(root))))
    if not test_paths:
        return []
    classes: dict[str, list[str]] = {}
    for symbol in sorted(symbols, key=lambda s: (s.file, s.line, s.name)):
        if _is_test_path(symbol.file) and symbol.kind == "class":
            names = classes.setdefault(symbol.file, [])
            if symbol.name not in names:
                names.append(symbol.name)
    first_parts = {Path(path).parts[0] for path in test_paths if Path(path).parts}
    strip_first = (len(first_parts) == 1
                   and next(iter(first_parts)).casefold() in ("test", "tests", "__tests__"))
    payloads: dict[str, list[str]] = {}
    for path in test_paths:
        display = Path(*Path(path).parts[1:]) if strip_first else Path(path)
        payloads.setdefault(str(display.parent), []).extend(
            _braced_lines(display.name, classes.get(path, [])))
    return ["? tests", *(" " + line for line in _tree_lines(payloads))]


def render_simple(root: Path, symbols: list[Symbol], files: list[Path],
                  state: str = "",
                  deps: list[str] | None = None,
                  zero_usage: set[str] | None = None) -> str:
    """Compact project facts as a package trie.

    pkg
      Class(K{fields})
        sig > callee, callee
        - privateName,privateName
    """
    prod = [s for s in symbols if not _is_test_path(s.file)]
    if zero_usage is None:
        zero_usage = set()
    resolved_calls, resolved_tested = _resolved_project_calls(symbols)
    tested = resolved_tested
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if (s.kind in TYPE_KINDS + ("fn",) and s.container is None
                and s.visibility == "pub"):
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    # owner keys carry lang so same-named types from different languages in one
    # dir (Pricer in go + rust) don't merge their method lists
    methods_by_owner: dict[tuple[str, str, str], list[Symbol]] = {}
    for s in prod:
        if (s.container and s.kind == "method"
                and s.visibility == "pub"):
            methods_by_owner.setdefault(
                (str(Path(s.file).parent), s.container, s.lang), []).append(s)
    # Lossless names-only private inventory.
    priv_methods_by_owner: dict[tuple[str, str, str], list[str]] = {}
    priv_top_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.visibility != "priv":
            continue
        marked = s.kind in ("fn", "method", "class") and s.name in zero_usage
        name = f"{s.name}×0" if marked else s.name
        if s.container and s.kind in ("method", "ctor"):
            owner_key = (str(Path(s.file).parent), s.container, s.lang)
            priv_methods_by_owner.setdefault(owner_key, []).append(name)
        elif s.container is None and s.kind in TYPE_KINDS + ("fn",):
            file_key = (str(Path(s.file).parent), Path(s.file).name)
            priv_top_by_file.setdefault(file_key, []).append(name)

    def _norm(text: str, own: str) -> str:
        return re.sub(rf"\b{re.escape(own)}\b", "⟨X⟩", text)

    def _argument_names(sym: Symbol) -> list[str]:
        return [sym.param_names[index] if index < len(sym.param_names)
                and sym.param_names[index] else param
                for index, param in enumerate(sym.params)]

    signature_shapes = Counter(
        (s.file, s.container or "", s.name, tuple(_argument_names(s)))
        for s in prod if s.kind in ("fn", "method")
    )
    top_locations: dict[str, set[tuple[str, str]]] = {}
    for symbol in prod:
        if (symbol.container is None and symbol.visibility == "pub"
                and symbol.kind in TYPE_KINDS + ("fn",)):
            top_locations.setdefault(symbol.name, set()).add((symbol.file, symbol.lang))

    def _top_display(sym: Symbol) -> str:
        if len(top_locations.get(sym.name, ())) > 1:
            return f"{Path(sym.file).name}:{sym.name}"
        return sym.name

    def _display_signature(sym: Symbol, display_name: str | None = None) -> str:
        args = _argument_names(sym)
        shape = (sym.file, sym.container or "", sym.name, tuple(args))
        if signature_shapes[shape] > 1:
            args = [f"{name}:{sym.params[index]}" if name != sym.params[index] else name
                    for index, name in enumerate(args)]
        returns = (f":{sym.returns}" if sym.returns
                   and sym.returns not in ("void", "Unit", "None") else "")
        if sym.signature and "(" not in sym.signature and sym.lang in ("helm", "html"):
            return sym.signature
        return f"{display_name or sym.name}({','.join(args)}){returns}"

    def _sig_line(sym: Symbol, own: str, grouped: bool,
                  display_name: str | None = None) -> str:
        sig = _display_signature(sym, display_name)
        if sym.size >= 40:
            sig = f"{sig} ⋮{sym.size}"
        if id(sym) in tested:
            sig = f"{sig} ✓"
        if sym.kind in ("fn", "method") and sym.name in zero_usage:
            sig = f"{sig} ×0"
        kept = resolved_calls.get(id(sym), [])
        if sym.raises:
            sig = f"{sig} !{','.join(_strip_exc(r) for r in sym.raises)}"
        if grouped:
            kept = [_norm(c, own) for c in kept]
            sig = _norm(sig, own)
        return f"{sig} > {','.join(kept)}" if kept else sig

    payload_by_dir: dict[str, list[str]] = {}
    for d, types in sorted(types_by_dir.items()):
        payload = payload_by_dir.setdefault(d, [])
        groups: dict[tuple, list[Symbol]] = {}
        for t in sorted(types, key=lambda s: (s.kind == "fn", s.name)):
            if t.kind == "fn":
                payload.append(_sig_line(t, t.name, False, _top_display(t)))
                continue
            components = (t.params if t.kind == "enum" else
                          t.params[:1] if t.kind == "type" and not t.fields else
                          t.fields or t.params)
            unused = t.kind == "class" and t.name in zero_usage
            group_key = (t.lang, t.kind, t.visibility, tuple(components), tuple(t.supers),
                         tuple(t.permits), unused, bool(t.fields))
            groups.setdefault(group_key, []).append(t)
        for (_, kind, vis, components, supers, permits, unused, named_fields), members \
                in groups.items():
            members.sort(key=lambda s: s.name)
            names = ",".join(_top_display(m) for m in members)
            letter = KIND_LETTER.get(kind, "?")
            if kind == "type" and components and not named_fields:
                inner = f"{letter}:{components[0]}"
            elif components:
                inner = f"{letter}{{{','.join(components)}}}"
            else:
                inner = letter
            permit_suffix = f" sealed:{'|'.join(permits)}" if permits else ""
            rel_suffix = f" : {','.join(supers)}" if supers else ""
            hot_suffix = " ×0" if unused else ""
            payload.append(f"{names}({inner}){rel_suffix}{permit_suffix}{hot_suffix}")
            # Methods shared by every member print once (⟨X⟩-normalized); each
            # member's remaining methods print on its own `Name: …` line.
            member_methods = {id(m): methods_by_owner.get((d, m.name, m.lang), [])
                              for m in members}
            head_member = members[0]
            def _priv_lines(m: Symbol, prefix: str = "", directory: str = d
                            ) -> list[str]:
                names_only = priv_methods_by_owner.get((directory, m.name, m.lang))
                if not names_only:
                    return []
                return _private_lines(f" {prefix}- ", names_only)

            if len(members) == 1:
                for ms in member_methods[id(head_member)]:
                    payload.append(" " + _sig_line(ms, head_member.name, False))
                payload.extend(_priv_lines(head_member))
                continue
            normed = {id(m): [_sig_line(ms, m.name, True)
                              for ms in member_methods[id(m)]] for m in members}
            shared = set.intersection(*(set(v) for v in normed.values()))
            emitted: set[str] = set()
            for line in normed[id(head_member)]:
                if line in shared and line not in emitted:
                    payload.append(" " + line)
                    emitted.add(line)
            for m in members:
                extras = [ms for ms, ln in zip(member_methods[id(m)], normed[id(m)])
                          if ln not in shared]
                if extras:
                    payload.append(f" {m.name}: "
                                   + "; ".join(_sig_line(ms, m.name, False)
                                               for ms in extras))
                payload.extend(_priv_lines(m, f"{m.name} "))

    for (d, stem), names_only in sorted(priv_top_by_file.items()):
        payload_by_dir.setdefault(d, []).extend(
            _private_lines(f"- {stem}: ", names_only))
    public_owners = {(d, member.name, member.lang)
                     for d, types in types_by_dir.items() for member in types
                     if member.kind in TYPE_KINDS}
    for (d, owner, lang), names_only in sorted(priv_methods_by_owner.items()):
        if (d, owner, lang) not in public_owners:
            payload_by_dir.setdefault(d, []).extend(
                _private_lines(f"- {owner}: ", names_only))
    reex_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "reexport":
            reexport_key = (str(Path(s.file).parent), Path(s.file).name)
            names_r = reex_by_file.setdefault(reexport_key, [])
            if s.name not in names_r:
                names_r.append(s.name)
    for (d, fname), names_r in sorted(reex_by_file.items()):
        payload_by_dir.setdefault(d, []).append(f"» {fname}: {','.join(names_r)}")

    loc = _total_loc(files)
    state_part = f" · state {state}" if state else ""
    header = (f"# hologram · {loc:,} LOC{state_part}\n"
              "· C/R/I{fields} E{values} T:target · f(args):Ret > project calls · "
              "-=private · ?=tests · ×0=no static use · ✓=tested · ⋮N=lines · "
              "!E=throws · p{a,b}=pa,pb\n")
    dep_part = ("\n".join(deps) + "\n") if deps else ""
    body = _tree_lines(payload_by_dir)
    tests = _test_index_lines(files, symbols, root)
    if tests:
        body.extend(tests)
    return header + dep_part + "\n".join(body) + "\n"


def build_digest(root: Path, langs: set[str] | None = None) -> str:
    files, symbols, file_tokens, usage_tokens, state = _gather(root, langs)
    deps = _dep_lines(symbols, file_tokens)
    return render_simple(root, symbols, files, state=state, deps=deps,
                         zero_usage=_zero_usage_names(symbols, usage_tokens))


# ---------------------------------------------------------------------------
# Embed: put the digest INSIDE the agent's context files so every session starts
# with the whole map in context — push, not pull; no retrieval decision to lose.
# ---------------------------------------------------------------------------

_EMBED_START = "<!-- hologram:start — generated, do not edit; refreshed by git hooks -->"
_EMBED_END = "<!-- hologram:end -->"


_EMBED_NOTE = (
    "This is a hologram map of this repository: a deterministic, always-current "
    "index of its public callables and their signatures, type fields, "
    "project-internal call chains, private identifiers, and test locations — "
    "the shape of the code without its bodies. Read it before exploring: it says "
    "what exists and where, so you can find the helper that already does the job, "
    "extend the conventions in place, and open the right file first. It says "
    "nothing about whether that code is correct. Line 2 is the notation legend."
)


def _embed_block(digest: str) -> str:
    return (f"{_EMBED_START}\n{_EMBED_NOTE}\n\n```\n{digest.rstrip()}\n```\n"
            f"{_EMBED_END}")


def _block_span(existing: str) -> tuple[int, int] | None:
    """Offsets of the managed block, or None. The end marker is located *after* the
    start one, so prose that mentions a marker before the block can't misplace it."""
    start = existing.find(_EMBED_START)
    if start < 0:
        return None
    end = existing.find(_EMBED_END, start + len(_EMBED_START))
    if end < 0:
        return None
    return start, end + len(_EMBED_END)


def embedded_digest(path: Path) -> str:
    """The digest text inside a context file's managed block, "" when there is none."""
    try:
        existing = path.read_text(errors="replace")
    except OSError:
        return ""
    span = _block_span(existing)
    if span is None:
        return ""
    body = existing[span[0] + len(_EMBED_START):span[1] - len(_EMBED_END)]
    m = re.search(r"```\n(.*?)\n```", body, re.S)
    return m.group(1) if m else ""


def embed_digest(path: Path, digest: str) -> None:
    """Insert or refresh one exact, non-degraded digest block in a context file,
    preserving hand-written content around it."""
    block = _embed_block(digest)
    existing = path.read_text() if path.exists() else _seed_content(path)
    span = _block_span(existing)
    if span is not None:
        updated = existing[:span[0]] + block + existing[span[1]:]
    else:
        sep = "\n\n" if existing.strip() else ""
        updated = existing.rstrip("\n") + sep + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated)


# Context files of the popular coding agents. Files are only touched when they
# already exist; rule *directories* get one managed file of ours. When a repo has
# none of them, CLAUDE.md is created.
CONTEXT_FILES = (
    "CLAUDE.md",                        # Claude Code
    "AGENTS.md",                        # Codex, opencode, Amp, Jules, Zed
    "GEMINI.md",                        # Gemini CLI
    "QWEN.md",                          # Qwen Code
    ".clinerules",                       # Cline (single-file form)
    ".cursorrules",                      # Cursor (legacy single-file form)
    ".windsurfrules",                    # Windsurf (legacy single-file form)
    ".roorules",                         # Roo Code (single-file form)
    ".rules",                            # Zed / generic
    ".github/copilot-instructions.md",   # GitHub Copilot
)

CONTEXT_DIRS = (
    (".clinerules", "hologram.md"),
    (".cursor/rules", "hologram.mdc"),
    (".roo/rules", "hologram.md"),
    (".windsurf/rules", "hologram.md"),
    (".github/instructions", "hologram.instructions.md"),
)

_SEEDS = {
    ".mdc": "---\ndescription: hologram project map\nalwaysApply: true\n---\n",
    ".instructions.md": "---\napplyTo: '**'\n---\n",
}


def _seed_content(path: Path) -> str:
    """Front matter a newly created rule file needs to be picked up by its agent."""
    for suffix, seed in _SEEDS.items():
        if path.name.endswith(suffix):
            return seed
    return ""


def context_targets(root: Path) -> list[Path]:
    """Every agent context file in `root` to attach the map to. Falls back to
    CLAUDE.md when the repo has no agent context file yet."""
    targets = [root / rel for rel in CONTEXT_FILES if (root / rel).is_file()]
    targets += [root / rel / name for rel, name in CONTEXT_DIRS
                if (root / rel).is_dir()]
    return targets or [root / "CLAUDE.md"]


# ---------------------------------------------------------------------------
# Self-bootstrap: get tree-sitter grammars without manual setup
# ---------------------------------------------------------------------------

def _venv_python() -> Path:
    return Path(__file__).resolve().parent / ".venv" / "bin" / "python"


def _missing_parser_langs(files: list[Path]) -> set[str]:
    """Languages present in `files` that need a tree-sitter parser we don't have."""
    return {l for l in {detect_language(f) for f in files}
            if l in _GRAMMAR_MODULES and not has_parser(l)}


def _venv_has_grammars(venv_py: Path, langs: set[str]) -> bool:
    mods = sorted({_GRAMMAR_MODULES[l][0] for l in langs})
    r = subprocess.run([str(venv_py), "-c", "import " + ",".join(mods)],
                       capture_output=True)
    return r.returncode == 0


def _bootstrap_or_die(missing: set[str], argv: list[str]) -> None:
    """Make parsers for `missing` available: re-exec into the tool's venv when it has
    the grammars, else offer to create the venv and pip-install them (interactive only).
    On success the process is replaced; otherwise exits with manual instructions."""
    venv_py = _venv_python()
    venv_dir = venv_py.parent.parent
    script = str(Path(__file__).resolve())
    pkgs = _grammar_pkgs(missing)
    manual = (f"missing tree-sitter parser for: {', '.join(sorted(missing))}\n"
              f"install with: python3 -m venv {venv_dir} && "
              f"{venv_py} -m pip install {' '.join(pkgs)}")

    def _reexec() -> None:
        os.environ["HOLOGRAM_BOOTSTRAPPED"] = "1"
        os.execv(str(venv_py), [str(venv_py), script, *argv])

    if os.environ.get("HOLOGRAM_BOOTSTRAPPED"):
        raise SystemExit(manual)  # second attempt failed too; don't exec-loop
    # NB: compare unresolved paths — the venv python is a symlink to the base
    # interpreter, but only invoking it via the venv path selects the venv's packages.
    if (venv_py.exists() and Path(sys.executable) != venv_py
            and _venv_has_grammars(venv_py, missing)):
        _reexec()
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise SystemExit(manual)
    reply = input(f"hologram: no parser for {', '.join(sorted(missing))}; "
                  f"install {' '.join(pkgs)} into {venv_dir}? [Y/n] ")
    if reply.strip().lower() not in ("", "y", "yes"):
        raise SystemExit(manual)
    if not venv_py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    subprocess.run([str(venv_py), "-m", "pip", "install", "--quiet", *pkgs], check=True)
    _reexec()


# ---------------------------------------------------------------------------
# CLI: build / init (self-installing git hooks)
# ---------------------------------------------------------------------------

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout")
_HOOK_MARKER = "# hologram:managed"


def _hook_python() -> str:
    """The tool's own venv python when present (tree-sitter grammars), else python3."""
    venv_py = _venv_python()
    return str(venv_py) if venv_py.exists() else "python3"


def _managed_hook_line(line: str, script: Path, repo: Path) -> bool:
    """Recognize exact hook commands generated by current and older hologram."""
    executable = r'(?:"[^"\n]+"|\S+)'
    pattern = (
        rf'^{executable} "{re.escape(str(script))}" build --root '
        rf'"{re.escape(str(repo.resolve()))}"(?: --lang \S+)*'
        rf'(?: --(?:no-)?embed)? --quiet \|\| true'
        rf'(?: {re.escape(_HOOK_MARKER)})?$'
    )
    return re.fullmatch(pattern, line) is not None


def _install_hooks(repo: Path, quiet: bool, langs: set[str] | None = None) -> None:
    script = Path(__file__).resolve()
    lang_args = "".join(f' --lang {l}' for l in sorted(langs)) if langs else ""
    hook_line = (f'"{_hook_python()}" "{script}" build --root "{repo.resolve()}"'
                 f'{lang_args} --quiet || true {_HOOK_MARKER}\n')
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        if hook.exists():
            content = hook.read_text()
            lines = content.splitlines()
            managed = [i for i, line in enumerate(lines)
                       if _managed_hook_line(line, script, repo)]
            if managed:
                first = managed[0]
                lines[first] = hook_line.rstrip("\n")
                lines = [line for i, line in enumerate(lines)
                         if i == first or i not in managed]
                hook.write_text("\n".join(lines) + "\n")
            else:
                hook.write_text(content.rstrip("\n") + "\n" + hook_line)
        else:
            hook.write_text("#!/bin/sh\n" + hook_line)
        hook.chmod(0o755)
    if not quiet:
        print(f"hooks installed: {', '.join(HOOK_NAMES)}")


def run_cli(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path.cwd())
    common.add_argument("--lang", action="append", default=None,
                        help="restrict to language(s), repeatable or comma-separated "
                             "(java, python, typescript, javascript)")
    common.add_argument("--quiet", action="store_true")

    parser = argparse.ArgumentParser(prog="hologram", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", parents=[common],
                             help="(re)generate the map embedded in CLAUDE.md")
    p_build.add_argument("--if-stale", action="store_true",
                         help="skip the rebuild when the embedded map's state stamp "
                              "matches the current sources")
    sub.add_parser("init", parents=[common],
                   help="install git hooks, then build")
    sub.add_parser("check", parents=[common],
                   help="exit 0 if the embedded map is fresh, 1 if stale or missing")
    p_diff = sub.add_parser("diff", parents=[common],
                            help="diff the map against another git revision")
    p_diff.add_argument("rev", nargs="?", default="HEAD~1")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    langs = None
    if getattr(args, "lang", None):
        langs = {l.strip() for arg in args.lang for l in arg.split(",") if l.strip()}
    targets = context_targets(root)
    state = _state_hash(root, langs)
    stale = [t for t in targets if _digest_state(embedded_digest(t)) != state]

    if args.cmd == "check":
        if not args.quiet:
            for t in targets:
                mark = "stale or missing" if t in stale else "fresh"
                print(f"{t.relative_to(root)}: {mark}")
        return 1 if stale else 0
    if args.cmd == "build" and args.if_stale and not stale:
        if not args.quiet:
            print("fresh, skipping rebuild")
        return 0

    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    missing = _missing_parser_langs(files)
    if missing:
        _bootstrap_or_die(missing, argv if argv is not None else sys.argv[1:])

    if args.cmd == "diff":
        with tempfile.TemporaryDirectory(prefix="hologram-diff-") as tmp:
            wt = Path(tmp) / "wt"
            r = subprocess.run(
                ["git", "-C", str(root), "worktree", "add", "--detach", "-f",
                 str(wt), args.rev],
                capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"git worktree failed: {r.stderr.strip()}")
            try:
                old = build_digest(wt, langs=langs)
                new = build_digest(root, langs=langs)
            finally:
                subprocess.run(["git", "-C", str(root), "worktree", "remove",
                                "--force", str(wt)], capture_output=True)
        body_old = old.splitlines()[2:]  # drop state-bearing header + notation line
        body_new = new.splitlines()[2:]
        for ln in difflib.unified_diff(body_old, body_new, fromfile=args.rev,
                                       tofile="worktree", lineterm=""):
            print(ln)
        return 0

    if args.cmd == "init":
        _install_hooks(root, args.quiet, langs)
    digest = build_digest(root, langs=langs)
    for t in targets:
        embed_digest(t, digest)
    if not args.quiet:
        names = ", ".join(str(t.relative_to(root)) for t in targets)
        print(f"hologram: {estimate_tokens(digest)} tokens embedded in {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
