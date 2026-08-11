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
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)
from .scan import ScanEntry, ScanResult, ScanStatus, detect_language, scan_project
from .state import (
    STATE_FORMAT_VERSION,
    StateResult,
    compute_state,
    read_digest_state,
)

__version__ = "0.2.0"

_RESOLVE_NAMES = frozenset(
    {
        "UNKNOWN_TYPE_KEY",
        "ResolutionResult",
        "ResolutionStatus",
        "ResolvedCall",
        "ResolvedImport",
        "ResolvedReference",
        "canonical_type_key",
        "resolve_project",
    }
)

_PIPELINE_NAMES = frozenset(
    {
        "BuildSnapshot",
        "IncompleteBuildError",
        "build_project",
    }
)

_ANALYSIS_NAMES = frozenset(
    {
        "AnalyzedProject",
        "analyze_project",
    }
)

_RENDER_NAMES = frozenset(
    {
        "RenderIR",
        "decode_render",
        "project_render_ir",
        "render_project",
    }
)

__all__ = [
    "CONFIG_NAME",
    "CONFIG_SCHEMA_VERSION",
    "STATE_FORMAT_VERSION",
    "UNKNOWN_TYPE_KEY",
    "AnalyzedProject",
    "Binding",
    "BodyEvent",
    "BodyEventKind",
    "BodyIR",
    "BuildSnapshot",
    "CallKind",
    "CallRef",
    "ConfigError",
    "Diagnostic",
    "DiagnosticSeverity",
    "FileIR",
    "ImportRef",
    "IncompleteBuildError",
    "Language",
    "ProjectConfig",
    "ProjectIR",
    "ReferenceConfidence",
    "ReferenceContext",
    "ReferenceKind",
    "ReferenceRef",
    "RenderIR",
    "ResolutionResult",
    "ResolutionStatus",
    "ResolvedCall",
    "ResolvedImport",
    "ResolvedReference",
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
    "__version__",
    "analyze_project",
    "build_project",
    "canonical_config_bytes",
    "canonical_type_key",
    "compute_state",
    "decode_render",
    "default_config",
    "detect_language",
    "load_config",
    "project_render_ir",
    "read_digest_state",
    "render_config",
    "render_project",
    "resolve_project",
    "scan_project",
]


def __getattr__(name: str):
    """Load modular phase APIs lazily."""
    if name in _PIPELINE_NAMES:
        value = getattr(import_module(".pipeline", __name__), name)
        globals()[name] = value
        return value
    if name in _RESOLVE_NAMES:
        value = getattr(import_module(".resolve", __name__), name)
        globals()[name] = value
        return value
    if name in _ANALYSIS_NAMES:
        value = getattr(import_module(".analysis", __name__), name)
        globals()[name] = value
        return value
    if name in _RENDER_NAMES:
        value = getattr(import_module(".render", __name__), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
