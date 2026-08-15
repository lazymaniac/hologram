"""`hologram review` — the map talking back.

A deterministic drift engine over two snapshots of the same repository:
what did this change add that already existed, re-cover, orphan, or
misplace? Pure map-diff, no LLM; the post-commit hook surfaces the report
inside the committing agent's own context. Informational only — never a
gate when findings are produced; invalid inputs and setup failures still fail.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import NamedTuple

from .gather import _gather, _git_env, _zero_usage_names
from .render import (TYPE_KINDS, _decorator_notes, _is_production_symbol,
                     _is_test_path, _strip_exc,
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
_REPORT_SCHEMA_VERSION = 1
_FINDING_ID_VERSION = 1


@dataclass(frozen=True)
class Snapshot:
    symbols: list[Symbol]
    file_tokens: dict[str, set[str]]
    usage_tokens: Counter


@dataclass(frozen=True)
class Finding:
    """A machine-readable review finding with a stable semantic identity.

    The first three fields retain the original constructor shape.  ``kind``
    and ``path`` add structured context without forcing older callers to
    change.  Human wording is deliberately excluded from ``id`` so a copy
    edit cannot make an unresolved finding look resolved.
    """

    check: str    # dup | recover | dead | orphan | api | place
    subject: str
    detail: str
    kind: str = ""
    path: str | None = None
    _discriminator: InitVar[str] = field(default="", repr=False,
                                         kw_only=True)
    id: str = field(init=False)

    def __post_init__(self, _discriminator: str) -> None:
        kind = self.kind or self.check
        path = (str(self.path).replace("\\", "/")
                if self.path is not None else None)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "path", path)
        identity = {
            "version": _FINDING_ID_VERSION,
            "check": self.check,
            "kind": kind,
            "subject": self.subject,
            "path": path,
            "discriminator": _discriminator,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
        object.__setattr__(self, "id", f"hr{_FINDING_ID_VERSION}-{digest}")

    def to_dict(self) -> dict[str, str | None]:
        """Return the stable public, JSON-serializable finding schema."""
        return {
            "id": self.id,
            "check": self.check,
            "kind": self.kind,
            "subject": self.subject,
            "path": self.path,
            "detail": self.detail,
        }


def snapshot(root: Path, langs: set[str] | None = None) -> Snapshot:
    _files, symbols, file_tokens, usage_tokens, _state = _gather(root, langs)
    return Snapshot(symbols=symbols, file_tokens=file_tokens,
                    usage_tokens=usage_tokens)


def _key(s: Symbol) -> tuple[str, str, str, str]:
    # line numbers excluded on purpose: a pure move is not drift
    return (s.lang, s.file, s.container or "", s.name)


def _symbol_identity(s: Symbol) -> tuple[object, ...]:
    """Logical identity that survives line, signature, and wording edits."""
    return (*_key(s), s.kind)


def _finding(check: str, subject: str, detail: str, *, kind: str,
             path: str | None, discriminator: object = ()) -> Finding:
    stable_discriminator = json.dumps(
        discriminator, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True)
    return Finding(check, subject, detail, kind=kind, path=path,
                   _discriminator=stable_discriminator)


def _prod_callables(snap: Snapshot) -> list[Symbol]:
    return [s for s in snap.symbols
            if s.kind in ("fn", "method") and s.visibility == "pub"
            and _is_production_symbol(s)]


class _ApiAtom(NamedTuple):
    """One public symbol's map-visible calling and declaration shape."""

    kind: str
    params: tuple[str, ...]
    param_names: tuple[str, ...]
    returns: str
    fields: tuple[str, ...]
    supers: tuple[str, ...]
    permits: tuple[str, ...]
    decorators: tuple[str, ...]
    raises: tuple[str, ...]
    value: str


_ApiSurface = tuple[_ApiAtom, ...]


def _api_atom(s: Symbol) -> _ApiAtom:
    # Keep only decorators represented by the map. An implementation-only
    # annotation must not create API drift merely because extraction stores it.
    notes = tuple(_decorator_notes(s.decorators, s.lang))
    raises = tuple(_strip_exc(name) for name in s.raises)
    returns = ("" if s.kind == "ctor"
               or s.returns in (None, "void", "Unit", "None")
               else s.returns or "")
    return _ApiAtom(
        s.kind, tuple(s.params), tuple(s.param_names), returns,
        tuple(s.fields), tuple(s.supers), tuple(s.permits), notes, raises,
        s.signature if s.kind == "const" else "")


def _prod_api(snap: Snapshot) -> dict[tuple[str, str, str, str],
                                      _ApiSurface]:
    # A logical name can have overloads. Preserve every distinct shape instead
    # of letting the last extracted declaration silently overwrite its peers.
    atoms: dict[tuple[str, str, str, str], set[_ApiAtom]] = {}
    for s in snap.symbols:
        if (s.kind in TYPE_KINDS + ("fn", "method", "ctor", "const")
                and s.visibility == "pub" and _is_production_symbol(s)):
            atoms.setdefault(_key(s), set()).add(_api_atom(s))
    return {key: tuple(sorted(values)) for key, values in atoms.items()}


