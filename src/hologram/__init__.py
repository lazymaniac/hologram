from importlib import import_module

from .config import (
    CONFIG_NAME,
    CONFIG_SCHEMA_VERSION,
    ConfigError,
    ProjectConfig,
    canonical_config_bytes,
    default_config,
    load_config,
    render_config,
)
from .model import (
    Binding,
    BodyEvent,
    BodyEventKind,
    BodyIR,
    CallKind,
    CallRef,
    Diagnostic,
    DiagnosticSeverity,
    FileIR,
    ImportRef,
    Language,
    ProjectIR,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceRole,
    SourceSpan,
    SymbolId,
    SymbolKind,
    Visibility,
)
from .model import (
    Symbol as CanonicalSymbol,
)
from .scan import ScanEntry, ScanResult, ScanStatus, detect_language, scan_project
from .state import (
    STATE_FORMAT_VERSION,
    StateResult,
    compute_state,
    read_digest_state,
)

_LEGACY_COMPAT_NAMES = frozenset(
    {
        "Symbol",
        "_dep_lines",
        "_digest_state",
        "_hook_python",
        "_missing_parser_langs",
        "_reduce_for_embed",
        "_state_hash",
        "_tree_lines",
        "build_digest",
        "embed_digest",
        "extract_file",
        "has_parser",
        "legacy",
        "render_simple",
        "run_cli",
        "scan_files",
    }
)

__all__ = [
    "CONFIG_NAME",
    "CONFIG_SCHEMA_VERSION",
    "STATE_FORMAT_VERSION",
    "Binding",
    "BodyEvent",
    "BodyEventKind",
    "BodyIR",
    "CallKind",
    "CallRef",
    "CanonicalSymbol",
    "ConfigError",
    "Diagnostic",
    "DiagnosticSeverity",
    "FileIR",
    "ImportRef",
    "Language",
    "ProjectConfig",
    "ProjectIR",
    "ReferenceConfidence",
    "ReferenceContext",
    "ReferenceKind",
    "ReferenceRef",
    "ScanEntry",
    "ScanResult",
    "ScanStatus",
    "SourceFile",
    "SourceRole",
    "SourceSpan",
    "StateResult",
    "Symbol",
    "SymbolId",
    "SymbolKind",
    "Visibility",
    "build_digest",
    "canonical_config_bytes",
    "compute_state",
    "default_config",
    "detect_language",
    "embed_digest",
    "extract_file",
    "has_parser",
    "load_config",
    "read_digest_state",
    "render_config",
    "render_simple",
    "run_cli",
    "scan_files",
    "scan_project",
]


def __getattr__(name: str):
    """Temporarily expose legacy module attributes for v1 compatibility."""
    if name not in _LEGACY_COMPAT_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    legacy = import_module(".legacy", __name__)
    value = legacy if name == "legacy" else getattr(legacy, name)
    globals()[name] = value
    return value
