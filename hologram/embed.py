"""Managed digest blocks inside agent context files."""
from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Embed: put the digest INSIDE the agent's context files so every session starts
# with the whole map in context — push, not pull; no retrieval decision to lose.
# ---------------------------------------------------------------------------

_EMBED_START = "<!-- hologram:start — generated, do not edit; refreshed by git hooks -->"
_EMBED_END = "<!-- hologram:end -->"


_COACH_SENTENCE = (
    " Before writing tests or helpers, check `? tests` for existing coverage "
    "and *-marked helpers. When `hologram review` reports findings, address "
    "them before finishing: reuse the named original instead of a duplicate; "
    "consolidate re-covered tests."
)

_EMBED_NOTE = (
    "This is a hologram map of this repository: a deterministic index of its "
    "public API — signatures, fields, call chains, private names, test "
    "locations. Read it before exploring to find what exists and open the "
    "right file first. Line 2 is the legend." + _COACH_SENTENCE
)


def _embed_block(digest: str) -> str:
    return (f"{_EMBED_START}\n{_EMBED_NOTE}\n\n```\n{digest.rstrip()}\n```\n"
            f"{_EMBED_END}")


def _block_span(existing: str) -> tuple[int, int] | None:
    """Offsets of the managed block, or None. The end marker is located *after* the
    start one, so prose that mentions a marker before the block can't misplace it."""
    start = existing.find(_EMBED_START)
    if start < 0:
        return None
    end = existing.find(_EMBED_END, start + len(_EMBED_START))
    if end < 0:
        return None
    return start, end + len(_EMBED_END)


def embedded_digest(path: Path) -> str:
    """The digest text inside a context file's managed block, "" when there is none."""
    try:
        existing = path.read_text(errors="replace")
    except OSError:
        return ""
    span = _block_span(existing)
    if span is None:
        return ""
    body = existing[span[0] + len(_EMBED_START):span[1] - len(_EMBED_END)]
    m = re.search(r"```\n(.*?)\n```", body, re.S)
    return m.group(1) if m else ""


def embed_digest(path: Path, digest: str) -> None:
    """Insert or refresh one exact, non-degraded digest block in a context file,
    preserving hand-written content around it."""
    block = _embed_block(digest)
    existing = path.read_text() if path.exists() else _seed_content(path)
    span = _block_span(existing)
    if span is not None:
        updated = existing[:span[0]] + block + existing[span[1]:]
    else:
        sep = "\n\n" if existing.strip() else ""
        updated = existing.rstrip("\n") + sep + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated)


# Context files of the popular coding agents. Files are only touched when they
# already exist; rule *directories* get one managed file of ours. When a repo has
# none of them, CLAUDE.md is created.
CONTEXT_FILES = (
    "CLAUDE.md",                        # Claude Code
    "AGENTS.md",                        # Codex, opencode, Jules, Zed
    "AGENT.md",                          # Amp
    "GEMINI.md",                        # Gemini CLI
    "QWEN.md",                          # Qwen Code
    "CONVENTIONS.md",                    # Aider
    ".clinerules",                       # Cline (single-file form)
    ".cursorrules",                      # Cursor (legacy single-file form)
    ".windsurfrules",                    # Windsurf (legacy single-file form)
    ".roorules",                         # Roo Code (single-file form)
    ".rules",                            # Zed / generic
    ".github/copilot-instructions.md",   # GitHub Copilot
)

CONTEXT_DIRS = (
    (".clinerules", "hologram.md"),
    (".cursor/rules", "hologram.mdc"),
    (".roo/rules", "hologram.md"),
    (".windsurf/rules", "hologram.md"),
    (".github/instructions", "hologram.instructions.md"),
    (".junie", "guidelines.md"),         # JetBrains Junie
    (".continue/rules", "hologram.md"),  # Continue
    (".kiro/steering", "hologram.md"),   # Kiro (steering docs load by default)
)

# Path-tail seeds win over suffix seeds: .continue rules need front matter while
# the same basename under .clinerules/.roo/.windsurf/.kiro must stay seedless.
_DIR_SEEDS = {
    ".continue/rules/hologram.md":
        "---\nname: hologram project map\nalwaysApply: true\n---\n",
}

_SEEDS = {
    ".mdc": "---\ndescription: hologram project map\nalwaysApply: true\n---\n",
    ".instructions.md": "---\napplyTo: '**'\n---\n",
}


def _seed_content(path: Path) -> str:
    """Front matter a newly created rule file needs to be picked up by its agent."""
    tail = "/".join(path.parts[-3:])
    if tail in _DIR_SEEDS:
        return _DIR_SEEDS[tail]
    for suffix, seed in _SEEDS.items():
        if path.name.endswith(suffix):
            return seed
    return ""


def context_targets(root: Path) -> list[Path]:
    """Every agent context file in `root` to attach the map to. Falls back to
    CLAUDE.md when the repo has no agent context file yet."""
    targets = [root / rel for rel in CONTEXT_FILES if (root / rel).is_file()]
    targets += [root / rel / name for rel, name in CONTEXT_DIRS
                if (root / rel).is_dir()]
    return targets or [root / "CLAUDE.md"]


def _target_candidates(root: Path) -> list[Path]:
    """The full universe --target values may name, present on disk or not."""
    return ([root / rel for rel in CONTEXT_FILES]
            + [root / rel / name for rel, name in CONTEXT_DIRS])

