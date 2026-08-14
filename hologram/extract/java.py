from __future__ import annotations

import re

from ..symbols import (Symbol, _base_type, _heritage, _parse_throws,
                       const_signature, split_params, tight_annotation, tight_type)
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines)

# ---------------------------------------------------------------------------
# Java extraction
# ---------------------------------------------------------------------------

_CONST_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")

_JAVA_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


def _ast_modifiers(node) -> str:
    for c in node.children:
        if c.type == "modifiers":
            return _ast_text(c)
    return ""


def _java_annotations(node) -> list[str]:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    return [tight_annotation(_ast_text(a).lstrip("@"))
            for a in (mods.children if mods is not None else ())
            if a.type in ("marker_annotation", "annotation")]


def _ast_param_types(node) -> list[str]:
    """Declared parameter types from a formal_parameters node."""
    raw = _ast_text(node)
    return split_params(raw[1:-1]) if raw.startswith("(") else split_params(raw)


def _java_param_names(node) -> list[str]:
    names: list[str] = []
    for p in (node.children if node is not None else ()):
        if p.type not in ("formal_parameter", "spread_parameter"):
            continue
        name = _ast_field(p, "name")
        names.append(_ast_text(name) if name is not None else "")
    return names


def _ast_vis(mods: str) -> str:
    return "priv" if any(v in mods for v in ("private", "protected")) else "pub"


def _java_call_entry(n) -> tuple[str, str]:
    if n.type == "object_creation_expression":
        entry = re.sub(r"<.*", "", _ast_text(_ast_field(n, "type")))
        return entry, entry
    name = _ast_text(_ast_field(n, "name"))
    obj = _ast_field(n, "object")
    entry = (f"{_ast_text(obj)}.{name}"
             if obj is not None and obj.type == "identifier" else name)
    return name, entry


def _java_calls(body, own_name: str) -> list[str]:
    return _ast_calls(body, own_name,
                      ("method_invocation", "object_creation_expression"),
                      _java_call_entry)


def _java_param_bindings(params_node) -> dict[str, str]:
    binds: dict[str, str] = {}
    if params_node is None:
        return binds
    for p in params_node.children:
        if p.type == "formal_parameter":
            t, n = _ast_field(p, "type"), _ast_field(p, "name")
            if t is not None and n is not None:
                binds[_ast_text(n)] = _base_type(_ast_text(t))
    return binds


def _java_class_bindings(tn) -> dict[str, str]:
    """Field and record-component types visible to every method of the type."""
    binds = _java_param_bindings(_ast_field(tn, "parameters"))  # record components
    body = _ast_field(tn, "body")
    for f in (body.children if body is not None else ()):
        if f.type == "field_declaration":
            t = _ast_text(_ast_field(f, "type"))
            for dec in f.children:
                if dec.type == "variable_declarator":
                    binds[_ast_text(_ast_field(dec, "name"))] = _base_type(t)
    return binds


def _java_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for d in _ast_collect(body, ("local_variable_declaration", "enhanced_for_statement")):
        t = _ast_text(_ast_field(d, "type"))
        if d.type == "enhanced_for_statement":
            n = _ast_field(d, "name")
            if n is not None and t != "var":
                binds[_ast_text(n)] = _base_type(t)
            continue
        for dec in d.children:
            if dec.type != "variable_declarator":
                continue
            name = _ast_text(_ast_field(dec, "name"))
            if t == "var":
                val = _ast_field(dec, "value")
                if val is not None and val.type == "object_creation_expression":
                    binds[name] = _base_type(_ast_text(_ast_field(val, "type")))
            else:
                binds[name] = _base_type(t)
    return binds


