from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .symbols import detect_language
from .treesitter import _GRAMMAR_MODULES, _grammar_pkgs, has_parser


# ---------------------------------------------------------------------------
# Self-bootstrap: get tree-sitter grammars without manual setup
# ---------------------------------------------------------------------------

def _pyz_path() -> Path | None:
    """The zipapp archive we are running from, if any: the first existing ancestor
    of this module's path is the .pyz file itself when the package lives in a zip."""
    for anc in Path(__file__).resolve().parents:
        if anc.exists():
            return anc if anc.is_file() else None
    return None


def _tool_anchor() -> Path:
    """The directory the grammar `.venv` lives next to: the pyz's directory when
    zipped, else the package's parent (checkout root or site-packages)."""
    pyz = _pyz_path()
    if pyz is not None:
        return pyz.parent
    return Path(__file__).resolve().parent.parent


def _venv_python() -> Path:
    return _tool_anchor() / ".venv" / "bin" / "python"


def _missing_parser_langs(files: list[Path]) -> set[str]:
    """Languages present in `files` that need a tree-sitter parser we don't have."""
    return {l for l in {detect_language(f) for f in files}
            if l in _GRAMMAR_MODULES and not has_parser(l)}


def _venv_has_grammars(venv_py: Path, langs: set[str]) -> bool:
    mods = sorted({_GRAMMAR_MODULES[l][0] for l in langs})
    r = subprocess.run([str(venv_py), "-c", "import " + ",".join(mods)],
                       capture_output=True)
    return r.returncode == 0


def _bootstrap_or_die(missing: set[str], argv: list[str]) -> None:
    """Make parsers for `missing` available: re-exec into the tool's venv when it has
    the grammars, else offer to create the venv and pip-install them (interactive only).
    On success the process is replaced; otherwise exits with manual instructions."""
    venv_py = _venv_python()
    venv_dir = venv_py.parent.parent
    pkgs = _grammar_pkgs(missing)
    manual = (f"missing tree-sitter parser for: {', '.join(sorted(missing))}\n"
              f"install with: python3 -m venv {venv_dir} && "
              f"{venv_py} -m pip install {' '.join(pkgs)}")

    def _reexec() -> None:
        os.environ["HOLOGRAM_BOOTSTRAPPED"] = "1"
        pyz = _pyz_path()
        if pyz is not None:
            os.execv(str(venv_py), [str(venv_py), str(pyz), *argv])
        # The grammar venv doesn't contain hologram itself — put the package's
        # parent on PYTHONPATH so `-m hologram` resolves there.
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        existing = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = (pkg_parent + os.pathsep + existing
                                    if existing else pkg_parent)
        os.execv(str(venv_py), [str(venv_py), "-m", "hologram", *argv])

    if os.environ.get("HOLOGRAM_BOOTSTRAPPED"):
        raise SystemExit(manual)  # second attempt failed too; don't exec-loop
    # NB: compare unresolved paths — the venv python is a symlink to the base
    # interpreter, but only invoking it via the venv path selects the venv's packages.
    if (venv_py.exists() and Path(sys.executable) != venv_py
            and _venv_has_grammars(venv_py, missing)):
        _reexec()
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise SystemExit(manual)
    reply = input(f"hologram: no parser for {', '.join(sorted(missing))}; "
                  f"install {' '.join(pkgs)} into {venv_dir}? [Y/n] ")
    if reply.strip().lower() not in ("", "y", "yes"):
        raise SystemExit(manual)
    if not venv_py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    subprocess.run([str(venv_py), "-m", "pip", "install", "--quiet", *pkgs], check=True)
    _reexec()
