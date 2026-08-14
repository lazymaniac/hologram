"""All digest layout: render_simple owns every formatting decision."""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from .gather import _gather, _zero_usage_names
from .symbols import (MARKER_DECORATORS, ROUTE_DECORATORS, TYPE_KINDS, Symbol,
                      _base_type)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F",
               "type": "T"}


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _test_stem(raw_stem: str) -> bool:
    stem = raw_stem.casefold()
    return (stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))
            or raw_stem.endswith(("Test", "Tests", "Spec", "IT")))


def _is_test_path(rel: str) -> bool:
    path = Path(rel)
    parts = [p.casefold() for p in path.parts[:-1]]
    return (any(p in ("test", "tests", "__tests__") for p in parts)
            or _test_stem(path.stem))


def _tree_lines(payload_by_dir: dict[str, list[str]]) -> list[str]:
    """Render dir paths as a path-compressed trie: shared prefixes stated once.
    Payload lines carry their own relative indent; the trie adds depth indent."""
    tree: dict = {}
    for d in sorted(payload_by_dir):
        node = tree
        for part in Path(d).parts:
            node = node.setdefault(part, {})
        node.setdefault("\0", []).extend(payload_by_dir[d])

    out: list[str] = []

    def emit(node: dict, label: str | None, depth: int) -> None:
        children = {k: v for k, v in node.items() if k != "\0"}
        payload = node.get("\0", [])
        while label is not None and len(children) == 1 and not payload:
            (k, child), = children.items()
            label = f"{label}/{k}"
            payload = child.get("\0", [])
            children = {kk: vv for kk, vv in child.items() if kk != "\0"}
        base = depth
        if label is not None:
            out.append(" " * depth + label)
            base = depth + 1
        for ln in payload:
            out.append(" " * base + ln)
        for k in sorted(children):
            emit(children[k], k, base)

    emit(tree, None, 0)
    return out


def _strip_exc(name: str) -> str:
    return name.removesuffix("Exception") or name


_BOILERPLATE_PARTS = ("src", "main", "java", "kotlin", "test", "tests", "lib")


def _dep_lines(symbols: list[Symbol], file_tokens: dict[str, set[str]],
               min_refs: int = 2) -> list[str]:
    """Module dependency edges (`a→b` = code in a references types defined in b),
    from data already in hand. Modules are top path segments after boilerplate
    and the corpus-wide shared prefix."""
    type_dir: dict[str, str] = {}
    for s in symbols:
        if s.kind in TYPE_KINDS and not _is_test_path(s.file):
            type_dir.setdefault(s.name, str(Path(s.file).parent))
    dirs = {str(Path(rel).parent) for rel in file_tokens} | set(type_dir.values())
    stripped = {d: [p for p in Path(d).parts if p not in _BOILERPLATE_PARTS]
                for d in dirs}
    common: list[str] = []
    lists = [p for p in stripped.values() if p]
    while lists and all(len(p) > len(common) + 1 for p in lists) \
            and len({p[len(common)] for p in lists}) == 1:
        common.append(lists[0][len(common)])

    def label(d: str) -> str:
        parts = stripped[d]
        if common and parts[:len(common)] == common and len(parts) > len(common):
            parts = parts[len(common):]
        return parts[0] if parts else "."

    counts: dict[tuple[str, str], int] = {}
    for rel, toks in file_tokens.items():
        if _is_test_path(rel):
            continue
        m_from = label(str(Path(rel).parent))
        for t in toks & set(type_dir):
            m_to = label(type_dir[t])
            if m_from != m_to:
                counts[(m_from, m_to)] = counts.get((m_from, m_to), 0) + 1
    by_src: dict[str, list[str]] = {}
    for (a, b), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= min_refs:
            by_src.setdefault(a, []).append(b)
    cells = [f"{a}→{','.join(bs)}" for a, bs in sorted(by_src.items())]
    lines, cur = [], ""
    for c in cells:
        if cur and len(cur) + len(c) + 3 > 110:
            lines.append(f"· deps {cur}")
            cur = c
        else:
            cur = f"{cur} | {c}" if cur else c
    if cur:
        lines.append(f"· deps {cur}")
    return lines


def _total_loc(files: list[Path]) -> int:
    loc = 0
    for f in files:
        try:
            loc += len(f.read_text(errors="replace").splitlines())
        except OSError:
            pass
    return loc


def _symbol_identity(symbol: Symbol) -> tuple[str, str, str, str, int]:
    return (symbol.file, symbol.lang, symbol.container or "", symbol.name, symbol.line)


