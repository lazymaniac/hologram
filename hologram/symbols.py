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
    ".mk": "make",
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

# Selectable map content. Every entry names one fact class the renderer can
# omit whole; the always-present remainder — the package trie, type headers,
# and public signatures — is the map's identity and is not selectable. Order
# is the order the picker lists them: semantics first, then evidence markers,
# then inventories. Selecting a subset changes *which facts* render, never how
# a retained fact is written, so it composes with the budget ladder instead of
# competing with it.
FEATURES: tuple[tuple[str, str], ...] = (
    ("calls", "call chains between project symbols (sig > callee)"),
    ("relations", "supers, implements, sealed permits, implementors"),
    ("fields", "field names, record components, enum values"),
    ("constants", "public constants and their short values"),
    ("decorators", "routes and framework annotations (@GET/path)"),
    ("raises", "declared or thrown exception types (!E)"),
    ("tested", "the ✓ marker on symbols reached from a test"),
    ("usage", "the ×0 marker on symbols with no static reference"),
    ("size", "the ~N body-size marker on large bodies"),
    ("private", "the names-only private member inventory"),
    ("tests", "the test index: files, case names, fixtures"),
    ("support", "tools/ and benchmark/ landmark lines"),
)
FEATURE_NAMES = frozenset(name for name, _ in FEATURES)


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
    decorators: list[str] = field(default_factory=list)  # verbatim, sigil-stripped; render filters
    size: int = 0  # body line count (0 = bodyless/unknown)


# Decorator/annotation allowlists live here so gather and render can both import
# them without cycles. Extraction stores every decorator; these decide rendering.
# base name -> HTTP verb; None = path-only (verb elsewhere or class-level prefix)
ROUTE_DECORATORS: dict[str, str | None] = {
    # Spring
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": None,
    # JAX-RS (verb annotations are argument-less; @Path carries the route)
    "GET": "GET", "POST": "POST", "PUT": "PUT", "DELETE": "DELETE",
    "PATCH": "PATCH", "Path": None,
    # NestJS
    "Controller": None, "Get": "GET", "Post": "POST", "Put": "PUT",
    "Delete": "DELETE", "Patch": "PATCH",
    # ASP.NET Core attributes; Route is shared with Symfony's #[Route]
    "HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT",
    "HttpDelete": "DELETE", "HttpPatch": "PATCH", "Route": None,
    # Python web frameworks match on the dotted tail: app.route / router.get …
    # (rust is the one language where these also match bare — actix macros)
    "route": None, "get": "GET", "post": "POST", "put": "PUT",
    "delete": "DELETE", "patch": "PATCH",
}

# argument-less markers rendered verbatim as @Name
# (@dataclass is absent on purpose: it flips the symbol's kind to record)
MARKER_DECORATORS = {
    "Transactional", "Scheduled", "KafkaListener", "EventListener",
    "Injectable", "property", "staticmethod", "classmethod",
    "abstractmethod", "cached_property", "fixture", "memo", "forwardRef",
    "ApiController",
}

# decorators that mean "invoked by a framework, not by project code": their
# bearers are exempt from the ×0 no-static-use marker
ENTRYPOINT_DECORATORS = (set(ROUTE_DECORATORS) | {
    "Scheduled", "EventListener", "KafkaListener", "fixture", "Component",
}) - {"Path"}

# Constant values matching either pattern render name-only. The map is copied
# into context files that get committed, so a secret-shaped value must never
# survive rendering even though it already sits in the source.
_SECRET_NAME_RE = re.compile(
    r"(?i)(?:^|_)(KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|CREDENTIALS?|APIKEY|"
    r"AUTH|BEARER|SALT|NONCE|DSN|SESSION|COOKIE|SIGNATURE)(?:_|$|S(?:_|$))")
_SECRET_VALUE_RE = re.compile(
    r"""['"](sk-|pk-|ghp_|gho_|github_pat_|glpat-|AKIA|ASIA|xox[a-z]-|eyJ|"""
    r"""-----BEGIN|AIza|ya29\.)""")


def const_signature(name: str, value_text: str | None) -> str:
    """Display form of a constant: NAME=value for short scalar literals,
    name-only for long values, containers, and anything secret-shaped."""
    if (value_text is None or len(value_text) > 24
            or _SECRET_NAME_RE.search(name)
            or _SECRET_VALUE_RE.match(value_text)):
        return name
    return f"{name}={value_text}"


def detect_language(path: Path) -> str | None:
    if path.name in ("Makefile", "makefile", "GNUmakefile"):
        return "make"
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


def tight_annotation(text: str) -> str:
    """tight_type for annotation text — but verbatim when it carries a string
    literal, so quoted arguments like @DisplayName("a, b") keep their interior
    spacing."""
    return text if '"' in text or "'" in text else tight_type(text)


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
                for n in _split_top_commas(m.group(1), "<", ">") if n.strip()]
    supers = names("extends") + names("implements")
    return supers, names("permits")

