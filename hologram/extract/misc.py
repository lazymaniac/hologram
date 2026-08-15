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



# ---------------------------------------------------------------------------
# Makefile extraction — targets are the commands, recipe variables the knobs
# ---------------------------------------------------------------------------

_MAKE_NAME = r"[A-Za-z0-9_][\w./-]*"
_MAKE_VAR_NAME = r"[A-Za-z_][\w-]*"
_MAKE_VAR_NAME_RE = re.compile(rf"{_MAKE_VAR_NAME}\Z")
_MAKE_RULE_RE = re.compile(
    rf"^[ ]*(?P<targets>{_MAKE_NAME}(?:[ \t]+{_MAKE_NAME})*)"
    r"[ \t]*(?P<sep>::?)(?![=:])(?P<body>.*)$")
_MAKE_ASSIGN_OP = r"(?:\?=|:::=|::=|:=|\+=|!=|=)"
_MAKE_OVERRIDE_RE = re.compile(
    rf"^[ ]*(?:(?:export|private)[ \t]+)*override[ \t]+"
    rf"(?:(?:export|private)[ \t]+)*(?:define[ \t]+)?"
    rf"({_MAKE_VAR_NAME})[ \t]*{_MAKE_ASSIGN_OP}")
_MAKE_OVERRIDE_DEFINE_RE = re.compile(
    r"^[ ]*(?:(?:export|private)[ \t]+)*override[ \t]+"
    rf"(?:(?:export|private)[ \t]+)*define[ \t]+({_MAKE_VAR_NAME})")
_MAKE_TARGET_ASSIGN_RE = re.compile(
    rf"^[ \t]*(?P<mods>(?:(?:export|private|override)[ \t]+)*)"
    rf"(?P<name>{_MAKE_VAR_NAME})[ \t]*{_MAKE_ASSIGN_OP}")
_MAKE_DEFINE_RE = re.compile(
    r"^[ ]*(?:(?:export|private|override)[ \t]+)*define(?:[ \t]|$)")
_MAKE_ENDEF_RE = re.compile(r"^[ ]*endef(?:[ \t]|$)")
_MAKE_RECIPEPREFIX_RE = re.compile(
    r"^[ ]*(?:(?:export|private|override)[ \t]+)*"
    r"\.RECIPEPREFIX[ \t]*(?P<op>\?=|:::=|::=|:=|\+=|!=|=)"
    r"(?P<value>.*)$")
_MAKE_CONDITIONAL_RE = re.compile(
    r"^[ ]*(?:ifeq|ifneq|ifdef|ifndef|else|endif)(?:[ \t(]|$)")

# Make supplies these to recipes as execution metadata rather than caller knobs.
# Ordinary tool variables such as CC and CXX deliberately stay eligible: unlike
# these bookkeeping values, they are useful command-line configuration inputs.
_MAKE_INTERNAL_VARS = {
    "CURDIR", "GNUMAKEFLAGS", "MAKE", "MAKECMDGOALS", "MAKEFILE_LIST",
    "MAKEFILES", "MAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "MAKE_RESTARTS",
    "MAKE_TERMERR", "MAKE_TERMOUT", "MAKE_VERSION", "MAKE_HOST", "MFLAGS",
}


def _make_extend_unique(dest: list[str],
                        values: list[str] | tuple[str, ...]) -> None:
    for value in values:
        if value not in dest:
            dest.append(value)


def _make_continues(line: str) -> bool:
    stripped = line.rstrip()
    return (bool(stripped)
            and (len(stripped) - len(stripped.rstrip("\\"))) % 2 == 1)


def _make_without_comment(recipe: str) -> str:
    """Drop shell-style recipe comments without treating quoted # as comments."""
    quote = ""
    escaped = False
    for i, char in enumerate(recipe):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in ("'", '"'):
            quote = "" if quote == char else char if not quote else quote
        elif char == "#" and not quote and (i == 0 or recipe[i - 1].isspace()
                                             or recipe[i - 1] in "@-+;|&()"):
            return recipe[:i]
    return recipe


