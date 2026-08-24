"""hologram: compress a codebase into a compact markdown map for LLM sessions.

Deterministic. One layout: named type fields, public name-based signatures,
project-internal calls, factored private names, and test locations.

Extraction uses tree-sitter for supported languages, stdlib `ast` for Python,
and a narrow template scanner for Helm.
"""

from __future__ import annotations

from ._version import __version__
from .bootstrap import _bootstrap_or_die, _missing_parser_langs, _venv_python
from .cli import _hook_python, _install_hooks, _managed_hook_line, main, run_cli
from .embed import (_EMBED_END, _EMBED_NOTE, _EMBED_START, CONTEXT_DIRS,
                    CONTEXT_FILES, ManagedContextCost, context_targets,
                    embed_digest, embedded_digest, managed_context_cost)
from .extract import EXTRACTORS, extract_file
from .extract.java import _extract_java
from .gather import (_digest_features, _digest_state, _gather,
                     _generator_fingerprint, _state_hash,
                     _zero_usage_names, scan_files)
from .render import (BudgetStats, _factored_name_tokens, _is_test_path,
                     _tree_lines, build_digest, build_digest_with_stats,
                     estimate_tokens, render_simple)
from .symbols import (DENYLIST_DIRS, FEATURE_NAMES, FEATURES, LANG_EXTENSIONS,
                      TYPE_KINDS, Symbol, detect_language, split_params,
                      strip_comments_and_strings, tight_type)
from .treesitter import (_GRAMMAR_MODULES, _PARSERS, USING_TREESITTER,
                         _grammar_pkgs, has_parser)

# NB: `_PARSERS` is re-exported by reference and only ever mutated, never rebound —
# tests monkeypatch entries through this module and every extractor must see it.
