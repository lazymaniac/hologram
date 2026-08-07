#!/usr/bin/env python3
"""mdl-digest: compress a codebase into a single token-budgeted markdown file for LLM sessions.

Deterministic. Engine is language-neutral; language specifics live in detector packs.
"""

from __future__ import annotations

import ast
import hashlib
import keyword
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LANG_EXTENSIONS = {
    ".java": "java",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
}

DENYLIST_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "out", "bin", "obj",
    "vendor", "generated", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    "fixtures", "testdata", "resources",
}

TYPE_KINDS = ("class", "interface", "record", "enum")

CACHE_VERSION = 4

JAVA_PRIMITIVE_RETURNS = {
    "void", "boolean", "int", "long", "double", "float", "short", "byte", "char", "var",
}

JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "non-sealed", "package", "permits", "private",
    "protected", "public", "record", "return", "sealed", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws", "transient", "try",
    "var", "void", "volatile", "while", "yield",
}


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
    doc: str = ""
    lang: str = ""
    skeleton_hash: str = ""
    size: int = 0
    calls: list[str] = field(default_factory=list)
    supers: list[str] = field(default_factory=list)
    permits: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)


@dataclass
class Archetype:
    skeleton_hash: str
    members: list[Symbol]


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
    import os
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
# Normalization / skeletons
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_LINE_COMMENT_RE = re.compile(r"//[^\n]*|#[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_NUMBER_RE = re.compile(r"\b\d[\w.]*\b")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def strip_comments_and_strings(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub('"s"', text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


def skeletonize(code: str, keywords: frozenset[str] | set[str]) -> str:
    """Structure-preserving normalization: identifiers/literals become holes."""
    code = strip_comments_and_strings(code)
    code = _NUMBER_RE.sub("0", code)
    code = _IDENT_RE.sub(lambda m: m.group(0) if m.group(0) in keywords else "_", code)
    return " ".join(code.split())


def skeleton_hash(code: str, keywords) -> str:
    return hashlib.md5(skeletonize(code, keywords).encode()).hexdigest()[:12]


def _match_braces(text: str, open_idx: int) -> int:
    """Index just past the brace that closes text[open_idx] == '{'. -1 if unbalanced."""
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


_CALL_RE = re.compile(r"\bnew\s+(\w+)|(?:(\w+)\.)?(\w+)\s*\(")


def extract_calls(body: str, keywords, own_name: str = "") -> list[str]:
    """Called names in first-occurrence order: `Ctor` for `new Ctor(...)`, `recv.method`
    for qualified calls, bare `fn` otherwise. Keywords and the function's own name excluded."""
    body = strip_comments_and_strings(body)
    brace = body.find("{")
    colon = body.find(":")
    start = brace + 1 if brace != -1 else (colon + 1 if colon != -1 else 0)
    seen: list[str] = []
    for m in _CALL_RE.finditer(body, start):
        ctor, recv, name = m.groups()
        if ctor:
            entry = ctor
            name = ctor
        elif recv and recv not in keywords:
            entry = f"{recv}.{name}"
        else:
            entry = name
        if name in keywords or name == own_name or entry in seen:
            continue
        seen.append(entry)
    return seen[:12]


def _parse_throws(clause: str | None) -> list[str]:
    if not clause:
        return []
    return [t.strip().split(".")[-1] for t in clause.split(",") if t.strip()]


def _blank_nested_blocks(inner: str) -> str:
    """Replace the contents of every brace block with spaces, keeping offsets stable."""
    out = list(inner)
    depth = 0
    for i, c in enumerate(inner):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth > 0 and c != "\n":
            out[i] = " "
    return "".join(out)


def split_params(raw: str) -> list[str]:
    """Split a parameter list on top-level commas, return declared types only."""
    parts, depth, cur = [], 0, ""
    for c in raw:
        if c in "<([":
            depth += 1
        elif c in ">)]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    types = []
    for p in parts:
        p = re.sub(r"@\w+(\([^)]*\))?", "", p).strip()
        p = re.sub(r"^final\s+", "", p)
        tokens = p.rsplit(None, 1)
        if len(tokens) == 2:
            types.append(tight_type(tokens[0].strip()))
        elif tokens:
            types.append(tight_type(tokens[0].strip()))
    return types


def tight_type(t: str) -> str:
    """Collapse interior whitespace in a type expression: Map<K, V> -> Map<K,V>."""
    return re.sub(r",\s+", ",", t)


# ---------------------------------------------------------------------------
# Java extraction
# ---------------------------------------------------------------------------

_JAVA_TYPE_RE = re.compile(
    r"(?m)^[ \t]*(?:(public|protected|private)\s+)?"
    r"((?:(?:static|final|abstract|sealed|non-sealed|strictfp)\s+)*)"
    r"(class|interface|record|enum)\s+(\w+)(?:<[^>{;]*>)?\s*(\(([^)]*)\))?([^{;]*)"
)


def _java_heritage(segment: str) -> tuple[list[str], list[str]]:
    """(supers, permits) from the text between a type's name and its `{`."""
    def names(kw: str) -> list[str]:
        m = re.search(rf"\b{kw}\s+([\w.<>, \t\n]+?)(?=\bextends\b|\bimplements\b|\bpermits\b|$)",
                      segment)
        if not m:
            return []
        return [re.sub(r"<.*", "", n.strip()).split(".")[-1]
                for n in m.group(1).split(",") if n.strip()]
    supers = names("extends") + names("implements")
    return supers, names("permits")

_JAVA_METHOD_RE = re.compile(
    r"(?m)^[ \t]*(?:(public|protected|private)\s+)?"
    r"(?:(?:static|final|abstract|synchronized|default|native)\s+)*"
    r"(?:<[^>]+>\s*)?"
    r"([\w.$]+(?:<[^;{}()]*>)?(?:\[\])*)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+([\w., \t]+?))?\s*\{"
)

_JAVA_CTOR_RE = re.compile(
    r"(?m)^[ \t]*(?:(public|protected|private)\s+)?(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w., \t]+)?\s*\{"
)

_JAVA_ABSTRACT_METHOD_RE = re.compile(
    r"(?m)^[ \t]*(?:(public|protected|private)\s+)?"
    r"(?:(?:static|final|abstract|default)\s+)*"
    r"(?:<[^>]+>\s*)?"
    r"([\w.$]+(?:<[^;{}()]*>)?(?:\[\])*)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+([\w., \t]+?))?\s*;"
)

_JAVADOC_RE = re.compile(r"/\*\*(.*?)\*/", re.S)


def _java_enum_constants(inner: str) -> list[str]:
    """Constant names from an enum body: the leading identifier of each top-level
    comma-separated entry before the first top-level ';'."""
    inner = strip_comments_and_strings(inner)
    depth = 0
    entries, cur = [], ""
    for c in inner:
        if c in "({<[":
            depth += 1
        elif c in ")}>]":
            depth -= 1
        elif depth == 0 and c in ",;":
            entries.append(cur)
            if c == ";":
                cur = None
                break
            cur = ""
            continue
        if cur is not None:
            cur += c
    if cur:
        entries.append(cur)
    constants = []
    for e in entries:
        m = re.match(r"\s*(?:@\w+\s*)*(\w+)", e)
        if m:
            constants.append(m.group(1))
    return constants


def _first_doc_sentence(text: str, before: int) -> str:
    window = text[max(0, before - 2000):before]
    docs = _JAVADOC_RE.findall(window)
    if not docs:
        return ""
    doc = re.sub(r"\s*\*\s?", " ", docs[-1])
    doc = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", doc)
    doc = re.sub(r"@\w+[^\n]*", "", doc)
    doc = " ".join(doc.split())
    return doc.split(". ")[0].rstrip(".{ ")[:160]


def _extract_java(text: str, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    for m in _JAVA_TYPE_RE.finditer(text):
        vis_kw, modifiers, kind, name, _, rec_params, heritage = m.groups()
        line = text.count("\n", 0, m.start()) + 1
        brace = text.find("{", m.end() - 1)
        end = _match_braces(text, brace) if brace != -1 else -1
        body = text[m.start():end] if end != -1 else text[m.start():m.start() + 4000]
        params = split_params(rec_params) if rec_params else []
        if kind == "enum" and brace != -1 and end != -1:
            params = _java_enum_constants(text[brace + 1:end - 1])
        supers, permits = _java_heritage(heritage or "")
        sig = m.group(0).strip()
        if "sealed" in (modifiers or ""):
            sig = f"sealed {kind} {name}"
        type_sym = Symbol(
            name=name, kind=kind, file=rel, line=line,
            signature=sig, params=params,
            visibility="pub" if vis_kw in (None, "public") else "priv",
            doc=_first_doc_sentence(text, m.start()), lang="java",
            skeleton_hash=skeleton_hash(body, JAVA_KEYWORDS),
            size=body.count("\n") + 1,
            supers=supers, permits=permits,
        )
        symbols.append(type_sym)

        inner = text[brace + 1:end - 1] if (brace != -1 and end != -1) else ""
        inner_offset_line = text.count("\n", 0, brace) + 1
        for mm in _JAVA_METHOD_RE.finditer(inner):
            vis, returns, mname, mparams, mthrows = mm.groups()
            if returns in JAVA_KEYWORDS and returns not in JAVA_PRIMITIVE_RETURNS:
                continue
            if mname in JAVA_KEYWORDS:
                continue
            mline = inner_offset_line + inner.count("\n", 0, mm.start())
            mb = inner.find("{", mm.end() - 1)
            mend = _match_braces(inner, mb) if mb != -1 else -1
            mbody = inner[mm.start():mend] if mend != -1 else mm.group(0)
            symbols.append(Symbol(
                name=mname, kind="method", file=rel, line=mline,
                signature=f"{mname}({','.join(split_params(mparams))}):{returns}",
                params=split_params(mparams), returns=returns,
                visibility="pub" if vis in (None, "public") else "priv",
                container=name, lang="java",
                skeleton_hash=skeleton_hash(mbody, JAVA_KEYWORDS),
                size=mbody.count("\n") + 1,
                calls=extract_calls(mbody, JAVA_KEYWORDS, mname),
                raises=_parse_throws(mthrows),
            ))
        blanked = _blank_nested_blocks(inner)
        for mm in _JAVA_ABSTRACT_METHOD_RE.finditer(blanked):
            vis, returns, mname, mparams, mthrows = mm.groups()
            if returns in JAVA_KEYWORDS and returns not in JAVA_PRIMITIVE_RETURNS:
                continue
            if mname in JAVA_KEYWORDS:
                continue
            symbols.append(Symbol(
                name=mname, kind="method", file=rel,
                line=inner_offset_line + blanked.count("\n", 0, mm.start()),
                signature=f"{mname}({','.join(split_params(mparams))}):{returns}",
                params=split_params(mparams), returns=returns,
                visibility="pub" if vis in (None, "public") else "priv",
                container=name, lang="java",
                raises=_parse_throws(mthrows),
            ))
        for mm in _JAVA_CTOR_RE.finditer(inner):
            vis, cname, cparams = mm.groups()
            if cname != name:
                continue
            mline = inner_offset_line + inner.count("\n", 0, mm.start())
            symbols.append(Symbol(
                name=cname, kind="ctor", file=rel, line=mline,
                signature=f"{cname}({','.join(split_params(cparams))})",
                params=split_params(cparams), returns=cname,
                visibility="pub" if vis in (None, "public") else "priv",
                container=name, lang="java",
            ))
    return symbols


# ---------------------------------------------------------------------------
# Python extraction (stdlib ast — precise and dependency-free)
# ---------------------------------------------------------------------------

PY_KEYWORDS = frozenset(keyword.kwlist) | {"self", "cls"}


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


def _py_fn_symbol(node, rel: str, container: str | None, source: str) -> Symbol:
    seg = ast.get_source_segment(source, node) or ""
    returns = tight_type(ast.unparse(node.returns)) if node.returns else None
    params = _py_params(node)
    return Symbol(
        name=node.name, kind="method" if container else "fn", file=rel,
        line=node.lineno,
        signature=f"{node.name}({','.join(params)})" + (f":{returns}" if returns else ""),
        params=params, returns=returns,
        visibility="priv" if node.name.startswith("_") else "pub",
        container=container, doc=(ast.get_docstring(node) or "").split("\n")[0][:160],
        lang="python", skeleton_hash=skeleton_hash(seg, PY_KEYWORDS),
        size=seg.count("\n") + 1,
        calls=[c for c in _py_calls(node) if c != node.name],
        raises=_py_raises(node),
    )


def _extract_python(text: str, rel: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            seg = ast.get_source_segment(text, node) or ""
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
                doc=(ast.get_docstring(node) or "").split("\n")[0][:160],
                lang="python", skeleton_hash=skeleton_hash(seg, PY_KEYWORDS),
                size=seg.count("\n") + 1,
            ))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_py_fn_symbol(sub, rel, node.name, text))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_py_fn_symbol(node, rel, None, text))
    return symbols


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction
# ---------------------------------------------------------------------------