def _make_syntax_without_comment(text: str) -> str:
    """Strip an unescaped Make comment, including one attached to a word."""
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "#":
            return text[:index]
    return text


def _make_rule_parts(body: str) -> tuple[str, str, bool]:
    """Split prerequisites from an inline recipe using Make comment rules."""
    escaped = False
    for index, char in enumerate(body):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "#":
            return body[:index], "", False
        elif char == ";":
            return body[:index], body[index + 1:], True
    return body, "", False


def _make_recipe_prefixes(lines: list[str],
                          define_mask: list[bool]) -> list[str]:
    """Return the recipe-prefix character active for each physical line."""
    value = ""
    defined = False
    prefixes: list[str] = []
    for index, line in enumerate(lines):
        prefixes.append(value[:1] or "\t")
        if define_mask[index]:
            continue
        match = _MAKE_RECIPEPREFIX_RE.match(line)
        if not match:
            continue
        new_value = _make_syntax_without_comment(match.group("value")).lstrip()
        if new_value.startswith(r"\#"):
            new_value = new_value[1:]
        op = match.group("op")
        if op == "?=" and defined:
            continue
        if op == "+=" and defined and value:
            value += (" " if new_value else "") + new_value
        else:
            value = new_value
        defined = True
    return prefixes


def _make_reference_end(recipe: str, start: int) -> int | None:
    """Find the close of a possibly nested `$(...)` or `${...}` reference."""
    closes = {"(": ")", "{": "}"}
    stack = [closes[recipe[start + 1]]]
    index = start + 2
    while index < len(recipe):
        char = recipe[index]
        if char == "$" and index + 1 < len(recipe) \
                and recipe[index + 1] in closes:
            stack.append(closes[recipe[index + 1]])
            index += 2
            continue
        if char == stack[-1]:
            stack.pop()
            if not stack:
                return index
        index += 1
    return None


def _make_reference_name(content: str) -> str | None:
    """Return a plain/substitution variable name, never a Make function."""
    name, separator, substitution = content.partition(":")
    if not _MAKE_VAR_NAME_RE.fullmatch(name):
        return None
    if separator and "=" not in substitution:
        return None
    return name


def _make_var_refs(recipe: str) -> list[str]:
    """Caller variables, including substitutions but excluding functions."""
    refs: list[str] = []
    index = 0
    while index + 1 < len(recipe):
        if recipe[index] != "$" or recipe[index + 1] not in "({":
            index += 1
            continue
        dollar_count = 1
        previous = index - 1
        while previous >= 0 and recipe[previous] == "$":
            dollar_count += 1
            previous -= 1
        end = _make_reference_end(recipe, index)
        if end is not None and dollar_count % 2:
            name = _make_reference_name(recipe[index + 2:end])
            if name is not None and name not in refs:
                refs.append(name)
        index += 1
    return refs


