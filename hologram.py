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
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

LANG_EXTENSIONS = {
    ".java": "java",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
}

DENYLIST_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "out", "bin", "obj",
    "vendor", "generated", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    "fixtures", "testdata", "resources",
}

TYPE_KINDS = ("class", "interface", "record", "enum")


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
}

_PARSERS = {
    "java": _load_parser("tree_sitter_java"),
    "typescript": _load_parser("tree_sitter_typescript", "language_typescript"),
}
_PARSERS["javascript"] = _PARSERS["typescript"]

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
            calls=_java_calls(body, name), bindings=binds,
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
                  class_binds: dict[str, str] | None = None) -> Symbol:
    name = _ast_text(_ast_field(node, "name"))
    params = _ts_params(_ast_field(node, "parameters"))
    returns = _ts_return(node)
    body = _ast_field(node, "body")
    ret_suffix = f":{returns}" if returns and returns != "void" else ""
    return Symbol(
        name=name, kind="method" if container else "fn", file=rel,
        line=node.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, returns=returns,
        visibility=visibility, container=container, lang="typescript",
        calls=_ts_calls(body, name),
        bindings={**(class_binds or {}),
                  **_ts_param_bindings(_ast_field(node, "parameters")),
                  **_ts_local_bindings(body)},
    )


def _extract_ts(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["typescript"].parse(text.encode())
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
    """Extract symbols and identifier-token sets per file.
    `langs` restricts to those languages (e.g. {"java"}); None means all."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    symbols: list[Symbol] = []
    file_tokens: dict[str, set[str]] = {}
    for f in files:
        rel = str(f.relative_to(root))
        text = f.read_text(errors="replace")
        symbols.extend(extract_file(f, root, text))
        file_tokens[rel] = set(_IDENT_RE.findall(strip_comments_and_strings(text)))
    return files, symbols, file_tokens


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

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F"}


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
                  regen_cmd: str, scores: dict[str, float] | None = None) -> str:
    """Signatures only, as a package trie; each function's calls inline after `>`.

    pkg
      Class(K: components)
        sig > callee, callee
    """
    prod = [s for s in symbols if not _is_test_path(s.file)]
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if s.kind in TYPE_KINDS + ("fn",) and s.container is None and s.visibility == "pub":
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    methods_by_owner: dict[tuple[str, str], list[Symbol]] = {}
    for s in prod:
        if s.container and s.kind == "method" and s.visibility == "pub":
            methods_by_owner.setdefault((str(Path(s.file).parent), s.container), []).append(s)

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
        kept = kept_by_sym.get(id(sym), [])
        if sym.raises:
            sig = f"{sig} !{','.join(_strip_exc(r) for r in sym.raises)}"
        if grouped:
            kept = [_norm(c, own) for c in kept]
            sig = _norm(sig, own)
        return f"{sig} > {','.join(kept)}" if kept else sig

    ctors_by_owner: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "ctor" or (s.kind == "method" and s.name == "__init__"):
            key = (str(Path(s.file).parent), s.container)
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
            components = t.params or ctors_by_owner.get((d, t.name), [])
            key = (t.kind, tuple(components), tuple(t.supers), tuple(t.permits))
            groups.setdefault(key, []).append(t)
        for (kind, components, supers, permits), members in groups.items():
            members.sort(key=lambda s: s.name)
            names = ",".join(m.name for m in members)
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
            member_methods = {m.name: methods_by_owner.get((d, m.name), [])
                              for m in members}
            head = members[0]
            if len(members) == 1:
                for ms in member_methods[head.name]:
                    payload.append(" " + _sig_line(ms, head.name, False))
                continue
            normed = {m.name: [_sig_line(ms, m.name, True)
                               for ms in member_methods[m.name]] for m in members}
            shared = set.intersection(*(set(v) for v in normed.values()))
            emitted: set[str] = set()
            for line in normed[head.name]:
                if line in shared and line not in emitted:
                    payload.append(" " + line)
                    emitted.add(line)
            for m in members:
                extras = [ms for ms, ln in zip(member_methods[m.name], normed[m.name])
                          if ln not in shared]
                if extras:
                    payload.append(f" {m.name}: "
                                   + "; ".join(_sig_line(ms, m.name, False)
                                               for ms in extras))

    loc = _total_loc(files)
    head = (f"# {root.name} @{git_head(root)} {date.today().isoformat()} · "
            f"{loc:,} LOC · regen: {regen_cmd}\n"
            "· legend: (C)lass (R)ecord (I)nterface (E)num (F)n · (R: …)=components "
            "(C: …)=ctor deps (E: …)=values · name(params):Ret, no :Ret=void · "
            ": X=extends/implements · sealed: A|B=permits · sig > calls, project-only, "
            "transitively reduced · !E=throws, Exception suffix dropped · "
            "⟨X⟩=member's own name · ×N=referenced from N files\n")
    return head + "\n".join(_tree_lines(payload_by_dir)) + "\n"


def build_digest(root: Path, regen_cmd: str = "hologram build",
                 langs: set[str] | None = None) -> str:
    files, symbols, file_tokens = _gather(root, langs)
    scores = _fan_in_from_tokens(symbols, file_tokens)
    return render_simple(root, symbols, files, regen_cmd, scores)


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


def _install_hooks(repo: Path, quiet: bool, langs: set[str] | None = None) -> None:
    script = Path(__file__).resolve()
    lang_args = "".join(f' --lang {l}' for l in sorted(langs)) if langs else ""
    hook_line = (f'{_hook_python()} "{script}" build --root "{repo.resolve()}"'
                 f'{lang_args} --quiet || true\n')
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
    common.add_argument("--quiet", action="store_true")

    parser = argparse.ArgumentParser(prog="hologram", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", parents=[common],
                             help="(re)generate the digest file")
    p_build.add_argument("--out", type=Path, default=None)
    sub.add_parser("init", parents=[common],
                   help="install git hooks and gitignore entry, then build")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    langs = None
    if getattr(args, "lang", None):
        langs = {l.strip() for arg in args.lang for l in arg.split(",") if l.strip()}
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    missing = _missing_parser_langs(files)
    if missing:
        _bootstrap_or_die(missing, argv if argv is not None else sys.argv[1:])
    if args.cmd == "init":
        _install_hooks(root, args.quiet, langs)
    out_path = getattr(args, "out", None) or root / "PROJECT_DIGEST.md"
    digest = build_digest(root,
                          regen_cmd=f'{_hook_python()} "{Path(__file__).resolve()}" build',
                          langs=langs)
    out_path.write_text(digest)
    if not args.quiet:
        print(f"{out_path} written: {estimate_tokens(digest)} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
