"""`hologram review` — the map talking back.

A deterministic drift engine over two snapshots of the same repository:
what did this change add that already existed, re-cover, orphan, or
misplace? Pure map-diff, no LLM; the post-commit hook surfaces the report
inside the committing agent's own context. Informational only — never a
gate, always exit 0.
"""
from __future__ import annotations

import difflib
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .gather import _gather, _zero_usage_names
from .render import (TYPE_KINDS, _decorator_notes, _is_test_path,
                     _raw_call_targets, build_digest)
from .symbols import Symbol

# Tuning knobs for the duplicate detector; conservative on purpose — a
# false "duplicate" erodes trust faster than a miss.
_DUP_RATIO = 0.7
_DUP_MIN_NAME = 5
_DUP_STOPLIST = {
    "main", "toString", "equals", "hashCode", "close", "run", "setUp",
    "tearDown", "__init__", "__str__", "__repr__", "get", "set", "build",
}
_DUP_REPORT_CAP = 10
_PLACE_MIN_MASS = 3


@dataclass(frozen=True)
class Snapshot:
    symbols: list[Symbol]
    file_tokens: dict[str, set[str]]
    usage_tokens: Counter


@dataclass(frozen=True)
class Finding:
    check: str    # dup | recover | dead | orphan | api | place
    subject: str
    detail: str


def snapshot(root: Path, langs: set[str] | None = None) -> Snapshot:
    _files, symbols, file_tokens, usage_tokens, _state = _gather(root, langs)
    return Snapshot(symbols=symbols, file_tokens=file_tokens,
                    usage_tokens=usage_tokens)


def _key(s: Symbol) -> tuple[str, str, str, str]:
    # line numbers excluded on purpose: a pure move is not drift
    return (s.lang, s.file, s.container or "", s.name)


def _prod_callables(snap: Snapshot) -> list[Symbol]:
    return [s for s in snap.symbols
            if s.kind in ("fn", "method") and s.visibility == "pub"
            and not _is_test_path(s.file)]


def _prod_api(snap: Snapshot) -> dict[tuple[str, str, str, str],
                                      tuple[tuple[str, ...], str | None]]:
    return {_key(s): (tuple(s.params), s.returns)
            for s in snap.symbols
            if s.kind in TYPE_KINDS + ("fn", "method")
            and s.visibility == "pub" and not _is_test_path(s.file)}


def _test_edges(snap: Snapshot,
                targets: dict[int, list[Symbol]]
                ) -> dict[tuple[str, str], list[Symbol]]:
    edges: dict[tuple[str, str], list[Symbol]] = {}
    for s in snap.symbols:
        if (_is_test_path(s.file) and s.kind in ("fn", "method", "ctor")):
            merged = edges.setdefault((s.file, s.container or ""), [])
            for t in targets.get(id(s), []):
                if all(_key(t) != _key(m) for m in merged):
                    merged.append(t)
    return edges


def _sig_lines(digest: str) -> list[str]:
    out = []
    for ln in digest.splitlines():
        s = ln.strip()
        if s and not s.startswith(("#", "·", "-", "»", "?", "*", "=")) and "(" in s:
            out.append(s)
    return out


def _map_line_for(digest: str, name: str) -> str | None:
    for ln in _sig_lines(digest):
        if ln.split("(", 1)[0].strip().split(",")[-1] == name:
            return ln
    return None


def _describe(s: Symbol) -> str:
    qual = f"{s.container}.{s.name}" if s.container else s.name
    return f"{qual}({','.join(s.params)}) in {s.file}"


