from __future__ import annotations


from ..symbols import Symbol, _base_type, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines)

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
            bounds = next((c for c in tn.children if c.type == "trait_bounds"), None)
            supers = [_base_type(_ast_text(b))
                      for b in (bounds.children if bounds is not None else ())
                      if b.type in ("type_identifier", "scoped_type_identifier",
                                    "generic_type")]
            sym = Symbol(name=name, kind="interface", file=rel, line=line,
                         signature=f"trait {name}", supers=supers,
                         visibility=vis, lang="rust")
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

