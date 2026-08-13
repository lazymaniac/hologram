from __future__ import annotations

import re

from pathlib import Path

from ..symbols import (Symbol, _IDENT_RE, _base_type, _heritage, _split_top_commas, tight_type)
from ..treesitter import (_PARSERS, _ast_calls, _ast_collect, _ast_field, _ast_text, _body_lines)

# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction
# ---------------------------------------------------------------------------

_TS_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}


def _ts_exported(n) -> bool:
    return n.parent is not None and n.parent.type == "export_statement"


def _ts_params(node) -> list[str]:
    """Declared parameter types from a formal_parameters node; `?` when untyped."""
    raw = _ast_text(node)
    if raw.startswith("("):
        raw = raw[1:-1]
    types = []
    for p in _split_top_commas(raw, "<([{", ">)]}"):
        p = re.sub(r"^(private|public|protected|readonly)\s+", "", p.strip())
        p = p.split("=")[0]
        types.append(tight_type(p.split(":", 1)[1].strip()) if ":" in p else "?")
    return types


def _ts_param_names(node) -> list[str]:
    raw = _ast_text(node)
    if raw.startswith("("):
        raw = raw[1:-1]
    names: list[str] = []
    for p in _split_top_commas(raw, "<([{", ">)]}"):
        p = re.sub(r"^(private|public|protected|readonly)\s+", "", p.strip())
        p = p.split("=", 1)[0].strip().removeprefix("...")
        name = p.split(":", 1)[0].strip().rstrip("?")
        names.append(name if _IDENT_RE.fullmatch(name) else "")
    return names


def _ts_return(node) -> str | None:
    rt = _ast_field(node, "return_type")
    return tight_type(_ast_text(rt).lstrip(":").strip()) if rt is not None else None


def _ts_call_entry(n) -> tuple[str, str]:
    if n.type == "new_expression":
        entry = re.sub(r"<.*", "", _ast_text(_ast_field(n, "constructor")))
        return entry, entry
    fn = _ast_field(n, "function")
    if fn is None:
        return "", ""
    if fn.type == "member_expression":
        name = _ast_text(_ast_field(fn, "property"))
        obj = _ast_field(fn, "object")
        entry = (f"{_ast_text(obj)}.{name}"
                 if obj is not None and obj.type == "identifier" else name)
        return name, entry
    if fn.type == "identifier":
        name = _ast_text(fn)
        return name, name
    return "", ""


def _ts_calls(body, own_name: str) -> list[str]:
    return _ast_calls(body, own_name, ("call_expression", "new_expression"),
                      _ts_call_entry)


def _ts_param_bindings(params_node) -> dict[str, str]:
    binds: dict[str, str] = {}
    if params_node is None:
        return binds
    for p in params_node.children:
        if p.type in ("required_parameter", "optional_parameter"):
            pat, t = _ast_field(p, "pattern"), _ast_field(p, "type")
            if pat is not None and pat.type == "identifier" and t is not None:
                binds[_ast_text(pat)] = _base_type(_ast_text(t).lstrip(":").strip())
    return binds


def _ts_class_bindings(body) -> dict[str, str]:
    """Typed fields, including constructor parameter properties (private x: T)."""
    binds: dict[str, str] = {}
    for c in (body.children if body is not None else ()):
        if c.type == "public_field_definition":
            n, t = _ast_field(c, "name"), _ast_field(c, "type")
            if n is not None and t is not None:
                binds[_ast_text(n)] = _base_type(_ast_text(t).lstrip(":").strip())
        if c.type == "method_definition" and _ast_text(_ast_field(c, "name")) == "constructor":
            for p in (_ast_field(c, "parameters") or c).children:
                if p.type == "required_parameter" and any(
                        ch.type == "accessibility_modifier" for ch in p.children):
                    binds.update(_ts_param_bindings_one(p))
    return binds


def _ts_param_bindings_one(p) -> dict[str, str]:
    pat, t = _ast_field(p, "pattern"), _ast_field(p, "type")
    if pat is not None and pat.type == "identifier" and t is not None:
        return {_ast_text(pat): _base_type(_ast_text(t).lstrip(":").strip())}
    return {}


