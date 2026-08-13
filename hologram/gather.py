"""Scan + extract + state hash: everything upstream of rendering."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from .extract import extract_file
from .symbols import (DENYLIST_DIRS, ENTRYPOINT_DECORATORS, Symbol, _IDENT_RE,
                      detect_language, strip_comments_and_strings)


def scan_files(root: Path) -> list[Path]:
    """Source files under root: git-tracked only when root is a git repo (so .gitignore
    excludes vendored/data trees), else a pruned filesystem walk. Deterministic order."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                 capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                results = []
                for rel in out.stdout.split("\0"):
                    if not rel or detect_language(Path(rel)) is None:
                        continue
                    if any(part in DENYLIST_DIRS or part.startswith(".")
                           for part in Path(rel).parts[:-1]):
                        continue
                    p = root / rel
                    if p.is_file():
                        results.append(p)
                return sorted(results)
        except (OSError, subprocess.TimeoutExpired):
            pass
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in DENYLIST_DIRS and not d.startswith("."))
        for fn in filenames:
            p = Path(dirpath) / fn
            if detect_language(p) is not None:
                results.append(p)
    return sorted(results)


# ---------------------------------------------------------------------------
# Gather + fan-in
# ---------------------------------------------------------------------------

def _generator_fingerprint() -> bytes:
    """Tool bytes make old rendering/extraction logic stale for every target repo.
    Hashes every .py source in the package, sorted by relative path, via
    importlib.resources so checkout, wheel install, and zipapp all agree."""
    try:
        import importlib.resources as _resources
        entries: list[tuple[str, bytes]] = []
        stack = [(_resources.files("hologram"), "")]
        while stack:
            node, prefix = stack.pop()
            for child in node.iterdir():
                rel = f"{prefix}/{child.name}" if prefix else child.name
                if child.is_dir():
                    if child.name != "__pycache__":
                        stack.append((child, rel))
                elif child.name.endswith(".py"):
                    entries.append((rel, child.read_bytes()))
        h = hashlib.sha256()
        for rel, data in sorted(entries):
            h.update(rel.encode())
            h.update(data)
        return h.digest()
    except OSError:
        return b"hologram"


def _new_state_hash():
    state = hashlib.md5()
    state.update(_generator_fingerprint())
    return state


def _gather(root: Path, langs: set[str] | None = None):
    """Extract symbols, identifier-token sets per file, and the corpus state hash.
    `langs` restricts to those languages (e.g. {"java"}); None means all."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    symbols: list[Symbol] = []
    file_tokens: dict[str, set[str]] = {}
    usage_tokens: Counter[str] = Counter()
    state = _new_state_hash()
    for f in files:
        rel = str(f.relative_to(root))
        raw = f.read_bytes()
        state.update(rel.encode())
        state.update(hashlib.md5(raw).digest())
        text = raw.decode(errors="replace")
        symbols.extend(extract_file(f, root, text))
        identifiers = _IDENT_RE.findall(strip_comments_and_strings(text))
        file_tokens[rel] = set(identifiers)
        usage_tokens.update(identifiers)
        # The string stripper necessarily removes f-string expressions.  Restore
        # Python identifier/attribute reads from the AST without counting comments,
        # ordinary string contents, or declaration names.
        if detect_language(f) == "python":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                pass
            else:
                usage_tokens.update(
                    node.id for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                )
                usage_tokens.update(
                    node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                )
    return files, symbols, file_tokens, usage_tokens, state.hexdigest()[:12]


def _state_hash(root: Path, langs: set[str] | None = None) -> str:
    """The corpus hash `_gather` would produce, without parsing anything — cheap
    freshness probe for `check` / `--if-stale`."""
    files = scan_files(root)
    if langs is not None:
        files = [f for f in files if detect_language(f) in langs]
    state = _new_state_hash()
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        state.update(str(f.relative_to(root)).encode())
        state.update(hashlib.md5(raw).digest())
    return state.hexdigest()[:12]


def _digest_state(digest: str) -> str | None:
    """The `state` stamp recorded in a digest's header line, if any."""
    m = re.search(r"· state (\w{12})", digest.split("\n", 1)[0])
    return m.group(1) if m else None


def _framework_invoked(sym: Symbol) -> bool:
    """Bearers of route/scheduler/listener decorators are called by the
    framework, so zero static use is expected, not evidence of dead code."""
    web_verbs = ("route", "get", "post", "put", "delete", "patch")
    for d in sym.decorators:
        base = d.split("(", 1)[0].strip()
        tail = base.split(".")[-1]
        if tail in ENTRYPOINT_DECORATORS and ("." in base or tail not in web_verbs):
            return True
    return False


def _zero_usage_names(symbols: list[Symbol], usage_tokens: Counter[str]) -> set[str]:
    """Code functions/classes with no statically observed project reference."""
    declarations = Counter(s.name for s in symbols if s.kind != "reexport")
    return {
        s.name for s in symbols
        if s.kind in ("fn", "method", "class")
        and s.lang not in ("html", "helm")
        and not (s.name.startswith("__") and s.name.endswith("__"))
        and not _framework_invoked(s)
        and usage_tokens[s.name] <= declarations[s.name]
    }

