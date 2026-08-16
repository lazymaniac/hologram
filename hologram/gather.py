"""Scan + extract + state hash: everything upstream of rendering."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .extract import extract_file
from .symbols import (DENYLIST_DIRS, ENTRYPOINT_DECORATORS, Symbol, _IDENT_RE,
                      detect_language, strip_comments_and_strings)


_GIT_CONTEXT_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
    "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
)


def _git_env() -> dict[str, str]:
    """A subprocess environment independent of an invoking Git hook."""
    env = os.environ.copy()
    for name in _GIT_CONTEXT_VARS:
        env.pop(name, None)
    return env


def scan_files(root: Path) -> list[Path]:
    """Source files under root: git-tracked only when root is a git repo (so .gitignore
    excludes vendored/data trees), else a pruned filesystem walk. Deterministic order."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                 capture_output=True, text=True, timeout=60,
                                 env=_git_env())
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
        try:
            raw = f.read_bytes()
        except OSError as exc:
            # `_state_hash` skips unreadable files, so extraction must too or
            # `check` reports stale forever while `build` dies on a traceback.
            # Omitting a source file from a map agents trust is never silent.
            print(f"hologram: warning: skipping unreadable file {rel}: {exc} "
                  f"— its API is absent from the map", file=sys.stderr)
            continue
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


def _digest_metadata_line(digest: str) -> str:
    """Canonical metadata only: current footer or legacy first-line header.

    Semantic facts may legally contain text such as ``· budget 5``. Searching
    the whole digest would let such a constant override freshness/settings on
    rebuild, so readers accept metadata only at the two versioned locations.
    """
    lines = [line for line in digest.splitlines() if line.strip()]
    if not lines:
        return ""
    if re.match(r"^· [\d,]+ LOC(?: ·|$)", lines[-1]):
        return lines[-1]
    if re.match(r"^# hologram · [\d,]+ LOC(?: ·|$)", lines[0]):
        return lines[0]
    return ""


def _digest_state(digest: str) -> str | None:
    """The `state` stamp recorded in a digest's metadata, if any.

    Current maps place volatile metadata last for prompt-cache stability;
    legacy maps carried the same fields on the first line.
    """
    m = re.search(r"· state (\w{12})", _digest_metadata_line(digest))
    return m.group(1) if m else None


def _digest_langs(digest: str) -> set[str] | None:
    """The `langs` filter recorded in a digest's metadata, if any — how a
    `--lang`-restricted map remembers its own scope across rebuilds."""
    m = re.search(r"· langs ([\w,]+)", _digest_metadata_line(digest))
    return set(m.group(1).split(",")) if m else None


def _digest_budget(digest: str) -> int | None:
    """The token budget recorded in a digest's metadata, if any."""
    m = re.search(r"· budget (\d+)", _digest_metadata_line(digest))
    return int(m.group(1)) if m else None


def _digest_targets(digest: str) -> list[str] | None:
    """The `targets` restriction recorded in a digest's metadata, if any —
    which context files carry the map, remembered across rebuilds."""
    m = re.search(r"· targets ([^·\n]+)", _digest_metadata_line(digest))
    return [t.strip() for t in m.group(1).split(",")] if m else None


def _framework_invoked(sym: Symbol) -> bool:
    """Bearers of route/scheduler/listener decorators are called by the
    framework, so zero static use is expected, not evidence of dead code."""
    if sym.lang == "make" and (sym.kind == "class"
                               or (sym.kind == "method"
                                   and sym.visibility == "pub")):
        return True  # synthetic owner and `make target` are external entrypoints
    if (sym.lang == "typescript" and sym.kind == "method"
            and re.match(r"ng[A-Z]", sym.name)):
        return True  # Angular lifecycle hooks (ngOnInit, ngOnDestroy, …)
    web_verbs = ("route", "get", "post", "put", "delete", "patch")
    for d in sym.decorators:
        base = d.split("(", 1)[0].strip()
        tail = base.split(".")[-1]
        if tail in ENTRYPOINT_DECORATORS and (
                "." in base or tail not in web_verbs or sym.lang == "rust"):
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