TS_KEYWORDS = frozenset({
    "abstract", "any", "as", "async", "await", "boolean", "break", "case", "catch",
    "class", "const", "constructor", "continue", "declare", "default", "delete", "do",
    "else", "enum", "export", "extends", "finally", "for", "from", "function", "get",
    "if", "implements", "import", "in", "instanceof", "interface", "let", "new",
    "number", "of", "private", "protected", "public", "readonly", "return", "set",
    "static", "string", "super", "switch", "this", "throw", "try", "type", "typeof",
    "undefined", "var", "void", "while", "yield",
})

_TS_TYPE_RE = re.compile(
    r"(?m)^[ \t]*(export\s+)?(?:declare\s+)?(?:abstract\s+)?(class|interface|enum)\s+(\w+)"
)
_TS_FN_RE = re.compile(
    r"(?m)^[ \t]*(export\s+)?(?:async\s+)?function\s+(\w+)\s*(?:<[^>]*>)?\(([^)]*)\)\s*(?::\s*([\w<>\[\], .|&]+?))?\s*\{"
)
_TS_METHOD_RE = re.compile(
    r"(?m)^[ \t]*(?:(public|private|protected)\s+)?(?:static\s+)?(?:async\s+)?"
    r"(\w+)\s*(?:<[^>]*>)?\(([^)]*)\)\s*(?::\s*([\w<>\[\], .|&]+?))?\s*\{"
)
_TS_CONTROL = {"if", "for", "while", "switch", "catch", "return", "function", "constructor"}