def _target_descriptions(targets: list[Symbol]) -> dict[int, str]:
    """Shortest stable project-wide name that identifies each call target."""
    by_name = Counter(s.name for s in targets)
    qualified = {id(s): (f"{s.container}.{s.name}" if s.container else s.name)
                 for s in targets}
    by_qualified = Counter(qualified.values())
    stemmed = {id(s): f"{Path(s.file).stem}.{qualified[id(s)]}" for s in targets}
    by_stemmed = Counter(stemmed.values())
    out: dict[int, str] = {}
    for symbol in targets:
        if by_name[symbol.name] == 1:
            out[id(symbol)] = symbol.name
        elif by_qualified[qualified[id(symbol)]] == 1:
            out[id(symbol)] = qualified[id(symbol)]
        elif by_stemmed[stemmed[id(symbol)]] == 1:
            out[id(symbol)] = stemmed[id(symbol)]
        else:
            out[id(symbol)] = f"{symbol.file}:{qualified[id(symbol)]}"
    return out


def _raw_call_targets(symbols: list[Symbol]) -> dict[int, list[Symbol]]:
    """Each symbol's raw calls resolved to project Symbol targets — the shared
    resolution core under both rendering and `hologram review`."""
    production = [s for s in symbols if not _is_test_path(s.file)]
    targets = [s for s in production if s.kind in TYPE_KINDS + ("fn", "method")]
    types = [s for s in targets if s.kind in TYPE_KINDS]
    callables = [s for s in targets if s.kind in ("fn", "method")]

    type_index: dict[tuple[str, str], list[Symbol]] = {}
    method_index: dict[tuple[str, str, str], list[Symbol]] = {}
    file_top_index: dict[tuple[str, str, str], list[Symbol]] = {}
    module_top_index: dict[tuple[str, str, str], list[Symbol]] = {}
    lang_name_index: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in types:
        type_index.setdefault((symbol.lang, symbol.name), []).append(symbol)
    for symbol in callables:
        lang_name_index.setdefault((symbol.lang, symbol.name), []).append(symbol)
        if symbol.container:
            method_index.setdefault(
                (symbol.lang, symbol.container, symbol.name), []).append(symbol)
        else:
            file_top_index.setdefault(
                (symbol.lang, symbol.file, symbol.name), []).append(symbol)
            module_top_index.setdefault(
                (symbol.lang, Path(symbol.file).stem, symbol.name), []).append(symbol)

    def one(items: list[Symbol] | None) -> Symbol | None:
        return items[0] if items is not None and len(items) == 1 else None

    # Angular selector → component class; a selector claimed twice is
    # ambiguous and resolves to nothing (same contract as one()).
    selector_class: dict[str, Symbol | None] = {}
    for s in production:
        if s.kind in TYPE_KINDS and s.lang == "typescript":
            for d in s.decorators:
                if d.startswith("Component(") and "selector" in d:
                    m = re.search(r"""selector\s*:\s*['"]([^'"]+)['"]""", d)
                    if m:
                        sel = m.group(1)
                        selector_class[sel] = (None if sel in selector_class
                                               else s)
    # templateUrl: elements of the referenced html file count as the
    # component's own template usage
    html_tags_by_file: dict[str, list[str]] = {}
    for s in production:
        if s.lang == "html" and "-" in s.name:
            html_tags_by_file.setdefault(s.file, []).append(s.name)
    extra_calls: dict[int, list[str]] = {}
    for s in production:
        url = (s.bindings.get("__templateUrl__")
               if s.kind in TYPE_KINDS else None)
        if url:
            ref = os.path.normpath(str(Path(s.file).parent / url))
            extra_calls[id(s)] = html_tags_by_file.get(ref, [])

    same_container_languages = {
        "java", "typescript", "javascript", "tsx", "vue", "svelte",
        "csharp", "kotlin", "cpp", "rust",
    }

    def resolve(caller: Symbol, raw: str) -> Symbol | None:
        if "-" in raw and caller.lang in ("typescript", "html"):
            return selector_class.get(raw)
        receiver, dot, name = raw.rpartition(".")
        if not dot:
            name = raw
            target_type = one(type_index.get((caller.lang, name)))
            if target_type is not None:
                return target_type
            if caller.container and caller.lang in same_container_languages:
                target = one(method_index.get((caller.lang, caller.container, name)))
                if target is not None:
                    return target
            target = one(file_top_index.get((caller.lang, caller.file, name)))
            if target is not None:
                return target
            return one(lang_name_index.get((caller.lang, name)))

        if receiver in ("self", "cls", "this") and caller.container:
            return one(method_index.get((caller.lang, caller.container, name)))
        if receiver in caller.bindings:
            owner = _base_type(caller.bindings[receiver])
            return one(method_index.get((caller.lang, owner, name)))
        owner = receiver.rsplit(".", 1)[-1]
        if (caller.lang, owner) in type_index:
            return one(method_index.get((caller.lang, owner, name)))
        module = receiver.rsplit(".", 1)[-1]
        return one(module_top_index.get((caller.lang, module, name)))

    raw_targets: dict[int, list[Symbol]] = {}
    for caller in symbols:
        found: list[Symbol] = []
        seen: set[tuple[str, str, str, str, int]] = set()
        for raw in caller.calls + extra_calls.get(id(caller), []):
            target = resolve(caller, raw)
            if target is None:
                continue
            key = _symbol_identity(target)
            if key not in seen:
                seen.add(key)
                found.append(target)
        raw_targets[id(caller)] = found
    return raw_targets


