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
    legacy = import_module(".legacy", __name__)
    value = legacy if name == "legacy" else getattr(legacy, name)
    globals()[name] = value
    return value