def _ts_params(raw: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for c in raw:
        if c in "<([{":
            depth += 1
        elif c in ">)]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    types = []
    for p in parts:
        p = re.sub(r"^(private|public|protected|readonly)\s+", "", p.strip())
        p = p.split("=")[0]
        types.append(p.split(":", 1)[1].strip() if ":" in p else "?")
    return types


def _extract_ts(text: str, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    for m in _TS_TYPE_RE.finditer(text):
        exported, kind, name = m.groups()
        line = text.count("\n", 0, m.start()) + 1
        brace = text.find("{", m.end())
        end = _match_braces(text, brace) if brace != -1 else -1
        body = text[m.start():end] if end != -1 else m.group(0)
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=line,
            signature=m.group(0).strip(),
            visibility="pub" if exported else "priv", lang="typescript",
            skeleton_hash=skeleton_hash(body, TS_KEYWORDS),
            size=body.count("\n") + 1,
        ))
        if kind == "class" and brace != -1 and end != -1:
            inner = text[brace + 1:end - 1]
            inner_line = text.count("\n", 0, brace) + 1
            for mm in _TS_METHOD_RE.finditer(inner):
                vis, mname, mparams, mret = mm.groups()
                if mname in _TS_CONTROL or mname in TS_KEYWORDS:
                    continue
                mb = inner.find("{", mm.end() - 1)
                mend = _match_braces(inner, mb) if mb != -1 else -1
                mbody = inner[mm.start():mend] if mend != -1 else mm.group(0)
                symbols.append(Symbol(
                    name=mname, kind="method", file=rel,
                    line=inner_line + inner.count("\n", 0, mm.start()),
                    signature=f"{mname}({','.join(_ts_params(mparams))})"
                              + (f":{mret.strip()}" if mret else ""),
                    params=_ts_params(mparams),
                    returns=mret.strip() if mret else None,
                    visibility="priv" if vis == "private" else "pub",
                    container=name, lang="typescript",
                    skeleton_hash=skeleton_hash(mbody, TS_KEYWORDS),
                    size=mbody.count("\n") + 1,
                    calls=extract_calls(mbody, TS_KEYWORDS, mname),
                ))
    for m in _TS_FN_RE.finditer(text):
        exported, name, params, ret = m.groups()
        symbols.append(Symbol(
            name=name, kind="fn", file=rel,
            line=text.count("\n", 0, m.start()) + 1,
            signature=f"{name}({','.join(_ts_params(params))})"
                      + (f":{ret.strip()}" if ret else ""),
            params=_ts_params(params), returns=ret.strip() if ret else None,
            visibility="pub" if exported else "priv", lang="typescript",
        ))
    return symbols



# ---------------------------------------------------------------------------
# Tree-sitter Java extraction (AST-grade; used when the optional lib is present)
# ---------------------------------------------------------------------------

try:
    from tree_sitter import Language as _TSLanguage, Parser as _TSParser
    import tree_sitter_java as _ts_java
    _JAVA_TS_PARSER = _TSParser(_TSLanguage(_ts_java.language()))
    USING_TREESITTER = True
except ImportError:
    _JAVA_TS_PARSER = None
    USING_TREESITTER = False

_AST_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


def _ast_text(node) -> str:
    return node.text.decode(errors="replace") if node is not None else ""


def _ast_field(node, name):
    return node.child_by_field_name(name)


def _ast_modifiers(node) -> str:
    for c in node.children:
        if c.type == "modifiers":
            return _ast_text(c)
    return ""


def _ast_param_types(node) -> list[str]:
    """Declared parameter types from a formal_parameters node."""
    raw = _ast_text(node)
    return split_params(raw[1:-1]) if raw.startswith("(") else split_params(raw)


def _ast_calls(body, own_name: str) -> list[str]:
    if body is None:
        return []
    seen: list[str] = []
    stack = [body]
    ordered = []
    while stack:
        n = stack.pop()
        if n.type in ("method_invocation", "object_creation_expression"):
            ordered.append(n)
        stack.extend(n.children)
    ordered.sort(key=lambda n: n.start_byte)
    for n in ordered:
        if n.type == "object_creation_expression":
            entry = re.sub(r"<.*", "", _ast_text(_ast_field(n, "type")))
            name = entry
        else:
            name = _ast_text(_ast_field(n, "name"))
            obj = _ast_field(n, "object")
            entry = (f"{_ast_text(obj)}.{name}"
                     if obj is not None and obj.type == "identifier" else name)
        if not name or name == own_name or entry in seen:
            continue
        seen.append(entry)
    return seen[:12]


def _ast_method_symbol(m, type_name: str, rel: str, text: str) -> Symbol:
    name = _ast_text(_ast_field(m, "name"))
    params = _ast_param_types(_ast_field(m, "parameters"))
    mods = _ast_modifiers(m)
    body = _ast_field(m, "body")
    throws = []
    for c in m.children:
        if c.type == "throws":
            throws = _parse_throws(_ast_text(c).removeprefix("throws"))
    if m.type == "constructor_declaration":
        return Symbol(
            name=name, kind="ctor", file=rel, line=m.start_point[0] + 1,
            signature=f"{name}({','.join(params)})", params=params, returns=name,
            visibility="priv" if any(v in mods for v in ("private", "protected")) else "pub",
            container=type_name, lang="java", raises=throws,
            calls=_ast_calls(body, name),
        )
    returns = _ast_text(_ast_field(m, "type"))
    seg = _ast_text(m)
    return Symbol(
        name=name, kind="method", file=rel, line=m.start_point[0] + 1,
        signature=f"{name}({','.join(params)}):{returns}",
        params=params, returns=returns,
        visibility="priv" if any(v in mods for v in ("private", "protected")) else "pub",
        container=type_name, lang="java",
        skeleton_hash=skeleton_hash(seg, JAVA_KEYWORDS) if body is not None else "",
        size=seg.count("\n") + 1,
        calls=_ast_calls(body, name), raises=throws,
    )


