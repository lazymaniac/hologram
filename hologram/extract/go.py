from __future__ import annotations


from ..symbols import Symbol, _base_type, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines)

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

