#!/usr/bin/env python3
"""hologram: compress a codebase into a single markdown signature listing for LLM sessions.

Deterministic. One layout: a path-compressed package trie of public signatures,
each function's project-internal calls inline after `>`.

Extraction is AST-based everywhere: tree-sitter for Java and TypeScript/JavaScript,
stdlib `ast` for Python.
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
from dataclasses import dataclass, field
from datetime import date
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
    ".vue": "vue",
    ".svelte": "svelte",
    ".yaml": "helm",
    ".yml": "helm",
    ".tpl": "helm",
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
    returns: str | None = None
    visibility: str = "pub"
    container: str | None = None
    lang: str = ""
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

_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
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
    from tree_sitter import Language as _TSLanguage, Parser as _TSParser
except ImportError:
    _TSLanguage = _TSParser = None


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
    """Called names in source order, receiver-qualified, deduped, capped at 12."""
    if body is None:
        return []
    seen: list[str] = []
    for n in _ast_collect(body, call_kinds):
        name, entry = entry_fn(n)
        if not name or name == own_name or entry in seen:
            continue
        seen.append(entry)
    return seen[:12]



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
            signature=f"{name}({','.join(params)})", params=params, returns=name,
            visibility=_ast_vis(mods),
            container=type_name, lang="java", raises=throws,
            calls=_java_calls(body, name), bindings=binds, size=_body_lines(body),
        )
    returns = _ast_text(_ast_field(m, "type"))
    ret_suffix = f":{returns}" if returns != "void" else ""
    return Symbol(
        name=name, kind="method", file=rel, line=m.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, returns=returns,
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
        params=params, returns=returns,
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
        symbols.append(Symbol(
            name=_ast_text(_ast_field(al, "name")), kind="type", file=rel,
            line=al.start_point[0] + 1,
            signature=f"type {_ast_text(_ast_field(al, 'name'))}",
            params=[target] if target else [],
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
                        params=_ts_params(_ast_field(c, "parameters")), returns=name,
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
                signature=f"struct {name}", params=components, supers=supers,
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
                    params=params, returns=returns,
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
            params=params, returns=returns,
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
        params=params, returns=returns,
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
            components = [tight_type(_ast_text(_ast_field(f, "type")))
                          for f in _ast_collect(body, ("field_declaration",))
                          ] if body is not None else []
            sym = Symbol(name=name, kind="class", file=rel, line=line,
                         signature=f"struct {name}", params=components,
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
        if kind == "record":
            for c in tn.children:
                if c.type == "parameter_list":
                    params, _ = _cs_params(c)
        elif kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(m, "name"))
                      for m in _ast_collect(body, ("enum_member_declaration",))]
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=params, supers=supers,
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
                    params=mparams, returns=mname,
                    visibility=_cs_vis(m), container=name, lang="csharp",
                    calls=calls, bindings=binds,
                ))
                continue
            returns = tight_type(_ast_text(_ast_field(m, "returns")))
            ret_suffix = f":{returns}" if returns and returns != "void" else ""
            symbols.append(Symbol(
                name=mname, kind="method", file=rel, line=m.start_point[0] + 1,
                signature=f"{mname}({','.join(mparams)}){ret_suffix}",
                params=mparams, returns=returns,
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
        params=params, returns=returns,
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
        if ctor is not None:
            pl = next((c for c in ctor.children if c.type == "class_parameters"), None)
            cparams, class_binds = _kt_params(pl)
        supers = []
        for ds in tn.children:
            if ds.type == "delegation_specifiers":
                supers = [_base_type(re.sub(r"\(.*", "", _ast_text(d)))
                          for d in ds.children if d.type == "delegation_specifier"]
        body = next((c for c in tn.children
                     if c.type in ("class_body", "enum_class_body")), None)
        if is_enum and body is not None:
            cparams = [_ast_text(e.children[0])
                       for e in _ast_collect(body, ("enum_entry",)) if e.children]
        kind = ("enum" if is_enum else "interface" if is_iface
                else "record" if is_data else "class")
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=cparams, supers=supers,
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
            params=params, returns=returns,
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
            params=params, returns=returns,
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
                    params=params, returns=returns or (cname if kind == "ctor" else None),
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
                params=params, returns=returns,
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
                params=params, returns=returns,
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
            visibility="priv" if is_local or name.startswith("_") else "pub",
            container=container, lang="lua",
            calls=_ast_calls(_ast_field(fn, "body"), name,
                             ("function_call",), _lua_call_entry),
            size=_body_lines(_ast_field(fn, "body")),
        ))
    return symbols


# ---------------------------------------------------------------------------
# HTML extraction (ids and custom elements, names only)
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

def _py_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    types = []
    for arg in node.args.posonlyargs + node.args.args:
        if arg.arg in ("self", "cls"):
            continue
        types.append(tight_type(ast.unparse(arg.annotation)) if arg.annotation else "?")
    return types


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
                         if isinstance(base, ast.Name) and base.id not in ("self", "cls")
                         else fn.attr)
            else:
                continue
            if entry not in seen:
                seen.append(entry)
    return seen[:12]


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
    for arg in node.args.posonlyargs + node.args.args:
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
    params = _py_params(node)
    ret_suffix = f":{returns}" if returns and returns != "None" else ""
    return Symbol(
        name=node.name, kind="method" if container else "fn", file=rel,
        line=node.lineno,
        signature=f"{node.name}({','.join(params)}){ret_suffix}",
        params=params, returns=returns,
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
            fields = [ast.unparse(t.annotation) for t in node.body
                      if isinstance(t, ast.AnnAssign)]
            supers = [] if is_enum else [
                re.sub(r"\[.*", "", ast.unparse(b)).split(".")[-1] for b in node.bases]
            symbols.append(Symbol(
                name=node.name, kind="enum" if is_enum else "class", file=rel,
                line=node.lineno,
                signature=f"class {node.name}",
                params=members if is_enum else fields,
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
}


def extract_file(path: Path, root: Path, text: str | None = None) -> list[Symbol]:
    lang = detect_language(path)
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

def _gather(root: Path, langs: set[str] | None = None):
    """Extract symbols, identifier-token sets per file, and the corpus state hash.
    `langs` restricts to those languages (e.g. {"java"}); None means all."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    symbols: list[Symbol] = []
    file_tokens: dict[str, set[str]] = {}
    state = hashlib.md5()
    for f in files:
        rel = str(f.relative_to(root))
        raw = f.read_bytes()
        state.update(rel.encode())
        state.update(hashlib.md5(raw).digest())
        text = raw.decode(errors="replace")
        symbols.extend(extract_file(f, root, text))
        file_tokens[rel] = set(_IDENT_RE.findall(strip_comments_and_strings(text)))
    return files, symbols, file_tokens, state.hexdigest()[:12]