def _extract_java_treesitter(text: str, rel: str) -> list[Symbol]:
    tree = _JAVA_TS_PARSER.parse(text.encode())
    symbols: list[Symbol] = []
    stack = [tree.root_node]
    type_nodes = []
    while stack:
        n = stack.pop()
        if n.type in _AST_TYPE_NODE_KINDS:
            type_nodes.append(n)
        stack.extend(n.children)
    type_nodes.sort(key=lambda n: n.start_byte)
    for tn in type_nodes:
        kind = _AST_TYPE_NODE_KINDS[tn.type]
        name = _ast_text(_ast_field(tn, "name"))
        body = _ast_field(tn, "body")
        header_end = body.start_byte if body is not None else tn.end_byte
        header = text.encode()[tn.start_byte:header_end].decode(errors="replace")
        supers, permits = _java_heritage(re.sub(r"\(.*?\)", "", header, flags=re.S))
        mods = _ast_modifiers(tn)
        params: list[str] = []
        if kind == "record":
            pnode = _ast_field(tn, "parameters")
            params = _ast_param_types(pnode) if pnode is not None else []
        elif kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(c, "name"))
                      for c in body.children if c.type == "enum_constant"]
        seg = _ast_text(tn)
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=(f"sealed {kind} {name}" if "sealed" in mods else f"{kind} {name}"),
            params=params,
            visibility="priv" if any(v in mods for v in ("private", "protected")) else "pub",
            doc=_first_doc_sentence(text, seg and text.find(seg[:40]) or 0), lang="java",
            skeleton_hash=skeleton_hash(seg, JAVA_KEYWORDS),
            size=seg.count("\n") + 1,
            supers=supers, permits=permits,
        ))
        if body is None:
            continue
        containers = list(body.children)
        if kind == "enum":
            containers = [c for c in body.children if c.type == "enum_body_declarations"]
            containers = [gc for c in containers for gc in c.children] or list(body.children)
        for c in containers:
            if c.type in ("method_declaration", "constructor_declaration"):
                symbols.append(_ast_method_symbol(c, name, rel, text))
    return symbols


EXTRACTORS = {
    "java": _extract_java_treesitter if USING_TREESITTER else _extract_java,
    "python": _extract_python,
    "typescript": _extract_ts,
    "javascript": _extract_ts,
}


def extract_file(path: Path, root: Path) -> list[Symbol]:
    lang = detect_language(path)
    extractor = EXTRACTORS.get(lang)
    if extractor is None:
        return []
    rel = str(path.relative_to(root))
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return extractor(text, rel)


# ---------------------------------------------------------------------------
# Clustering / ranking
# ---------------------------------------------------------------------------

def cluster_skeletons(symbols: list[Symbol], min_cluster: int = 3) -> tuple[list[Archetype], list[Symbol]]:
    groups: dict[str, list[Symbol]] = {}
    for s in symbols:
        groups.setdefault(s.skeleton_hash or f"∅{s.name}", []).append(s)
    archetypes, outliers = [], []
    for h, members in groups.items():
        if len(members) >= min_cluster and not h.startswith("∅"):
            archetypes.append(Archetype(skeleton_hash=h, members=sorted(members, key=lambda s: s.name)))
        else:
            outliers.extend(members)
    archetypes.sort(key=lambda a: (-len(a.members), a.members[0].name))
    return archetypes, outliers


# ---------------------------------------------------------------------------
# Type lineage & capability index
# ---------------------------------------------------------------------------

@dataclass
class Lineage:
    type_name: str
    producers: list[str] = field(default_factory=list)
    consumers: list[str] = field(default_factory=list)
    holders: list[str] = field(default_factory=list)


def _qual(sym: Symbol) -> str:
    return f"{sym.container}.{sym.name}" if sym.container else sym.name


def _types_in(type_expr: str | None, project_types: set[str]) -> set[str]:
    if not type_expr:
        return set()
    return {t for t in _IDENT_RE.findall(type_expr) if t in project_types}


def type_lineage(symbols: list[Symbol]) -> dict[str, Lineage]:
    project_types = {s.name for s in symbols if s.kind in TYPE_KINDS}
    lineage = {t: Lineage(type_name=t) for t in project_types}
    for s in symbols:
        if s.kind in ("fn", "method", "ctor"):
            for t in _types_in(s.returns, project_types):
                lineage[t].producers.append(_qual(s))
            for p in s.params:
                for t in _types_in(p, project_types):
                    lineage[t].consumers.append(_qual(s))
        elif s.kind in TYPE_KINDS:
            for p in s.params:
                for t in _types_in(p, project_types):
                    if t != s.name:
                        lineage[t].holders.append(s.name)
    return lineage


def capability_index(symbols: list[Symbol]) -> dict[tuple[str, str], list[str]]:
    """(input types, output type) -> functions already providing that transformation."""
    project_types = {s.name for s in symbols if s.kind in TYPE_KINDS}
    caps: dict[tuple[str, str], list[str]] = {}
    for s in symbols:
        if s.kind not in ("fn", "method") or s.visibility != "pub":
            continue
        if not s.returns or s.returns in ("void", "None"):
            continue
        if not (_types_in(s.returns, project_types)
                or any(_types_in(p, project_types) for p in s.params)):
            continue
        key = (", ".join(s.params), s.returns)
        caps.setdefault(key, []).append(_qual(s))
    return caps


# ---------------------------------------------------------------------------
# Detector packs (data-only language/ecosystem knowledge)
# ---------------------------------------------------------------------------

@dataclass
class Detector:
    concept: str
    pattern: re.Pattern
    value_group: int
    path: re.Pattern | None = None
    transform: str | None = None
    kind: str = ""


@dataclass
class Pack:
    lang: str
    detectors: list[Detector]


@dataclass
class Match:
    concept: str
    value: str
    file: str
    line: int
    kind: str = ""


_TRANSFORMS = {
    None: lambda v: v,
    "snake_to_words": lambda v: re.sub(r"^test_", "", v).replace("_", " ").strip(),
    "camel_to_words": lambda v: re.sub(r"(?<!^)(?=[A-Z])", " ", v).lower().strip(),
}

PACKS_DIR = Path(__file__).resolve().parent / "packs"


def load_packs(packs_dir: Path = PACKS_DIR) -> list[Pack]:
    import tomllib
    packs = []
    for f in sorted(packs_dir.glob("*.toml")):
        data = tomllib.loads(f.read_text())
        detectors = [
            Detector(
                concept=d["concept"],
                pattern=re.compile(d["pattern"]),
                value_group=d.get("value", 0),
                path=re.compile(d["path"]) if "path" in d else None,
                transform=d.get("transform"),
                kind=d.get("kind", ""),
            )
            for d in data.get("detector", [])
        ]
        packs.append(Pack(lang=data["lang"], detectors=detectors))
    return packs