def _resolved_project_calls(symbols: list[Symbol]
                            ) -> tuple[dict[int, list[str]], set[int]]:
    """Resolve raw calls to project symbols; omit external and ambiguous targets.

    Returns display calls by caller identity plus production targets called by tests.
    """
    production = [s for s in symbols if not _is_test_path(s.file)]
    targets = [s for s in production if s.kind in TYPE_KINDS + ("fn", "method")]
    raw_targets = _raw_call_targets(symbols)

    public_callers = {
        _symbol_identity(s): s for s in production
        if s.kind in ("fn", "method") and s.visibility == "pub"
    }
    adjacency = {
        key: [_symbol_identity(t) for t in raw_targets[id(caller)]
              if t.kind in ("fn", "method")]
        for key, caller in public_callers.items()
    }

    def reaches(source: tuple[str, str, str, str, int],
                target: tuple[str, str, str, str, int],
                seen: set[tuple[str, str, str, str, int]] | None = None) -> bool:
        if source not in public_callers:
            return False
        seen = set() if seen is None else seen
        if source in seen:
            return False
        seen.add(source)
        for child in adjacency.get(source, ()):
            if child == target or reaches(child, target, seen):
                return True
        return False

    descriptions = _target_descriptions(targets)
    displayed: dict[int, list[str]] = {}
    for caller in symbols:
        found = raw_targets[id(caller)]
        if caller.visibility == "pub" and not _is_test_path(caller.file):
            reduced: list[Symbol] = []
            for target in found:
                target_key = _symbol_identity(target)
                implied = any(
                    other is not target
                    and reaches(_symbol_identity(other), target_key)
                    and not reaches(target_key, _symbol_identity(other))
                    for other in found
                )
                if not implied:
                    reduced.append(target)
            found = reduced
        displayed[id(caller)] = [descriptions[id(target)] for target in found]

    tested = {
        id(target)
        for caller in symbols if _is_test_path(caller.file)
        for target in raw_targets[id(caller)]
    }
    return displayed, tested


_FIRST_STRING_RE = re.compile(r"""['"]([^'"]+)['"]""")
_METHODS_VERB_RE = re.compile(r"""['"](GET|POST|PUT|DELETE|PATCH)['"]""")


def _decorator_notes(decorators: list[str], lang: str = "") -> list[str]:
    """Displayable atoms from a symbol's decorators, allowlist-filtered.

    Routes collapse to VERB/path (JAX-RS pairs a verb annotation with @Path;
    Flask recovers the verb from methods=[...]; Spring's method= is read from
    RequestMethod.X; Symfony's from methods: [...]). Angular @Component yields
    its selector. Markers render bare. Everything else is dropped —
    conservative: on parse doubt emit the marker form, never a wrong path."""
    notes: list[str] = []
    verb_pending: str | None = None
    path_pending: str | None = None
    for d in decorators:
        base = d.split("(", 1)[0].strip()
        args = d[len(base):].strip()
        args = args[1:-1] if args.startswith("(") else ""
        tail = base.split(".")[-1]
        dotted = "." in base
        first = _FIRST_STRING_RE.search(args)
        if tail == "Component" and "selector" in args:
            m = re.search(r"""selector\s*:\s*['"]([^'"]+)['"]""", args)
            if m:
                notes.append(m.group(1))
            continue
        # lowercase route names (route/get/post/…) only match as dotted tails
        # (app.route, router.get); bare lowercase identifiers stay unmatched —
        # except in Rust, where they are actix's attribute macros #[get("/x")]
        if tail in ROUTE_DECORATORS and (dotted or not tail.islower()
                                         or lang == "rust"):
            verb = ROUTE_DECORATORS[tail]
            if tail in ("route", "RequestMapping", "Route"):
                verbs = _METHODS_VERB_RE.findall(args)
                m = re.search(r"RequestMethod\.(\w+)", args)
                if m:
                    verbs.append(m.group(1))
                if verbs:
                    verb = "|".join(dict.fromkeys(verbs))
                elif tail == "route" and lang != "rust":
                    verb = "GET"  # Flask default when methods= is absent
            path = first.group(1) if first is not None else None
            if path is not None and not path.startswith("/"):
                path = "/" + path
            if verb and path:
                notes.append(f"{verb}{path}")
            elif verb:
                verb_pending = verb_pending or verb
            elif path:
                path_pending = path_pending or path
            continue
        if tail in MARKER_DECORATORS:
            notes.append(tail)
            continue
        if first is not None and (first.group(1).startswith("/")
                                  or "{" in first.group(1)):
            notes.append(f"{tail}:{first.group(1)}")
    if verb_pending and path_pending:
        notes.append(f"{verb_pending}{path_pending}")
    elif verb_pending:
        notes.append(verb_pending)
    elif path_pending:
        notes.append(path_pending)
    return notes


_PRIVATE_SEPARATORS = "_./-"


