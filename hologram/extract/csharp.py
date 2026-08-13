from __future__ import annotations


from ..symbols import Symbol, _base_type, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines)

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


def _cs_param_names(plist) -> list[str]:
    names: list[str] = []
    for p in (plist.children if plist is not None else ()):
        if p.type != "parameter":
            continue
        name = _ast_field(p, "name")
        names.append(_ast_text(name) if name is not None else "")
    return names


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
        type_fields: list[str] = []
        if kind == "record":
            for c in tn.children:
                if c.type == "parameter_list":
                    params, record_binds = _cs_params(c)
                    type_fields.extend(record_binds)
        elif kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(m, "name"))
                      for m in _ast_collect(body, ("enum_member_declaration",))]
        for member in (body.children if body is not None else ()):
            if member.type == "field_declaration":
                type_fields.extend(_cs_local_bindings(member))
            elif member.type == "property_declaration":
                field_name = _ast_field(member, "name")
                if field_name is not None:
                    type_fields.append(_ast_text(field_name))
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=params, fields=list(dict.fromkeys(type_fields)),
            supers=supers,
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
                    params=mparams,
                    param_names=_cs_param_names(_ast_field(m, "parameters")),
                    returns=mname,
                    visibility=_cs_vis(m), container=name, lang="csharp",
                    calls=calls, bindings=binds,
                ))
                continue
            returns = tight_type(_ast_text(_ast_field(m, "returns")))
            ret_suffix = f":{returns}" if returns and returns != "void" else ""
            symbols.append(Symbol(
                name=mname, kind="method", file=rel, line=m.start_point[0] + 1,
                signature=f"{mname}({','.join(mparams)}){ret_suffix}",
                params=mparams,
                param_names=_cs_param_names(_ast_field(m, "parameters")),
                returns=returns,
                visibility=_cs_vis(m), container=name, lang="csharp",
                calls=calls, bindings=binds, size=_body_lines(mbody),
            ))
    return symbols