def apply_packs(packs: list[Pack], files: list[Path], root: Path) -> list[Match]:
    by_lang: dict[str, list[Detector]] = {}
    for p in packs:
        by_lang.setdefault(p.lang, []).extend(p.detectors)
    matches: list[Match] = []
    for f in files:
        detectors = by_lang.get(detect_language(f) or "", [])
        if not detectors:
            continue
        rel = str(f.relative_to(root))
        text = f.read_text(errors="replace")
        for d in detectors:
            if d.path and not d.path.search(rel):
                continue
            for m in d.pattern.finditer(text):
                raw = m.group(d.value_group)
                if raw is None:
                    continue
                matches.append(Match(
                    concept=d.concept,
                    value=_TRANSFORMS[d.transform](raw),
                    file=rel,
                    line=text.count("\n", 0, m.start()) + 1,
                    kind=d.kind,
                ))
    return matches


def fan_in_scores(symbols: list[Symbol], files: list[Path], root: Path) -> dict[str, int]:
    """name -> number of files (other than its defining file) whose tokens reference it."""
    defined: dict[str, set[str]] = {}
    for s in symbols:
        defined.setdefault(s.name, set()).add(s.file)
    file_tokens: dict[str, set[str]] = {}
    for f in files:
        rel = str(f.relative_to(root))
        text = strip_comments_and_strings(f.read_text(errors="replace"))
        file_tokens[rel] = set(_IDENT_RE.findall(text))
    scores: dict[str, int] = {}
    for name, own_files in defined.items():
        scores[name] = sum(
            1 for rel, tokens in file_tokens.items()
            if name in tokens and rel not in own_files
        )
    return scores


# ---------------------------------------------------------------------------
# Header, evolution
# ---------------------------------------------------------------------------

@dataclass
class Header:
    purpose: str = ""
    stack: str = ""


_MANIFEST_STACK = {
    "pom.xml": "maven/java",
    "build.gradle": "gradle/java",
    "build.gradle.kts": "gradle/kotlin",
    "package.json": "node",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "docker-compose.yml": "docker-compose",
    "compose.yaml": "docker-compose",
}


def harvest_header(root: Path) -> Header:
    purpose = ""
    for name in ("README.md", "README.rst", "README.txt", "readme.md"):
        readme = root / name
        if readme.exists():
            for para in readme.read_text(errors="replace").split("\n\n"):
                lines = [ln for ln in para.strip().splitlines()
                         if ln.strip() and not ln.startswith(("#", "[![", "<", "!["))]
                if lines:
                    purpose = " ".join(" ".join(lines).split())
                    if len(purpose) > 400:
                        purpose = purpose[:398].rsplit(" ", 1)[0] + " …"
                    break
            break
    stack = " ".join(sorted({tag for mf, tag in _MANIFEST_STACK.items() if (root / mf).exists()}))
    return Header(purpose=purpose, stack=stack)


def parse_git_numstat(out: str) -> tuple[dict[str, int], list[str]]:
    """Parse `git log --format=%h%x09%s --numstat` output -> (file->commit count, subjects)."""
    churn: dict[str, int] = {}
    subjects: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            subjects.append(parts[1])
        elif len(parts) == 3:
            churn[parts[2]] = churn.get(parts[2], 0) + 1
    return churn, subjects


def gather_evolution(root: Path, days: int = 90) -> tuple[dict[str, int], list[str]]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", f"--since={days}.days", "--max-count=400",
             "--format=%h%x09%s", "--numstat"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}, []
    return parse_git_numstat(out)


