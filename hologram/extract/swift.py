from __future__ import annotations

from ..symbols import Symbol, _base_type, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_text,
                          _body_lines)

# ---------------------------------------------------------------------------
# Swift extraction (class/struct/enum share class_declaration; protocols,
# inheritance, typed params, let/var bindings) — modeled on the Kotlin extractor
# ---------------------------------------------------------------------------

_SW_TYPE_NODES = ("user_type", "dictionary_type", "array_type", "optional_type",
                  "tuple_type", "function_type")


def _sw_vis(node) -> str:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    for m in (mods.children if mods is not None else ()):
        if m.type in ("visibility_modifier",) and _ast_text(m) in (
                "private", "fileprivate"):
            return "priv"
    return "pub"


def _sw_params(fn) -> tuple[list[str], list[str], dict[str, str]]:
    types: list[str] = []
    names: list[str] = []
    binds: dict[str, str] = {}
    for p in [c for c in fn.children if c.type == "parameter"]:
        idents = [c for c in p.children if c.type == "simple_identifier"]
        t = next((c for c in p.children if c.type in _SW_TYPE_NODES), None)
        # `quote(id x: T)` has an external and an internal name; the last
        # identifier is the one the body uses
        name = _ast_text(idents[-1]) if idents else ""
        ttext = tight_type(_ast_text(t)) if t is not None else "?"
        types.append(ttext)
        names.append(name)
        if name and t is not None and t.type == "user_type":
            binds[name] = _base_type(ttext.rstrip("?"))
    return types, names, binds


def _sw_return(fn) -> str | None:
    arrow = False
    for c in fn.children:
        if c.type == "->":
            arrow = True
        elif arrow and c.type in _SW_TYPE_NODES:
            return tight_type(_ast_text(c))
    return None


def _sw_call_entry(n) -> tuple[str, str]:
    head = n.children[0] if n.children else None
    if head is None:
        return "", ""
    if head.type == "simple_identifier":
        name = _ast_text(head)
        return name, name
    if head.type == "navigation_expression":
        target = next((c for c in head.children
                       if c.type == "simple_identifier"), None)
        suffix = next((c for c in head.children
                       if c.type == "navigation_suffix"), None)
        member = next((c for c in (suffix.children if suffix is not None else ())
                       if c.type == "simple_identifier"), None)
        if member is None:
            return "", ""
        name = _ast_text(member)
        if target is not None:
            return name, f"{_ast_text(target)}.{name}"
        return name, name
    return "", ""


def _sw_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for prop in _ast_collect(body, ("property_declaration",)):
        pat = next((c for c in prop.children if c.type == "pattern"), None)
        ident = next((c for c in (pat.children if pat is not None else ())
                      if c.type == "simple_identifier"), None)
        if ident is None:
            continue
        ann = next((c for c in prop.children if c.type == "type_annotation"), None)
        t = next((c for c in (ann.children if ann is not None else ())
                  if c.type in _SW_TYPE_NODES), None)
        if t is not None:
            binds[_ast_text(ident)] = _base_type(tight_type(_ast_text(t)))
            continue
        rhs = next((c for c in prop.children if c.type == "call_expression"), None)
        if rhs is not None and rhs.children \
                and rhs.children[0].type == "simple_identifier":
            callee = _ast_text(rhs.children[0])
            if callee[:1].isupper():
                binds[_ast_text(ident)] = callee
    return binds


def _sw_fn_symbol(fn, rel: str, container: str | None, kind: str) -> Symbol:
    if kind == "ctor":
        name = container or "init"
    else:
        name = _ast_text(next((c for c in fn.children
                               if c.type == "simple_identifier"), None))
    params, pnames, pbinds = _sw_params(fn)
    body = next((c for c in fn.children if c.type == "function_body"), None)
    returns = _sw_return(fn)
    ret_suffix = (f":{returns}" if returns and kind != "ctor"
                  and returns != "Void" else "")
    return Symbol(
        name=name, kind=kind, file=rel, line=fn.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=pnames,
        returns=container if kind == "ctor" else returns,
        visibility=_sw_vis(fn), container=container, lang="swift",
        calls=_ast_calls(body, name, ("call_expression",), _sw_call_entry),
        bindings={**pbinds, **_sw_local_bindings(body)},
        size=_body_lines(body),
    )


def _extract_swift(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["swift"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in tree.root_node.children:
        if tn.type in ("class_declaration", "protocol_declaration"):
            name = _ast_text(next((c for c in tn.children
                                   if c.type == "type_identifier"), None))
            keyword = next((_ast_text(c) for c in tn.children
                            if c.type in ("class", "struct", "enum", "protocol",
                                          "extension")), "class")
            kind = {"protocol": "interface", "enum": "enum",
                    "struct": "record"}.get(keyword, "class")
            supers = [
                _base_type(_ast_text(s))
                for spec in tn.children if spec.type == "inheritance_specifier"
                for s in spec.children if s.type == "user_type"]
            body = next((c for c in tn.children
                         if c.type in ("class_body", "protocol_body",
                                       "enum_class_body")), None)
            fields: list[str] = []
            for prop in (body.children if body is not None else ()):
                if prop.type == "property_declaration":
                    pat = next((c for c in prop.children if c.type == "pattern"),
                               None)
                    ident = next((c for c in (pat.children if pat is not None
                                              else ())
                                  if c.type == "simple_identifier"), None)
                    if ident is not None:
                        fields.append(_ast_text(ident))
            if kind == "enum" and body is not None:
                fields = []
            symbols.append(Symbol(
                name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
                signature=f"{keyword} {name}", fields=fields, supers=supers,
                visibility=_sw_vis(tn), lang="swift",
            ))
            for m in (body.children if body is not None else ()):
                if m.type in ("function_declaration",
                              "protocol_function_declaration"):
                    symbols.append(_sw_fn_symbol(m, rel, name, "method"))
                elif m.type == "init_declaration":
                    symbols.append(_sw_fn_symbol(m, rel, name, "ctor"))
        elif tn.type == "function_declaration":
            symbols.append(_sw_fn_symbol(tn, rel, None, "fn"))
    return symbols
