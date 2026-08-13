from __future__ import annotations


from ..symbols import Symbol, _base_type, tight_type
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines)

# ---------------------------------------------------------------------------
# C / C++ extraction (shared declarator machinery)
# ---------------------------------------------------------------------------

def _c_fn_declarator(node):
    """(function_declarator, name_node) beneath a definition/declaration, peeling
    pointer/reference wrappers. name_node may be identifier/field_identifier/
    qualified_identifier."""
    fd = None
    n = _ast_field(node, "declarator")
    while n is not None:
        if n.type == "function_declarator":
            fd = n
            n = _ast_field(n, "declarator")
        elif n.type in ("pointer_declarator", "reference_declarator"):
            n = _ast_field(n, "declarator")
        else:
            break
    return fd, n


def _c_params(plist) -> tuple[list[str], dict[str, str]]:
    types: list[str] = []
    binds: dict[str, str] = {}
    if plist is None:
        return types, binds
    for p in plist.children:
        if p.type != "parameter_declaration":
            continue
        base = tight_type(_ast_text(_ast_field(p, "type")))
        d = _ast_field(p, "declarator")
        stars = _ast_text(d).count("*") if d is not None else 0
        types.append(base + "*" * stars)
        while d is not None and d.type in ("pointer_declarator", "reference_declarator"):
            d = _ast_field(d, "declarator")
        if d is not None and d.type == "identifier":
            binds[_ast_text(d)] = _base_type(base)
    return types, binds


def _c_param_names(plist) -> list[str]:
    names: list[str] = []
    for p in (plist.children if plist is not None else ()):
        if p.type != "parameter_declaration":
            continue
        d = _ast_field(p, "declarator")
        while d is not None and d.type in ("pointer_declarator", "reference_declarator",
                                            "array_declarator"):
            d = _ast_field(d, "declarator")
        names.append(_ast_text(d) if d is not None
                     and d.type in ("identifier", "field_identifier") else "")
    return names


def _c_field_names(body) -> list[str]:
    names: list[str] = []
    for declaration in (body.children if body is not None else ()):
        if declaration.type != "field_declaration":
            continue
        fd, _ = _c_fn_declarator(declaration)
        if fd is not None:
            continue
        for node in _ast_collect(declaration, ("field_identifier", "identifier")):
            name = _ast_text(node)
            if name not in names:
                names.append(name)
    return names


def _c_call_entry(n) -> tuple[str, str]:
    if n.type == "new_expression":
        entry = _base_type(_ast_text(_ast_field(n, "type")))
        return entry, entry
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    if fn.type == "field_expression":
        name = _ast_text(_ast_field(fn, "field"))
        obj = _ast_field(fn, "argument")
        entry = (f"{_ast_text(obj)}.{name}"
                 if obj is not None and obj.type == "identifier" else name)
        return name, entry
    if fn.type == "qualified_identifier":
        name = _ast_text(_ast_field(fn, "name"))
        scope = _ast_field(fn, "scope")
        return name, (f"{_base_type(_ast_text(scope))}.{name}"
                      if scope is not None else name)
    return "", ""


def _c_static(node) -> bool:
    return any(c.type == "storage_class_specifier" and _ast_text(c) == "static"
               for c in node.children)


def _c_enum_symbol(tn, name: str, rel: str, lang: str) -> Symbol:
    body = _ast_field(tn, "body")
    values = [_ast_text(_ast_field(e, "name"))
              for e in (_ast_collect(body, ("enumerator",)) if body is not None else [])]
    return Symbol(name=name, kind="enum", file=rel, line=tn.start_point[0] + 1,
                  signature=f"enum {name}", params=values, visibility="pub", lang=lang)


