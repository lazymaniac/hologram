from __future__ import annotations

import ast
import re

from ..symbols import Symbol, _base_type, tight_type

# ---------------------------------------------------------------------------
# Python extraction (stdlib ast — precise and dependency-free)
# ---------------------------------------------------------------------------

_CONST_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")

def _py_param_facts(node: ast.FunctionDef | ast.AsyncFunctionDef
                    ) -> tuple[list[str], list[str]]:
    types: list[str] = []
    names: list[str] = []

    def add(arg: ast.arg | None, prefix: str = "") -> None:
        if arg is None or arg.arg in ("self", "cls"):
            return
        names.append(prefix + arg.arg)
        types.append(tight_type(ast.unparse(arg.annotation)) if arg.annotation else "?")

    for arg in node.args.posonlyargs + node.args.args:
        add(arg)
    add(node.args.vararg, "*")
    for arg in node.args.kwonlyargs:
        add(arg)
    add(node.args.kwarg, "**")
    return types, names


def _py_calls(node) -> list[str]:
    seen: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                entry = fn.id
            elif isinstance(fn, ast.Attribute):
                base = fn.value
                entry = (f"{base.id}.{fn.attr}"
                         if isinstance(base, ast.Name) else fn.attr)
            else:
                continue
            if entry not in seen:
                seen.append(entry)
    return seen


def _py_raises(node) -> list[str]:
    seen: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            target = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
            if name and name not in seen:
                seen.append(name)
    return seen


def _py_bindings(node) -> dict[str, str]:
    """Annotated params plus `x = Ctor(...)` locals (Ctor = capitalized name)."""
    binds: dict[str, str] = {}
    for arg in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])):
        if arg.annotation is not None and arg.arg not in ("self", "cls"):
            binds[arg.arg] = _base_type(ast.unparse(arg.annotation))
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id[:1].isupper()):
            binds[sub.targets[0].id] = sub.value.func.id
        elif (isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name)):
            binds[sub.target.id] = _base_type(ast.unparse(sub.annotation))
    return binds


def _py_decorators(node) -> list[str]:
    return [tight_type(ast.unparse(d)) for d in node.decorator_list]


def _py_fn_symbol(node, rel: str, container: str | None) -> Symbol:
    returns = tight_type(ast.unparse(node.returns)) if node.returns else None
    params, param_names = _py_param_facts(node)
    ret_suffix = f":{returns}" if returns and returns != "None" else ""
    return Symbol(
        name=node.name, kind="method" if container else "fn", file=rel,
        line=node.lineno,
        signature=f"{node.name}({','.join(params)}){ret_suffix}",
        params=params, param_names=param_names, returns=returns,
        visibility="priv" if node.name.startswith("_") else "pub",
        container=container, lang="python",
        calls=[c for c in _py_calls(node) if c != node.name],
        raises=_py_raises(node),
        bindings=_py_bindings(node),
        decorators=_py_decorators(node),
        size=(getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
    )


def _extract_python(text: str, rel: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            base_names = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                          for b in node.bases}
            is_enum = base_names & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
            members = [t.targets[0].id for t in node.body
                       if isinstance(t, ast.Assign) and len(t.targets) == 1
                       and isinstance(t.targets[0], ast.Name)] if is_enum else []
            field_types = [ast.unparse(t.annotation) for t in node.body
                           if isinstance(t, ast.AnnAssign)]
            field_names = [t.target.id for t in node.body
                           if isinstance(t, ast.AnnAssign)
                           and isinstance(t.target, ast.Name)]
            field_names.extend(
                t.targets[0].id for t in node.body
                if isinstance(t, ast.Assign) and len(t.targets) == 1
                and isinstance(t.targets[0], ast.Name) and not is_enum
            )
            supers = [] if is_enum else [
                re.sub(r"\[.*", "", ast.unparse(b)).split(".")[-1] for b in node.bases]
            symbols.append(Symbol(
                name=node.name, kind="enum" if is_enum else "class", file=rel,
                line=node.lineno,
                signature=f"class {node.name}",
                params=members if is_enum else field_types,
                fields=[] if is_enum else list(dict.fromkeys(field_names)),
                supers=supers,
                visibility="priv" if node.name.startswith("_") else "pub",
                lang="python",
                decorators=_py_decorators(node),
            ))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_py_fn_symbol(sub, rel, node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_py_fn_symbol(node, rel, None))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = (node.targets[0]
                      if isinstance(node, ast.Assign) and len(node.targets) == 1
                      else getattr(node, "target", None))
            if not (isinstance(target, ast.Name) and _CONST_NAME_RE.fullmatch(target.id)):
                continue
            value = node.value
            if isinstance(value, ast.Constant):
                text = tight_type(ast.unparse(value))
                sig = f"{target.id}={text}" if len(text) <= 24 else target.id
            elif isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                sig = target.id  # container consts: name only, values stay in code
            else:
                continue
            symbols.append(Symbol(
                name=target.id, kind="const", file=rel, line=node.lineno,
                signature=sig, visibility="pub", lang="python"))
    return symbols

