"""All digest layout: render_simple owns every formatting decision."""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from .gather import _framework_invoked, _gather, _zero_usage_names
from .symbols import (MARKER_DECORATORS, ROUTE_DECORATORS, TYPE_KINDS, Symbol,
                      _base_type)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

KIND_LETTER = {"record": "R", "class": "C", "interface": "I", "enum": "E", "fn": "F",
               "type": "T"}


def estimate_tokens(text: str) -> int:
    """Dependency-free planning estimate (ceil(characters / 4)).

    This is deterministic across installations, but it is not a hard limit
    from any provider's model tokenizer.
    """
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


def _is_production_symbol(symbol: Symbol) -> bool:
    # Make targets remain externally invokable even in tests/Makefile or
    # test.mk; their location does not turn them into test implementation.
    return symbol.lang == "make" or not _is_test_path(symbol.file)


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
    types = [s for s in targets if s.kind in TYPE_KINDS]
    by_file: dict[tuple[str, str, str], list[Symbol]] = {}
    by_dir: dict[tuple[str, str, str], list[Symbol]] = {}
    globally: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in types:
        by_file.setdefault((symbol.file, symbol.lang, symbol.name), []).append(symbol)
        by_dir.setdefault((str(Path(symbol.file).parent), symbol.lang, symbol.name),
                          []).append(symbol)
        globally.setdefault((symbol.lang, symbol.name), []).append(symbol)

    def owner_key(symbol: Symbol) -> tuple[str, str, str] | None:
        if not symbol.container:
            return None
        candidates = by_file.get((symbol.file, symbol.lang, symbol.container), [])
        if len(candidates) != 1:
            candidates = by_dir.get((str(Path(symbol.file).parent), symbol.lang,
                                     symbol.container), [])
        if len(candidates) != 1:
            candidates = globally.get((symbol.lang, symbol.container), [])
        if len(candidates) != 1:
            return symbol.file, symbol.container, symbol.lang
        owner = candidates[0]
        return owner.file, owner.name, owner.lang

    logical_groups: dict[tuple, list[Symbol]] = {}
    for symbol in targets:
        logical = (("member", owner_key(symbol), symbol.kind, symbol.name,
                    symbol.signature, tuple(symbol.params), symbol.returns)
                   if symbol.container else
                   ("top", _symbol_identity(symbol)))
        logical_groups.setdefault(logical, []).append(symbol)
    representatives = [members[0] for members in logical_groups.values()]
    by_name = Counter(s.name for s in representatives)
    qualified = {
        id(s): (f"{s.container}.{s.name}" if s.container else s.name)
        for s in representatives
    }
    by_qualified = Counter(qualified.values())
    stemmed = {id(s): f"{Path(s.file).stem}.{qualified[id(s)]}"
               for s in representatives}
    by_stemmed = Counter(stemmed.values())
    out: dict[int, str] = {}
    for members in logical_groups.values():
        symbol = members[0]
        if by_name[symbol.name] == 1:
            description = symbol.name
        elif by_qualified[qualified[id(symbol)]] == 1:
            description = qualified[id(symbol)]
        elif by_stemmed[stemmed[id(symbol)]] == 1:
            description = stemmed[id(symbol)]
        else:
            description = f"{symbol.file}:{qualified[id(symbol)]}"
        for member in members:
            out[id(member)] = description
    return out


def _raw_call_targets(symbols: list[Symbol]) -> dict[int, list[Symbol]]:
    """Each symbol's raw calls resolved to project Symbol targets — the shared
    resolution core under both rendering and `hologram review`."""
    production = [s for s in symbols if _is_production_symbol(s)]
    targets = [s for s in production if s.kind in TYPE_KINDS + ("fn", "method")]
    types = [s for s in targets if s.kind in TYPE_KINDS]
    callables = [s for s in targets if s.kind in ("fn", "method")]

    type_index: dict[tuple[str, str], list[Symbol]] = {}
    method_index: dict[tuple[str, str, str], list[Symbol]] = {}
    file_type_index: dict[tuple[str, str, str], list[Symbol]] = {}
    file_method_index: dict[tuple[str, str, str, str], list[Symbol]] = {}
    dir_type_index: dict[tuple[str, str, str], list[Symbol]] = {}
    dir_method_index: dict[tuple[str, str, str, str], list[Symbol]] = {}
    file_top_index: dict[tuple[str, str, str], list[Symbol]] = {}
    module_top_index: dict[tuple[str, str, str], list[Symbol]] = {}
    lang_name_index: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in types:
        type_index.setdefault((symbol.lang, symbol.name), []).append(symbol)
        file_type_index.setdefault(
            (symbol.lang, symbol.file, symbol.name), []).append(symbol)
        dir_type_index.setdefault(
            (symbol.lang, str(Path(symbol.file).parent), symbol.name),
            []).append(symbol)
    for symbol in callables:
        lang_name_index.setdefault((symbol.lang, symbol.name), []).append(symbol)
        if symbol.container:
            method_index.setdefault(
                (symbol.lang, symbol.container, symbol.name), []).append(symbol)
            file_method_index.setdefault(
                (symbol.lang, symbol.file, symbol.container, symbol.name),
                []).append(symbol)
            dir_method_index.setdefault(
                (symbol.lang, str(Path(symbol.file).parent),
                 symbol.container, symbol.name), []).append(symbol)
        else:
            file_top_index.setdefault(
                (symbol.lang, symbol.file, symbol.name), []).append(symbol)
            module_top_index.setdefault(
                (symbol.lang, Path(symbol.file).stem, symbol.name), []).append(symbol)

    def one(items: list[Symbol] | None) -> Symbol | None:
        return items[0] if items is not None and len(items) == 1 else None

    def canonical_method(items: list[Symbol] | None) -> Symbol | None:
        """Collapse an exact declaration/definition pair, not overloads."""
        if not items:
            return None
        shapes = {
            (item.kind, item.signature, tuple(item.params), item.returns)
            for item in items
        }
        if len(shapes) != 1:
            return None
        return max(items, key=lambda item: (
            bool(item.calls), len(item.calls), bool(item.raises),
            len(item.raises), item.size, len(item.bindings),
            len(item.decorators)))

    def owner_type(caller: Symbol, owner: str) -> Symbol | None:
        local = one(file_type_index.get((caller.lang, caller.file, owner)))
        if local is not None:
            return local
        directory = str(Path(caller.file).parent)
        nearby = one(dir_type_index.get((caller.lang, directory, owner)))
        if nearby is not None:
            return nearby
        return one(type_index.get((caller.lang, owner)))

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
        "csharp", "kotlin", "cpp", "rust", "make", "go", "bash",
    }

    def owner_method(caller: Symbol, owner: str, name: str) -> Symbol | None:
        """Resolve an owner method without crossing same-named file types.

        A file-local declaration is authoritative.  Global fallback is safe
        only when the owner type itself is unique: otherwise two unrelated
        ``Client`` classes can lend methods (and fan-in) to each other.
        """
        local = canonical_method(file_method_index.get(
            (caller.lang, caller.file, owner, name)))
        if local is not None:
            return local
        directory = str(Path(caller.file).parent)
        directory_types = dir_type_index.get(
            (caller.lang, directory, owner), ())
        if len(directory_types) == 1:
            return canonical_method(dir_method_index.get(
                (caller.lang, directory, owner, name)))
        # A local declaration amid multiple same-named directory types is an
        # ownership boundary: do not borrow a sibling class's method.
        if file_type_index.get((caller.lang, caller.file, owner)):
            return None
        if len(type_index.get((caller.lang, owner), ())) != 1:
            return None
        return canonical_method(method_index.get((caller.lang, owner, name)))

    def resolve(caller: Symbol, raw: str) -> Symbol | None:
        if "-" in raw and caller.lang in ("typescript", "html"):
            return selector_class.get(raw)
        receiver, dot, name = raw.rpartition(".")
        if not dot:
            name = raw
            target_type = owner_type(caller, name)
            if target_type is not None:
                return target_type
            if caller.container and caller.lang in same_container_languages:
                target = owner_method(caller, caller.container, name)
                if target is not None:
                    return target
            target = one(file_top_index.get((caller.lang, caller.file, name)))
            if target is not None:
                return target
            return one(lang_name_index.get((caller.lang, name)))

        if receiver == caller.container and caller.container:
            return owner_method(caller, caller.container, name)
        if receiver in ("self", "cls", "this") and caller.container:
            return owner_method(caller, caller.container, name)
        if receiver in caller.bindings:
            owner = _base_type(caller.bindings[receiver])
            return owner_method(caller, owner, name)
        owner = receiver.rsplit(".", 1)[-1]
        if owner_type(caller, owner) is not None:
            return owner_method(caller, owner, name)
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
                            ) -> tuple[dict[int, list[str]], set[int],
                                       dict[int, list[Symbol]]]:
    """Resolve raw calls to project symbols; omit external and ambiguous targets.

    Returns display calls by caller identity, production targets called by
    tests, and the raw caller-id → target-Symbol edges (for fan-in ranking).
    """
    production = [s for s in symbols if _is_production_symbol(s)]
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
        if caller.visibility == "pub" and _is_production_symbol(caller):
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
        for caller in symbols
        if _is_test_path(caller.file) and caller.lang != "make"
        for target in raw_targets[id(caller)]
    }
    return displayed, tested, raw_targets


