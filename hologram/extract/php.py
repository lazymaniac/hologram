from __future__ import annotations

import re

from ..symbols import Symbol, _base_type, const_signature, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field,
                          _ast_text, _body_lines)

# ---------------------------------------------------------------------------
# PHP extraction (classes/interfaces/traits/enums, typed params, $x = new T()
# bindings, throws) — modeled on the C# extractor
# ---------------------------------------------------------------------------

_PHP_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "class",
    "enum_declaration": "enum",
}

_PHP_TYPE_NODES = ("named_type", "primitive_type", "optional_type", "union_type")


def _php_vis(node) -> str:
    for m in node.children:
        if m.type == "visibility_modifier" and _ast_text(m) in ("private",
                                                                "protected"):
            return "priv"
    return "pub"


def _php_var_name(vnode) -> str:
    """`$name` without the sigil."""
    n = _ast_field(vnode, "name")
    return _ast_text(n) if n is not None else _ast_text(vnode).lstrip("$")


def _php_params(plist) -> tuple[list[str], list[str], dict[str, str]]:
    types: list[str] = []
    names: list[str] = []
    binds: dict[str, str] = {}
    for p in (plist.children if plist is not None else ()):
        if p.type not in ("simple_parameter", "property_promotion_parameter"):
            continue
        t = next((c for c in p.children if c.type in _PHP_TYPE_NODES), None)
        var = next((c for c in p.children if c.type == "variable_name"), None)
        name = _php_var_name(var) if var is not None else ""
        ttext = tight_type(_ast_text(t)) if t is not None else "?"
        types.append(ttext)
        names.append(name)
        if name and t is not None and t.type == "named_type":
            binds[name] = _base_type(ttext.lstrip("?"))
    return types, names, binds


def _php_return(m) -> str | None:
    rt = _ast_field(m, "return_type")
    if rt is None:
        return None
    return tight_type(_ast_text(rt).lstrip(":").strip())


def _php_call_entry(n) -> tuple[str, str]:
    if n.type == "object_creation_expression":
        t = next((c for c in n.children if c.type in ("name", "qualified_name")),
                 None)
        entry = _ast_text(t).split("\\")[-1] if t is not None else ""
        return entry, entry
    if n.type == "function_call_expression":
        fn = _ast_field(n, "function")
        if fn is not None and fn.type in ("name", "qualified_name"):
            name = _ast_text(fn).split("\\")[-1]
            return name, name
        return "", ""
    if n.type in ("member_call_expression", "scoped_call_expression"):
        name = _ast_text(_ast_field(n, "name"))
        obj = _ast_field(n, "object") or _ast_field(n, "scope")
        if obj is not None and obj.type == "variable_name":
            return name, f"{_php_var_name(obj)}.{name}"
        if obj is not None and obj.type == "name":
            return name, f"{_ast_text(obj)}.{name}"
        return name, name
    return "", ""


_PHP_CALL_KINDS = ("function_call_expression", "member_call_expression",
                   "scoped_call_expression", "object_creation_expression")


def _php_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for asn in _ast_collect(body, ("assignment_expression",)):
        lhs = _ast_field(asn, "left")
        rhs = _ast_field(asn, "right")
        if (lhs is not None and lhs.type == "variable_name"
                and rhs is not None and rhs.type == "object_creation_expression"):
            t = next((c for c in rhs.children
                      if c.type in ("name", "qualified_name")), None)
            if t is not None:
                binds[_php_var_name(lhs)] = _ast_text(t).split("\\")[-1]
    return binds


def _php_raises(body) -> list[str]:
    raises: list[str] = []
    if body is None:
        return raises
    for th in _ast_collect(body, ("throw_expression", "throw_statement")):
        obj = next((c for c in th.children
                    if c.type == "object_creation_expression"), None)
        if obj is None:
            continue
        t = next((c for c in obj.children if c.type in ("name", "qualified_name")),
                 None)
        if t is not None:
            name = _ast_text(t).split("\\")[-1]
            if name and name not in raises:
                raises.append(name)
    return raises


_PHP_CONST_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")


