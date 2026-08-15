from __future__ import annotations

import ast
import copy
import re

from ..symbols import Symbol, _base_type, const_signature, tight_type

# ---------------------------------------------------------------------------
# Python extraction (stdlib ast — precise and dependency-free)
# ---------------------------------------------------------------------------

_CONST_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
# Subscripts whose arguments are values rather than types: their string
# members must never be read as forward references.
_PY_VALUE_SUBSCRIPTS = ("Literal", "Annotated")


def _py_subscript_head(node: ast.expr) -> str:
    return (node.attr if isinstance(node, ast.Attribute)
            else node.id if isinstance(node, ast.Name) else "")


def _py_resolve_forward_refs(node: ast.expr) -> ast.expr:
    """Replace PEP 484 string forward references with the types they name.

    `def f(e: "Engine")` and `def f(e: Engine)` must produce identical type
    text: the quoted form otherwise never matches its class, so `bindings`
    cannot resolve the receiver and the call edge silently disappears from
    the map. `Literal[...]` members and `Annotated[...]` metadata stay
    verbatim — those strings are values, not type names.
    """
    if not any(isinstance(sub, ast.Constant) and isinstance(sub.value, str)
               for sub in ast.walk(node)):
        return node  # the common case pays nothing

    def resolve(current: ast.expr) -> ast.expr:
        if isinstance(current, ast.Constant) and isinstance(current.value, str):
            try:
                parsed = ast.parse(current.value.strip(), mode="eval").body
            except (SyntaxError, ValueError):
                return current  # not a type expression; leave it alone
            return resolve(parsed)
        if isinstance(current, ast.Subscript):
            current.value = resolve(current.value)
            head = _py_subscript_head(current.value)
            if head == "Literal":
                return current
            if head == "Annotated":
                # only the first argument is a type; the rest is metadata
                if isinstance(current.slice, ast.Tuple) and current.slice.elts:
                    current.slice.elts[0] = resolve(current.slice.elts[0])
                else:
                    current.slice = resolve(current.slice)
                return current
        for field, value in ast.iter_fields(current):
            if isinstance(value, ast.expr):
                setattr(current, field, resolve(value))
            elif isinstance(value, list):
                setattr(current, field,
                        [resolve(item) if isinstance(item, ast.expr) else item
                         for item in value])
        return current

    return resolve(copy.deepcopy(node))


def _py_annotation(node: ast.expr) -> str:
    """Annotation text with forward references resolved and spacing tightened."""
    return tight_type(ast.unparse(_py_resolve_forward_refs(node)))


def _py_param_facts(node: ast.FunctionDef | ast.AsyncFunctionDef
                    ) -> tuple[list[str], list[str]]:
    types: list[str] = []
    names: list[str] = []

    def add(arg: ast.arg | None, prefix: str = "") -> None:
        if arg is None or arg.arg in ("self", "cls"):
            return
        names.append(prefix + arg.arg)
        types.append(_py_annotation(arg.annotation) if arg.annotation else "?")

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
            binds[arg.arg] = _base_type(_py_annotation(arg.annotation))
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id[:1].isupper()):
            binds[sub.targets[0].id] = sub.value.func.id
        elif (isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name)):
            binds[sub.target.id] = _base_type(_py_annotation(sub.annotation))
    return binds


def _py_decorators(node) -> list[str]:
    return [tight_type(ast.unparse(d)) for d in node.decorator_list]


def _py_fn_symbol(node, rel: str, container: str | None) -> Symbol:
    returns = _py_annotation(node.returns) if node.returns else None
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
            field_types = [_py_annotation(t.annotation) for t in node.body
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
            decorators = _py_decorators(node)
            is_record = any(d.split("(", 1)[0].split(".")[-1] == "dataclass"
                            for d in decorators)
            symbols.append(Symbol(
                name=node.name,
                kind="enum" if is_enum else "record" if is_record else "class",
                file=rel, line=node.lineno,
                signature=f"class {node.name}",
                params=members if is_enum else field_types,
                fields=[] if is_enum else list(dict.fromkeys(field_names)),
                supers=supers,
                visibility="priv" if node.name.startswith("_") else "pub",
                lang="python",
                decorators=decorators,
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
                sig = const_signature(target.id, tight_type(ast.unparse(value)))
            elif isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                sig = const_signature(target.id, None)  # containers: name only
            else:
                continue
            symbols.append(Symbol(
                name=target.id, kind="const", file=rel, line=node.lineno,
                signature=sig, visibility="pub", lang="python"))
    return symbols

