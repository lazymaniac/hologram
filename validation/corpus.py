from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from hologram.config import ProjectConfig, default_config
from hologram.scan import ScanStatus, scan_project

from .schema import (
    CensusRecord,
    CorpusRegistry,
    CorpusSpec,
    GoldSample,
    write_jsonl,
)

__all__ = (
    "build_census",
    "load_registry",
    "resolve_checkout",
    "select_gold_sample",
    "verify_checkout",
)


_ROOT_FIELDS = frozenset({"census", "corpora"})
_CENSUS_FIELDS = frozenset(
    {
        "expected_files",
        "expected_ordinary_yaml_exclusions",
        "outside_candidate_extensions",
    }
)
_CORPUS_FIELDS = frozenset({"name", "url", "revision", "path_env", "sample_files"})
_SCP_REMOTE = re.compile(
    r"(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/:\s]+):(?P<path>[^\s]+)\Z"
)
_PATH_ENV = re.compile(r"HOLOGRAM_VALIDATION_[A-Z0-9_]+\Z")
_VALIDATION_EXCLUDES = (
    "**/.*",
    "**/.*/**",
    "**/fixtures/**",
)
_YAML_SUFFIXES = frozenset({".yaml", ".yml"})
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _invalid(path: Path, detail: str) -> ValueError:
    return ValueError(f"{path}: {detail}")


def _strict_fields(
    path: Path,
    payload: Mapping[str, object],
    expected: frozenset[str],
    location: str,
) -> None:
    unknown = frozenset(payload) - expected
    if unknown:
        names = ", ".join(repr(name) for name in sorted(unknown))
        raise _invalid(path, f"{location}: unknown field(s): {names}")
    missing = expected - frozenset(payload)
    if missing:
        names = ", ".join(repr(name) for name in sorted(missing))
        raise _invalid(path, f"{location}: missing field(s): {names}")