def _factored_name_tokens(names: list[str]) -> list[str]:
    """Losslessly factor repeated prefixes (`p{a,b}`=pa,pb) and suffixes
    (`{a,b}s`=as,bs) when bytes strictly shrink. Suffix boundaries are a
    separator char or a lower→upper camel step; ×0-marked names never join
    suffix groups (the marker must stay outermost)."""
    ordered = list(dict.fromkeys(names))
    remaining = set(range(len(ordered)))
    groups: list[tuple[int, str, str, list[int]]] = []
    while True:
        candidates: dict[tuple[str, str], list[int]] = {}
        for index in remaining:
            base = ordered[index].removesuffix("×0")
            for pos, char in enumerate(base):
                prefix = base[:pos + 1]
                if (char in _PRIVATE_SEPARATORS
                        and any(c not in _PRIVATE_SEPARATORS for c in prefix)):
                    candidates.setdefault((prefix, ""), []).append(index)
            if base != ordered[index]:
                continue
            for pos in range(1, len(base)):
                if (base[pos] in _PRIVATE_SEPARATORS
                        or (base[pos].isupper() and base[pos - 1].islower())):
                    suffix = base[pos:]
                    if any(c not in _PRIVATE_SEPARATORS for c in suffix):
                        candidates.setdefault(("", suffix), []).append(index)
        choices: list[tuple[int, int, str, str, list[int]]] = []
        for (prefix, suffix), indexes in candidates.items():
            indexes = sorted(set(indexes))
            if len(indexes) < 3:
                continue
            plain = ",".join(ordered[i] for i in indexes)
            inner = ",".join(
                ordered[i][len(prefix):len(ordered[i]) - len(suffix)]
                for i in indexes)
            compact = prefix + "{" + inner + "}" + suffix
            saving = len(plain.encode()) - len(compact.encode())
            if saving > 0:
                choices.append((saving, len(prefix) + len(suffix),
                                prefix, suffix, indexes))
        if not choices:
            break
        _, _, prefix, suffix, indexes = min(
            choices, key=lambda item: (-item[0], -item[1], item[2], item[3]))
        groups.append((min(indexes), prefix, suffix, indexes))
        remaining.difference_update(indexes)
    tokens = [(index, ordered[index]) for index in sorted(remaining)]
    for first, prefix, suffix, indexes in groups:
        inner = ",".join(ordered[i][len(prefix):len(ordered[i]) - len(suffix)]
                         for i in indexes)
        tokens.append((first, prefix + "{" + inner + "}" + suffix))
    return [value for _, value in sorted(tokens)]


def _private_lines(prefix: str, names: list[str], width: int = 120) -> list[str]:
    """Wrap factored names only between independently reconstructable tokens."""
    lines: list[str] = []
    continuation = " " * len(prefix)
    current = prefix
    for token in _factored_name_tokens(names):
        candidate = current + ("," if current.strip() != prefix.strip() else "") + token
        if len(candidate) > width and current != prefix:
            lines.append(current + ",")
            current = continuation + token
        else:
            current = candidate
    if current != prefix:
        lines.append(current)
    return lines


def _braced_lines(label: str, names: list[str], width: int = 120) -> list[str]:
    if not names:
        return [label]
    prefix = label + "{"
    continuation = " " * len(prefix)
    lines: list[str] = []
    current = prefix
    for name in _factored_name_tokens(names):
        candidate = current + ("," if current != prefix else "") + name
        if len(candidate) + 1 > width and current != prefix:
            lines.append(current + ",")
            current = continuation + name
        else:
            current = candidate
    lines.append(current + "}")
    return lines


def _helper_class_ids(symbols: list[Symbol],
                      file_tokens: dict[str, set[str]] | None) -> dict[int, bool]:
    """Test-support classes worth surfacing: reusable drivers, builders, and
    shared bases that agents otherwise re-invent. A class qualifies when
    (a) it lives on a test path only through its directory — the file isn't
    test-named and neither is the class — or (b) two or more *other* test
    files reference it by name. The value says whether the class earns full
    method signatures: only helpers actually referenced from another test
    file do; unreferenced ones render name-only."""
    ids: dict[int, bool] = {}
    ref_counts: dict[str, int] = {}
    test_files: list[str] = ([f for f in sorted(file_tokens) if _is_test_path(f)]
                             if file_tokens else [])

    def refs(name: str, own_file: str) -> int:
        if not file_tokens:
            return 1  # no reference data: keep full signatures
        if name not in ref_counts:
            ref_counts[name] = sum(1 for f in test_files
                                   if f != own_file and name in file_tokens[f])
        return ref_counts[name]

    for s in symbols:
        if s.kind != "class" or not _is_test_path(s.file):
            continue
        stem_ok = not _test_stem(Path(s.file).stem)
        name_ok = not s.name.endswith(("Test", "Tests", "Spec", "IT"))
        if stem_ok and name_ok:
            ids[id(s)] = refs(s.name, s.file) >= 1
            continue
        # (b) has no name gate on purpose: shared Base*Test classes are the
        # most common Java helper shape and are exactly what (a) misses
        if file_tokens and refs(s.name, s.file) >= 2:
            ids[id(s)] = True
    return ids