_MAX_LEVEL = 7  # deepest budget-ladder degradation level (the skeleton)

_BUDGET_POLICY_VERSION = "adaptive-bundles-v1"
_ADAPTIVE_MAX_TRIALS = 128
# Slice of the cap the greedy pass may not spend, so the repair pass always
# runs even when greedy packing exhausts its own allowance.
_ADAPTIVE_REPAIR_TRIALS = 32
_ADAPTIVE_SATURATION = 0.99
_BUNDLE_CATEGORY_ORDER = {
    "public-methods": 0,
    "const-values": 1,
    "tested-call-chains": 2,
    "cold-methods": 3,
    "untested-call-chains": 4,
    "private-names": 5,
    "helper-signatures": 6,
    "test-coverage": 7,
}


@dataclass(frozen=True, order=True)
class BudgetBundle:
    """One independently retainable map fact.

    ``key`` is stable for identical source facts and file-qualified so equal
    symbol names in different ownership scopes never share a budget decision.
    ``detail`` is the first ladder level at which the fact is absent.
    """

    detail: int
    category: str
    key: str
    estimated_chars: int = field(default=0, compare=False)

    @property
    def name(self) -> str:
        return f"{self.category}:{self.key}"


@dataclass(frozen=True)
class BudgetStats:
    """Inspectable, JSON-ready evidence for one budget decision."""

    policy_version: str
    requested_budget: int | None
    full_tokens: int
    selected_tokens: int
    skeleton_tokens: int
    effective_detail: str
    utilization: float | None
    fits: bool
    retained_categories: tuple[tuple[str, int], ...]
    dropped_categories: tuple[tuple[str, int], ...]
    retained_bundles: tuple[str, ...]
    dropped_bundles: tuple[str, ...]
    selection_trials: int = 0
    selection_candidates: int = 0
    search_truncated: bool = False
    stop_reason: str = "not-limited"

    def as_dict(self) -> dict[str, object]:
        """Return only JSON-native values; ordering stays deterministic."""
        return {
            "policy_version": self.policy_version,
            "requested_budget": self.requested_budget,
            "full_tokens": self.full_tokens,
            "selected_tokens": self.selected_tokens,
            "skeleton_tokens": self.skeleton_tokens,
            "effective_detail": self.effective_detail,
            "utilization": self.utilization,
            "fits": self.fits,
            "retained_categories": dict(self.retained_categories),
            "dropped_categories": dict(self.dropped_categories),
            "retained_bundles": list(self.retained_bundles),
            "dropped_bundles": list(self.dropped_bundles),
            "selection_trials": self.selection_trials,
            "selection_candidates": self.selection_candidates,
            "search_truncated": self.search_truncated,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class _BudgetSelection:
    names: frozenset[str]
    level: int
    available: int


def _bundle_key(symbol: Symbol) -> str:
    """Stable logical identity; never use process-local ``id(symbol)``."""
    signature = symbol.signature or ",".join(symbol.params)
    return "|".join((symbol.file, symbol.lang, symbol.container or "",
                     symbol.kind, symbol.name, signature, str(symbol.line)))


def _bundle_estimated_chars(category: str, symbol: Symbol,
                            suffix: str = "") -> int:
    """Cheap rendered-size hint used only to order exact bounded trials.

    The hard ceiling still comes from a complete re-render. Including payload
    facts here prevents a short caller name with a very long call chain from
    starving a later small fact merely because the search has a trial cap.
    """
    if category == "private-names":
        parts = [symbol.name]
    elif category in ("tested-call-chains", "untested-call-chains"):
        parts = [symbol.name, *symbol.calls]
    elif category == "const-values":
        parts = [symbol.signature or symbol.name]
    elif category == "test-coverage":
        parts = [symbol.name, *symbol.calls]
    else:
        parts = [
            symbol.signature or symbol.name,
            *symbol.params,
            symbol.returns or "",
            *symbol.raises,
            *symbol.decorators,
            *symbol.fields,
            *symbol.supers,
            *symbol.permits,
        ]
    return max(1, len(suffix) + sum(len(part) + 1 for part in parts))


def summarize_budget(*, requested_budget: int | None, full_tokens: int,
                     selected_tokens: int, skeleton_tokens: int,
                     effective_detail: str,
                     bundles: set[BudgetBundle],
                     retained: set[BudgetBundle],
                     selection_trials: int = 0,
                     selection_candidates: int = 0,
                     search_truncated: bool = False,
                     stop_reason: str = "not-limited") -> BudgetStats:
    """Pure conversion from selection facts to stable public statistics."""
    retained = retained & bundles
    dropped = bundles - retained

    def categories(items: set[BudgetBundle]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(Counter(bundle.category for bundle in items).items()))

    limited = requested_budget is not None and requested_budget > 0
    return BudgetStats(
        policy_version=_BUDGET_POLICY_VERSION,
        requested_budget=requested_budget,
        full_tokens=full_tokens,
        selected_tokens=selected_tokens,
        skeleton_tokens=skeleton_tokens,
        effective_detail=effective_detail,
        utilization=(selected_tokens / requested_budget if limited else None),
        fits=(not limited or selected_tokens <= requested_budget),
        retained_categories=categories(retained),
        dropped_categories=categories(dropped),
        retained_bundles=tuple(sorted(bundle.name for bundle in retained)),
        dropped_bundles=tuple(sorted(bundle.name for bundle in dropped)),
        selection_trials=selection_trials,
        selection_candidates=selection_candidates,
        search_truncated=search_truncated,
        stop_reason=stop_reason,
    )

# level → human label for the in-map disclosure line (cumulative)
_LEVEL_DROPS = (
    (1, "test coverage edges"),
    (2, "test-helper signatures"),
    (3, "private names"),
    (4, "untested call chains"),
    (5, "unreferenced types' methods"),
    (6, "all call chains"),
    (7, "non-entrypoint method lines and const values"),
)


def _essential_method(symbol: Symbol) -> bool:
    """Methods whose names/signatures are part of an external calling surface.

    Static project fan-in cannot see framework dispatch or a human/CI invoking a
    Make target.  These entrypoints therefore survive every degradation level.
    """
    return (symbol.kind in ("fn", "method")
            and (_framework_invoked(symbol)
                 or (symbol.lang == "make" and symbol.kind == "method"
                     and symbol.visibility == "pub")))

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
    make_files = {s.file for s in symbols if s.lang == "make"}
    test_files: list[str] = ([f for f in sorted(file_tokens)
                              if _is_test_path(f) and f not in make_files]
                             if file_tokens else [])

    def refs(name: str, own_file: str) -> int:
        if not file_tokens:
            return 1  # no reference data: keep full signatures
        if name not in ref_counts:
            ref_counts[name] = sum(1 for f in test_files
                                   if f != own_file and name in file_tokens[f])
        return ref_counts[name]

    for s in symbols:
        if (s.kind != "class" or s.lang == "make"
                or not _is_test_path(s.file)):
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
    make_files = {s.file for s in symbols if s.lang == "make"}
    test_paths = sorted(
        str(path.relative_to(root)) for path in files
        if (_is_test_path(str(path.relative_to(root)))
            and str(path.relative_to(root)) not in make_files))
    if not test_paths:
        return []
    classes: dict[str, list[str]] = {}
    # (file, class) -> production symbols the class's methods resolve to; the
    # per-class view of the same edges the ✓ marker flattens
    edges: dict[tuple[str, str], list[str]] = {}
    for symbol in sorted(symbols, key=lambda s: (s.file, s.line, s.name)):
        if symbol.lang == "make" or not _is_test_path(symbol.file):
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
    suffixes = {Path(p).suffix for p in test_paths}
    shared_ext = (next(iter(suffixes))
                  if len(suffixes) == 1 and next(iter(suffixes)) else "")
    payloads: dict[str, list[str]] = {}
    for path in test_paths:
        display_path = Path(*Path(path).parts[1:]) if strip_first else Path(path)
        display_name = display_path.name.removesuffix(shared_ext)
        own_classes = [n for n in classes.get(path, [])
                       if (path, n) not in helper_names]
        if own_classes == [Path(path).stem]:
            # single class named like its file: the braces restate the stem
            file_line = [display_name + _edge_suffix(
                own_classes[0], edges.get((path, own_classes[0]), []),
                braced=True)]
        else:
            in_braces = [n + _edge_suffix(n, edges.get((path, n), []),
                                          braced=True)
                         for n in own_classes]
            file_line = _braced_lines(display_name, in_braces)
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
    header = f"? tests ·{shared_ext}" if shared_ext else "? tests"
    return [header, *(" " + line for line in _tree_lines(payloads))]


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
    items = [first, "f(args):Ret > project calls" if " > " in text
             else "f(args):Ret"]  # skeleton maps carry no chains
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
    return "· " + " · ".join(items)


def render_simple(root: Path, symbols: list[Symbol], files: list[Path],
                  state: str = "",
                  zero_usage: set[str] | None = None,
                  langs: set[str] | None = None,
                  targets: list[str] | None = None,
                  file_tokens: dict[str, set[str]] | None = None,
                  detail: int = 0,
                  budget: int | None = None,
                  loc: int | None = None,
                  resolved: tuple | None = None,
                  helpers: dict[int, bool] | None = None,
                  budget_selection: _BudgetSelection | None = None,
                  budget_catalog: set[BudgetBundle] | None = None,
                  budget_retained: set[BudgetBundle] | None = None) -> str:
    """Compact project facts as a package trie.

    `loc`, `resolved` (the _resolved_project_calls triple) and `helpers`
    (_helper_class_ids) are level-invariant and may be precomputed by the
    caller — they MUST come from the same id()-keyed `symbols` list; when
    None they are computed here, so the function stays independently usable.

    pkg
      Class(K{fields})
        sig > callee, callee
        - privateName,privateName
    """
    prod = [s for s in symbols if _is_production_symbol(s)]
    if zero_usage is None:
        zero_usage = set()

    def _make_bundle(category: str, level: int, symbol: Symbol,
                     suffix: str = "", payload_chars: int | None = None
                     ) -> BudgetBundle:
        key = _bundle_key(symbol) + suffix
        return BudgetBundle(
            level, category, key,
            estimated_chars=(payload_chars if payload_chars is not None else
                             _bundle_estimated_chars(category, symbol, suffix)))

    def _record_bundle(bundle: BudgetBundle, kept: bool) -> None:
        if budget_catalog is not None:
            budget_catalog.add(bundle)
        if kept and budget_retained is not None:
            budget_retained.add(bundle)

    def _keep_bundle(category: str, level: int, symbol: Symbol,
                     *, default: bool, suffix: str = "",
                     payload_chars: int | None = None,
                     record: bool = True) -> bool:
        bundle = _make_bundle(category, level, symbol, suffix, payload_chars)
        kept = (default or (budget_selection is not None
                            and bundle.name in budget_selection.names))
        if record:
            _record_bundle(bundle, kept)
        return kept

    resolved_calls, resolved_tested, raw_targets = (
        resolved if resolved is not None else _resolved_project_calls(symbols))
    # Candidate renders add ephemeral merged declaration/definition symbols;
    # never leak those ids into the level-invariant precomputed structures.
    resolved_calls = dict(resolved_calls)
    tested = set(resolved_tested)
    types_by_dir: dict[str, list[Symbol]] = {}
    for s in prod:
        if (s.kind in TYPE_KINDS + ("fn",) and s.container is None
                and (s.visibility == "pub" or _essential_method(s))):
            types_by_dir.setdefault(str(Path(s.file).parent), []).append(s)
    # Ownership must see non-public types too: a private controller can expose
    # an externally invoked route method and therefore needs a visible owner.
    owner_types = [s for s in prod
                   if s.kind in TYPE_KINDS and s.container is None]
    owner_types_by_file: dict[tuple[str, str, str], list[Symbol]] = {}
    owner_types_by_dir: dict[tuple[str, str, str], list[Symbol]] = {}
    owner_types_global: dict[tuple[str, str], list[Symbol]] = {}
    for owner in owner_types:
        owner_types_by_file.setdefault(
            (owner.file, owner.lang, owner.name), []).append(owner)
        owner_types_by_dir.setdefault(
            (str(Path(owner.file).parent), owner.lang, owner.name),
            []).append(owner)
        owner_types_global.setdefault((owner.lang, owner.name), []).append(owner)

    def _member_owner_key(member: Symbol) -> tuple[str, str, str] | None:
        """Attach a member to the narrowest unambiguous type declaration.

        Exact-file ownership separates unrelated same-named classes. The
        directory and global fallbacks retain normal split declarations such
        as Go receivers or C++ header/source definitions.
        """
        if not member.container:
            return None
        candidates = owner_types_by_file.get(
            (member.file, member.lang, member.container), [])
        if len(candidates) != 1:
            candidates = owner_types_by_dir.get(
                (str(Path(member.file).parent), member.lang,
                 member.container), [])
        if len(candidates) != 1:
            candidates = owner_types_global.get(
                (member.lang, member.container), [])
        if len(candidates) != 1:
            return None
        owner = candidates[0]
        return owner.file, owner.name, owner.lang

    member_groups: dict[tuple, list[Symbol]] = {}
    member_order: list[tuple] = []
    for member in prod:
        owner_key = _member_owner_key(member)
        if (owner_key is None
                or member.kind not in ("method", "ctor")):
            continue
        shape = (owner_key, member.kind, member.name, member.signature,
                 tuple(member.params), member.returns)
        if shape not in member_groups:
            member_order.append(shape)
        member_groups.setdefault(shape, []).append(member)
    canonical_members: list[Symbol] = []
    for shape in member_order:
        variants = member_groups[shape]
        chosen = max(variants, key=lambda member: (
            bool(resolved_calls.get(id(member))),
            len(resolved_calls.get(id(member), ())),
            bool(member.raises), len(member.raises), member.size,
            len(member.bindings), len(member.decorators)))
        merged = replace(
            chosen,
            # Out-of-line C++ definitions carry no access section and extract
            # as public; an explicit private declaration remains authoritative.
            visibility=("priv" if any(v.visibility == "priv" for v in variants)
                        else "pub" if any(v.visibility == "pub" for v in variants)
                        else chosen.visibility),
            calls=list(dict.fromkeys(call for v in variants for call in v.calls)),
            raises=list(dict.fromkeys(item for v in variants for item in v.raises)),
            bindings={key: value for v in variants
                      for key, value in v.bindings.items()},
            decorators=list(dict.fromkeys(
                item for v in variants for item in v.decorators)),
            size=max(v.size for v in variants),
        )
        resolved_calls[id(merged)] = list(dict.fromkeys(
            call for v in variants for call in resolved_calls.get(id(v), ())))
        if any(id(v) in tested for v in variants):
            tested.add(id(merged))
        canonical_members.append(merged)
    member_ids = {id(s) for variants in member_groups.values() for s in variants}
    render_prod = [s for s in prod if id(s) not in member_ids] + canonical_members

    # Module-shaped extractors can emit repeated ownerless declarations (for
    # example Lua ``M.run``) without a synthetic owner type. Canonicalize those
    # before budget accounting so one rendered fact is one bundle, and merge
    # complementary declaration/definition metadata just like owned methods.
    orphan_groups: dict[tuple, list[Symbol]] = {}
    orphan_order: list[tuple] = []
    for member in render_prod:
        if (not member.container or member.kind not in ("method", "ctor")
                or _member_owner_key(member) is not None):
            continue
        shape = (member.file, member.lang, member.container, member.kind,
                 member.name, member.signature, tuple(member.params),
                 member.returns)
        if shape not in orphan_groups:
            orphan_order.append(shape)
        orphan_groups.setdefault(shape, []).append(member)
    canonical_orphans: list[Symbol] = []
    for shape in orphan_order:
        variants = orphan_groups[shape]
        chosen = max(variants, key=lambda member: (
            bool(resolved_calls.get(id(member))),
            len(resolved_calls.get(id(member), ())),
            bool(member.raises), len(member.raises), member.size,
            len(member.bindings), len(member.decorators)))
        merged = replace(
            chosen,
            visibility=("priv" if any(v.visibility == "priv" for v in variants)
                        else "pub"),
            calls=list(dict.fromkeys(call for v in variants for call in v.calls)),
            raises=list(dict.fromkeys(item for v in variants for item in v.raises)),
            bindings={key: value for v in variants
                      for key, value in v.bindings.items()},
            decorators=list(dict.fromkeys(
                item for v in variants for item in v.decorators)),
            size=max(v.size for v in variants),
        )
        resolved_calls[id(merged)] = list(dict.fromkeys(
            call for v in variants for call in resolved_calls.get(id(v), ())))
        if any(id(v) in tested for v in variants):
            tested.add(id(merged))
        canonical_orphans.append(merged)
    orphan_ids = {id(s) for variants in orphan_groups.values() for s in variants}
    render_prod = ([s for s in render_prod if id(s) not in orphan_ids]
                   + canonical_orphans)
    entrypoint_owners = {
        owner_key for s in canonical_members if _essential_method(s)
        if (owner_key := _member_owner_key(s)) is not None
    }
    for owner in owner_types:
        identity = (owner.file, owner.name, owner.lang)
        if identity not in entrypoint_owners:
            continue
        directory_types = types_by_dir.setdefault(
            str(Path(owner.file).parent), [])
        if owner not in directory_types:
            directory_types.append(owner)
    # Owner keys carry the file as well as lang: two `Client` classes in sibling
    # files are distinct owners even when their directory/language match.
    # L6 cold types: real fan-in from the raw call edges — a type is cold
    # only when neither it nor any of its methods is referenced from outside
    # the type. File-qualified identities keep same-named twins independent.
    cold_types: set[tuple[str, str, str]] = set()
    if (detail >= 5 or budget_selection is not None
            or budget_catalog is not None or budget_retained is not None):
        all_types = {(s.file, s.name, s.lang) for s in prod
                     if s.kind in TYPE_KINDS and s.container is None}
        warm: set[tuple[str, str, str]] = set()
        by_id = {id(s): s for s in symbols}
        for caller_id, targets_ in raw_targets.items():
            caller = by_id.get(caller_id)
            for t in targets_:
                target_owner = (_member_owner_key(t) if t.container else
                                (t.file, t.name, t.lang)
                                if t.kind in TYPE_KINDS else None)
                if target_owner is None:
                    continue
                caller_owner = (_member_owner_key(caller)
                                if caller is not None and caller.container
                                else None)
                if caller_owner != target_owner:
                    warm.add(target_owner)
        cold_types = all_types - warm

    def _method_payload_chars(method: Symbol, cold: bool) -> int:
        base = _bundle_estimated_chars(
            "cold-methods" if cold else "public-methods", method)
        # At the L5 boundary tested chains still survive; L7 has dropped every
        # chain already. Use renderer-resolved display names, never raw calls.
        calls = (resolved_calls.get(id(method), ())
                 if cold and id(method) in tested else ())
        return base + sum(len(call) + 1 for call in calls)

    def _method_budget_bundle(method: Symbol) -> BudgetBundle:
        cold = _member_owner_key(method) in cold_types
        return _make_bundle(
            "cold-methods" if cold else "public-methods",
            5 if cold else 7, method,
            payload_chars=_method_payload_chars(method, cold))

    methods_by_owner: dict[tuple[str, str, str], list[Symbol]] = {}
    for s in canonical_members:
        if (s.container and s.kind in ("method", "ctor")
                and (s.visibility == "pub" or _essential_method(s))):
            if not _essential_method(s):
                cold = _member_owner_key(s) in cold_types
                if not _keep_bundle(
                        "cold-methods" if cold else "public-methods",
                        5 if cold else 7, s,
                        default=detail < (5 if cold else 7),
                        payload_chars=_method_payload_chars(s, cold),
                        record=False):
                    continue
            owner_key = _member_owner_key(s)
            if owner_key is not None:
                methods_by_owner.setdefault(owner_key, []).append(s)
    orphan_methods: dict[tuple[str, str, str], list[Symbol]] = {}
    for s in render_prod:
        if (s.container and s.kind in ("method", "ctor")
                and _member_owner_key(s) is None
                and (s.visibility == "pub" or _essential_method(s))):
            if (not _essential_method(s)
                    and not _keep_bundle("public-methods", 7, s,
                                         default=detail < 7,
                                         payload_chars=_bundle_estimated_chars(
                                             "public-methods", s))):
                continue
            orphan_methods.setdefault(
                (s.file, s.container, s.lang), []).append(s)
    orphan_owner_files: dict[tuple[str, str, str], set[str]] = {}
    for s in render_prod:
        if (s.container and s.kind in ("method", "ctor")
                and _member_owner_key(s) is None):
            orphan_owner_files.setdefault(
                (str(Path(s.file).parent), s.lang, s.container),
                set()).add(s.file)

    def _orphan_owner_display(file: str, owner: str, lang: str) -> str:
        peers = orphan_owner_files.get(
            (str(Path(file).parent), lang, owner), ())
        return f"{Path(file).name}:{owner}" if len(peers) > 1 else owner
    # Lossless names-only private inventory. Gated at build time so the
    # per-class inventories consumed inside the types loop drop too — the
    # old post-loop wipe left `- name` lines under public headers at every
    # budget level.
    priv_methods_by_owner: dict[tuple[str, str, str], list[str]] = {}
    priv_top_by_file: dict[tuple[str, str], list[str]] = {}
    for s in render_prod:
        if s.visibility != "priv" or _essential_method(s):
            continue
        member_inventory = s.container and s.kind in ("method", "ctor")
        top_inventory = s.container is None and s.kind in TYPE_KINDS + ("fn",)
        if not (member_inventory or top_inventory):
            continue
        if (member_inventory and s.name in ("__init__", "__repr__")):
            continue  # never-rendered redundancies are not budget bundles
        if (top_inventory
                and (s.file, s.name, s.lang) in entrypoint_owners):
            continue
        if not _keep_bundle("private-names", 3, s, default=detail < 3):
            continue
        marked = s.kind in ("fn", "method", "class") and s.name in zero_usage
        name = f"{s.name}×0" if marked else s.name
        if s.container and s.kind in ("method", "ctor"):
            owner_key = (_member_owner_key(s)
                         or (s.file, s.container, s.lang))
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
        for s in render_prod if s.kind in ("fn", "method", "ctor")
    )
    top_locations: dict[str, set[tuple[str, str]]] = {}
    for symbol in prod:
        if (symbol.container is None
                and (symbol.visibility == "pub" or _essential_method(symbol)
                     or (symbol.file, symbol.name, symbol.lang)
                     in entrypoint_owners)
                and symbol.kind in TYPE_KINDS + ("fn",)):
            top_locations.setdefault(symbol.name, set()).add((symbol.file, symbol.lang))

    def _top_display(sym: Symbol) -> str:
        locations = top_locations.get(sym.name, ())
        local_locations = [file for file, _ in locations
                           if Path(file).parent == Path(sym.file).parent]
        if len(local_locations) > 1:
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
        if (sym.kind in ("fn", "method") and sym.name in zero_usage
                and not _essential_method(sym)):
            sig = f"{sig} ×0"
        kept = resolved_calls.get(id(sym), [])
        if kept and not (_is_test_path(sym.file) and sym.lang != "make"):
            tested_chain = id(sym) in tested
            if not _keep_bundle(
                    "tested-call-chains" if tested_chain
                    else "untested-call-chains",
                    6 if tested_chain else 4, sym,
                    default=detail < (6 if tested_chain else 4),
                    payload_chars=sum(len(call) + 1 for call in kept)):
                kept = []
        if sym.raises:
            sig = f"{sig} !{','.join(_strip_exc(r) for r in sym.raises)}"
        if grouped:
            kept = [_norm(c, own) for c in kept]
            sig = _norm(sig, own)
        return f"{sig} > {','.join(kept)}" if kept else sig

    def _redundant_ctor(ms: Symbol, kind: str, components: tuple) -> bool:
        # Record components define their canonical public construction shape.
        # Ordinary class fields do not: an equal-looking constructor still
        # carries the otherwise absent fact that callers can construct it.
        # Comparing the full line also keeps record ctors with any extra fact.
        return (kind == "record" and bool(components)
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
            unused = (t.kind == "class" and t.name in zero_usage
                      and (t.file, t.name, t.lang) not in entrypoint_owners)
            shown_supers = tuple(sup for sup in t.supers
                                 if (t.lang, sup) not in iface_index)
            impls = (tuple(n for n in impls_of.get((t.lang, t.name), ())
                           if n not in t.permits)
                     if t.kind == "interface" else ())
            # Same-named top-level types must not be grouped across files: their
            # method owners and fan-in are independent even when headers match.
            duplicate_identity = (t.file
                                  if len(top_locations.get(t.name, ())) > 1
                                  else "")
            selected_type_calls = tuple(resolved_calls.get(id(t), ()))
            if selected_type_calls:
                tested_chain = id(t) in tested
                if not _keep_bundle(
                        "tested-call-chains" if tested_chain
                        else "untested-call-chains",
                        6 if tested_chain else 4, t,
                        default=detail < (6 if tested_chain else 4),
                        payload_chars=sum(len(call) + 1
                                          for call in selected_type_calls)):
                    selected_type_calls = ()
            group_key = (duplicate_identity, t.lang, t.kind, t.visibility,
                         tuple(components),
                         shown_supers, tuple(t.permits), impls, unused,
                         bool(t.fields),
                         tuple(_decorator_notes(t.decorators, t.lang)),
                         selected_type_calls)
            groups.setdefault(group_key, []).append(t)
        for (_, _, kind, vis, components, supers, permits, impls, unused,
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
                id(m): [ms for ms in methods_by_owner.get(
                    (m.file, m.name, m.lang), [])
                        if not _redundant_ctor(ms, kind, components)]
                for m in members}
            for methods in member_methods.values():
                for method in methods:
                    if not _essential_method(method):
                        _record_bundle(_method_budget_bundle(method), True)
            head_member = members[0]
            def _priv_lines(m: Symbol, prefix: str = "") -> list[str]:
                names_only = priv_methods_by_owner.get((m.file, m.name, m.lang))
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

    # Module-shaped languages such as Lua expose public `M.fn` methods without
    # emitting a synthetic M type. Keep that API as a file-scoped owner line.
    for (file, owner, _), members in sorted(orphan_methods.items()):
        lines = list(dict.fromkeys(
            _sig_line(member, owner, False) for member in members))
        payload_by_dir.setdefault(str(Path(file).parent), []).append(
            f"{_orphan_owner_display(file, owner, members[0].lang)}: "
            + "; ".join(lines))

    consts_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "const" and s.visibility == "pub":
            const_key = (str(Path(s.file).parent), Path(s.file).name)
            vals = consts_by_file.setdefault(const_key, [])
            entry = s.signature or s.name
            name_only = entry.split("=", 1)[0]
            value_bundle: BudgetBundle | None = None
            value_kept = entry != name_only
            if value_kept:
                value_bundle = _make_bundle("const-values", 7, s)
                value_kept = _keep_bundle(
                    "const-values", 7, s, default=detail < 7, record=False)
                if not value_kept:
                    entry = name_only
            if entry not in vals:
                vals.append(entry)
                if value_bundle is not None:
                    _record_bundle(value_bundle, value_kept)
    for (d, fname), vals in sorted(consts_by_file.items()):
        payload_by_dir.setdefault(d, []).extend(
            _private_lines(f"= {fname}: ", vals))

    for (d, stem), names_only in sorted(priv_top_by_file.items()):
        payload_by_dir.setdefault(d, []).extend(
            _private_lines(f"- {stem}: ", names_only))
    public_owners = {(member.file, member.name, member.lang)
                     for types in types_by_dir.values() for member in types
                     if member.kind in TYPE_KINDS}
    for (file, owner, lang), names_only in sorted(priv_methods_by_owner.items()):
        if (file, owner, lang) not in public_owners:
            payload_by_dir.setdefault(str(Path(file).parent), []).extend(
                _private_lines(
                    f"- {_orphan_owner_display(file, owner, lang)}: ",
                    names_only))
    reex_by_file: dict[tuple[str, str], list[str]] = {}
    for s in prod:
        if s.kind == "reexport":
            reexport_key = (str(Path(s.file).parent), Path(s.file).name)
            names_r = reex_by_file.setdefault(reexport_key, [])
            if s.name not in names_r:
                names_r.append(s.name)
    for (d, fname), names_r in sorted(reex_by_file.items()):
        payload_by_dir.setdefault(d, []).append(f"» {fname}: {','.join(names_r)}")

    if loc is None:
        loc = _total_loc(files)
    state_part = f" · state {state}" if state else ""
    if langs:
        state_part += f" · langs {','.join(sorted(langs))}"
    if targets:
        state_part += f" · targets {','.join(sorted(targets))}"
    if budget:
        state_part += f" · budget {budget}"
        if budget_selection is not None:
            state_part += f" A{budget_selection.level}"
        elif detail:
            state_part += f" L{detail}"
    body = _tree_lines(payload_by_dir)
    helper_ids = (helpers if helpers is not None
                  else _helper_class_ids(symbols, file_tokens))
    by_id = {id(symbol): symbol for symbol in symbols}
    selected_helpers: dict[int, bool] = {}
    for helper_id, has_signatures in helper_ids.items():
        helper = by_id.get(helper_id)
        helper_methods = ([] if helper is None else [
            method for method in symbols
            if (method.file == helper.file
                and method.container == helper.name
                and method.kind in ("method", "ctor")
                and method.visibility == "pub")])
        has_public_methods = bool(helper_methods)
        helper_payload = sum(
            _bundle_estimated_chars("helper-signatures", method)
            for method in helper_methods)
        selected_helpers[helper_id] = bool(
            has_signatures and has_public_methods and helper is not None
            and _keep_bundle("helper-signatures", 2, helper,
                             default=detail < 2,
                             payload_chars=helper_payload))

    test_edge_groups: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in symbols:
        calls = resolved_calls.get(id(symbol), [])
        if (symbol.lang == "make" or not _is_test_path(symbol.file)
                or symbol.kind not in ("fn", "method", "ctor") or not calls):
            continue
        test_edge_groups.setdefault(
            (symbol.file, symbol.container or ""), []).append(symbol)
    selected_test_edges: dict[int, list[str]] = {}
    for (file, owner), callers in sorted(test_edge_groups.items()):
        merged_calls = list(dict.fromkeys(
            call for caller in callers
            for call in resolved_calls.get(id(caller), ())))
        display_owner = owner or Path(file).stem
        if not _edge_suffix(display_owner, merged_calls,
                            braced=bool(owner)):
            continue  # suppressed guessable edge is not a rendered fact
        representative = callers[0]
        edge_text = _edge_suffix(display_owner, merged_calls,
                                 braced=bool(owner))
        if _keep_bundle("test-coverage", 1, representative,
                        default=detail < 1,
                        suffix=f"|coverage:{owner}",
                        payload_chars=len(edge_text)):
            selected_test_edges[id(representative)] = merged_calls
    tests = _test_index_lines(files, symbols, root,
                              selected_test_edges,
                              selected_helpers,
                              _sig_line)
    if tests:
        body.extend(tests)
    has_priv = bool(priv_top_by_file or priv_methods_by_owner)
    has_helpers = bool(tests) and any("\n  *" in "\n" + ln or ln.lstrip().startswith("*")
                                      for ln in tests)
    legend = _legend_line("\n".join(body), has_priv, bool(tests),
                          has_helpers)
    header = f"# hologram · {loc:,} LOC{state_part}\n{legend}\n"
    if detail:
        # silent omission misleads: a reader answering off a degraded map
        # must know which fact classes need a file read to confirm
        dropped: list[str] = []
        for level, label in _LEVEL_DROPS:
            if detail < level:
                continue
            if (budget_selection is not None
                    and level == budget_selection.level):
                label += " (partial)"
            dropped.append(label)
        header += ("‥ budget dropped: " + ", ".join(dropped)
                   + " — the map no longer carries these facts; NEVER guess "
                   "them, read the source file first\n")
    return header + "\n".join(body) + "\n"


def _build_digest(root: Path, langs: set[str] | None,
                  targets: list[str] | None, budget: int | None, *,
                  collect_stats: bool) -> tuple[str, BudgetStats | None]:
    """Build a map and return deterministic evidence for its budget policy.

    Selection first finds the least-degraded complete ladder level that fits,
    then restores whole facts from exactly the next quality boundary. Every
    trial is rendered and measured as a complete map, including its header,
    legend, and loss disclosure; a retained fact is never byte-truncated.
    """
    if budget is not None and budget < 0:
        raise ValueError("budget must be non-negative")
    files, symbols, file_tokens, usage_tokens, state = _gather(root, langs)
    zero = _zero_usage_names(symbols, usage_tokens)
    # level-invariant work computed once — the ladder re-renders up to
    # _MAX_LEVEL times and _total_loc reads every file from disk
    loc = _total_loc(files)
    resolved = _resolved_project_calls(symbols)
    helpers = _helper_class_ids(symbols, file_tokens)

    def render(level: int, *, selection: _BudgetSelection | None = None,
               catalog: set[BudgetBundle] | None = None,
               retained: set[BudgetBundle] | None = None) -> str:
        return render_simple(root, symbols, files, state=state,
                             zero_usage=zero, langs=langs, targets=targets,
                             file_tokens=file_tokens, detail=level,
                             budget=budget, loc=loc, resolved=resolved,
                             helpers=helpers, budget_selection=selection,
                             budget_catalog=catalog,
                             budget_retained=retained)

    catalog: set[BudgetBundle] = set()
    full_retained: set[BudgetBundle] = set()
    track_bundles = collect_stats or bool(budget)
    full = render(0,
                  catalog=catalog if track_bundles else None,
                  retained=full_retained if track_bundles else None)
    full_tokens = estimate_tokens(full)
    if not collect_stats and (not budget or full_tokens <= budget):
        return full, None
    skeleton_retained: set[BudgetBundle] = set()
    skeleton = render(_MAX_LEVEL, retained=skeleton_retained)
    skeleton_tokens = estimate_tokens(skeleton)

    def stats(digest: str, retained: set[BudgetBundle],
              effective_detail: str, *, selection_trials: int = 0,
              selection_candidates: int = 0,
              search_truncated: bool = False,
              stop_reason: str = "not-limited") -> BudgetStats | None:
        if not collect_stats:
            return None
        return summarize_budget(
            requested_budget=budget,
            full_tokens=full_tokens,
            selected_tokens=estimate_tokens(digest),
            skeleton_tokens=skeleton_tokens,
            effective_detail=effective_detail,
            bundles=catalog,
            retained=retained,
            selection_trials=selection_trials,
            selection_candidates=selection_candidates,
            search_truncated=search_truncated,
            stop_reason=stop_reason,
        )

    if not budget or full_tokens <= budget:
        return full, stats(
            full, full_retained, "full",
            stop_reason="full-fits" if budget else "unlimited")
    # Compare complete candidates, including their budget stamp, legend, and
    # loss disclosure. Those lines can make a deeper level larger than its
    # predecessor on small maps, so ladder order is not a size ordering.
    candidates = [full]
    level_retained = [full_retained]
    for level in range(1, _MAX_LEVEL):
        retained: set[BudgetBundle] = set()
        candidates.append(render(level, retained=retained))
        level_retained.append(retained)
    candidates.append(skeleton)
    level_retained.append(skeleton_retained)
    sizes = [estimate_tokens(candidate) for candidate in candidates]
    fitting = [level for level, size in enumerate(sizes) if size <= budget]
    if fitting:
        level = fitting[0]  # least-degraded candidate that satisfies the budget
    else:
        level = min(range(len(candidates)), key=lambda i: (sizes[i], i))
    digest = candidates[level]
    if sizes[level] > budget:
        import sys
        print(f"hologram: warning: no complete map fits the budget; "
              f"the smallest complete candidate is L{level} at "
              f"~{sizes[level]:,} tokens against a budget of {budget:,}; "
              f"emitting it whole — narrowing with --lang may help",
              file=sys.stderr)
        return digest, stats(digest, level_retained[level],
                             f"minimum-L{level}", stop_reason="minimum")

    # Facts gained by exactly one step toward the full map. Filtering through
    # emitted sets automatically respects dependencies: a call chain whose
    # method is still absent cannot consume this boundary's budget.
    boundary = level_retained[level - 1] - level_retained[level]
    by_category: dict[str, list[BudgetBundle]] = {}
    for bundle in boundary:
        by_category.setdefault(bundle.category, []).append(bundle)
    categories = sorted(
        by_category,
        key=lambda category: (_BUNDLE_CATEGORY_ORDER.get(category, 99),
                              category))
    for bundles in by_category.values():
        bundles.sort(key=lambda bundle: (bundle.estimated_chars, bundle.key))
    # Interleave cost-ranked categories: the cheapest high-priority fact is
    # tried first, but one category cannot consume the global trial cap and
    # starve every lower category whose small facts would use the budget.
    transition = [
        by_category[category][index]
        for index in range(max((len(items) for items in by_category.values()),
                               default=0))
        for category in categories
        if index < len(by_category[category])
    ]
    selected: set[str] = set()
    trials = 0
    search_truncated = False

    def saturated() -> bool:
        return estimate_tokens(digest) / budget >= _ADAPTIVE_SATURATION

    def try_selection(names: frozenset[str]) -> str | None:
        nonlocal trials
        if trials >= _ADAPTIVE_MAX_TRIALS:
            return None
        trials += 1
        trial = render(level, selection=_BudgetSelection(
            names, level, len(transition)))
        return trial if estimate_tokens(trial) <= budget else None

    if transition:
        # Ranked greedy packing preserves the ladder's semantic priority.
        # Each exact trial is a whole rendered map, so hard fit does not rely
        # on additive token estimates (factoring and legends are non-additive).
        # Facts are admitted in growing runs and the run halves on a miss, so
        # the number of facts a trial can buy is not capped at one: a fixed
        # trial cap still bounds hook latency, but it no longer bounds how much
        # of the budget can be filled. Stop once the complete output is within
        # one percent of the target.
        greedy_limit = max(1, _ADAPTIVE_MAX_TRIALS - _ADAPTIVE_REPAIR_TRIALS)
        index = 0
        run = 1
        while index < len(transition):
            if saturated():
                break
            if trials >= greedy_limit:
                search_truncated = True
                break
            chunk = transition[index:index + run]
            trial = try_selection(
                frozenset((*selected, *(bundle.name for bundle in chunk))))
            if trial is not None:
                selected.update(bundle.name for bundle in chunk)
                digest = trial
                index += len(chunk)
                run = min(run * 2, len(transition))
            elif run > 1:
                run //= 2  # the run overflows the budget; admit a smaller one
            else:
                index += 1  # this single fact does not fit; keep its successors

        # One repair pass catches prefix/suffix factoring unlocked by later
        # selections, from a reserved slice of the trial budget so a saturated
        # greedy pass can never leave it unreachable.
        for bundle in transition:
            if saturated():
                break
            if trials >= _ADAPTIVE_MAX_TRIALS:
                search_truncated = True
                break
            if bundle.name in selected:
                continue
            trial_names = frozenset((*selected, bundle.name))
            trial = try_selection(trial_names)
            if trial is not None:
                selected.add(bundle.name)
                digest = trial

    final_retained: set[BudgetBundle] = set()
    if selected:
        selection = _BudgetSelection(
            frozenset(selected), level, len(transition))
        digest = render(level, selection=selection, retained=final_retained)
        effective = f"L{level}-adaptive:{len(selected)}/{len(transition)}"
    else:
        final_retained = level_retained[level]
        effective = f"L{level}"
    did_saturate = saturated()
    stop_reason = ("saturated" if did_saturate else
                   "trial-limit" if search_truncated else
                   "no-boundary" if not transition else "exhausted")
    return digest, stats(
        digest, final_retained, effective,
        selection_trials=trials,
        selection_candidates=len(transition),
        search_truncated=search_truncated,
        stop_reason=stop_reason)


def build_digest_with_stats(root: Path, langs: set[str] | None = None,
                            targets: list[str] | None = None,
                            budget: int | None = None
                            ) -> tuple[str, BudgetStats]:
    """Build a map plus the complete, JSON-ready budget decision."""
    digest, stats = _build_digest(
        root, langs, targets, budget, collect_stats=True)
    assert stats is not None
    return digest, stats


def build_digest(root: Path, langs: set[str] | None = None,
                 targets: list[str] | None = None,
                 budget: int | None = None) -> str:
    # Normal hooks need the selected map, not sorted bundle diagnostics. Rich
    # statistics remain available through build_digest_with_stats / `stats`.
    return _build_digest(
        root, langs, targets, budget, collect_stats=False)[0]