def _extract_c(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["c"].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, ("struct_specifier", "enum_specifier",
                                            "type_definition")):
        if tn.type == "type_definition":
            inner = _ast_field(tn, "type")
            alias = _ast_text(_ast_field(tn, "declarator"))
            if inner is None or _ast_field(inner, "body") is None or not alias:
                continue
            if inner.type == "enum_specifier":
                symbols.append(_c_enum_symbol(inner, alias, rel, "c"))
            elif inner.type == "struct_specifier":
                comps = [tight_type(_ast_text(_ast_field(f, "type")))
                         for f in _ast_collect(inner, ("field_declaration",))]
                symbols.append(Symbol(
                    name=alias, kind="class", file=rel, line=tn.start_point[0] + 1,
                    signature=f"struct {alias}", params=comps,
                    fields=_c_field_names(_ast_field(inner, "body")),
                    visibility="pub", lang="c"))
            continue
        name_node = _ast_field(tn, "name")
        if name_node is None or _ast_field(tn, "body") is None:
            continue
        name = _ast_text(name_node)
        if tn.type == "enum_specifier":
            symbols.append(_c_enum_symbol(tn, name, rel, "c"))
        else:
            comps = [tight_type(_ast_text(_ast_field(f, "type")))
                     for f in _ast_collect(_ast_field(tn, "body"),
                                           ("field_declaration",))]
            symbols.append(Symbol(
                name=name, kind="class", file=rel, line=tn.start_point[0] + 1,
                signature=f"struct {name}", params=comps,
                fields=_c_field_names(_ast_field(tn, "body")),
                visibility="pub", lang="c"))
    defined_fns: set[str] = set()
    for fn in _ast_collect(tree.root_node, ("function_definition",)):
        fd, name_node = _c_fn_declarator(fn)
        if fd is None or name_node is None or name_node.type != "identifier":
            continue
        name = _ast_text(name_node)
        defined_fns.add(name)
        params, binds = _c_params(_ast_field(fd, "parameters"))
        returns = tight_type(_ast_text(_ast_field(fn, "type")))
        body = _ast_field(fn, "body")
        symbols.append(Symbol(
            name=name, kind="fn", file=rel, line=fn.start_point[0] + 1,
            signature=f"{name}({','.join(params)})"
                      + (f":{returns}" if returns != "void" else ""),
            params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
            returns=returns,
            visibility="priv" if _c_static(fn) else "pub", lang="c",
            calls=_ast_calls(body, name, ("call_expression",), _c_call_entry),
            size=_body_lines(body),
            bindings=binds,
        ))
    for decl in tree.root_node.children:  # top-level prototypes (headers)
        if decl.type != "declaration":
            continue
        fd, name_node = _c_fn_declarator(decl)
        if fd is None or name_node is None or name_node.type != "identifier":
            continue
        name = _ast_text(name_node)
        if name in defined_fns:
            continue
        params, _ = _c_params(_ast_field(fd, "parameters"))
        returns = tight_type(_ast_text(_ast_field(decl, "type")))
        symbols.append(Symbol(
            name=name, kind="fn", file=rel, line=decl.start_point[0] + 1,
            signature=f"{name}({','.join(params)})"
                      + (f":{returns}" if returns != "void" else ""),
            params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
            returns=returns,
            visibility="priv" if _c_static(decl) else "pub", lang="c",
        ))
    return symbols


def _cpp_raises(body) -> list[str]:
    """Types thrown by value or constructed in throw statements."""
    raises: list[str] = []
    if body is None:
        return raises
    for th in _ast_collect(body, ("throw_statement",)):
        call = next((c for c in th.children if c.type == "call_expression"), None)
        fn = _ast_field(call, "function") if call is not None else None
        if fn is not None and fn.type in ("identifier", "qualified_identifier"):
            name = _base_type(_ast_text(fn)).split("::")[-1]
            if name and name not in raises:
                raises.append(name)
    return raises