_EDGE_CAP = 1  # coverage-edge targets shown per test class; rest summarize to +N


def _informative_targets(owner: str, targets: list[str]) -> list[str]:
    """Coverage targets not already guessable from the test's own name:
    `TaskLoaderTest > load_tasks` says nothing, so it renders as `+1`;
    surprising targets keep their names."""
    flat_owner = re.sub(r"[_./-]", "", owner).casefold()
    out = []
    for t in targets:
        base = re.sub(r"[_./-]", "", t.rsplit(".", 1)[-1]).casefold()
        if base and base in flat_owner:
            continue
        out.append(t)
    return out


def _edge_suffix(owner: str, targets: list[str], braced: bool = False) -> str:
    """`>headline+N` coverage note. Inside braces it glues to the member name
    with no spaces so the comma stays the member separator."""
    if not targets:
        return ""
    informative = _informative_targets(owner, targets)
    shown = informative[:_EDGE_CAP]
    more = len(targets) - len(shown)
    if braced:
        if not shown:
            # every target was guessable from the name; a bare +1/+2 says
            # nearly nothing, so only larger surfaces earn the marker
            return f"+{more}" if more > 2 else ""
        return f">{shown[0]}" + (f"+{more}" if more else "")
    if not shown:
        return f" +{more}" if more > 2 else ""
    return f" > {','.join(shown)}" + (f" +{more}" if more else "")


def _test_index_lines(files: list[Path], symbols: list[Symbol], root: Path,
                      resolved_calls: dict[int, list[str]] | None = None,
                      helper_ids: dict[int, bool] | None = None,
                      sig_line=None) -> list[str]:
    test_paths = sorted(str(path.relative_to(root)) for path in files
                        if _is_test_path(str(path.relative_to(root))))
    if not test_paths:
        return []
    classes: dict[str, list[str]] = {}
    # (file, class) -> production symbols the class's methods resolve to; the
    # per-class view of the same edges the ✓ marker flattens
    edges: dict[tuple[str, str], list[str]] = {}
    for symbol in sorted(symbols, key=lambda s: (s.file, s.line, s.name)):
        if not _is_test_path(symbol.file):
            continue
        if symbol.kind == "class":
            names = classes.setdefault(symbol.file, [])
            if symbol.name not in names:
                names.append(symbol.name)
        elif symbol.kind in ("fn", "method", "ctor") and resolved_calls:
            key = (symbol.file, symbol.container or "")
            merged = edges.setdefault(key, [])
            merged[:] = list(dict.fromkeys(
                merged + resolved_calls.get(id(symbol), [])))
    first_parts = {Path(path).parts[0] for path in test_paths if Path(path).parts}
    strip_first = (len(first_parts) == 1
                   and next(iter(first_parts)).casefold() in ("test", "tests", "__tests__"))
    helper_ids = helper_ids or {}
    helpers: dict[str, list[Symbol]] = {}
    for symbol in sorted(symbols, key=lambda s: (s.file, s.line, s.name)):
        if id(symbol) in helper_ids:
            helpers.setdefault(symbol.file, []).append(symbol)
    helper_names = {(s.file, s.name)
                    for hs in helpers.values() for s in hs}
    payloads: dict[str, list[str]] = {}
    for path in test_paths:
        display_path = Path(*Path(path).parts[1:]) if strip_first else Path(path)
        in_braces = [n + _edge_suffix(n, edges.get((path, n), []), braced=True)
                     for n in classes.get(path, [])
                     if (path, n) not in helper_names]
        file_line = _braced_lines(display_path.name, in_braces)
        file_line[-1] += _edge_suffix(Path(path).stem, edges.get((path, ""), []))
        own_lines: list[str] = []
        helper_lines: list[str] = []
        for h in helpers.get(path, []):
            # helper fields are internals; the public methods are the API
            helper_lines.append(f" *{h.name}({KIND_LETTER.get(h.kind, 'C')})")
            if not helper_ids.get(id(h), False):
                continue  # unreferenced helper: name it, skip its signatures
            for ms in sorted(symbols, key=lambda s: (s.line, s.name)):
                if (ms.file == path and ms.container == h.name
                        and ms.kind in ("method", "ctor")
                        and ms.visibility == "pub" and sig_line is not None):
                    helper_lines.append("  " + sig_line(ms, h.name, False))
        payloads.setdefault(str(display_path.parent), []).extend(
            file_line + own_lines + helper_lines)
    return ["? tests", *(" " + line for line in _tree_lines(payloads))]


