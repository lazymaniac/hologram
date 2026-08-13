"""Language registry, the Symbol dataclass, and shared text utilities."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LANG_EXTENSIONS = {
    ".java": "java",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "tsx",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".lua": "lua",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".yaml": "helm",
    ".yml": "helm",
    ".tpl": "helm",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sc": "scala",
}

DENYLIST_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "out", "bin", "obj",
    "vendor", "generated", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    "fixtures", "testdata", "resources",
}

TYPE_KINDS = ("class", "interface", "record", "enum", "type")


@dataclass
class Symbol:
    name: str
    kind: str
    file: str
    line: int
    signature: str = ""
    params: list[str] = field(default_factory=list)
    param_names: list[str] = field(default_factory=list)
    returns: str | None = None
    visibility: str = "pub"
    container: str | None = None
    lang: str = ""
    fields: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    supers: list[str] = field(default_factory=list)
    permits: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)  # var/param/field name -> declared type
    size: int = 0  # body line count (0 = bodyless/unknown)


def detect_language(path: Path) -> str | None:
    return LANG_EXTENSIONS.get(path.suffix)


# ---------------------------------------------------------------------------
# Shared text utilities
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(
    r'"""(?:\\.|(?!""").)*"""'
    r"|'''(?:\\.|(?!''').)*'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'",
    re.S,
)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*|#[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def strip_comments_and_strings(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub('"s"', text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


def _parse_throws(clause: str | None) -> list[str]:
    if not clause:
        return []
    return [t.strip().split(".")[-1] for t in clause.split(",") if t.strip()]


def _split_top_commas(raw: str, opens: str, closes: str) -> list[str]:
    """Split on commas that sit outside any bracket nesting."""
    parts, depth, cur = [], 0, ""
    for c in raw:
        if c in opens:
            depth += 1
        elif c in closes:
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    return parts


def split_params(raw: str) -> list[str]:
    """Split a Java parameter list on top-level commas, return declared types only."""
    types = []
    for p in _split_top_commas(raw, "<([", ">)]"):
        p = re.sub(r"@\w+(\([^)]*\))?", "", p).strip()
        p = re.sub(r"^final\s+", "", p)
        tokens = p.rsplit(None, 1)
        if tokens:
            types.append(tight_type(tokens[0].strip()))
    return types


def tight_type(t: str) -> str:
    """Collapse interior whitespace in a type expression: Map<K, V> -> Map<K,V>."""
    return re.sub(r",\s+", ",", t)


def _base_type(t: str) -> str:
    """Bare type name: Map<K,V> -> Map, list[X] -> list, String[] -> String."""
    return re.sub(r"[<\[(].*", "", t).strip()


def _heritage(segment: str) -> tuple[list[str], list[str]]:
    """(supers, permits) from the text between a type's name and its body
    (Java and TS share the extends/implements keywords)."""
    def names(kw: str) -> list[str]:
        m = re.search(rf"\b{kw}\s+([\w.<>, \t\n]+?)(?=\bextends\b|\bimplements\b|\bpermits\b|$)",
                      segment)
        if not m:
            return []
        return [re.sub(r"<.*", "", n.strip()).split(".")[-1]
                for n in m.group(1).split(",") if n.strip()]
    supers = names("extends") + names("implements")
    return supers, names("permits")

