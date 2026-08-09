from . import legacy as _legacy
from .legacy import (
    Symbol,
    build_digest,
    embed_digest,
    extract_file,
    has_parser,
    render_simple,
    run_cli,
    scan_files,
)

__all__ = [
    "Symbol",
    "build_digest",
    "embed_digest",
    "extract_file",
    "has_parser",
    "render_simple",
    "run_cli",
    "scan_files",
]


def __getattr__(name: str):
    """Temporarily expose legacy module attributes for v1 compatibility."""
    return getattr(_legacy, name)