def _ts_local_bindings(body) -> dict[str, str]:
    binds: dict[str, str] = {}
    if body is None:
        return binds
    for dec in _ast_collect(body, ("variable_declarator",)):
        n, t, val = _ast_field(dec, "name"), _ast_field(dec, "type"), _ast_field(dec, "value")
        if n is None or n.type != "identifier":
            continue
        if t is not None:
            binds[_ast_text(n)] = _base_type(_ast_text(t).lstrip(":").strip())
        elif val is not None and val.type == "new_expression":
            binds[_ast_text(n)] = _base_type(_ast_text(_ast_field(val, "constructor")))
    return binds


def _ts_fn_symbol(node, rel: str, container: str | None, visibility: str,
                  class_binds: dict[str, str] | None = None,
                  name: str | None = None, fn_node=None) -> Symbol:
    """Function/method Symbol. `fn_node` carries params/body when the name lives on a
    different node (arrow assigned to a const or a class field)."""
    fn = fn_node if fn_node is not None else node
    name = name if name is not None else _ast_text(_ast_field(node, "name"))
    params = _ts_params(_ast_field(fn, "parameters"))
    returns = _ts_return(fn)
    body = _ast_field(fn, "body")
    ret_suffix = f":{returns}" if returns and returns != "void" else ""
    return Symbol(
        name=name, kind="method" if container else "fn", file=rel,
        line=node.start_point[0] + 1,
        signature=f"{name}({','.join(params)}){ret_suffix}",
        params=params, param_names=_ts_param_names(_ast_field(fn, "parameters")),
        returns=returns,
        visibility=visibility, container=container, lang="typescript",
        calls=_ts_calls(body, name), size=_body_lines(body),
        bindings={**(class_binds or {}),
                  **_ts_param_bindings(_ast_field(fn, "parameters")),
                  **_ts_local_bindings(body)},
    )


_TS_FN_VALUES = ("arrow_function", "function_expression")


def _ts_top_level_arrows(root_node, rel: str) -> list[Symbol]:
    """Module-scope `const f = (…) => …` plus object-literal APIs
    (`export const api = { get(){}, … }`). Nested closures are deliberately
    excluded — only declarations at program/export level count."""
    symbols = []
    for top in root_node.children:
        exported = top.type == "export_statement"
        decls = top.children if exported else [top]
        for decl in decls:
            if decl.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for d in decl.children:
                if d.type != "variable_declarator":
                    continue
                name = _ast_text(_ast_field(d, "name"))
                val = _ast_field(d, "value")
                if val is None:
                    continue
                if val.type in _TS_FN_VALUES:
                    symbols.append(_ts_fn_symbol(
                        d, rel, None, "pub" if exported else "priv",
                        name=name, fn_node=val))
                elif val.type == "object":
                    fns = []
                    for c in val.children:
                        if c.type == "method_definition":
                            fns.append((_ast_text(_ast_field(c, "name")), c, c))
                        elif c.type == "pair":
                            v = _ast_field(c, "value")
                            if v is not None and v.type in _TS_FN_VALUES:
                                fns.append((_ast_text(_ast_field(c, "key")), c, v))
                    if not fns:
                        continue  # plain config object, not an API
                    symbols.append(Symbol(
                        name=name, kind="class", file=rel, line=d.start_point[0] + 1,
                        signature=f"const {name}",
                        visibility="pub" if exported else "priv", lang="typescript"))
                    for mname, node, fn_node in fns:
                        symbols.append(_ts_fn_symbol(
                            node, rel, name, "pub", name=mname, fn_node=fn_node))
    return symbols


def _ts_aliases_and_reexports(root_node, rel: str) -> list[Symbol]:
    symbols = []
    for al in _ast_collect(root_node, ("type_alias_declaration",)):
        target = tight_type(_ast_text(_ast_field(al, "value")))[:40]
        value = _ast_field(al, "value")
        fields = [_ast_text(_ast_field(p, "name"))
                  for p in (value.children if value is not None else ())
                  if p.type == "property_signature" and _ast_field(p, "name") is not None]
        symbols.append(Symbol(
            name=_ast_text(_ast_field(al, "name")), kind="type", file=rel,
            line=al.start_point[0] + 1,
            signature=f"type {_ast_text(_ast_field(al, 'name'))}",
            params=[target] if target else [], fields=fields,
            visibility="pub" if _ts_exported(al) else "priv", lang="typescript"))
    for ex in root_node.children:
        if ex.type != "export_statement" or _ast_field(ex, "source") is None:
            continue  # only `export … from './x'` barrels
        for spec in _ast_collect(ex, ("export_specifier",)):
            nm = _ast_field(spec, "alias") or _ast_field(spec, "name")
            if nm is not None:
                symbols.append(Symbol(
                    name=_ast_text(nm), kind="reexport", file=rel,
                    line=ex.start_point[0] + 1, signature=_ast_text(nm),
                    visibility="pub", lang="typescript"))
    return symbols


