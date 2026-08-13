from __future__ import annotations

import re

from ..symbols import Symbol, _IDENT_RE, _base_type, tight_type
from ..treesitter import _PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text

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


def _kt_local_bindings(body) -> dict[str, str]:
    """val/var declarations: explicit `: T` annotations, plus `val e = Engine()`
    inference when the initializer calls a single capitalized identifier."""
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for prop in _ast_collect(body, ("property_declaration",)):
        var = next((c for c in prop.children if c.type == "variable_declaration"), None)
        if var is None:
            continue
        ident = next((c for c in var.children if c.type == "identifier"), None)
        if ident is None:
            continue
        t = next((c for c in var.children
                  if c.type in ("user_type", "nullable_type")), None)
        if t is not None:
            binds[_ast_text(ident)] = _base_type(tight_type(_ast_text(t)).rstrip("?"))
            continue
        rhs = next((c for c in prop.children if c.type == "call_expression"), None)
        if rhs is not None and rhs.children:
            head = rhs.children[0]
            if head.type == "identifier":
                callee = _ast_text(head)
                if callee[:1].isupper() and _IDENT_RE.fullmatch(callee):
                    binds[_ast_text(ident)] = callee
    return binds


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
        bindings={**class_binds, **binds, **_kt_local_bindings(body)},
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