def _java_method_symbol(m, type_name: str, rel: str, class_binds: dict[str, str]) -> Symbol:
    name = _ast_text(_ast_field(m, "name"))
    params = _ast_param_types(_ast_field(m, "parameters"))
    mods = _ast_modifiers(m)
    body = _ast_field(m, "body")
    binds = {**class_binds,
             **_java_param_bindings(_ast_field(m, "parameters")),
             **_java_local_bindings(body)}
    throws = []
    for c in m.children:
        if c.type == "throws":
            throws = _parse_throws(_ast_text(c).removeprefix("throws"))
    if m.type == "constructor_declaration":
        return Symbol(
            name=name, kind="ctor", file=rel, line=m.start_point[0] + 1,
            signature=f"{name}({','.join(params)})", params=params,
            param_names=_java_param_names(_ast_field(m, "parameters")), returns=name,
            visibility=_ast_vis(mods),
            container=type_name, lang="java", raises=throws,
            calls=_java_calls(body, name), bindings=binds,
            decorators=_java_annotations(m), size=_body_lines(body),
        )
    returns = _ast_text(_ast_field(m, "type"))
    ret_suffix = f":{returns}" if returns != "void" else ""
    return Symbol(
        name=name, kind="method", file=rel, line=m.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=_java_param_names(_ast_field(m, "parameters")),
        returns=returns,
        visibility=_ast_vis(mods),
        container=type_name, lang="java",
        calls=_java_calls(body, name), raises=throws, bindings=binds,
        decorators=_java_annotations(m), size=_body_lines(body),
    )


def _extract_java(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["java"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, _JAVA_TYPE_NODE_KINDS):
        kind = _JAVA_TYPE_NODE_KINDS[tn.type]
        name = _ast_text(_ast_field(tn, "name"))
        body = _ast_field(tn, "body")
        header_end = body.start_byte if body is not None else tn.end_byte
        header = text.encode()[tn.start_byte:header_end].decode(errors="replace")
        supers, permits = _heritage(re.sub(r"\(.*?\)", "", header, flags=re.S))
        mods = _ast_modifiers(tn)
        params: list[str] = []
        if kind == "record":
            pnode = _ast_field(tn, "parameters")
            params = _ast_param_types(pnode) if pnode is not None else []
        elif kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(c, "name"))
                      for c in body.children if c.type == "enum_constant"]
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=(f"sealed {kind} {name}" if "sealed" in mods else f"{kind} {name}"),
            params=params,
            fields=(list(_java_class_bindings(tn)) if kind != "enum" else []),
            visibility=_ast_vis(mods), lang="java",
            supers=supers, permits=permits,
            decorators=_java_annotations(tn),
        ))
        if body is None:
            continue
        type_sym = symbols[-1]
        const_names: set[str] = set()
        for f in body.children:
            if f.type != "field_declaration":
                continue
            fmods = _ast_modifiers(f)
            if "static" not in fmods or "final" not in fmods:
                continue
            for dec in f.children:
                if dec.type != "variable_declarator":
                    continue
                cname = _ast_text(_ast_field(dec, "name"))
                val = _ast_field(dec, "value")
                if not _CONST_NAME_RE.fullmatch(cname) or val is None:
                    continue
                if val.type.endswith("_literal") or val.type in ("true", "false"):
                    csig = const_signature(cname, _ast_text(val))
                elif val.type == "array_initializer":
                    csig = const_signature(cname, None)
                else:
                    continue
                const_names.add(cname)
                symbols.append(Symbol(
                    name=cname, kind="const", file=rel,
                    line=dec.start_point[0] + 1, signature=csig,
                    visibility=_ast_vis(fmods), lang="java"))
        # consts already carry their own `=` line; don't restate them as fields
        type_sym.fields = [f for f in type_sym.fields if f not in const_names]
        class_binds = _java_class_bindings(tn)
        containers = list(body.children)
        if kind == "enum":
            containers = [c for c in body.children if c.type == "enum_body_declarations"]
            containers = [gc for c in containers for gc in c.children] or list(body.children)
        for c in containers:
            if c.type in ("method_declaration", "constructor_declaration"):
                symbols.append(_java_method_symbol(c, name, rel, class_binds))
    return symbols