def _legend_line(text: str, has_priv: bool, has_tests: bool,
                 has_helpers: bool = False) -> str:
    """Legend restricted to notation the rendered body actually uses.

    Needles must never miss real usage (a clause too many wastes a few tokens,
    a clause too few leaves notation unexplained); brace detection strips the
    always-explained type-header form `(K{` first so only factored/expansion
    braces trigger the `p{a,b}` clause."""
    first = "C/R/I{fields}"
    if "(E{" in text or "(E)" in text:
        first += " E{values}"
    if "(T:" in text:
        first += " T:target"
    items = [first, "f(args):Ret > project calls"]
    if has_priv:
        items.append("-=private")
    if has_tests:
        items.append("?=tests")
    if has_helpers:
        items.append("*=test helper")
    if "×0" in text:
        items.append("×0=no static use")
    if "✓" in text:
        items.append("✓=tested")
    if re.search(r" ~\d", text):
        items.append("~N=lines")
    if re.search(r" !\S", text):
        items.append("!E=throws")
    if " @" in text:
        items.append("@=route/annotation")
    if re.search(r"(?m)^\s*= ", text):
        items.append("= consts")
    if "{" in re.sub(r"\([CRIE]\{| @\S+", "", text):
        items.append("p{a,b}=pa,pb")
    if re.search(r"\}[\w-]", text):
        items.append("{a,b}s=as,bs")
    if re.search(r"\+\d+\b", text):
        items.append("+N=more")
    if " : " in text:
        items.append(":T=supers")
    if "sealed:" in text:
        items.append("sealed:A|B")
    if "←" in text:
        items.append("←A|B=implementors")
    if "»" in text:
        items.append("»=re-exports")
    if "Self" in text:
        items.append("Self=own type")
    if "· deps " in text:
        items.append("deps a→b=a uses b")
    return "· " + " · ".join(items)


