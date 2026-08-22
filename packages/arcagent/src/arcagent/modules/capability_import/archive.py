"""Safe, non-executing intake of agent-scoped capability archives.

This module deliberately validates paths and bytes itself rather than delegating
to ``ZipFile.extractall``. Intake is only a filesystem operation: it never
imports, executes, previews, or otherwise interprets uploaded code.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import unicodedata
import zipfile
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from typing import IO

from arcagent.modules.capability_import.errors import (
    CapabilityImportLayoutError,
    CapabilityImportLimitError,
    CapabilityImportPathError,
    CapabilityImportSourceError,
)
from arcagent.modules.capability_import.models import (
    CapabilityImportFile,
    CapabilityImportLimits,
    CapabilityImportResult,
)

_CHUNK_SIZE = 64 * 1024
_NESTED_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".tar.gz", ".gz")
_RESOURCE_DIRS = frozenset({"references", "scripts", "templates", "assets"})
_ROOT_FILES = frozenset({"capability-import.toml", "sbom.cdx.json", "README.md", "LICENSE"})


@dataclass(frozen=True)
class _Candidate:
    """A preflighted regular file supplied by a ZIP or local tree."""

    path: PurePosixPath
    size: int
    compressed_size: int
    source: Path | zipfile.ZipInfo


def intake(
    source: Path,
    capabilities_root: Path,
    *,
    limits: CapabilityImportLimits | None = None,
) -> CapabilityImportResult:
    """Safely copy a ZIP or local directory into agent-local import staging.

    The returned directory is not active and is never a loader scan root. A
    later approval/activation layer owns signing and promotion.
    """
    source = Path(source)
    limits = limits or CapabilityImportLimits()
    candidates, archive_sha256, archive_bytes = _preflight(source, limits)
    _validate_layout(candidate.path for candidate in candidates)
    import_id = archive_sha256
    imports_root = Path(capabilities_root) / "imports"
    quarantine = imports_root / ".quarantine" / import_id / "source.zip"
    staging = imports_root / ".staging" / import_id
    if staging.is_dir():
        return CapabilityImportResult(
            import_id=import_id,
            archive_sha256=archive_sha256,
            quarantine_path=quarantine,
            staging_dir=staging,
            files=_inventory(staging),
        )

    temporary = Path(mkdtemp(prefix="capability-import-", dir=_staging_parent(imports_root)))
    try:
        files = _copy_candidates(source, candidates, temporary, limits)
        _copy_quarantine(source, quarantine, archive_bytes, limits)
        staging.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(temporary, staging)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return CapabilityImportResult(
        import_id=import_id,
        archive_sha256=archive_sha256,
        quarantine_path=quarantine,
        staging_dir=staging,
        files=tuple(files),
    )


def _staging_parent(imports_root: Path) -> Path:
    parent = imports_root / ".staging"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    return parent


def _preflight(source: Path, limits: CapabilityImportLimits) -> tuple[list[_Candidate], str, int]:
    if source.is_dir():
        candidates = _preflight_directory(source, limits)
        digest = _tree_digest(candidates)
        return candidates, digest, sum(candidate.size for candidate in candidates)
    if not source.is_file() or source.is_symlink():
        raise CapabilityImportSourceError("source must be a regular ZIP file or directory")
    if source.stat().st_size > limits.max_compressed_bytes:
        raise CapabilityImportLimitError("compressed source exceeds configured limit")
    if not zipfile.is_zipfile(source):
        raise CapabilityImportSourceError("source must be a ZIP file or directory")
    return _preflight_zip(source, limits)


def _preflight_zip(
    source: Path, limits: CapabilityImportLimits
) -> tuple[list[_Candidate], str, int]:
    try:
        with zipfile.ZipFile(source) as archive:
            candidates: list[_Candidate] = []
            seen: set[str] = set()
            compressed = 0
            expanded = 0
            for info in archive.infolist():
                _reject_zip_special(info)
                if info.is_dir():
                    _safe_path(info.filename, seen, limits=limits, directory=True)
                    continue
                path = _safe_path(info.filename, seen, limits=limits)
                _check_zip_limits(info, limits)
                compressed += info.compress_size
                expanded += info.file_size
                if (
                    compressed > limits.max_compressed_bytes
                    or expanded > limits.max_expanded_bytes
                ):
                    raise CapabilityImportLimitError("archive exceeds configured aggregate limit")
                candidates.append(_Candidate(path, info.file_size, info.compress_size, info))
    except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise CapabilityImportSourceError("unreadable ZIP source") from exc
    _check_count(candidates, limits)
    return candidates, _file_digest(source), source.stat().st_size


def _preflight_directory(source: Path, limits: CapabilityImportLimits) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            _safe_path(relative, seen, limits=limits, directory=True)
            continue
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise CapabilityImportSourceError("source tree contains a non-regular or linked file")
        normalized = _safe_path(relative, seen, limits=limits)
        _check_file_size(status.st_size, limits)
        total += status.st_size
        if total > limits.max_expanded_bytes:
            raise CapabilityImportLimitError("source tree exceeds configured expanded limit")
        candidates.append(_Candidate(normalized, status.st_size, status.st_size, path))
    _check_count(candidates, limits)
    return candidates


def _safe_path(
    raw: str, seen: set[str], *, limits: CapabilityImportLimits, directory: bool = False
) -> PurePosixPath:
    if "\x00" in raw or "\\" in raw or raw.startswith(("/", "\\")):
        raise CapabilityImportPathError("archive contains an unsafe path")
    path = PurePosixPath(raw)
    if not raw or any(part in ("", ".", "..") or ":" in part for part in path.parts):
        raise CapabilityImportPathError("archive contains an unsafe path")
    if len(path.parts) > limits.max_depth or len(raw) > 240:
        raise CapabilityImportPathError("archive path exceeds configured shape limit")
    key = unicodedata.normalize("NFC", path.as_posix()).casefold()
    if not directory:
        if key in seen:
            raise CapabilityImportPathError("archive contains duplicate or colliding paths")
        seen.add(key)
    return path


def _reject_zip_special(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if info.flag_bits & 0x1:
        raise CapabilityImportSourceError("encrypted ZIP entries are not accepted")
    if kind not in (0, stat.S_IFREG):
        raise CapabilityImportSourceError("ZIP contains a non-regular file entry")


def _check_zip_limits(info: zipfile.ZipInfo, limits: CapabilityImportLimits) -> None:
    _check_file_size(info.file_size, limits)
    ratio = info.file_size / max(1, info.compress_size)
    if ratio > limits.max_compression_ratio:
        raise CapabilityImportLimitError("archive entry exceeds configured compression ratio")


def _check_file_size(size: int, limits: CapabilityImportLimits) -> None:
    if size > limits.max_file_bytes:
        raise CapabilityImportLimitError("file exceeds configured size limit")


def _check_count(candidates: Iterable[_Candidate], limits: CapabilityImportLimits) -> None:
    if len(list(candidates)) > limits.max_files:
        raise CapabilityImportLimitError("source exceeds configured file count")


def _validate_layout(paths: Iterable[PurePosixPath]) -> None:
    saw_capability = False
    for path in paths:
        lowered = path.name.casefold()
        if lowered.endswith(_NESTED_ARCHIVE_SUFFIXES):
            raise CapabilityImportLayoutError("nested archives are not accepted")
        if lowered in {"__init__.py"} or path.suffix.casefold() in {".pyc", ".pyo"}:
            raise CapabilityImportLayoutError("Python packages and bytecode are not accepted")
        if _allowed_path(path):
            saw_capability = saw_capability or path.parts[0] in {"tools", "skills"}
            continue
        raise CapabilityImportLayoutError("source contains an unsupported path")
    if not saw_capability:
        raise CapabilityImportLayoutError("source contains no tool or skill")


def _allowed_path(path: PurePosixPath) -> bool:
    parts = path.parts
    if len(parts) == 2 and parts[0] == "tools" and path.suffix == ".py":
        return path.stem.isidentifier() and not path.name.startswith("_")
    if len(parts) >= 3 and parts[0] == "skills" and _safe_name(parts[1]):
        return (parts[2] == "SKILL.md" and len(parts) == 3) or (
            len(parts) >= 4 and parts[2] in _RESOURCE_DIRS
        )
    return len(parts) == 1 and (path.name in _ROOT_FILES or path.name.startswith("LICENSE"))


def _safe_name(name: str) -> bool:
    return name.isidentifier() and not name.startswith("_")


def _copy_candidates(
    source: Path,
    candidates: Iterable[_Candidate],
    destination: Path,
    limits: CapabilityImportLimits,
) -> list[CapabilityImportFile]:
    files: list[CapabilityImportFile] = []
    copied = 0
    archive_context = zipfile.ZipFile(source) if source.is_file() else nullcontext(None)
    with archive_context as archive:
        for candidate in candidates:
            target = destination.joinpath(*candidate.path.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stream: IO[bytes]
            if archive is None:
                if not isinstance(candidate.source, Path):
                    raise CapabilityImportSourceError("invalid local source entry")
                stream = candidate.source.open("rb")
            else:
                if not isinstance(candidate.source, zipfile.ZipInfo):
                    raise CapabilityImportSourceError("invalid ZIP source entry")
                stream = archive.open(candidate.source)
            with stream:
                digest, size = _write_regular(stream, target, limits, copied)
            if size != candidate.size:
                raise CapabilityImportSourceError("source entry size changed during intake")
            copied += size
            files.append(CapabilityImportFile(candidate.path.as_posix(), digest, size))
    return sorted(files, key=lambda entry: entry.path)


def _write_regular(
    stream: IO[bytes], target: Path, limits: CapabilityImportLimits, copied: int
) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            while chunk := stream.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > limits.max_file_bytes or copied + size > limits.max_expanded_bytes:
                    raise CapabilityImportLimitError("expanded content exceeds configured limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    target.chmod(0o600)
    return digest.hexdigest(), size


def _copy_quarantine(
    source: Path, target: Path, archive_bytes: int, limits: CapabilityImportLimits
) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    if target.exists():
        return
    if source.is_dir():
        target.write_text(_tree_digest(_preflight_directory(source, limits)), encoding="ascii")
        target.chmod(0o600)
        return
    if archive_bytes > limits.max_compressed_bytes:
        raise CapabilityImportLimitError("compressed source exceeds configured limit")
    with source.open("rb") as stream:
        raw_limits = CapabilityImportLimits(
            max_compressed_bytes=limits.max_compressed_bytes,
            max_expanded_bytes=limits.max_compressed_bytes,
            max_file_bytes=limits.max_compressed_bytes,
            max_files=limits.max_files,
            max_depth=limits.max_depth,
            max_compression_ratio=limits.max_compression_ratio,
        )
        _write_regular(stream, target, raw_limits, 0)


def _inventory(staging: Path) -> tuple[CapabilityImportFile, ...]:
    return tuple(
        CapabilityImportFile(
            path.relative_to(staging).as_posix(), _file_digest(path), path.stat().st_size
        )
        for path in sorted(staging.rglob("*"))
        if path.is_file()
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(candidates: Iterable[_Candidate]) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(candidates, key=lambda item: item.path.as_posix()):
        digest.update(candidate.path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(candidate.size).encode("ascii"))
        digest.update(b"\0")
        if not isinstance(candidate.source, Path):
            raise CapabilityImportSourceError("invalid local source entry")
        digest.update(_file_digest(candidate.source).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