def _php_attributes(node) -> list[str]:
    """PHP 8 #[Attr(...)] groups on a class or method declaration."""
    return [tight_type(_ast_text(a).lstrip("\\"))
            for al in node.children if al.type == "attribute_list"
            for grp in al.children if grp.type == "attribute_group"
            for a in grp.children if a.type == "attribute"]


def _php_fn_symbol(m, rel: str, container: str | None, cname_binds: dict[str, str],
                   kind: str, vis: str) -> Symbol:
    name = _ast_text(_ast_field(m, "name"))
    params, pnames, pbinds = _php_params(_ast_field(m, "parameters"))
    body = _ast_field(m, "body")
    returns = _php_return(m)
    display = container if kind == "ctor" else name
    ret_suffix = f":{returns}" if returns and returns != "void" else ""
    return Symbol(
        name=display, kind=kind, file=rel, line=m.start_point[0] + 1,
        signature=f"{display}({','.join(params)})"
                  + ("" if kind == "ctor" else ret_suffix),
        params=params, param_names=pnames,
        returns=container if kind == "ctor" else returns,
        visibility=vis, container=container, lang="php",
        calls=_ast_calls(body, name, _PHP_CALL_KINDS, _php_call_entry),
        bindings={**cname_binds, **pbinds, **_php_local_bindings(body)},
        size=_body_lines(body), raises=_php_raises(body),
        decorators=_php_attributes(m),
    )


def _extract_php(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["php"].parse(text.encode())
    symbols: list[Symbol] = []
    root = tree.root_node
    tops = list(root.children)
    for ns in [c for c in root.children if c.type == "namespace_definition"]:
        nsbody = _ast_field(ns, "body")
        if nsbody is not None:
            tops.extend(nsbody.children)
    for tn in tops:
        kind = _PHP_TYPE_NODE_KINDS.get(tn.type)
        if kind is None:
            if tn.type == "function_definition":
                symbols.append(_php_fn_symbol(tn, rel, None, {}, "fn", "pub"))
            continue
        name = _ast_text(_ast_field(tn, "name"))
        supers: list[str] = []
        for cl in tn.children:
            if cl.type in ("base_clause", "class_interface_clause"):
                supers += [_ast_text(c).split("\\")[-1] for c in cl.children
                           if c.type in ("name", "qualified_name")]
        fields: list[str] = []
        params: list[str] = []
        body = _ast_field(tn, "body")
        if kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(e, "name"))
                      for e in _ast_collect(body, ("enum_case",))]
        class_binds = {"this": name}
        for member in (body.children if body is not None else ()):
            if member.type == "property_declaration":
                t = next((c for c in member.children if c.type in _PHP_TYPE_NODES),
                         None)
                for el in _ast_collect(member, ("property_element",)):
                    var = next((c for c in el.children
                                if c.type == "variable_name"), None)
                    if var is not None:
                        fname = _php_var_name(var)
                        fields.append(fname)
                        if t is not None and t.type == "named_type":
                            class_binds[fname] = _base_type(_ast_text(t))
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=params, fields=fields,
            supers=supers, visibility="pub", lang="php",
            decorators=_php_attributes(tn),
        ))
        for member in (body.children if body is not None else ()):
            if member.type != "const_declaration":
                continue
            for el in _ast_collect(member, ("const_element",)):
                cname_node = next((c for c in el.children if c.type == "name"),
                                  None)
                if cname_node is None:
                    continue
                cname = _ast_text(cname_node)
                if not _PHP_CONST_NAME_RE.fullmatch(cname):
                    continue
                value = next(
                    (_ast_text(c) for c in el.children
                     if c.type in ("integer", "float", "string",
                                   "encapsed_string", "boolean")), None)
                symbols.append(Symbol(
                    name=cname, kind="const", file=rel,
                    line=el.start_point[0] + 1,
                    signature=const_signature(cname, value),
                    visibility=_php_vis(member), lang="php"))
        for m in (body.children if body is not None else ()):
            if m.type != "method_declaration":
                continue
            mname = _ast_text(_ast_field(m, "name"))
            mkind = "ctor" if mname == "__construct" else "method"
            symbols.append(_php_fn_symbol(m, rel, name, class_binds, mkind,
                                          _php_vis(m)))
    return symbols
