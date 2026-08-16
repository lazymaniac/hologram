"""Managed digest blocks inside agent context files."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Embed: put the digest INSIDE the agent's context files so every session starts
# with the whole map in context — push, not pull; no retrieval decision to lose.
# ---------------------------------------------------------------------------

_EMBED_START = "<!-- hologram:start — generated, do not edit; refreshed by git hooks -->"
_EMBED_END = "<!-- hologram:end -->"


_COACH_SENTENCE = (
    " Before adding tests/helpers, check `? tests` and `*` helpers. Address "
    "`hologram review` findings before finishing; reuse named originals and "
    "consolidate duplicate coverage."
)

_EMBED_NOTE_BASE = (
    "Hologram project map: exact files, signatures, fields, and retained call "
    "paths. Read it before searching source. Line 2 is the legend; omissions "
    "are marked."
)
_EMBED_NOTE = _EMBED_NOTE_BASE + _COACH_SENTENCE


def _embed_block(digest: str, *, include_coaching: bool = True) -> str:
    note = _EMBED_NOTE if include_coaching else _EMBED_NOTE_BASE
    return (f"{_EMBED_START}\n{note}\n\n```\n{digest.rstrip()}\n```\n"
            f"{_EMBED_END}")


@dataclass(frozen=True)
class ManagedContextCost:
    """Estimated token components of one canonical managed context block.

    ``wrapper_tokens`` includes the explanatory base note, markers, and code
    fences. ``coaching_tokens`` is zero when coaching is not present. The
    components are allocated by marginal differences so they always sum
    exactly despite the estimator's ceiling operation.
    """

    digest_tokens: int
    wrapper_tokens: int
    coaching_tokens: int
    managed_block_tokens: int


def managed_context_cost(digest: str, *,
                         include_coaching: bool = True) -> ManagedContextCost:
    """Planning estimates for the digest and the block actually loaded.

    Budget selection still applies to the digest alone. This helper accounts
    for the separate embedding overhead without changing that contract.
    """
    # Lazy import keeps embedding independent during module initialization;
    # render does not need to know how or where its digest will be delivered.
    from .render import estimate_tokens

    payload = digest.rstrip()
    # Account the bytes actually embedded. Normalizing both the component and
    # total prevents arbitrary trailing whitespace from producing a negative
    # wrapper allocation.
    # The block always contributes one delimiter newline after the normalized
    # digest. Allocate that byte to the digest so canonical build output keeps
    # the same estimate as render/stats while arbitrary padding is discarded.
    digest_tokens = estimate_tokens(payload + "\n")
    uncoached_tokens = estimate_tokens(
        _embed_block(payload, include_coaching=False))
    managed_block_tokens = estimate_tokens(
        _embed_block(payload, include_coaching=include_coaching))
    coaching_tokens = (managed_block_tokens - uncoached_tokens
                       if include_coaching else 0)
    return ManagedContextCost(
        digest_tokens=digest_tokens,
        wrapper_tokens=uncoached_tokens - digest_tokens,
        coaching_tokens=coaching_tokens,
        managed_block_tokens=managed_block_tokens,
    )


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