def _extract_ts(text: str, rel: str, lang: str = "typescript") -> list[Symbol]:
    tree = _PARSERS[lang].parse(text.encode())
    symbols: list[Symbol] = []
    for tn in _ast_collect(tree.root_node, _TS_TYPE_NODE_KINDS):
        kind = _TS_TYPE_NODE_KINDS[tn.type]
        name = _ast_text(_ast_field(tn, "name"))
        body = _ast_field(tn, "body")
        header_end = body.start_byte if body is not None else tn.end_byte
        header = text.encode()[tn.start_byte:header_end].decode(errors="replace")
        supers, _ = _heritage(header)
        params: list[str] = []
        if kind == "enum" and body is not None:
            params = [_ast_text(_ast_field(c, "name") or c)
                      for c in body.children
                      if c.type in ("enum_assignment", "property_identifier")]
        symbols.append(Symbol(
            name=name, kind=kind, file=rel, line=tn.start_point[0] + 1,
            signature=f"{kind} {name}", params=params, supers=supers,
            fields=(list(_ts_class_bindings(body)) if kind == "class"
                    else [_ast_text(_ast_field(p, "name"))
                          for p in (body.children if body is not None else ())
                          if p.type == "property_signature"
                          and _ast_field(p, "name") is not None]),
            visibility="pub" if _ts_exported(tn) else "priv", lang="typescript",
        ))
        if kind == "class" and body is not None:
            class_binds = _ts_class_bindings(body)
            for c in body.children:
                if c.type == "public_field_definition":
                    val = _ast_field(c, "value")
                    if val is not None and val.type in _TS_FN_VALUES:
                        vis = "priv" if any(ch.type == "accessibility_modifier"
                                            and _ast_text(ch) == "private"
                                            for ch in c.children) else "pub"
                        symbols.append(_ts_fn_symbol(
                            c, rel, name, vis, class_binds,
                            name=_ast_text(_ast_field(c, "name")), fn_node=val))
                    continue
                if c.type != "method_definition":
                    continue
                mname = _ast_text(_ast_field(c, "name"))
                if mname == "constructor":
                    symbols.append(Symbol(
                        name=name, kind="ctor", file=rel, line=c.start_point[0] + 1,
                        signature=f"{name}({','.join(_ts_params(_ast_field(c, 'parameters')))})",
                        params=_ts_params(_ast_field(c, "parameters")),
                        param_names=_ts_param_names(_ast_field(c, "parameters")),
                        returns=name,
                        container=name, lang="typescript",
                    ))
                    continue
                vis = "priv" if any(ch.type == "accessibility_modifier"
                                    and _ast_text(ch) == "private"
                                    for ch in c.children) else "pub"
                symbols.append(_ts_fn_symbol(c, rel, name, vis, class_binds))
    for fn in _ast_collect(tree.root_node, ("function_declaration",)):
        symbols.append(_ts_fn_symbol(
            fn, rel, None, "pub" if _ts_exported(fn) else "priv"))
    symbols.extend(_ts_top_level_arrows(tree.root_node, rel))
    symbols.extend(_ts_aliases_and_reexports(tree.root_node, rel))
    return symbols


def _extract_tsx(text: str, rel: str) -> list[Symbol]:
    return _extract_ts(text, rel, "tsx")


_SFC_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)


def _extract_sfc(text: str, rel: str) -> list[Symbol]:
    """Vue/Svelte single-file components: the component itself plus everything the
    TS extractor finds inside its <script> blocks (line numbers preserved)."""
    stem = Path(rel).stem
    symbols = [Symbol(name=stem, kind="class", file=rel, line=1,
                      signature=f"component {stem}", visibility="pub",
                      lang=Path(rel).suffix.lstrip("."))]
    for m in _SFC_SCRIPT_RE.finditer(text):
        offset = text.count("\n", 0, m.start(1))
        for s in _extract_ts(m.group(1), rel):
            s.line += offset
            symbols.append(s)
    return symbols