def _extract_make(text: str, rel: str) -> list[Symbol]:
    """Targets, prerequisite calls, and caller-settable recipe variables."""
    stem = Path(rel).name
    lines = text.splitlines()
    # Mask (rather than delete) define bodies so symbol locations remain source
    # locations. Nested defines are legal and their tabbed text is not a recipe.
    define_mask = [False] * len(lines)
    depth = 0
    for index, line in enumerate(lines):
        if _MAKE_DEFINE_RE.match(line):
            depth += 1
        if depth:
            define_mask[index] = True
        if depth and _MAKE_ENDEF_RE.match(line):
            depth -= 1

    recipe_prefixes = _make_recipe_prefixes(lines, define_mask)

    pinned: set[str] = set()
    for index, line in enumerate(lines):
        if line.startswith(recipe_prefixes[index]):
            continue
        match = (_MAKE_OVERRIDE_DEFINE_RE.match(line)
                 if define_mask[index] else _MAKE_OVERRIDE_RE.match(line))
        if match:
            pinned.add(match.group(1))
    symbols: list[Symbol] = [Symbol(
        name=stem, kind="class", file=rel, line=1,
        signature=f"makefile {stem}", visibility="pub", lang="make")]

    # name -> first source location plus ordered, mergeable rule facts
    targets: dict[str, dict] = {}
    i = 0
    while i < len(lines):
        if define_mask[i] or lines[i].startswith(recipe_prefixes[i]):
            i += 1
            continue
        line_no = i + 1
        logical = lines[i]
        end = i
        while _make_continues(logical) and end + 1 < len(lines):
            logical = logical.rstrip()[:-1] + " " + lines[end + 1].strip()
            end += 1
        match = _MAKE_RULE_RE.match(logical)
        if not match:
            i = end + 1
            continue

        names = [name for name in match.group("targets").split()
                 if not name.startswith(".") and "%" not in name]
        if not names:
            i = end + 1
            continue
        body, inline, has_inline_recipe = _make_rule_parts(match.group("body"))
        body = body.strip()
        assignment = _MAKE_TARGET_ASSIGN_RE.match(body)
        prereqs = [] if assignment else [
            word for word in body.split()
            if word not in ("|", ".WAIT")
            and re.fullmatch(_MAKE_NAME, word) and "%" not in word
        ]

        recipe: list[str] = []
        if _make_without_comment(inline).strip():
            recipe.append(inline)
        next_line = end + 1
        continuing_recipe = bool(recipe and _make_continues(recipe[-1]))
        while next_line < len(lines):
            candidate = lines[next_line]
            prefix = recipe_prefixes[next_line]
            if continuing_recipe:
                piece = (candidate[len(prefix):]
                         if candidate.startswith(prefix) else candidate)
                recipe.append(piece)
                continuing_recipe = _make_continues(piece)
                next_line += 1
            elif candidate.startswith(prefix):
                piece = candidate[len(prefix):]
                recipe.append(piece)
                continuing_recipe = _make_continues(piece)
                next_line += 1
            elif not candidate.strip() or candidate.lstrip().startswith("#"):
                next_line += 1
            elif _MAKE_CONDITIONAL_RE.match(
                    _make_syntax_without_comment(candidate).rstrip()):
                # Conditionals are resolved before Make parses rules. Recipes
                # in every branch therefore still belong to this target; the
                # static map conservatively retains all branch dependencies.
                next_line += 1
            else:
                break

        refs: list[str] = []
        for recipe_line in recipe:
            for ref in _make_var_refs(_make_without_comment(recipe_line)):
                if ref not in _MAKE_INTERNAL_VARS:
                    _make_extend_unique(refs, (ref,))
        for name in names:
            facts = targets.setdefault(name, {
                "line": line_no, "params": [], "calls": [], "size": 0,
                "pinned": set(), "double_colon": match.group("sep") == "::",
            })
            _make_extend_unique(facts["calls"], prereqs)
            if has_inline_recipe or recipe:
                if match.group("sep") == "::" or facts["double_colon"]:
                    _make_extend_unique(facts["params"], refs)
                    facts["size"] += len(recipe)
                else:
                    # GNU Make keeps all prerequisites from repeated ordinary
                    # rules, but only the last rule's non-empty recipe.
                    facts["params"] = list(refs)
                    facts["size"] = len(recipe)
            if assignment and "override" in assignment.group("mods").split():
                facts["pinned"].add(assignment.group("name"))
        i = next_line

    for name, facts in targets.items():
        params = [p for p in facts["params"]
                  if p not in pinned and p not in facts["pinned"]]
        symbols.append(Symbol(
            name=name, kind="method", file=rel, line=facts["line"],
            signature=f"{name}({','.join(params)})",
            params=params, param_names=list(params),
            visibility="priv" if name.startswith("_") else "pub",
            container=stem, lang="make", calls=facts["calls"],
            size=facts["size"]))
    return symbols