def _api_signature(atom: _ApiAtom) -> str:
    return (f"({','.join(atom.params)})"
            + (f":{atom.returns}" if atom.returns else ""))


def _api_values(values: tuple[str, ...], *, sigil: str = "") -> str:
    return "[" + ",".join(f"{sigil}{value}" for value in values) + "]"


def _api_atom_label(atom: _ApiAtom) -> str:
    """Compact complete label used only for an overload-set replacement."""
    if atom.kind == "const":
        return atom.value or "const"
    if atom.kind in ("fn", "method", "ctor"):
        label = f"{atom.kind} {_api_signature(atom)}"
    else:
        components = atom.fields or atom.params
        label = atom.kind + ("{" + ",".join(components) + "}"
                             if components else "")
        if atom.supers:
            label += ":" + ",".join(atom.supers)
        if atom.permits:
            label += " sealed:" + "|".join(atom.permits)
    if atom.decorators:
        label += " " + " ".join(f"@{note}" for note in atom.decorators)
    if atom.raises:
        label += " !" + ",".join(atom.raises)
    return label


def _api_delta(old: _ApiSurface, new: _ApiSurface) -> str:
    """Describe one logical symbol's changed mapped shape concisely."""
    if len(old) != 1 or len(new) != 1:
        def variants(surface: _ApiSurface) -> str:
            shown = [_api_atom_label(atom) for atom in surface[:3]]
            extra = len(surface) - len(shown)
            return ("[" + " | ".join(shown)
                    + (f" | +{extra}" if extra else "") + "]")
        return f"variants {variants(old)}→{variants(new)}"

    before, after = old[0], new[0]
    parts: list[str] = []
    if before.kind != after.kind:
        parts.append(f"{before.kind}→{after.kind}")
    if (before.params, before.returns) != (after.params, after.returns):
        parts.append(f"{_api_signature(before)}→{_api_signature(after)}")
    if before.param_names != after.param_names:
        parts.append(
            f"args {_api_values(before.param_names)}→"
            f"{_api_values(after.param_names)}")
    for label, old_values, new_values, sigil in (
        ("fields", before.fields, after.fields, ""),
        ("supers", before.supers, after.supers, ""),
        ("permits", before.permits, after.permits, ""),
        ("decorators", before.decorators, after.decorators, "@"),
        ("raises", before.raises, after.raises, ""),
    ):
        if old_values != new_values:
            parts.append(
                f"{label} {_api_values(old_values, sigil=sigil)}→"
                f"{_api_values(new_values, sigil=sigil)}")
    if before.value != after.value:
        parts.append(f"value {before.value or '-'}→{after.value or '-'}")
    # Every atom field is covered above. Keep a complete fallback so future
    # fields cannot turn a detected change into an empty human summary.
    return " ".join(parts) or (
        f"{_api_atom_label(before)}→{_api_atom_label(after)}")


