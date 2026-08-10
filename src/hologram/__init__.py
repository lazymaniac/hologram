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
        "_digest_state",
        "_hook_python",
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
    "CanonicalSymbol",
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
    "analyze_project",
    "build_digest",
    "build_project",
    "canonical_config_bytes",
    "canonical_type_key",
    "compute_state",
    "decode_render",
    "default_config",
    "detect_language",
    "embed_digest",
    "extract_file",
    "has_parser",
    "load_config",
    "project_render_ir",
    "read_digest_state",
    "render_config",
    "render_project",
    "render_simple",
    "resolve_project",
    "run_cli",
    "scan_files",
    "scan_project",
]


def __getattr__(name: str):
    """Load modular APIs lazily and retain the temporary v1 compatibility surface."""
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
    if name not in _LEGACY_COMPAT_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    legacy = import_module(".legacy", __name__)
    value = legacy if name == "legacy" else getattr(legacy, name)
    globals()[name] = value
    return value