def _state_hash(root: Path, langs: set[str] | None = None) -> str:
    """The corpus hash `_gather` would produce, without parsing anything — cheap
    freshness probe for `check` / `--if-stale`."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    state = hashlib.md5()
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        state.update(str(f.relative_to(root)).encode())
        state.update(hashlib.md5(raw).digest())
    return state.hexdigest()[:12]


def _digest_state(out_path: Path) -> str | None:
    """The `state` stamp recorded in an existing digest's header, if any."""
    try:
        head = out_path.read_text(errors="replace").split("\n", 1)[0]
    except OSError:
        return None
    m = re.search(r"· state (\w{12})", head)
    return m.group(1) if m else None


def _fan_in_from_tokens(symbols: list[Symbol], file_tokens: dict[str, set[str]]) -> dict[str, float]:
    """Cross-file references per defining file: names defined everywhere (main, build) score low."""
    defined: dict[str, set[str]] = {}
    for s in symbols:
        defined.setdefault(s.name, set()).add(s.file)
    return {
        name: sum(1 for rel, tokens in file_tokens.items()
                  if name in tokens and rel not in own_files) / len(own_files)
        for name, own_files in defined.items()
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F",
               "type": "T"}


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def git_head(root: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "worktree"
    except (OSError, subprocess.TimeoutExpired):
        return "worktree"


def _is_test_path(rel: str) -> bool:
    parts = [p.lower() for p in Path(rel).parts]
    stem = Path(rel).stem
    return (any(p in ("test", "tests") for p in parts)
            or stem.endswith("Test") or stem.startswith("test_"))


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


def _sccs(edges: dict[str, set[str]]) -> dict[str, int]:
    """Tarjan SCC (iterative) -> node -> component id. `edges` must contain every
    node as a key (empty set for leaves)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp: dict[str, int] = {}
    counter = ncomp = 0

    for root in sorted(edges):
        if root in index:
            continue
        work: list[tuple[str, list[str], int]] = [(root, sorted(edges[root]), 0)]
        index[root] = low[root] = counter; counter += 1
        stack.append(root); on_stack.add(root)
        while work:
            v, succs, i = work[-1]
            if i < len(succs):
                work[-1] = (v, succs, i + 1)
                w = succs[i]
                if w not in index:
                    index[w] = low[w] = counter; counter += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, sorted(edges[w]), 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                while True:
                    w = stack.pop(); on_stack.discard(w)
                    comp[w] = ncomp
                    if w == v:
                        break
                ncomp += 1
    return comp


def _reduce_calls(edges: dict[str, set[str]],
                  nodes_by_sym: dict[int, list[str]],
                  kept_by_sym: dict[int, list[str]]) -> dict[int, list[str]]:
    """Transitive reduction per call list: drop an entry whose callee is already
    reachable through a sibling entry. Nodes are Type.method where the receiver was
    resolved (precise) and bare names otherwise (conservative merge); SCC-safe."""
    all_nodes = set(edges) | {c for cs in edges.values() for c in cs}
    edges = {n: set(edges.get(n, ())) for n in all_nodes}
    comp = _sccs(edges)
    cedges: dict[int, set[int]] = {}
    for src, dsts in edges.items():
        for d in dsts:
            if comp[src] != comp[d]:
                cedges.setdefault(comp[src], set()).add(comp[d])
    reach_memo: dict[int, set[int]] = {}

    def reach(c: int) -> set[int]:
        if c in reach_memo:
            return reach_memo[c]
        reach_memo[c] = set()  # DAG of SCCs: placeholder never read
        out: set[int] = set()
        for d in cedges.get(c, ()):
            out.add(d)
            out |= reach(d)
        reach_memo[c] = out
        return out

    reduced: dict[int, list[str]] = {}
    for sid, calls in kept_by_sym.items():
        nodes = nodes_by_sym[sid]
        keep = []
        for i, c in enumerate(calls):
            ci = comp.get(nodes[i])
            implied = ci is not None and any(
                comp.get(nodes[j]) not in (None, ci) and ci in reach(comp[nodes[j]])
                for j in range(len(calls)) if j != i)
            if not implied:
                keep.append(c)
        reduced[sid] = keep
    return reduced


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


def _ubiquitous_calls(fns_by_lang: dict[str, list[Symbol]]) -> set[str]:
    """Callees named by >25% of a language's functions (log/guard helpers): noise."""
    ubiquitous: set[str] = set()
    for lang_fns in fns_by_lang.values():
        if len(lang_fns) < 20:
            continue
        df: dict[str, int] = {}
        for s in lang_fns:
            for c in set(s.calls):
                df[c] = df.get(c, 0) + 1
        ubiquitous |= {c for c, n in df.items() if n / len(lang_fns) > 0.25}
    return ubiquitous


def _total_loc(files: list[Path]) -> int:
    loc = 0
    for f in files:
        try:
            loc += f.read_text(errors="replace").count("\n") + 1
        except OSError:
            pass
    return loc


def render_simple(root: Path, symbols: list[Symbol], files: list[Path],
                  regen_cmd: str, scores: dict[str, float] | None = None,
                  private_sigs: bool = False, tested: set[str] | None = None,
                  behaviors: bool = False, state: str = "",
                  deps: list[str] | None = None) -> str:
    """Signatures only, as a package trie; each function's calls inline after `>`.
    Private members render as packed name lists (`- a,b`), or as full `-`-prefixed
    signatures when `private_sigs` is set.

    pkg
      Class(K: components)
        sig > callee, callee
        - privateName,privateName
    """
    prod = [s for s in symbols if not _is_test_path(s.file)]
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if (s.kind in TYPE_KINDS + ("fn",) and s.container is None
                and (s.visibility == "pub" or private_sigs)):
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    # owner keys carry lang so same-named types from different languages in one
    # dir (Pricer in go + rust) don't merge their method lists
    methods_by_owner: dict[tuple[str, str, str], list[Symbol]] = {}
    for s in prod:
        if (s.container and s.kind == "method"
                and (s.visibility == "pub" or private_sigs)):
            methods_by_owner.setdefault(
                (str(Path(s.file).parent), s.container, s.lang), []).append(s)
    # names-only private inventory (used when private_sigs is off)
    priv_methods_by_owner: dict[tuple[str, str, str], list[str]] = {}
    priv_top_by_file: dict[tuple[str, str], list[str]] = {}
    if not private_sigs:
        for s in prod:
            if s.visibility != "priv":
                continue
            if s.container and s.kind == "method":
                key = (str(Path(s.file).parent), s.container, s.lang)
                priv_methods_by_owner.setdefault(key, []).append(s.name)
            elif s.container is None and s.kind in TYPE_KINDS + ("fn",):
                key = (str(Path(s.file).parent), Path(s.file).name)
                priv_top_by_file.setdefault(key, []).append(s.name)

    defined = {s.name for s in symbols}
    project_types = {s.name for s in prod if s.kind in TYPE_KINDS}
    by_lang: dict[str, list[Symbol]] = {}
    for s in prod:
        if s.kind in ("fn", "method"):
            by_lang.setdefault(s.lang, []).append(s)
    ubiquitous = _ubiquitous_calls(by_lang)

    # A call is shown only if it names something defined in this project — platform
    # calls carry no project semantics. Receivers with a known declared type are
    # resolved: rendered as Type.method when the type is project-owned, dropped when
    # it is a platform type (kills name-collision noise like bigint.signum). Unknown
    # receivers fall back to the name-based rule. Ubiquitous helpers always drop.
    def _filter_calls(sym: Symbol) -> list[str]:
        kept: list[str] = []
        for c in sym.calls:
            recv, _, m = c.rpartition(".")
            if m in ubiquitous:
                continue
            if not recv:
                if m in defined:
                    kept.append(m)
                continue
            if recv in sym.bindings:
                t = _base_type(sym.bindings[recv])
                if t in project_types and m in defined:
                    kept.append(f"{t}.{m}")
                continue
            if recv in project_types:
                if m in defined:
                    kept.append(c)
                continue
            if recv[:1].isupper():
                continue  # unresolved TypeName (List, SpringApplication) -> platform
            if m in defined:
                kept.append(c)
        return list(dict.fromkeys(kept))

    # Call graph for transitive reduction. A method of a project type is the node
    # Type.name; a resolved call targets exactly that node, a bare call targets the
    # name node, which fans out to every Type.name defining it.
    def _call_node(entry: str) -> str:
        recv, _, m = entry.rpartition(".")
        return entry if recv in project_types else m

    fns = [s for s in prod if s.kind in ("fn", "method")]
    kept_by_sym = {id(s): _filter_calls(s) for s in fns}
    nodes_by_sym = {sid: [_call_node(c) for c in calls]
                    for sid, calls in kept_by_sym.items()}
    edges: dict[str, set[str]] = {}
    for s in fns:
        src = (f"{s.container}.{s.name}"
               if s.container in project_types else s.name)
        edges.setdefault(src, set()).update(nodes_by_sym[id(s)])
        if "." in src:  # bare-name node fans out to each qualified definition
            edges.setdefault(s.name, set()).add(src)
    kept_by_sym = _reduce_calls(edges, nodes_by_sym, kept_by_sym)

    def _norm(text: str, own: str) -> str:
        return re.sub(rf"\b{re.escape(own)}\b", "⟨X⟩", text)

    def _sig_line(sym: Symbol, own: str, grouped: bool) -> str:
        sig = sym.signature or sym.name
        if sym.visibility == "priv":
            sig = f"-{sig}"
        if sym.size >= 40:
            sig = f"{sig} ⋮{sym.size}"
        if tested and sym.visibility == "pub" and sym.name in tested:
            sig = f"{sig} ✓"
        kept = kept_by_sym.get(id(sym), [])
        if sym.raises:
            sig = f"{sig} !{','.join(_strip_exc(r) for r in sym.raises)}"
        if grouped:
            kept = [_norm(c, own) for c in kept]
            sig = _norm(sig, own)
        return f"{sig} > {','.join(kept)}" if kept else sig

    ctors_by_owner: dict[tuple[str, str, str], list[str]] = {}
    for s in prod:
        if s.kind == "ctor" or (s.kind == "method" and s.name == "__init__"):
            key = (str(Path(s.file).parent), s.container, s.lang)
            if len(s.params) > len(ctors_by_owner.get(key, [])):
                ctors_by_owner[key] = s.params

    payload_by_dir: dict[str, list[str]] = {}
    for d, types in sorted(types_by_dir.items()):
        payload = payload_by_dir.setdefault(d, [])
        groups: dict[tuple, list[Symbol]] = {}
        for t in sorted(types, key=lambda s: (s.kind == "fn", s.name)):
            if t.kind == "fn":
                payload.append(_sig_line(t, t.name, False))
                continue
            components = t.params or ctors_by_owner.get((d, t.name, t.lang), [])
            key = (t.kind, t.visibility, tuple(components), tuple(t.supers), tuple(t.permits))
            groups.setdefault(key, []).append(t)
        for (kind, vis, components, supers, permits), members in groups.items():
            members.sort(key=lambda s: s.name)
            names = ",".join(("-" if vis == "priv" else "") + m.name for m in members)
            letter = KIND_LETTER.get(kind, "?")
            if permits:
                inner = f"{letter} sealed: {'|'.join(permits)}"
            elif components:
                inner = f"{letter}: {','.join(components)}"
            else:
                inner = letter
            rel_suffix = f" : {','.join(supers)}" if supers else ""
            hot = max((scores.get(m.name, 0) for m in members), default=0) if scores else 0
            hot_suffix = f" ×{int(hot)}" if hot >= 10 else ""
            payload.append(f"{names}({inner}){rel_suffix}{hot_suffix}")
            # Methods shared by every member print once (⟨X⟩-normalized); each
            # member's remaining methods print on its own `Name: …` line.
            member_methods = {id(m): methods_by_owner.get((d, m.name, m.lang), [])
                              for m in members}
            head = members[0]
            def _priv_line(m: Symbol, prefix: str = "") -> str | None:
                names_only = priv_methods_by_owner.get((d, m.name, m.lang))
                if not names_only:
                    return None
                return f" {prefix}- {','.join(dict.fromkeys(names_only))}"

            if len(members) == 1:
                for ms in member_methods[id(head)]:
                    payload.append(" " + _sig_line(ms, head.name, False))
                if (pl := _priv_line(head)) is not None:
                    payload.append(pl)
                continue
            normed = {id(m): [_sig_line(ms, m.name, True)
                              for ms in member_methods[id(m)]] for m in members}
            shared = set.intersection(*(set(v) for v in normed.values()))
            emitted: set[str] = set()
            for line in normed[id(head)]:
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
                if (pl := _priv_line(m, f"{m.name} ")) is not None:
                    payload.append(pl)

    for (d, stem), names_only in sorted(priv_top_by_file.items()):
        payload_by_dir.setdefault(d, []).append(
            f"- {stem}: {','.join(dict.fromkeys(names_only))}")
    reex_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "reexport":
            key = (str(Path(s.file).parent), Path(s.file).name)
            names_r = reex_by_file.setdefault(key, [])
            if s.name not in names_r:
                names_r.append(s.name)
    for (d, fname), names_r in sorted(reex_by_file.items()):
        payload_by_dir.setdefault(d, []).append(f"» {fname}: {','.join(names_r)}")

    tail = ""
    if behaviors:
        by_owner: dict[str, list[str]] = {}
        for s in symbols:
            if _is_test_path(s.file) and s.kind in ("fn", "method"):
                owner = re.sub(r"(Test|Tests|IT|Spec)$", "",
                               s.container or Path(s.file).stem) or s.container
                if s.name not in by_owner.setdefault(owner, []):
                    by_owner[owner].append(s.name)
        blines = []
        for owner, names_b in sorted(by_owner.items()):
            cur = f"? {owner}:"
            for n in names_b:
                if len(cur) + len(n) + 1 > 150:
                    blines.append(cur)
                    cur = f"? {owner}: {n}"
                else:
                    cur += f" {n},"
            blines.append(cur.rstrip(","))
        if blines:
            tail = "\n" + "\n".join(blines)

    loc = _total_loc(files)
    state_part = f" · state {state}" if state else ""
    head = (f"# {root.name} @{git_head(root)} {date.today().isoformat()} · "
            f"{loc:,} LOC{state_part} · regen: {regen_cmd}\n"
            "· legend: (C)lass (R)ecord (I)nterface (E)num (F)n (T)ype-alias · "
            "(R: …)=components (C: …)=ctor deps (E: …)=values · "
            "name(params):Ret, no :Ret=void · : X=extends/implements · "
            "sealed: A|B=permits · sig > calls, project-only, transitively reduced · "
            "!E=throws, Exception suffix dropped · ⟨X⟩=member's own name · "
            "×N=referenced from N files · ⋮N=body lines · ✓=referenced from tests · "
            "»file: re-exports · "
            + ("-sig=private" if private_sigs else "- x,y=private members, names only")
            + (" · ? Owner: test names" if behaviors else "")
            + "\n"
            "· query this file, don't read it: who calls X → grep '> .*X' · "
            "find a symbol → grep -i 'name(' · only tested APIs → grep '✓' · "
            "heavily-used types → grep '×' · a class's internals → its '- ' line · "
            "module coupling → the 'deps' line\n")
    dep_part = ("\n".join(deps) + "\n") if deps else ""
    return head + dep_part + "\n".join(_tree_lines(payload_by_dir)) + tail + "\n"


def build_digest(root: Path, regen_cmd: str = "hologram build",
                 langs: set[str] | None = None, private_sigs: bool = False,
                 behaviors: bool = False) -> str:
    files, symbols, file_tokens, state = _gather(root, langs)
    scores = _fan_in_from_tokens(symbols, file_tokens)
    test_tokens: set[str] = set()
    for rel, toks in file_tokens.items():
        if _is_test_path(rel):
            test_tokens |= toks
    tested = {s.name for s in symbols
              if not _is_test_path(s.file) and s.visibility == "pub"
              and s.kind in ("fn", "method")} & test_tokens
    deps = _dep_lines(symbols, file_tokens)
    return render_simple(root, symbols, files, regen_cmd, scores, private_sigs,
                         tested=tested, behaviors=behaviors, state=state, deps=deps)


# ---------------------------------------------------------------------------
# Embed: put the digest INSIDE CLAUDE.md so every agent session starts with
# the whole map in context — push, not pull; no retrieval decision to lose.
# ---------------------------------------------------------------------------

_EMBED_START = "<!-- hologram:start — generated, do not edit; refreshed by git hooks -->"
_EMBED_END = "<!-- hologram:end -->"

_EMBED_PREFACE = """## Project map (generated — the whole codebase at a glance)

The block below is the complete symbol map of this repository: every type,
signature, relation, and resolved call chain. You already have the holistic
view — use it directly for planning, placement, and reuse decisions instead of
exploring first. Before writing any new function, find the existing one here.
Grep the source only for implementation bodies.
"""


def _reduce_for_embed(digest: str, max_tokens: int) -> tuple[str, str]:
    """Graded degradation to fit the embed budget, holism-first:
    full → drop call chains → drop private/re-export/method lines (types keep
    the shape) → hard truncate. Returns (body, tier-name)."""
    if estimate_tokens(digest) <= max_tokens:
        return digest, "full"
    lines = digest.splitlines()
    no_chains = [ln.split(" > ")[0] if " > " in ln else ln for ln in lines]
    body = "\n".join(no_chains) + "\n"
    if estimate_tokens(body) <= max_tokens:
        return body, "no-chains"
    types_only = [ln for ln in no_chains
                  if not ln.strip().startswith(("-", "»", "?"))
                  and not ("(" in ln and ln.strip()[:1].islower())]
    body = "\n".join(types_only) + "\n"
    if estimate_tokens(body) <= max_tokens:
        return body, "types-only"
    keep, used = [], 0
    for ln in types_only:
        used += len(ln) // 4 + 1
        if used > max_tokens:
            keep.append("… (truncated to fit embed budget — full map in PROJECT_DIGEST.md)")
            break
        keep.append(ln)
    return "\n".join(keep) + "\n", "truncated"


def embed_digest(claude_path: Path, digest: str, max_tokens: int = 30000) -> str:
    """Insert or refresh the digest block in CLAUDE.md. Degrades gracefully to
    fit the budget; returns the tier used ('full', 'no-chains', 'types-only',
    'truncated')."""
    body, tier = _reduce_for_embed(digest, max_tokens)
    block = (f"{_EMBED_START}\n{_EMBED_PREFACE}\n```\n{body.rstrip()}\n```\n"
             f"{_EMBED_END}")
    existing = claude_path.read_text() if claude_path.exists() else ""
    if _EMBED_START in existing and _EMBED_END in existing:
        pre = existing.split(_EMBED_START, 1)[0]
        post = existing.split(_EMBED_END, 1)[1]
        updated = pre + block + post
    else:
        sep = "\n\n" if existing.strip() else ""
        updated = existing.rstrip("\n") + sep + block + "\n"
    claude_path.write_text(updated)
    return tier


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


def _hook_python() -> str:
    """The tool's own venv python when present (tree-sitter grammars), else python3."""
    venv_py = _venv_python()
    return str(venv_py) if venv_py.exists() else "python3"


def _install_hooks(repo: Path, quiet: bool, langs: set[str] | None = None,
                   embed: bool = False) -> None:
    script = Path(__file__).resolve()
    lang_args = "".join(f' --lang {l}' for l in sorted(langs)) if langs else ""
    embed_arg = " --embed" if embed else ""
    hook_line = (f'{_hook_python()} "{script}" build --root "{repo.resolve()}"'
                 f'{lang_args}{embed_arg} --quiet || true\n')
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        if hook.exists():
            content = hook.read_text()
            if str(script) in content:
                continue
            hook.write_text(content.rstrip("\n") + "\n" + hook_line)
        else:
            hook.write_text("#!/bin/sh\n" + hook_line)
        hook.chmod(0o755)
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if "PROJECT_DIGEST.md" not in existing:
        gitignore.write_text(existing.rstrip("\n") + ("\n" if existing else "")
                             + "PROJECT_DIGEST.md\n")
    if not quiet:
        print(f"hooks installed: {', '.join(HOOK_NAMES)}; PROJECT_DIGEST.md gitignored")


def run_cli(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path.cwd())
    common.add_argument("--lang", action="append", default=None,
                        help="restrict to language(s), repeatable or comma-separated "
                             "(java, python, typescript, javascript)")
    common.add_argument("--private", action="store_true",
                        help="full signatures for private members "
                             "(default: names only)")
    common.add_argument("--behaviors", action="store_true",
                        help="append test-method names as behavior specs "
                             "(costly on test-heavy repos)")
    common.add_argument("--embed", action="store_true",
                        help="also inject the digest into CLAUDE.md so every "
                             "agent session starts with the whole map in context")
    common.add_argument("--embed-max-tokens", type=int, default=30000,
                        help="embed budget; larger digests degrade gracefully "
                             "(chains, then methods, then truncation)")
    common.add_argument("--out", type=Path, default=None)
    common.add_argument("--quiet", action="store_true")

    parser = argparse.ArgumentParser(prog="hologram", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", parents=[common],
                             help="(re)generate the digest file")
    p_build.add_argument("--if-stale", action="store_true",
                         help="skip the rebuild when the digest's state stamp "
                              "matches the current sources")
    sub.add_parser("init", parents=[common],
                   help="install git hooks and gitignore entry, then build")
    sub.add_parser("check", parents=[common],
                   help="exit 0 if the digest is fresh, 1 if stale or missing")
    p_diff = sub.add_parser("diff", parents=[common],
                            help="diff the digest against another git revision")
    p_diff.add_argument("rev", nargs="?", default="HEAD~1")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    langs = None
    if getattr(args, "lang", None):
        langs = {l.strip() for arg in args.lang for l in arg.split(",") if l.strip()}
    out_path = args.out or root / "PROJECT_DIGEST.md"

    if args.cmd == "check":
        fresh = _digest_state(out_path) == _state_hash(root, langs)
        if not args.quiet:
            print(f"{out_path}: {'fresh' if fresh else 'stale or missing'}")
        return 0 if fresh else 1
    if args.cmd == "build" and args.if_stale \
            and _digest_state(out_path) == _state_hash(root, langs):
        if not args.quiet:
            print(f"{out_path}: fresh, skipping rebuild")
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
                old = build_digest(wt, langs=langs, private_sigs=args.private)
                new = build_digest(root, langs=langs, private_sigs=args.private)
            finally:
                subprocess.run(["git", "-C", str(root), "worktree", "remove",
                                "--force", str(wt)], capture_output=True)
        body_old = old.splitlines()[2:]  # drop header+legend: date/state/path noise
        body_new = new.splitlines()[2:]
        for ln in difflib.unified_diff(body_old, body_new, fromfile=args.rev,
                                       tofile="worktree", lineterm=""):
            print(ln)
        return 0

    if args.cmd == "init":
        _install_hooks(root, args.quiet, langs, embed=args.embed)
    digest = build_digest(root,
                          regen_cmd=f'{_hook_python()} "{Path(__file__).resolve()}" build',
                          langs=langs, private_sigs=args.private,
                          behaviors=args.behaviors)
    out_path.write_text(digest)
    if not args.quiet:
        print(f"{out_path} written: {estimate_tokens(digest)} tokens")
    if args.embed:
        tier = embed_digest(root / "CLAUDE.md", digest, args.embed_max_tokens)
        if not args.quiet:
            print(f"CLAUDE.md: digest embedded ({tier})")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