def _extract_cpp(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["cpp"].parse(text.encode())
    symbols: list[Symbol] = []
    members: dict[tuple[str, str], Symbol] = {}
    for tn in _ast_collect(tree.root_node, ("class_specifier", "struct_specifier",
                                            "enum_specifier")):
        name_node = _ast_field(tn, "name")
        body = _ast_field(tn, "body")
        if name_node is None or body is None:
            continue
        cname = _ast_text(name_node)
        if tn.type == "enum_specifier":
            symbols.append(_c_enum_symbol(tn, cname, rel, "cpp"))
            continue
        comps: list[str] = []
        supers = []
        for c in tn.children:
            if c.type == "base_class_clause":
                supers = [_base_type(_ast_text(b)) for b in c.children
                          if b.type in ("type_identifier", "qualified_identifier")]
        access = "private" if tn.type == "class_specifier" else "public"
        type_sym = Symbol(
            name=cname, kind="class", file=rel, line=tn.start_point[0] + 1,
            signature=f"class {cname}", supers=supers, visibility="pub", lang="cpp")
        symbols.append(type_sym)
        for m in body.children:
            if m.type == "access_specifier":
                access = _ast_text(m).rstrip(":")
                continue
            fd, name_node = _c_fn_declarator(m)
            if m.type in ("function_definition", "declaration", "field_declaration") \
                    and fd is not None and name_node is not None:
                mname = _ast_text(name_node)
                params, binds = _c_params(_ast_field(fd, "parameters"))
                rtype = _ast_field(m, "type")
                returns = tight_type(_ast_text(rtype)) if rtype is not None else None
                mbody = _ast_field(m, "body")
                kind = "ctor" if mname == cname else "method"
                ret_suffix = (f":{returns}"
                              if kind == "method" and returns and returns != "void"
                              else "")
                sym = Symbol(
                    name=mname, kind=kind, file=rel, line=m.start_point[0] + 1,
                    signature=f"{mname}({','.join(params)}){ret_suffix}",
                    params=params,
                    param_names=_c_param_names(_ast_field(fd, "parameters")),
                    returns=returns or (cname if kind == "ctor" else None),
                    visibility="pub" if access == "public" else "priv",
                    container=cname, lang="cpp",
                    calls=_ast_calls(mbody, mname,
                                     ("call_expression", "new_expression"),
                                     _c_call_entry),
                    bindings=binds, size=_body_lines(mbody),
                    raises=_cpp_raises(mbody),
                )
                symbols.append(sym)
                members[(cname, mname)] = sym
            elif m.type == "field_declaration" and fd is None:
                t = _ast_field(m, "type")
                if t is not None:
                    comps.append(tight_type(_ast_text(t)))
        type_sym.params = comps
        type_sym.fields = _c_field_names(body)
    for fn in _ast_collect(tree.root_node, ("function_definition",)):
        fd, name_node = _c_fn_declarator(fn)
        if fd is None or name_node is None:
            continue
        body = _ast_field(fn, "body")
        if name_node.type == "qualified_identifier":  # out-of-line member def
            container = _base_type(_ast_text(_ast_field(name_node, "scope")))
            mname = _ast_text(_ast_field(name_node, "name"))
            calls = _ast_calls(body, mname, ("call_expression", "new_expression"),
                               _c_call_entry)
            existing = members.get((container, mname))
            if existing is not None:
                if not existing.calls:
                    existing.calls = calls
                if not existing.raises:
                    existing.raises = _cpp_raises(body)
                continue
            params, binds = _c_params(_ast_field(fd, "parameters"))
            rtype = _ast_field(fn, "type")
            returns = tight_type(_ast_text(rtype)) if rtype is not None else None
            symbols.append(Symbol(
                name=mname, kind="method", file=rel, line=fn.start_point[0] + 1,
                signature=f"{mname}({','.join(params)})"
                          + (f":{returns}" if returns and returns != "void" else ""),
                params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
                returns=returns,
                visibility="pub", container=container, lang="cpp",
                calls=calls, bindings=binds, raises=_cpp_raises(body),
            ))
        elif name_node.type == "identifier" and fn.parent is not None \
                and fn.parent.type in ("translation_unit", "namespace_definition",
                                       "declaration_list"):
            name = _ast_text(name_node)
            params, binds = _c_params(_ast_field(fd, "parameters"))
            returns = tight_type(_ast_text(_ast_field(fn, "type")))
            symbols.append(Symbol(
                name=name, kind="fn", file=rel, line=fn.start_point[0] + 1,
                signature=f"{name}({','.join(params)})"
                          + (f":{returns}" if returns != "void" else ""),
                params=params, param_names=_c_param_names(_ast_field(fd, "parameters")),
                returns=returns,
                visibility="priv" if _c_static(fn) else "pub", lang="cpp",
                calls=_ast_calls(body, name, ("call_expression", "new_expression"),
                                 _c_call_entry),
                bindings=binds, raises=_cpp_raises(body),
            ))
    return symbols

