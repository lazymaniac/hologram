from __future__ import annotations

from ..symbols import Symbol
from ..treesitter import _PARSERS, _ast_calls, _ast_field, _ast_text, _body_lines

# ---------------------------------------------------------------------------
# Ruby extraction (untyped: methods with param names and call chains; visibility
# from bare private/protected statements, like C++ access sections)
# ---------------------------------------------------------------------------


def _rb_call_entry(n) -> tuple[str, str]:
    method = _ast_field(n, "method")
    if method is None or method.type not in ("identifier", "constant"):
        return "", ""
    name = _ast_text(method)
    recv = _ast_field(n, "receiver")
    if recv is not None and recv.type in ("identifier", "constant"):
        return name, f"{_ast_text(recv)}.{name}"
    return name, name


def _rb_params(pnode) -> list[str]:
    names: list[str] = []
    for p in (pnode.children if pnode is not None else ()):
        if p.type == "identifier":
            names.append(_ast_text(p))
        elif p.type in ("optional_parameter", "keyword_parameter",
                        "splat_parameter", "block_parameter"):
            ident = _ast_field(p, "name")
            names.append(_ast_text(ident) if ident is not None else "")
    return names


def _rb_method_symbol(m, rel: str, container: str | None, vis: str) -> Symbol:
    name = _ast_text(_ast_field(m, "name"))
    params = _rb_params(_ast_field(m, "parameters"))
    body = _ast_field(m, "body")
    kind = ("ctor" if name == "initialize" and container
            else "method" if container else "fn")
    return Symbol(
        name=container if kind == "ctor" else name, kind=kind, file=rel,
        line=m.start_point[0] + 1,
        signature=f"{name}({','.join(params)})", params=params,
        param_names=params,
        visibility="priv" if vis == "priv" or name.startswith("_") else "pub",
        container=container, lang="ruby",
        calls=_ast_calls(body, name, ("call",), _rb_call_entry),
        size=_body_lines(body),
    )


def _rb_fields(body) -> list[str]:
    """attr_accessor/attr_reader/attr_writer symbols plus @ivar assignments
    in initialize."""
    fields: list[str] = []
    if body is None:
        return fields
    for m in body.children:
        if (m.type == "call"
                and _ast_text(_ast_field(m, "method") or m.children[0]).startswith("attr_")):
            args = next((c for c in m.children if c.type == "argument_list"), None)
            for sym in (args.children if args is not None else ()):
                if sym.type == "simple_symbol":
                    fname = _ast_text(sym).lstrip(":")
                    if fname not in fields:
                        fields.append(fname)
        elif m.type == "method" and _ast_text(_ast_field(m, "name")) == "initialize":
            mbody = _ast_field(m, "body")
            for a in (mbody.children if mbody is not None else ()):
                if a.type == "assignment" and a.children \
                        and a.children[0].type == "instance_variable":
                    fname = _ast_text(a.children[0]).lstrip("@")
                    if fname not in fields:
                        fields.append(fname)
    return fields


def _rb_walk(node, rel: str, symbols: list[Symbol],
             container: str | None = None) -> None:
    """One pass over a program or body_statement node. Bare private/protected
    statements toggle visibility for the methods that follow, ruby-style."""
    access = "pub"
    for m in node.children:
        if m.type == "identifier" and _ast_text(m) in ("private", "protected"):
            access = "priv"
        elif m.type == "identifier" and _ast_text(m) == "public":
            access = "pub"
        elif m.type in ("module", "class"):
            name = _ast_text(_ast_field(m, "name"))
            sup = _ast_field(m, "superclass")
            body = _ast_field(m, "body")
            symbols.append(Symbol(
                name=name, kind="class", file=rel, line=m.start_point[0] + 1,
                signature=f"{m.type} {name}",
                supers=[_ast_text(sup).lstrip("< ").strip()] if sup is not None
                else [],
                fields=_rb_fields(body),
                visibility="pub", lang="ruby",
            ))
            if body is not None:
                _rb_walk(body, rel, symbols, container=name)
        elif m.type in ("method", "singleton_method"):
            symbols.append(_rb_method_symbol(m, rel, container, access))


def _extract_ruby(text: str, rel: str) -> list[Symbol]:
    tree = _PARSERS["ruby"].parse(text.encode())
    symbols: list[Symbol] = []
    _rb_walk(tree.root_node, rel, symbols)
    return symbols