def render_simple(root: Path, symbols: list[Symbol], files: list[Path],
                  state: str = "",
                  deps: list[str] | None = None,
                  zero_usage: set[str] | None = None,
                  langs: set[str] | None = None,
                  targets: list[str] | None = None,
                  file_tokens: dict[str, set[str]] | None = None,
                  detail: int = 0,
                  budget: int | None = None) -> str:
    """Compact project facts as a package trie.

    pkg
      Class(K{fields})
        sig > callee, callee
        - privateName,privateName
    """
    prod = [s for s in symbols if not _is_test_path(s.file)]
    if zero_usage is None:
        zero_usage = set()
    resolved_calls, resolved_tested = _resolved_project_calls(symbols)
    tested = resolved_tested
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if (s.kind in TYPE_KINDS + ("fn",) and s.container is None
                and s.visibility == "pub"):
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    # owner keys carry lang so same-named types from different languages in one
    # dir (Pricer in go + rust) don't merge their method lists
    methods_by_owner: dict[tuple[str, str, str], list[Symbol]] = {}
    for s in prod:
        if (s.container and s.kind in ("method", "ctor")
                and s.visibility == "pub"):
            if detail >= 5 and s.container in zero_usage:
                continue  # budget: cold types keep their header, lose methods
            methods_by_owner.setdefault(
                (str(Path(s.file).parent), s.container, s.lang), []).append(s)
    # Lossless names-only private inventory.
    priv_methods_by_owner: dict[tuple[str, str, str], list[str]] = {}
    priv_top_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.visibility != "priv":
            continue
        marked = s.kind in ("fn", "method", "class") and s.name in zero_usage
        name = f"{s.name}×0" if marked else s.name
        if s.container and s.kind in ("method", "ctor"):
            if s.name.startswith("__") and s.name.endswith("__"):
                continue  # __init__/__repr__ restate the class protocol
            owner_key = (str(Path(s.file).parent), s.container, s.lang)
            priv_methods_by_owner.setdefault(owner_key, []).append(name)
        elif s.container is None and s.kind in TYPE_KINDS + ("fn",):
            file_key = (str(Path(s.file).parent), Path(s.file).name)
            priv_top_by_file.setdefault(file_key, []).append(name)

    def _norm(text: str, own: str) -> str:
        return re.sub(rf"\b{re.escape(own)}\b", "Self", text)

    def _argument_names(sym: Symbol) -> list[str]:
        return [sym.param_names[index] if index < len(sym.param_names)
                and sym.param_names[index] else param
                for index, param in enumerate(sym.params)]

    signature_shapes = Counter(
        (s.file, s.container or "", s.name, tuple(_argument_names(s)))
        for s in prod if s.kind in ("fn", "method", "ctor")
    )
    top_locations: dict[str, set[tuple[str, str]]] = {}
    for symbol in prod:
        if (symbol.container is None and symbol.visibility == "pub"
                and symbol.kind in TYPE_KINDS + ("fn",)):
            top_locations.setdefault(symbol.name, set()).add((symbol.file, symbol.lang))

    def _top_display(sym: Symbol) -> str:
        if len(top_locations.get(sym.name, ())) > 1:
            return f"{Path(sym.file).name}:{sym.name}"
        return sym.name

    def _display_signature(sym: Symbol, display_name: str | None = None) -> str:
        args = _argument_names(sym)
        shape = (sym.file, sym.container or "", sym.name, tuple(args))
        if signature_shapes[shape] > 1:
            args = [f"{name}:{sym.params[index]}" if name != sym.params[index] else name
                    for index, name in enumerate(args)]
        returns = (f":{sym.returns}" if sym.returns and sym.kind != "ctor"
                   and sym.returns not in ("void", "Unit", "None") else "")
        if sym.signature and "(" not in sym.signature and sym.lang in ("helm", "html"):
            return sym.signature
        return f"{display_name or sym.name}({','.join(args)}){returns}"

    def _sig_line(sym: Symbol, own: str, grouped: bool,
                  display_name: str | None = None) -> str:
        sig = _display_signature(sym, display_name)
        for note in _decorator_notes(sym.decorators, sym.lang):
            sig = f"{sig} @{note}"
        if sym.size >= 40:
            sig = f"{sig} ~{sym.size}"
        if id(sym) in tested:
            sig = f"{sig} ✓"
        if sym.kind in ("fn", "method") and sym.name in zero_usage:
            sig = f"{sig} ×0"
        kept = resolved_calls.get(id(sym), [])
        if detail >= 4 and sym.name in zero_usage:
            kept = []  # budget: unreferenced symbols lose their chains first
        if sym.raises:
            sig = f"{sig} !{','.join(_strip_exc(r) for r in sym.raises)}"
        if grouped:
            kept = [_norm(c, own) for c in kept]
            sig = _norm(sig, own)
        return f"{sig} > {','.join(kept)}" if kept else sig

    def _redundant_ctor(ms: Symbol, kind: str, components: tuple) -> bool:
        # a bare ctor whose rendered arg list equals the type header's field
        # list restates the header; comparing the full _sig_line output keeps
        # any ctor that carries notes, ✓, ~size, !raises, calls, or typed args
        return (kind in ("class", "record") and bool(components)
                and ms.kind == "ctor"
                and _sig_line(ms, ms.container or ms.name, False)
                == f"{ms.name}({','.join(components)})")

    # Interface relations stated once, on the interface: `I ←Impl|Impl` replaces
    # each implementor's `: I` suffix (sealed permits already carry the list).
    iface_index = {(s.lang, s.name) for s in prod if s.kind == "interface"}
    impls_of: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind in TYPE_KINDS and s.container is None:
            for sup in s.supers:
                if (s.lang, sup) in iface_index:
                    entries = impls_of.setdefault((s.lang, sup), [])
                    if s.name not in entries:
                        entries.append(s.name)

    payload_by_dir: dict[str, list[str]] = {}
    for d, types in sorted(types_by_dir.items()):
        payload = payload_by_dir.setdefault(d, [])
        groups: dict[tuple, list[Symbol]] = {}
        for t in sorted(types, key=lambda s: (s.kind == "fn", s.name)):
            if t.kind == "fn":
                payload.append(_sig_line(t, t.name, False, _top_display(t)))
                continue
            components = (t.params if t.kind == "enum" else
                          t.params[:1] if t.kind == "type" and not t.fields else
                          t.fields or t.params)
            unused = t.kind == "class" and t.name in zero_usage
            shown_supers = tuple(sup for sup in t.supers
                                 if (t.lang, sup) not in iface_index)
            impls = (tuple(n for n in impls_of.get((t.lang, t.name), ())
                           if n not in t.permits)
                     if t.kind == "interface" else ())
            group_key = (t.lang, t.kind, t.visibility, tuple(components),
                         shown_supers, tuple(t.permits), impls, unused,
                         bool(t.fields),
                         tuple(_decorator_notes(t.decorators, t.lang)),
                         tuple(resolved_calls.get(id(t), ())))
            groups.setdefault(group_key, []).append(t)
        for (_, kind, vis, components, supers, permits, impls, unused,
             named_fields, deco_notes, type_calls), members in groups.items():
            members.sort(key=lambda s: s.name)
            names = ",".join(_top_display(m) for m in members)
            letter = KIND_LETTER.get(kind, "?")
            if kind == "type" and components and not named_fields:
                inner = f"{letter}:{components[0]}"
            elif components:
                inner = f"{letter}{{{','.join(components)}}}"
            else:
                inner = letter
            permit_suffix = f" sealed:{'|'.join(permits)}" if permits else ""
            rel_suffix = f" : {','.join(supers)}" if supers else ""
            impl_suffix = ("" if not impls else
                           f" ←{len(impls)} impls" if len(impls) > 6 else
                           f" ←{'|'.join(impls)}")
            hot_suffix = " ×0" if unused else ""
            deco_suffix = "".join(f" @{n}" for n in deco_notes)
            call_suffix = f" > {','.join(type_calls)}" if type_calls else ""
            payload.append(f"{names}({inner}){deco_suffix}{rel_suffix}"
                           f"{permit_suffix}{impl_suffix}{hot_suffix}{call_suffix}")
            # Methods shared by every member print once (Self-normalized); each
            # member's remaining methods print on its own `Name: …` line.
            member_methods = {
                id(m): [ms for ms in methods_by_owner.get((d, m.name, m.lang), [])
                        if not _redundant_ctor(ms, kind, components)]
                for m in members}
            head_member = members[0]
            def _priv_lines(m: Symbol, prefix: str = "", directory: str = d
                            ) -> list[str]:
                names_only = priv_methods_by_owner.get((directory, m.name, m.lang))
                if not names_only:
                    return []
                return _private_lines(f" {prefix}- ", names_only)

            if len(members) == 1:
                for ms in member_methods[id(head_member)]:
                    payload.append(" " + _sig_line(ms, head_member.name, False))
                payload.extend(_priv_lines(head_member))
                continue
            normed = {id(m): [_sig_line(ms, m.name, True)
                              for ms in member_methods[id(m)]] for m in members}
            shared = set.intersection(*(set(v) for v in normed.values()))
            emitted: set[str] = set()
            for line in normed[id(head_member)]:
                if line in shared and line not in emitted:
                    payload.append(" " + line)
                    emitted.add(line)
            for m in members:
                extras = [ms for ms, ln in zip(member_methods[id(m)], normed[id(m)])
                          if ln not in shared]
                if extras:
                    payload.append(f" {m.name}: "
                                   + "; ".join(_sig_line(ms, m.name, False)
                                               for ms in extras))
                payload.extend(_priv_lines(m, f"{m.name} "))

    consts_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "const" and s.visibility == "pub":
            const_key = (str(Path(s.file).parent), Path(s.file).name)
            vals = consts_by_file.setdefault(const_key, [])
            entry = s.signature or s.name
            if detail >= 1:
                entry = entry.split("=", 1)[0]  # budget: values drop first
            if entry not in vals:
                vals.append(entry)
    for (d, fname), vals in sorted(consts_by_file.items()):
        payload_by_dir.setdefault(d, []).extend(
            _private_lines(f"= {fname}: ", vals))

    if detail >= 3:
        priv_top_by_file = {}
        priv_methods_by_owner = {}
    for (d, stem), names_only in sorted(priv_top_by_file.items()):
        payload_by_dir.setdefault(d, []).extend(
            _private_lines(f"- {stem}: ", names_only))
    public_owners = {(d, member.name, member.lang)
                     for d, types in types_by_dir.items() for member in types
                     if member.kind in TYPE_KINDS}
    for (d, owner, lang), names_only in sorted(priv_methods_by_owner.items()):
        if (d, owner, lang) not in public_owners:
            payload_by_dir.setdefault(d, []).extend(
                _private_lines(f"- {owner}: ", names_only))
    reex_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "reexport":
            reexport_key = (str(Path(s.file).parent), Path(s.file).name)
            names_r = reex_by_file.setdefault(reexport_key, [])
            if s.name not in names_r:
                names_r.append(s.name)
    for (d, fname), names_r in sorted(reex_by_file.items()):
        payload_by_dir.setdefault(d, []).append(f"» {fname}: {','.join(names_r)}")

    loc = _total_loc(files)
    state_part = f" · state {state}" if state else ""
    if langs:
        state_part += f" · langs {','.join(sorted(langs))}"
    if targets:
        state_part += f" · targets {','.join(sorted(targets))}"
    if budget:
        state_part += f" · budget {budget}" + (f" L{detail}" if detail else "")
    dep_part = ("\n".join(deps) + "\n") if deps else ""
    body = _tree_lines(payload_by_dir)
    helper_ids = _helper_class_ids(symbols, file_tokens)
    tests = _test_index_lines(files, symbols, root,
                              resolved_calls if detail < 2 else None,
                              helper_ids if detail < 2 else
                              {k: False for k in helper_ids},
                              _sig_line)
    if tests:
        body.extend(tests)
    has_priv = bool(priv_top_by_file or priv_methods_by_owner)
    has_helpers = bool(tests) and any("\n  *" in "\n" + ln or ln.lstrip().startswith("*")
                                      for ln in tests)
    legend = _legend_line(dep_part + "\n".join(body), has_priv, bool(tests),
                          has_helpers)
    header = f"# hologram · {loc:,} LOC{state_part}\n{legend}\n"
    return header + dep_part + "\n".join(body) + "\n"


def build_digest(root: Path, langs: set[str] | None = None,
                 targets: list[str] | None = None,
                 budget: int | None = None) -> str:
    files, symbols, file_tokens, usage_tokens, state = _gather(root, langs)
    deps = _dep_lines(symbols, file_tokens)
    zero = _zero_usage_names(symbols, usage_tokens)

    def render(level: int) -> str:
        return render_simple(root, symbols, files, state=state, deps=deps,
                             zero_usage=zero, langs=langs, targets=targets,
                             file_tokens=file_tokens, detail=level,
                             budget=budget)

    digest = render(0)
    if not budget:
        return digest
    # deterministic degradation ladder: drop whole fact categories, never
    # truncate — stop at the first level that fits the budget
    level = 0
    while estimate_tokens(digest) > budget and level < 5:
        level += 1
        digest = render(level)
    if estimate_tokens(digest) > budget:
        import sys
        print(f"hologram: warning: even the sparsest map is "
              f"~{estimate_tokens(digest):,} tokens against a budget of "
              f"{budget:,}; emitting it whole — narrowing with --lang may "
              f"help", file=sys.stderr)
    return digest