def git_head(root: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "worktree"
    except (OSError, subprocess.TimeoutExpired):
        return "worktree"


# ---------------------------------------------------------------------------
# Rendering / budget allocation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _common_affix_label(names: list[str]) -> str:
    """A short label for a cluster: shared suffix or prefix if any, else first name."""
    def _suffix(a, b):
        i = 0
        while i < min(len(a), len(b)) and a[-1 - i] == b[-1 - i]:
            i += 1
        return a[len(a) - i:]
    suf = names[0]
    for n in names[1:]:
        suf = _suffix(suf, n)
    if len(suf) >= 2:
        return f"*{suf}"
    return names[0]


def _archetype_lines(archetypes, symbols) -> list[str]:
    methods_by_container: dict[str, list[Symbol]] = {}
    for s in symbols:
        if s.container:
            methods_by_container.setdefault(s.container, []).append(s)
    lines = []
    for i, a in enumerate(archetypes, 1):
        names = [m.name for m in a.members]
        label = _common_affix_label(names)
        proto = a.members[0]
        hole = "⟨X⟩"
        sig = proto.signature.replace(proto.name, hole)
        msigs = "; ".join(
            m.signature.replace(proto.name, hole)
            for m in methods_by_container.get(proto.name, [])[:3]
        )
        body = f"{sig} {{ {msigs} }}" if msigs else sig
        dirs = sorted({str(Path(m.file).parent) for m in a.members})
        shown = names[:12]
        more = f" … +{len(names) - len(shown)} more" if len(names) > len(shown) else ""
        lines.append(f"A{i} {label} ({len(names)}×): {body}")
        lines.append(f"   {' '.join(shown)}{more}   [{', '.join(dirs[:4])}]")
    return lines


# ---------------------------------------------------------------------------
# Module map & per-module API (coverage-first: every package, every public type)
# ---------------------------------------------------------------------------

_BOILERPLATE_PARTS = ("src", "main", "test", "java", "tests", "lib")

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F"}


def _short_dirs(dirs: list[str]) -> dict[str, str]:
    """Readable module names: strip src/main/java-style boilerplate, then shared leading parts."""
    stripped = {}
    for d in dirs:
        parts = [p for p in Path(d).parts if p not in _BOILERPLATE_PARTS]
        stripped[d] = parts or ["."]
    while len(stripped) > 1:
        firsts = {parts[0] for parts in stripped.values()}
        if len(firsts) == 1 and all(len(parts) > 1 for parts in stripped.values()):
            for d in stripped:
                stripped[d] = stripped[d][1:]
        else:
            break
    return {d: "/".join(parts) for d, parts in stripped.items()}


def _doc_from_text_block(text: str) -> str:
    docs = _JAVADOC_RE.findall(text)
    if docs:
        doc = re.sub(r"\s*\*\s?", " ", docs[0])
        doc = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", doc)
        doc = " ".join(doc.split())
        return doc.split(". ")[0].rstrip(".{ ")[:200]
    return ""


def module_docs(root: Path, files_by_rel: dict[str, str], symbols: list[Symbol]) -> dict[str, str]:
    """dir -> one semantic line, from package-info/__init__ docstring/dir README, else synthesized."""
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in symbols:
        if s.kind in TYPE_KINDS + ("fn",) and s.container is None:
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    docs: dict[str, str] = {}
    for d in sorted({str(Path(rel).parent) for rel in files_by_rel}):
        doc = ""
        pkg_info = files_by_rel.get(str(Path(d) / "package-info.java"))
        if pkg_info:
            doc = _doc_from_text_block(pkg_info)
        if not doc:
            init = files_by_rel.get(str(Path(d) / "__init__.py"))
            if init:
                try:
                    doc = (ast.get_docstring(ast.parse(init)) or "").split("\n")[0][:200]
                except SyntaxError:
                    doc = ""
        if not doc:
            readme = root / d / "README.md"
            if readme.exists():
                for ln in readme.read_text(errors="replace").splitlines():
                    if ln.strip() and not ln.startswith("#"):
                        doc = ln.strip()[:200]
                        break
        if not doc:
            members = sorted(types_by_dir.get(d, []), key=lambda s: -s.size)[:4]
            doc = "· " + ", ".join(m.name for m in members) if members else ""
        docs[d] = doc
    return docs


def _is_test_path(rel: str) -> bool:
    parts = [p.lower() for p in Path(rel).parts]
    stem = Path(rel).stem
    return (any(p in ("test", "tests") for p in parts)
            or stem.endswith("Test") or stem.startswith("test_"))


def _module_lines(docs: dict[str, str], short: dict[str, str]) -> list[str]:
    """One line per module name; when prod and test dirs collapse to the same name,
    the prod dir's (or the documented dir's) line wins."""
    by_short: dict[str, tuple[tuple[int, int], str]] = {}
    for d, doc in sorted(docs.items()):
        if not doc:
            continue
        rank = (1 if _is_test_path(d) else 0, 1 if doc.startswith("·") else 0)
        name = short[d]
        if name not in by_short or rank < by_short[name][0]:
            by_short[name] = (rank, doc)
    return [f"{name}/  {doc}".rstrip() for name, (_, doc) in sorted(by_short.items())]


def _api_lines(symbols: list[Symbol], scores: dict[str, float], short: dict[str, str]) -> list[str]:
    """Spend-ordered: complete per-package type inventories first, then per-type
    detail lines allocated round-robin across packages (breadth before depth)."""
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in symbols:
        if (s.kind in TYPE_KINDS + ("fn",) and s.container is None
                and s.visibility == "pub" and not _is_test_path(s.file)):
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    methods_by_container: dict[str, list[Symbol]] = {}
    for s in symbols:
        if s.container and s.visibility == "pub":
            methods_by_container.setdefault(s.container, []).append(s)

    lines: list[str] = []
    ranked_by_dir: dict[str, list[Symbol]] = {}
    for d, types in sorted(types_by_dir.items()):
        ranked = sorted(types, key=lambda s: (-scores.get(s.name, 0), s.name))
        ranked_by_dir[d] = ranked
        entries: list[str] = []
        counts: dict[str, int] = {}
        for t in ranked:
            key = f"{t.name}({KIND_LETTER.get(t.kind, '?')})"
            counts[key] = counts.get(key, 0) + 1
        for key, n in counts.items():
            entries.append(key if n == 1 else f"{key}×{n}")
        label = short.get(d, d)
        cur = f"{label}:"
        for e in entries:
            if len(cur) + len(e) + 1 > 160:
                lines.append(cur)
                cur = f"{label}: …{e}"
            else:
                cur += f" {e}"
        lines.append(cur)
    max_depth = max((len(r) for r in ranked_by_dir.values()), default=0)
    for depth in range(max_depth):
        for d, ranked in sorted(ranked_by_dir.items()):
            if depth >= len(ranked):
                continue
            t = ranked[depth]
            msigs = "; ".join(m.signature for m in methods_by_container.get(t.name, [])[:4])
            detail_parts = [p for p in (t.doc, msigs) if p]
            if detail_parts:
                lines.append(f"  {short.get(d, d)}.{t.name}: {' — '.join(detail_parts)}")
    return lines


def _lineage_lines(lineage, scores, matches, files_by_rel) -> list[str]:
    def fmt(names):
        seen = list(dict.fromkeys(names))
        return ",".join(seen[:4]) + ("…" if len(seen) > 4 else "")

    ranked = sorted(lineage.values(), key=lambda l: (-scores.get(l.type_name, 0), l.type_name))
    lines = []
    for l in ranked:
        if not (l.producers or l.consumers or l.holders):
            continue
        parts = []
        if l.producers:
            parts.append(f"born {fmt(l.producers)}")
        if l.consumers:
            parts.append(f"used {fmt(l.consumers)}")
        if l.holders:
            parts.append(f"held {fmt(l.holders)}")
        lines.append(f"{l.type_name}: {'; '.join(parts)}")
    project_types = set(lineage)
    for m in matches:
        if m.concept != "entry_point":
            continue
        text = files_by_rel.get(m.file, "")
        order = []
        for ident in _IDENT_RE.findall(strip_comments_and_strings(text)):
            if ident in project_types and ident not in order:
                order.append(ident)
        if order:
            label = m.kind or "entry"
            lines.append(f"trace {label} {m.file}: {' → '.join(order[:6])}")
    return lines


def _capability_lines(caps) -> list[str]:
    items = sorted(caps.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    cells = [f"({ins})→{out}: {', '.join(sorted(set(fns))[:2])}" for (ins, out), fns in items]
    return _pack_cells(cells)


def _pack_cells(cells: list[str], width: int = 110) -> list[str]:
    lines, cur = [], ""
    for c in cells:
        if cur and len(cur) + len(c) + 3 > width:
            lines.append(cur)
            cur = c
        else:
            cur = f"{cur} | {c}" if cur else c
    if cur:
        lines.append(cur)
    return lines


_NONNULL_RE = re.compile(r"requireNonNull\((\w+)")


def _invariant_lines(matches) -> list[str]:
    nonnull_by_type: dict[str, list[str]] = {}
    cells = []
    seen = set()
    for m in matches:
        if m.concept != "invariant":
            continue
        value = " ".join(m.value.split())
        stem = Path(m.file).stem
        nn = _NONNULL_RE.match(value)
        if nn:
            params = nonnull_by_type.setdefault(stem, [])
            if nn.group(1) not in params:
                params.append(nn.group(1))
            continue
        if value in seen:
            continue
        seen.add(value)
        cells.append(f"{value} [{stem}]")
    grouped = [f"{stem}: non-null {','.join(params)}"
               for stem, params in nonnull_by_type.items()]
    return _pack_cells(grouped + cells)


def _behavior_lines(matches) -> list[str]:
    by_module: dict[str, list[str]] = {}
    for m in matches:
        if m.concept != "test_spec":
            continue
        module = "/".join(Path(m.file).parent.parts[-2:]) or "."
        if m.value not in by_module.setdefault(module, []):
            by_module[module].append(m.value)
    lines: list[str] = []
    for mod, vals in sorted(by_module.items()):
        cur = f"{mod}:"
        for v in vals:
            if len(cur) + len(v) + 2 > 150:
                lines.append(cur)
                cur = f"{mod}: {v}"
            else:
                cur += f" {v};"
        lines.append(cur.rstrip(";"))
    return lines


def _index_lines(matches) -> list[str]:
    grouped: dict[tuple[str, str], list[str]] = {}
    tag = {"error": "err", "config": "cfg", "persistence": "db"}
    for m in matches:
        if m.concept not in tag:
            continue
        files = grouped.setdefault((tag[m.concept], m.value), [])
        stem = Path(m.file).stem
        if stem not in files:
            files.append(stem)
    cells = [f"{t} {v}: {','.join(fs[:3])}" for (t, v), fs in sorted(grouped.items())]
    return _pack_cells(cells)


def _evolution_lines(churn, subjects) -> list[str]:
    lines = []
    hot = sorted(churn.items(), key=lambda kv: -kv[1])[:8]
    if hot:
        lines.append("hot: " + " | ".join(f"{f} ({n})" for f, n in hot))
    if subjects:
        lines.append("last: " + " | ".join(f'"{s}"' for s in subjects[:5]))
    return lines


SECTION_CAPS = [
    ("## MODULES", 1.00),
    ("## API (every package; details breadth-first)", 0.45),
    ("## ARCHETYPES (this repo's grammar)", 0.12),
    ("## TYPE LINEAGE (flows)", 0.10),
    ("## INVARIANTS", 0.08),
    ("## BEHAVIORS (from tests)", 0.08),
    ("## EVOLUTION", 0.05),
    ("## CAPABILITIES (check before writing new code)", 0.05),
    ("## INDEXES", 0.05),
]

DOC_ORDER = [
    "## MODULES",
    "## ARCHETYPES (this repo's grammar)",
    "## API (every package; details breadth-first)",
    "## TYPE LINEAGE (flows)",
    "## CAPABILITIES (check before writing new code)",
    "## INVARIANTS",
    "## BEHAVIORS (from tests)",
    "## INDEXES",
    "## EVOLUTION",
]


def _gather(root: Path, cache_dir: Path | None, langs: set[str] | None = None):
    """Extract symbols/matches/token-sets per file, using a blob-hash cache when given.
    `langs` restricts to those languages (e.g. {"java"}); None means all."""
    import json
    from dataclasses import asdict

    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    packs = load_packs()
    detectors_by_lang: dict[str, list[Detector]] = {}
    for p in packs:
        detectors_by_lang.setdefault(p.lang, []).extend(p.detectors)

    cache: dict = {}
    cache_file = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "cache.json"
        if cache_file.exists():
            try:
                raw = json.loads(cache_file.read_text())
                if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                    cache = raw.get("files", {})
            except json.JSONDecodeError:
                cache = {}

    symbols: list[Symbol] = []
    matches: list[Match] = []
    files_by_rel: dict[str, str] = {}
    file_tokens: dict[str, set[str]] = {}
    new_cache: dict = {}
    for f in files:
        rel = str(f.relative_to(root))
        text = f.read_text(errors="replace")
        files_by_rel[rel] = text
        h = hashlib.md5(text.encode()).hexdigest()
        entry = cache.get(rel)
        if entry and entry.get("hash") == h:
            fsyms = [Symbol(**d) for d in entry["symbols"]]
            fmatches = [Match(**d) for d in entry["matches"]]
            tokens = set(entry["tokens"])
        else:
            fsyms = extract_file(f, root)
            fmatches = []
            for d in detectors_by_lang.get(detect_language(f) or "", []):
                if d.path and not d.path.search(rel):
                    continue
                for m in d.pattern.finditer(text):
                    raw = m.group(d.value_group)
                    if raw is None:
                        continue
                    fmatches.append(Match(
                        concept=d.concept, value=_TRANSFORMS[d.transform](raw),
                        file=rel, line=text.count("\n", 0, m.start()) + 1, kind=d.kind,
                    ))
            tokens = set(_IDENT_RE.findall(strip_comments_and_strings(text)))
        symbols.extend(fsyms)
        matches.extend(fmatches)
        file_tokens[rel] = tokens
        new_cache[rel] = {
            "hash": h,
            "symbols": [asdict(s) for s in fsyms],
            "matches": [asdict(m) for m in fmatches],
            "tokens": sorted(tokens),
        }
    if cache_file is not None:
        cache_file.write_text(json.dumps({"version": CACHE_VERSION, "files": new_cache}))
    return files, symbols, matches, files_by_rel, file_tokens


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
            out.append("  " * depth + label)
            base = depth + 1
        for ln in payload:
            out.append("  " * base + ln)
        for k in sorted(children):
            emit(children[k], k, base)

    emit(tree, None, 0)
    return out


def render_simple(root: Path, symbols: list[Symbol], files: list[Path],
                  regen_cmd: str, scores: dict[str, float] | None = None) -> str:
    """Signatures only, as a package trie; each function's calls inline after `>`.

    pkg
      Class(K: components)
        sig > callee, callee
    """
    from datetime import date
    prod = [s for s in symbols if not _is_test_path(s.file)]
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if s.kind in TYPE_KINDS + ("fn",) and s.container is None and s.visibility == "pub":
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    methods_by_owner: dict[tuple[str, str], list[Symbol]] = {}
    for s in prod:
        if s.container and s.kind == "method" and s.visibility == "pub":
            methods_by_owner.setdefault((str(Path(s.file).parent), s.container), []).append(s)

    # A call is shown only if it names something defined in this project — platform
    # calls (requireNonNull, toString, ArrayList) carry no project semantics. Among
    # project calls, drop the ubiquitous ones (a log/guard helper used everywhere).
    defined = {s.name for s in symbols}
    by_lang: dict[str, list[Symbol]] = {}
    for s in prod:
        if s.kind in ("fn", "method"):
            by_lang.setdefault(s.lang, []).append(s)
    ubiquitous: set[str] = set()
    for lang_fns in by_lang.values():
        if len(lang_fns) < 20:
            continue
        df: dict[str, int] = {}
        for s in lang_fns:
            for c in set(s.calls):
                df[c] = df.get(c, 0) + 1
        ubiquitous |= {c for c, n in df.items() if n / len(lang_fns) > 0.25}

    def _norm(text: str, own: str) -> str:
        return re.sub(rf"\b{re.escape(own)}\b", "⟨X⟩", text)

    def _sig_line(sig: str, calls: list[str], own: str, grouped: bool,
                  raises: list[str] | None = None) -> str:
        kept = [c for c in calls
                if c.rsplit(".", 1)[-1] in defined
                and c.rsplit(".", 1)[-1] not in ubiquitous]
        if raises:
            sig = f"{sig} !{','.join(raises)}"
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
                payload.append(_sig_line(t.signature or t.name, t.calls, t.name, False,
                                         t.raises))
                continue
            components = t.params or ctors_by_owner.get((d, t.name), [])
            methods = methods_by_owner.get((d, t.name), [])
            key = (t.kind, tuple(components), tuple(t.supers), tuple(t.permits),
                   tuple(_norm(m.signature, t.name) for m in methods))
            groups.setdefault(key, []).append(t)
        for (kind, components, supers, permits, _), members in groups.items():
            members.sort(key=lambda s: s.name)
            head_sym = members[0]
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
            grouped = len(members) > 1
            for m in methods_by_owner.get((d, head_sym.name), []):
                payload.append("  " + _sig_line(m.signature, m.calls, head_sym.name, grouped,
                                                m.raises))

    loc = 0
    for f in files:
        try:
            loc += f.read_text(errors="replace").count("\n") + 1
        except OSError:
            pass
    head = (f"# {root.name} @{git_head(root)} {date.today().isoformat()} · "
            f"{loc:,} LOC · regen: {regen_cmd}\n"
            "· legend: (C)lass (R)ecord (I)nterface (E)num (F)n · (R: …)=components "
            "(C: …)=ctor deps (E: …)=values · name(params):Ret · : X=extends/implements · "
            "sealed: A|B=permits · sig > calls · !E=throws · ⟨X⟩=each member's own name "
            "· ×N=referenced from N files\n")
    # No budget enforcement in the simple layout (yet): the complete listing is the product.
    return head + "\n".join(_tree_lines(payload_by_dir)) + "\n"


def build_digest(root: Path, budget: int = 8000, cache_dir: Path | None = None,
                 regen_cmd: str = "mdl-digest build", mode: str = "simple",
                 langs: set[str] | None = None) -> str:
    files, symbols, matches, files_by_rel, file_tokens = _gather(root, cache_dir, langs)
    scores = _fan_in_from_tokens(symbols, file_tokens)
    if mode == "simple":
        return render_simple(root, symbols, files, regen_cmd, scores)
    lineage = type_lineage(symbols)
    caps = capability_index(symbols)
    top_level = [s for s in symbols if s.kind in TYPE_KINDS + ("fn",)]
    archetypes, outliers = cluster_skeletons(top_level)

    header = harvest_header(root)
    churn, subjects = gather_evolution(root)
    loc = sum(t.count("\n") + 1 for t in files_by_rel.values())
    lang_counts: dict[str, int] = {}
    for f in files:
        lang = detect_language(f) or "?"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    langs = " ".join(f"{l} {round(100 * n / max(len(files), 1))}%"
                     for l, n in sorted(lang_counts.items(), key=lambda kv: -kv[1])[:4])
    covered = sum(len(a.members) for a in archetypes)
    total_types = max(covered + len(outliers), 1)

    from datetime import date
    head_lines = [
        f"# {root.name} — mdl-digest @{git_head(root)} {date.today().isoformat()}  "
        f"budget={budget}  (regen: {regen_cmd})",
        f"PURPOSE: {header.purpose}" if header.purpose else "PURPOSE: (no README found)",
        f"STACK: {header.stack or '?'}   LOC {loc:,}  FILES {len(files)}  LANGS {langs}",
        f"coverage: archetypes {round(100 * covered / total_types)}% of "
        f"{total_types} top-level symbols | check CAPABILITIES before writing new code",
    ]
    head = "\n".join(head_lines) + "\n"

    mod_docs = module_docs(root, files_by_rel, symbols)
    short = _short_dirs(sorted(mod_docs))
    section_lines = {
        "## MODULES": _module_lines(mod_docs, short),
        "## API (every package; details breadth-first)": _api_lines(symbols, scores, short),
        "## ARCHETYPES (this repo's grammar)": _archetype_lines(archetypes, symbols),
        "## TYPE LINEAGE (flows)": _lineage_lines(lineage, scores, matches, files_by_rel),
        "## CAPABILITIES (check before writing new code)": _capability_lines(caps),
        "## INVARIANTS": _invariant_lines(matches),
        "## BEHAVIORS (from tests)": _behavior_lines(matches),
        "## INDEXES": _index_lines(matches),
        "## EVOLUTION": _evolution_lines(churn, subjects),
    }

    char_budget = budget * 4 - len(head)
    granted: dict[str, list[str]] = {}
    remaining = char_budget
    for title, cap in SECTION_CAPS:
        lines = section_lines.get(title, [])
        if not lines:
            continue
        section_cap = int(char_budget * cap)
        used = len(title) + 2
        take: list[str] = []
        for ln in lines:
            cost = len(ln) + 1
            if used + cost > min(section_cap, remaining):
                break
            take.append(ln)
            used += cost
        dropped = len(lines) - len(take)
        if take and dropped > 0 and used + 12 <= remaining:
            take.append(f"… +{dropped} more")
            used += 12
        if take:
            granted[title] = take
            remaining -= used

    parts = [head]
    for title in DOC_ORDER:
        if title in granted:
            parts.append(f"\n{title}\n" + "\n".join(granted[title]) + "\n")
    out = "".join(parts)
    while estimate_tokens(out) > budget:
        lines = out.rstrip("\n").splitlines()
        out = "\n".join(lines[:-1]) + "\n"
    return out


# ---------------------------------------------------------------------------
# CLI: build / init (self-installing git hooks)
# ---------------------------------------------------------------------------

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout")


def _hook_python() -> str:
    """The tool's own venv python when present (tree-sitter fidelity), else python3."""
    venv_py = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
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


def _default_cache_dir(root: Path) -> Path | None:
    git_dir = root / ".git"
    return git_dir / "mdl-digest" if git_dir.is_dir() else None


def run_cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="mdl-digest", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="(re)generate the digest file")
    p_build.add_argument("--root", type=Path, default=Path.cwd())
    p_build.add_argument("--out", type=Path, default=None)
    p_build.add_argument("--budget", type=int, default=8000)
    p_build.add_argument("--full", action="store_true",
                         help="rich sectioned layout instead of the plain signature listing")
    p_build.add_argument("--lang", action="append", default=None,
                         help="restrict to language(s), repeatable or comma-separated "
                              "(java, python, typescript, javascript, go)")
    p_build.add_argument("--quiet", action="store_true")
    p_init = sub.add_parser("init", help="install git hooks and gitignore entry, then build")
    p_init.add_argument("--root", type=Path, default=Path.cwd())
    p_init.add_argument("--budget", type=int, default=8000)
    p_init.add_argument("--full", action="store_true")
    p_init.add_argument("--lang", action="append", default=None)
    p_init.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    langs = None
    if getattr(args, "lang", None):
        langs = {l.strip() for arg in args.lang for l in arg.split(",") if l.strip()}
    if args.cmd == "init":
        _install_hooks(root, args.quiet, langs)
    out_path = getattr(args, "out", None) or root / "PROJECT_DIGEST.md"
    digest = build_digest(root, budget=args.budget, cache_dir=_default_cache_dir(root),
                          regen_cmd=f'{_hook_python()} "{Path(__file__).resolve()}" build',
                          mode="full" if args.full else "simple", langs=langs)
    out_path.write_text(digest)
    if not args.quiet:
        print(f"{out_path} written: {estimate_tokens(digest)} tokens (budget {args.budget})")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
