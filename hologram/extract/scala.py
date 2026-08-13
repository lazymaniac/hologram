from __future__ import annotations

from ..symbols import Symbol, _base_type, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field,
                          _ast_text, _body_lines)

# ---------------------------------------------------------------------------
# Scala extraction (case classes as records, traits, objects, typed params,
# val bindings) — modeled on the Kotlin extractor
# ---------------------------------------------------------------------------

_SC_TYPE_NODES = ("type_identifier", "generic_type", "function_type",
                  "compound_type", "projected_type")


def _sc_vis(node) -> str:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    for m in (mods.children if mods is not None else ()):
        if m.type == "access_modifier":
            return "priv"
    return "pub"


def _sc_params(plist) -> tuple[list[str], list[str], dict[str, str]]:
    types: list[str] = []
    names: list[str] = []
    binds: dict[str, str] = {}
    for p in (plist.children if plist is not None else ()):
        if p.type not in ("parameter", "class_parameter"):
            continue
        ident = next((c for c in p.children if c.type == "identifier"), None)
        t = next((c for c in p.children if c.type in _SC_TYPE_NODES), None)
        name = _ast_text(ident) if ident is not None else ""
        ttext = tight_type(_ast_text(t)) if t is not None else "?"
        types.append(ttext)
        names.append(name)
        if name and t is not None:
            binds[name] = _base_type(ttext)
    return types, names, binds


def _sc_return(fn) -> str | None:
    rt = _ast_field(fn, "return_type")
    return tight_type(_ast_text(rt)) if rt is not None else None


def _sc_call_entry(n) -> tuple[str, str]:
    if n.type == "instance_expression":
        t = next((c for c in n.children if c.type in _SC_TYPE_NODES), None)
        entry = _base_type(_ast_text(t)) if t is not None else ""
        return entry, entry
    head = _ast_field(n, "function")
    if head is None:
        head = n.children[0] if n.children else None
    if head is None:
        return "", ""
    if head.type == "identifier":
        name = _ast_text(head)
        return name, name
    if head.type == "field_expression":
        value = _ast_field(head, "value")
        field = _ast_field(head, "field")
        if field is None:
            return "", ""
        name = _ast_text(field)
        if value is not None and value.type == "identifier":
            return name, f"{_ast_text(value)}.{name}"
        return name, name
    return "", ""


def _sc_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for vd in _ast_collect(body, ("val_definition", "var_definition")):
        ident = next((c for c in vd.children if c.type == "identifier"), None)
        if ident is None:
            continue
        t = _ast_field(vd, "type")
        if t is not None:
            binds[_ast_text(ident)] = _base_type(tight_type(_ast_text(t)))
            continue
        rhs = next((c for c in vd.children if c.type == "instance_expression"),
                   None)
        if rhs is not None:
            tn = next((c for c in rhs.children if c.type in _SC_TYPE_NODES), None)
            if tn is not None:
                binds[_ast_text(ident)] = _base_type(_ast_text(tn))
    return binds


def _sc_fn_symbol(fn, rel: str, container: str | None,
                  class_binds: dict[str, str]) -> Symbol:
    name = _ast_text(_ast_field(fn, "name") or next(
        (c for c in fn.children if c.type == "identifier"), None))
    params, pnames, pbinds = _sc_params(next(
        (c for c in fn.children if c.type == "parameters"), None))
    body = _ast_field(fn, "body")
    returns = _sc_return(fn)
    ret_suffix = f":{returns}" if returns and returns != "Unit" else ""
    return Symbol(
        name=name, kind="method" if container else "fn", file=rel,
        line=fn.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=pnames, returns=returns,
        visibility=_sc_vis(fn), container=container, lang="scala",
        calls=_ast_calls(body, name,
                         ("call_expression", "instance_expression"),
                         _sc_call_entry),
        bindings={**class_binds, **pbinds, **_sc_local_bindings(body)},
        size=_body_lines(body),
    )


def _extract_scala(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["scala"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node,
                           ("class_definition", "object_definition",
                            "trait_definition")):
        name = _ast_text(_ast_field(tn, "name") or next(
            (c for c in tn.children if c.type == "identifier"), None))
        if not name:
            continue
        is_case = any(c.type == "case" or _ast_text(c) == "case"
                      for c in tn.children if c.child_count == 0)
        kind = ("interface" if tn.type == "trait_definition"
                else "record" if is_case else "class")
        cparams, _, class_binds = _sc_params(next(
            (c for c in tn.children if c.type == "class_parameters"), None))
        supers = []
        ext = next((c for c in tn.children if c.type == "extends_clause"), None)
        if ext is not None:
            supers = [_base_type(_ast_text(c)) for c in ext.children
                      if c.type in _SC_TYPE_NODES]
        body = next((c for c in tn.children if c.type == "template_body"), None)
        fields = []
        if tn.type == "class_definition":
            pl = next((c for c in tn.children if c.type == "class_parameters"),
                      None)
            if is_case and pl is not None:
                fields = [_ast_text(i) for p in pl.children
                          if p.type == "class_parameter"
                          for i in p.children if i.type == "identifier"]
        for vd in (body.children if body is not None else ()):
            if vd.type in ("val_definition", "var_definition"):
                ident = next((c for c in vd.children if c.type == "identifier"),
                             None)
                if ident is not None:
                    fields.append(_ast_text(ident))
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{tn.type.split('_')[0]} {name}", params=cparams,
            fields=list(dict.fromkeys(fields)), supers=supers,
            visibility=_sc_vis(tn), lang="scala",
        ))
        for m in (body.children if body is not None else ()):
            if m.type in ("function_definition", "function_declaration"):
                symbols.append(_sc_fn_symbol(m, rel, name, class_binds))
    return symbols