def _normalized_https_remote(remote: str) -> str:
    if (
        not remote
        or remote != remote.strip()
        or any(character.isspace() or character == "\0" for character in remote)
    ):
        raise ValueError("remote URL must be a nonblank single-line string")

    scp_match = _SCP_REMOTE.fullmatch(remote) if "://" not in remote else None
    if scp_match is not None:
        if scp_match.group("user") not in (None, "git"):
            raise ValueError("remote URL has unsupported user information")
        host = scp_match.group("host")
        repository_path = scp_match.group("path")
    else:
        parsed = urlsplit(remote)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("remote URL has an invalid port") from error
        allowed_username = parsed.username is None or (
            parsed.scheme == "ssh" and parsed.username == "git"
        )
        if (
            parsed.scheme not in {"git", "http", "https", "ssh"}
            or parsed.hostname is None
            or not allowed_username
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("remote URL must identify a network Git repository")
        host = parsed.hostname
        repository_path = parsed.path

    repository_path = repository_path.strip("/")
    repository_path = repository_path.removesuffix(".git")
    pure = PurePosixPath(repository_path)
    if (
        not repository_path
        or repository_path == "."
        or len(pure.parts) < 2
        or ".." in pure.parts
        or repository_path != pure.as_posix()
    ):
        raise ValueError("remote URL must have a normalized owner/repository path")
    return f"https://{host.lower()}/{repository_path}.git"


def load_registry(path: Path) -> CorpusRegistry:
    """Load one strict, UTF-8 corpus registry."""

    selected = Path(path)
    try:
        raw = selected.read_bytes()
    except OSError as error:
        raise _invalid(selected, str(error)) from error
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _invalid(selected, "UTF-8 BOM is not allowed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid(selected, "invalid UTF-8") from error
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise _invalid(selected, f"malformed TOML: {error}") from error

    _strict_fields(selected, payload, _ROOT_FIELDS, "top level")
    census_payload = payload["census"]
    corpora_payload = payload["corpora"]
    if not isinstance(census_payload, dict):
        raise _invalid(selected, "census must be a table")
    if not isinstance(corpora_payload, list):
        raise _invalid(selected, "corpora must be an array of tables")
    _strict_fields(selected, census_payload, _CENSUS_FIELDS, "census")

    outside = census_payload["outside_candidate_extensions"]
    if not isinstance(outside, list):
        raise _invalid(selected, "census.outside_candidate_extensions must be an array")

    corpora: list[CorpusSpec] = []
    for index, corpus_payload in enumerate(corpora_payload, start=1):
        location = f"corpora[{index}]"
        if not isinstance(corpus_payload, dict):
            raise _invalid(selected, f"{location} must be a table")
        _strict_fields(selected, corpus_payload, _CORPUS_FIELDS, location)
        try:
            corpus = CorpusSpec(**corpus_payload)
            normalized = _normalized_https_remote(corpus.url)
        except (TypeError, ValueError) as error:
            raise _invalid(selected, f"{location}: {error}") from error
        if corpus.url != normalized:
            raise _invalid(
                selected,
                f"{location}.url must be normalized HTTPS URL {normalized!r}",
            )
        if _PATH_ENV.fullmatch(corpus.path_env) is None:
            raise _invalid(
                selected,
                f"{location}.path_env must match HOLOGRAM_VALIDATION_[A-Z0-9_]+",
            )
        corpora.append(corpus)

    try:
        return CorpusRegistry(
            corpora=tuple(corpora),
            expected_census_files=census_payload["expected_files"],
            expected_ordinary_yaml_exclusions=census_payload[
                "expected_ordinary_yaml_exclusions"
            ],
            outside_candidate_extensions=tuple(outside),
        )
    except (TypeError, ValueError) as error:
        raise _invalid(selected, str(error)) from error


def _external_checkout_path(selected: Path, field: str) -> Path:
    if not selected.is_absolute():
        raise ValueError(f"{field} must be an absolute path: {selected}")
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(
            f"{field} does not resolve to an existing checkout: {selected}"
        ) from error
    if not resolved.is_dir():
        raise ValueError(f"{field} must resolve to a directory: {resolved}")
    if (
        resolved == _PROJECT_ROOT
        or resolved.is_relative_to(_PROJECT_ROOT)
        or _PROJECT_ROOT.is_relative_to(resolved)
    ):
        raise ValueError(
            f"{field} must identify an external checkout outside "
            f"the Hologram worktree: {resolved}"
        )
    return resolved


def resolve_checkout(spec: CorpusSpec, environ: Mapping[str, str]) -> Path:
    """Resolve a corpus checkout from its explicitly named environment variable."""

    raw = environ.get(spec.path_env)
    if type(raw) is not str or not raw.strip():
        raise ValueError(
            f"missing nonblank environment variable {spec.path_env} for {spec.name}"
        )
    return _external_checkout_path(Path(raw), spec.path_env)


def _git(checkout: Path, *arguments: str) -> str:
    command = ("git", "-C", str(checkout), *arguments)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"{checkout}: Git command failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(
            f"{checkout}: Git {' '.join(arguments)} failed: "
            f"{detail or f'exit {result.returncode}'}"
        )
    return result.stdout


def verify_checkout(spec: CorpusSpec, checkout: Path) -> None:
    """Require the configured origin, exact detached content, and a clean tree."""

    selected = _external_checkout_path(Path(checkout), "checkout")
    top_level_raw = _git(selected, "rev-parse", "--show-toplevel").strip()
    top_level = Path(top_level_raw)
    try:
        resolved_top_level = top_level.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(
            f"{selected}: invalid Git worktree root {top_level_raw!r}"
        ) from error
    if not top_level.is_absolute() or resolved_top_level != selected:
        raise ValueError(
            f"{selected}: checkout must equal Git worktree root {resolved_top_level}"
        )
    remote = _git(selected, "config", "--get", "remote.origin.url").strip()
    try:
        normalized_remote = _normalized_https_remote(remote)
    except ValueError as error:
        raise ValueError(f"{selected}: invalid origin remote: {error}") from error
    if normalized_remote != spec.url:
        raise ValueError(
            f"{selected}: remote {normalized_remote!r} does not match "
            f"expected {spec.url!r}"
        )

    revision = _git(selected, "rev-parse", "HEAD").strip()
    if revision != spec.revision:
        raise ValueError(
            f"{selected}: revision {revision!r} does not match "
            f"expected {spec.revision!r}"
        )

    status = _git(
        selected,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if status:
        raise ValueError(f"{selected}: dirty checkout is not allowed")


def _validation_config() -> ProjectConfig:
    foundation = default_config()
    return dataclasses.replace(
        foundation,
        agents=(),
        output="PROJECT_DIGEST.md",
        exclude=foundation.exclude + _VALIDATION_EXCLUDES,
    )


def _ordinary_yaml_count(census: Sequence[CensusRecord]) -> int:
    count = 0
    for row in census:
        path = PurePosixPath(row.path)
        parts = tuple(part.casefold() for part in path.parts)
        if (
            row.language == "helm"
            and path.suffix.lower() in _YAML_SUFFIXES
            and path.name.casefold() not in {"chart.yaml", "values.yaml"}
            and "templates" not in parts[:-1]
        ):
            count += 1
    return count


def build_census(
    registry: CorpusRegistry,
    roots: Mapping[str, Path],
) -> tuple[CensusRecord, ...]:
    """Scan the configured roots with Hologram's candidate policy."""

    expected_names = {spec.name for spec in registry.corpora}
    missing = sorted(expected_names - frozenset(roots))
    if missing:
        raise ValueError(f"missing corpus root(s): {', '.join(missing)}")
    unknown = sorted(frozenset(roots) - expected_names)
    if unknown:
        raise ValueError(f"unknown corpus root(s): {', '.join(unknown)}")

    rows: list[CensusRecord] = []
    config = _validation_config()
    for corpus in registry.corpora:
        root = Path(roots[corpus.name])
        verify_checkout(corpus, root)
        scan = scan_project(root, config)
        if not scan.complete:
            failures = ", ".join(
                entry.file
                for entry in scan.entries
                if entry.status is ScanStatus.FAILED
            )
            raise ValueError(
                f"scan incomplete for {corpus.name}: {failures or 'unknown failure'}"
            )
        rows.extend(
            CensusRecord(
                corpus=corpus.name,
                revision=corpus.revision,
                path=source.file,
                language=source.language.value,
            )
            for source in scan.sources
        )

    result = tuple(sorted(rows, key=lambda row: (row.corpus, row.path)))
    if len(result) != registry.expected_census_files:
        raise ValueError(
            "census count drift: expected "
            f"{registry.expected_census_files}, got {len(result)}"
        )
    ordinary_yaml = _ordinary_yaml_count(result)
    if ordinary_yaml != registry.expected_ordinary_yaml_exclusions:
        raise ValueError(
            "ordinary YAML exclusion count drift: expected "
            f"{registry.expected_ordinary_yaml_exclusions}, got {ordinary_yaml}"
        )
    outside = frozenset(registry.outside_candidate_extensions)
    leaking = [row.path for row in result if Path(row.path).suffix.lower() in outside]
    if leaking:
        raise ValueError(f"outside candidate extension entered census: {leaking[0]}")
    return result


def select_gold_sample(
    census: Sequence[CensusRecord],
    registry: CorpusRegistry,
    *,
    seed: int = 20260809,
) -> tuple[GoldSample, ...]:
    """Select each corpus quota using only the frozen seed, corpus, and path."""

    if type(seed) is not int:
        raise TypeError("seed must be an integer")
    specs = {spec.name: spec for spec in registry.corpora}
    grouped: dict[str, list[CensusRecord]] = {name: [] for name in specs}
    seen: set[tuple[str, str]] = set()
    for row in census:
        if type(row) is not CensusRecord:
            raise TypeError("census must contain only CensusRecord rows")
        corpus = specs.get(row.corpus)
        if corpus is None:
            raise ValueError(f"unknown corpus in census: {row.corpus!r}")
        if row.revision != corpus.revision:
            raise ValueError(
                f"revision drift for {row.corpus}/{row.path}: "
                f"expected {corpus.revision}, got {row.revision}"
            )
        key = (row.corpus, row.path)
        if key in seen:
            raise ValueError(f"duplicate census row: {row.corpus}/{row.path}")
        seen.add(key)
        grouped[row.corpus].append(row)

    selected: list[GoldSample] = []
    for corpus in registry.corpora:
        ranked = [
            (
                hashlib.sha256(
                    f"{seed}\0{row.corpus}\0{row.path}".encode()
                ).hexdigest(),
                row,
            )
            for row in grouped[corpus.name]
        ]
        ranked.sort(key=lambda item: (item[0], item[1].path))
        if len(ranked) < corpus.sample_files:
            raise ValueError(
                f"sample quota for {corpus.name} is {corpus.sample_files}, "
                f"but census has only {len(ranked)} rows"
            )
        selected.extend(
            GoldSample(
                corpus=row.corpus,
                revision=row.revision,
                path=row.path,
                language=row.language,
                rank=rank,
            )
            for rank, row in ranked[: corpus.sample_files]
        )
    return tuple(sorted(selected, key=lambda row: (row.corpus, row.path)))


def _target_identity(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"invalid inventory target {path}: {error}") from error


def _targets_alias(first: Path, second: Path) -> bool:
    if _target_identity(first) == _target_identity(second):
        return True
    try:
        return first.exists() and second.exists() and first.samefile(second)
    except OSError as error:
        raise ValueError(f"cannot compare inventory targets: {error}") from error


def _validate_inventory_target(path: Path, label: str) -> Path:
    selected = path if path.is_absolute() else Path.cwd() / path
    selected = Path(os.path.normpath(selected))
    if selected.is_symlink():
        raise ValueError(f"{label} inventory target must not be a symbolic link")
    if selected.exists() and not selected.is_file():
        raise ValueError(f"{label} inventory target must be a regular file")

    existing_parent = selected.parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise ValueError(f"{label} inventory target parent must be a directory")
    return selected


def _preflight_inventory_targets(
    census_path: Path,
    sample_path: Path,
    *,
    reserved: Sequence[Path] = (),
) -> tuple[Path, Path]:
    census_target = _validate_inventory_target(Path(census_path), "census")
    sample_target = _validate_inventory_target(Path(sample_path), "sample")
    census_identity = _target_identity(census_target)
    sample_identity = _target_identity(sample_target)
    if (
        _targets_alias(census_target, sample_target)
        or census_identity in sample_identity.parents
        or sample_identity in census_identity.parents
    ):
        raise ValueError(
            "census and sample inventory targets must be distinct and non-aliasing"
        )
    for reserved_path in reserved:
        selected = Path(reserved_path)
        if _targets_alias(census_target, selected) or _targets_alias(
            sample_target, selected
        ):
            raise ValueError("inventory targets must not alias an input file")
    return census_target, sample_target


def _temporary_target(target: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(raw_path)


def _target_state(path: Path) -> tuple[bytes, int] | None:
    if not path.exists():
        return None
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_target(path: Path, state: tuple[bytes, int] | None) -> None:
    if state is None:
        path.unlink(missing_ok=True)
        return
    raw, mode = state
    staged = _temporary_target(path)
    try:
        staged.write_bytes(raw)
        staged.chmod(mode)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _write_inventory_pair(
    census_path: Path,
    census: Sequence[CensusRecord],
    sample_path: Path,
    sample: Sequence[GoldSample],
) -> None:
    census_target, sample_target = _preflight_inventory_targets(
        census_path,
        sample_path,
    )
    census_target.parent.mkdir(parents=True, exist_ok=True)
    sample_target.parent.mkdir(parents=True, exist_ok=True)
    census_state = _target_state(census_target)
    sample_state = _target_state(sample_target)
    census_staged: Path | None = None
    sample_staged: Path | None = None
    census_replaced = False
    try:
        census_staged = _temporary_target(census_target)
        sample_staged = _temporary_target(sample_target)
        write_jsonl(census_staged, census)
        census_staged.chmod(census_state[1] if census_state is not None else 0o644)
        write_jsonl(sample_staged, sample)
        sample_staged.chmod(sample_state[1] if sample_state is not None else 0o644)

        os.replace(census_staged, census_target)
        census_replaced = True
        os.replace(sample_staged, sample_target)
    except BaseException:
        if census_replaced:
            try:
                _restore_target(census_target, census_state)
            except BaseException as rollback_error:
                raise RuntimeError(
                    f"failed to roll back census inventory target {census_target}"
                ) from rollback_error
        raise
    finally:
        if census_staged is not None:
            census_staged.unlink(missing_ok=True)
        if sample_staged is not None:
            sample_staged.unlink(missing_ok=True)


def _freeze(arguments: argparse.Namespace) -> int:
    registry = load_registry(arguments.registry)
    census_target, sample_target = _preflight_inventory_targets(
        arguments.census,
        arguments.sample,
        reserved=(arguments.registry,),
    )
    roots = {
        corpus.name: resolve_checkout(corpus, os.environ) for corpus in registry.corpora
    }
    census = build_census(registry, roots)
    sample = select_gold_sample(census, registry, seed=arguments.seed)

    _write_inventory_pair(census_target, census, sample_target, sample)

    print(
        f"census={len(census)} sample={len(sample)} "
        f"ordinary_yaml_exclusions={_ordinary_yaml_count(census)} "
        "outside_candidates=" + ",".join(registry.outside_candidate_extensions)
    )
    counts = Counter(row.corpus for row in sample)
    print(
        "sample_counts="
        + ",".join(
            f"{corpus.name}:{counts[corpus.name]}" for corpus in registry.corpora
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze the public validation corpus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze", help="verify and freeze census files")
    freeze.add_argument("--registry", type=Path, required=True)
    freeze.add_argument("--census", type=Path, required=True)
    freeze.add_argument("--sample", type=Path, required=True)
    freeze.add_argument("--seed", type=int, default=20260809)
    freeze.set_defaults(handler=_freeze)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