def review_snapshots(old: Snapshot, new: Snapshot, old_digest: str = "",
                     checks: frozenset[str] | None = None) -> list[Finding]:
    on = checks or frozenset({"dup", "recover", "dead", "orphan", "api", "place"})
    findings: list[Finding] = []

    old_api = _prod_api(old)
    new_api = _prod_api(new)
    old_pairs = {(k[0], k[2], k[3]) for k in old_api}      # (lang, container, name)
    old_names = {(s.lang, s.name) for s in old.symbols}
    new_targets = _raw_call_targets(new.symbols)
    old_targets = _raw_call_targets(old.symbols)

    added_callables = sorted(
        (s for s in _prod_callables(new)
         if (s.lang, s.container or "", s.name) not in old_pairs),
        key=_key)

    if "dup" in on:
        dups: list[tuple[float, Symbol, Symbol]] = []
        old_callables = _prod_callables(old)
        for a in added_callables:
            if len(a.name) < _DUP_MIN_NAME or a.name in _DUP_STOPLIST:
                continue
            called = {(t.lang, t.container or "", t.name)
                      for t in new_targets.get(id(a), [])}
            best: tuple[float, Symbol] | None = None
            for c in sorted(old_callables, key=_key):
                if c.lang != a.lang or c.file == a.file:
                    continue
                if len(c.name) < _DUP_MIN_NAME or c.name in _DUP_STOPLIST:
                    continue
                if (c.lang, c.container or "", c.name) in called:
                    best = None
                    break  # delegates to the similar symbol: not a duplicate
                ratio = difflib.SequenceMatcher(
                    None, a.name.lower(), c.name.lower()).ratio()
                if ratio >= _DUP_RATIO and (best is None or ratio > best[0]):
                    best = (ratio, c)
            if best is not None:
                dups.append((best[0], a, best[1]))
        dups.sort(key=lambda d: (-d[0], _key(d[1])))
        for ratio, a, c in dups[:_DUP_REPORT_CAP]:
            pointer = _map_line_for(old_digest, c.name) or _describe(c)
            findings.append(Finding(
                "dup", a.name,
                f"dup: {_describe(a)} is {ratio:.0%} name-similar to existing "
                f"{pointer} and does not call it — probable duplicate"))

    old_edges = _test_edges(old, old_targets)
    new_edges = _test_edges(new, new_targets)

    if "recover" in on:
        covered_by: dict[tuple[str, str, str, str], list[str]] = {}
        for (_f, cls), ts in sorted(old_edges.items()):
            for t in ts:
                if cls:
                    covered_by.setdefault(_key(t), []).append(cls)
        reported: set[tuple[str, str, str, str]] = set()
        for (f, cls), ts in sorted(new_edges.items()):
            olds = {_key(t) for t in old_edges.get((f, cls), [])}
            for t in ts:
                k = _key(t)
                if k in olds or k in reported:
                    continue
                others = [c for c in covered_by.get(k, []) if c != cls]
                if others:
                    reported.add(k)
                    findings.append(Finding(
                        "recover", cls or f,
                        f"recover: {cls or f} now exercises {t.name}, already "
                        f"covered by {', '.join(dict.fromkeys(others[:2]))}"))

    if "dead" in on:
        zero = _zero_usage_names(new.symbols, new.usage_tokens)
        for s in sorted((s for s in new.symbols
                         if s.kind in ("fn", "method", "class")
                         and s.visibility == "pub"
                         and not _is_test_path(s.file)
                         and (s.lang, s.name) not in old_names
                         and s.name in zero), key=_key):
            findings.append(Finding(
                "dead", s.name,
                f"dead: new public {s.kind} {_describe(s)} has no observed "
                f"project reference (×0 on arrival)"))

    if "orphan" in on:
        new_prod_names = {(s.lang, s.name) for s in new.symbols
                          if not _is_test_path(s.file)}
        new_test_classes = {(s.file, s.name) for s in new.symbols
                            if s.kind == "class" and _is_test_path(s.file)}
        for (f, cls), ts in sorted(old_edges.items()):
            if cls and (f, cls) not in new_test_classes:
                continue
            for t in ts:
                if ((t.lang, t.name) not in new_prod_names
                        and t.name in new.file_tokens.get(f, set())):
                    findings.append(Finding(
                        "orphan", cls or f,
                        f"orphan: {cls or f} still references {t.name}, which "
                        f"no longer exists in production"))

    if "api" in on:
        added = sorted(k[3] for k in new_api.keys() - old_api.keys())
        removed = sorted(k[3] for k in old_api.keys() - new_api.keys())
        changed = sorted(
            f"{k[3]}: ({','.join(old_api[k][0])})→({','.join(new_api[k][0])})"
            for k in old_api.keys() & new_api.keys()
            if old_api[k] != new_api[k])

        def cap(names: list[str]) -> str:
            shown = ", ".join(names[:8])
            more = len(names) - min(len(names), 8)
            return shown + (f" +{more} more" if more else "")

        if added or removed or changed:
            parts = []
            if added:
                parts.append(f"+{len(added)} ({cap(added)})")
            if removed:
                parts.append(f"−{len(removed)} ({cap(removed)})")
            if changed:
                parts.append(f"~{len(changed)} ({cap(changed)})")
            findings.append(Finding("api", "surface",
                                    "api: " + " ".join(parts)))

    if "place" in on:
        added_top = sorted(
            (s for s in new.symbols
             if s.container is None and s.visibility == "pub"
             and s.kind in TYPE_KINDS + ("fn",)
             and not _is_test_path(s.file)
             and (s.lang, s.name) not in old_names), key=_key)
        for s in added_top:
            mass: Counter = Counter()
            for t in new_targets.get(id(s), []):
                mass[str(Path(t.file).parent)] += 1
            family_sig = (s.lang, s.kind, tuple(s.supers),
                          tuple(_decorator_notes(s.decorators, s.lang)))
            if s.supers or family_sig[3]:
                for f in new.symbols:
                    if (f is not s and f.container is None
                            and not _is_test_path(f.file)
                            and (f.lang, f.kind, tuple(f.supers),
                                 tuple(_decorator_notes(f.decorators, f.lang)))
                            == family_sig):
                        mass[str(Path(f.file).parent)] += 1
            total = sum(mass.values())
            own = str(Path(s.file).parent)
            if total < _PLACE_MIN_MASS:
                continue
            top = sorted(mass.items(), key=lambda kv: (-kv[1], kv[0]))
            if (len(top) >= 1 and top[0][0] != own
                    and top[0][1] * 3 >= total * 2
                    and (len(top) == 1 or top[0][1] > top[1][1])):
                findings.append(Finding(
                    "place", s.name,
                    f"place: {s.name} lives in {own}/ but its calls and "
                    f"nearest type family sit in {top[0][0]}/ — consider "
                    f"placing it there"))
    return findings


def render_report(findings: list[Finding], rev: str) -> str:
    if not findings:
        return ""
    lines = [f"hologram review vs {rev}: {len(findings)} finding(s)"]
    lines += [f"- {f.detail}" for f in findings]
    return "\n".join(lines) + "\n"


def run_review(root: Path, rev: str, langs: set[str] | None = None,
               checks: frozenset[str] | None = None) -> str:
    """Review the working tree against `rev` via a detached worktree."""
    new = snapshot(root, langs)
    with tempfile.TemporaryDirectory(prefix="hologram-review-") as tmp:
        wt = Path(tmp) / "wt"
        r = subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", "-f",
             str(wt), rev],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"git worktree failed: {r.stderr.strip()}")
        try:
            old = snapshot(wt, langs)
            old_digest = build_digest(wt, langs=langs)
        finally:
            subprocess.run(["git", "-C", str(root), "worktree", "remove",
                            "--force", str(wt)], capture_output=True)
    return render_report(review_snapshots(old, new, old_digest, checks), rev)
