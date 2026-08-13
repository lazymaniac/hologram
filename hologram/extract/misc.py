from __future__ import annotations

import re
from pathlib import Path

from ..symbols import Symbol, const_signature
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines, has_parser)
from .ts import _extract_ts

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


_BASH_VAR_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")


def _extract_bash(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["bash"].parse(text.encode())
    stem = Path(rel).name
    symbols: list[Symbol] = [Symbol(
        name=stem, kind="class", file=rel, line=1,
        signature=f"script {stem}", visibility="pub", lang="bash")]
    for fn in _ast_collect(tree.root_node, ("function_definition",)):
        name = _ast_text(_ast_field(fn, "name"))
        if not name:
            continue
        body = _ast_field(fn, "body")
        symbols.append(Symbol(
            name=name, kind="method", file=rel,
            line=fn.start_point[0] + 1, signature=f"{name}()",
            visibility="priv" if name.startswith("_") else "pub",
            container=stem, lang="bash",
            calls=_ast_calls(body, name, ("command",), _bash_call_entry),
            size=_body_lines(body),
        ))
    # top-level VAR=…, export VAR=…, readonly VAR=… — literal values only;
    # command substitutions and expansions render name-only
    for top in tree.root_node.children:
        if top.type == "variable_assignment":
            assign = top
        elif top.type == "declaration_command":
            assign = next((c for c in top.children
                           if c.type == "variable_assignment"), None)
        else:
            continue
        if assign is None:
            continue
        vname = _ast_text(next((c for c in assign.children
                                if c.type == "variable_name"), None))
        if not vname or not _BASH_VAR_NAME_RE.fullmatch(vname):
            continue
        val = next((c for c in assign.children
                    if c.type in ("number", "string", "raw_string", "word")), None)
        value = _ast_text(val) if val is not None else None
        if value is not None and ("$" in value or "`" in value):
            value = None  # expansion inside quotes: not a literal
        symbols.append(Symbol(
            name=vname, kind="const", file=rel, line=assign.start_point[0] + 1,
            signature=const_signature(vname, value),
            visibility="pub", lang="bash"))
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