def _test_edges(snap: Snapshot,
                targets: dict[int, list[Symbol]]
                ) -> dict[tuple[str, str], list[Symbol]]:
    edges: dict[tuple[str, str], list[Symbol]] = {}
    for s in snap.symbols:
        if (_is_test_path(s.file) and s.lang != "make"
                and s.kind in ("fn", "method", "ctor")):
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
            findings.append(_finding(
                "dup", a.name,
                f"dup: {_describe(a)} is {ratio:.0%} name-similar to existing "
                f"{pointer} and does not call it — probable duplicate",
                kind=a.kind, path=a.file,
                discriminator=("added", _symbol_identity(a),
                               "existing", _symbol_identity(c))))

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
                    findings.append(_finding(
                        "recover", cls or f,
                        f"recover: {cls or f} now exercises {t.name}, already "
                        f"covered by {', '.join(dict.fromkeys(others[:2]))}",
                        kind="coverage", path=f,
                        discriminator=("target", _symbol_identity(t),
                                       "test-container", cls or "")))

    if "dead" in on:
        zero = _zero_usage_names(new.symbols, new.usage_tokens)
        # a name any test file mentions is exercised, even when the call
        # arrives through framework indirection that leaves no static edge
        test_mentions: set[str] = set()
        make_files = {s.file for s in new.symbols if s.lang == "make"}
        for f, tokens in new.file_tokens.items():
            if _is_test_path(f) and f not in make_files:
                test_mentions |= tokens
        for s in sorted((s for s in new.symbols
                         if s.kind in ("fn", "method", "class")
                         and s.visibility == "pub"
                         and _is_production_symbol(s)
                         and (s.lang, s.name) not in old_names
                         and s.name in zero
                         and s.name not in test_mentions), key=_key):
            findings.append(_finding(
                "dead", s.name,
                f"dead: new public {s.kind} {_describe(s)} has no observed "
                f"project reference (×0 on arrival)",
                kind=s.kind, path=s.file,
                discriminator=_symbol_identity(s)))

    if "orphan" in on:
        new_prod_names = {(s.lang, s.name) for s in new.symbols
                          if _is_production_symbol(s)}
        new_test_classes = {(s.file, s.name) for s in new.symbols
                            if (s.kind == "class" and _is_test_path(s.file)
                                and s.lang != "make")}
        for (f, cls), ts in sorted(old_edges.items()):
            if cls and (f, cls) not in new_test_classes:
                continue
            for t in ts:
                if ((t.lang, t.name) not in new_prod_names
                        and t.name in new.file_tokens.get(f, set())):
                    findings.append(_finding(
                        "orphan", cls or f,
                        f"orphan: {cls or f} still references {t.name}, which "
                        f"no longer exists in production",
                        kind="test-reference", path=f,
                        discriminator=("target", _symbol_identity(t),
                                       "test-container", cls or "")))

    if "api" in on:
        added_keys = sorted(new_api.keys() - old_api.keys())
        removed_keys = sorted(old_api.keys() - new_api.keys())
        changed_keys = sorted(
            k for k in old_api.keys() & new_api.keys()
            if old_api[k] != new_api[k])
        added = [k[3] for k in added_keys]
        removed = [k[3] for k in removed_keys]

        changed = sorted(
            f"{k[3]}: {_api_delta(old_api[k], new_api[k])}"
            for k in changed_keys)

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
            findings.append(_finding(
                "api", "surface", "api: " + " ".join(parts),
                kind="surface", path=None,
                # The human report aggregates API drift into one line, but its
                # machine identity must still distinguish different change
                # sets.  Otherwise replacing one unrelated API addition with
                # another would be misclassified as a persisting finding.
                discriminator={
                    "added": [(k, new_api[k]) for k in added_keys],
                    "removed": [(k, old_api[k]) for k in removed_keys],
                    "changed": [
                        (k, old_api[k], new_api[k]) for k in changed_keys
                    ],
                }))

    if "place" in on:
        added_top = sorted(
            (s for s in new.symbols
             if s.container is None and s.visibility == "pub"
             and s.kind in TYPE_KINDS + ("fn",)
             and _is_production_symbol(s)
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
                            and _is_production_symbol(f)
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
                findings.append(_finding(
                    "place", s.name,
                    f"place: {s.name} lives in {own}/ but its calls and "
                    f"nearest type family sit in {top[0][0]}/ — consider "
                    f"placing it there",
                    kind=s.kind, path=s.file,
                    discriminator=_symbol_identity(s)))
    return findings


def render_report(findings: list[Finding], rev: str) -> str:
    if not findings:
        return ""
    lines = [f"hologram review vs {rev}: {len(findings)} finding(s)"]
    lines += [f"- {f.detail}" for f in findings]
    return "\n".join(lines) + "\n"


def _finding_order(finding: Finding) -> tuple[str, str, str, str, str, str]:
    return (finding.check, finding.kind, finding.path or "", finding.subject,
            finding.id, finding.detail)


def report_data(findings: Iterable[Finding], rev: str) -> dict[str, object]:
    """Return a deterministic, directly JSON-serializable review report."""
    ordered = sorted(findings, key=_finding_order)
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "revision": rev,
        "count": len(ordered),
        "findings": [finding.to_dict() for finding in ordered],
    }


def run_review_findings(root: Path, rev: str,
                        langs: set[str] | None = None,
                        checks: frozenset[str] | None = None
                        ) -> list[Finding]:
    """Review the working tree against ``rev`` via a detached worktree."""
    new = snapshot(root, langs)
    with tempfile.TemporaryDirectory(prefix="hologram-review-") as tmp:
        wt = Path(tmp) / "wt"
        r = subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", "-f",
             str(wt), rev],
            capture_output=True, text=True, env=_git_env())
        if r.returncode != 0:
            raise SystemExit(f"git worktree failed: {r.stderr.strip()}")
        try:
            old = snapshot(wt, langs)
            old_digest = build_digest(wt, langs=langs)
        finally:
            subprocess.run(["git", "-C", str(root), "worktree", "remove",
                            "--force", str(wt)], capture_output=True,
                           env=_git_env())
    return review_snapshots(old, new, old_digest, checks)


def run_review_data(root: Path, rev: str, langs: set[str] | None = None,
                    checks: frozenset[str] | None = None) -> dict[str, object]:
    """Run a repository review and return the structured report schema."""
    return report_data(run_review_findings(root, rev, langs, checks), rev)


def run_review(root: Path, rev: str, langs: set[str] | None = None,
               checks: frozenset[str] | None = None) -> str:
    """Run a repository review and retain the original human text format."""
    return render_report(run_review_findings(root, rev, langs, checks), rev)
