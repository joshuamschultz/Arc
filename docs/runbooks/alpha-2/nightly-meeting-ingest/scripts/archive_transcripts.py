"""Archive new meeting transcripts for nightly-meeting-ingest. No model, no network.

Inputs (set by the script-node runner, never spliced into a command line):
  ARC_UPSTREAM           JSON of ancestor outputs; reads filter_new.new_files.
  ARC_WORKDIR            the run's shared workspace (destination default).
  ARC_MEETINGS_SOURCE    local root where the Dropbox /Meetings folder is available
                         (a synced or mounted copy). Required.
  ARC_ARCHIVE_ROOT       optional destination root; default
                         $ARC_WORKDIR/crm/meetings/transcripts.
  ARC_ARCHIVE_MAX_FILE_BYTES  optional per-file cap (default 100 MiB).

Each file is copied byte for byte to <archive root>/<meeting>/<file name>, where
<meeting> is the entry's first folder under /Meetings (or the file stem when the
file sits at the top). Output is one JSON object on stdout.

Safe to re-run: an identical copy is skipped, a different existing copy is a
conflict and is never overwritten, and writes are atomic. A file that fails is
reported on stderr after the rest have been archived, and the script exits 1 so
the runner retries only what is left. Work is bounded by MAX_FILES and the
per-file byte cap.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 500
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
SOURCE_PREFIX = "Meetings"


class ArchiveError(Exception):
    """One file (or the whole request) cannot be archived."""


def _entry_path(entry: Any) -> str:
    """The path inside a new_files entry, which is a string or an object with ``path``."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("path", "path_display"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    raise ArchiveError(f"unreadable new_files entry: {str(entry)[:80]!r}")


def _relative_parts(raw: str) -> tuple[str, ...]:
    """Path components below the meetings root; refuses anything that climbs out."""
    parts = [p for p in PurePosixPath(raw).parts if p != "/"]
    if parts and parts[0] == SOURCE_PREFIX:
        parts = parts[1:]
    if not parts or any(p in ("..", ".", "") for p in parts):
        raise ArchiveError(f"path escapes the meetings root: {raw!r}")
    return tuple(parts)


def _meeting_name(parts: tuple[str, ...]) -> str:
    return parts[0] if len(parts) > 1 else Path(parts[0]).stem


def _confined_source(source_root: Path, parts: tuple[str, ...], raw: str) -> Path:
    """The real file under the source root. Symlinks that leave the root are refused."""
    target = source_root.joinpath(*parts).resolve()
    if not target.is_relative_to(source_root):
        raise ArchiveError(f"path escapes the meetings root: {raw!r}")
    if not target.is_file():
        raise ArchiveError(f"source file missing: {raw!r}")
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    shutil.copyfile(source, partial)
    os.replace(partial, destination)


def _archive_one(
    raw: str, source_root: Path, archive_root: Path, max_bytes: int
) -> tuple[str, bool]:
    """Archive one entry. Returns (archive-relative path, whether it was newly copied)."""
    parts = _relative_parts(raw)
    source = _confined_source(source_root, parts, raw)
    if source.stat().st_size > max_bytes:
        raise ArchiveError(f"file too large (> {max_bytes} bytes): {raw!r}")
    meeting = _meeting_name(parts)
    destination = archive_root / meeting / parts[-1]
    relative = f"{meeting}/{parts[-1]}"
    if destination.exists():
        if _sha256(destination) == _sha256(source):
            return relative, False
        raise ArchiveError(f"conflict: an archived copy of {raw!r} already differs")
    _copy_atomic(source, destination)
    return relative, True


def _new_files(upstream: dict[str, Any]) -> list[Any]:
    produced = upstream.get("filter_new")
    files = produced.get("new_files") if isinstance(produced, dict) else None
    if not isinstance(files, list):
        raise ArchiveError("filter_new.new_files is missing or not a list")
    if len(files) > MAX_FILES:
        raise ArchiveError(f"too many files in one night: {len(files)} > {MAX_FILES}")
    return files


def main() -> int:
    try:
        upstream = json.loads(os.environ.get("ARC_UPSTREAM", "{}"))
        files = _new_files(upstream if isinstance(upstream, dict) else {})
        source_env = os.environ.get("ARC_MEETINGS_SOURCE")
        if not source_env:
            raise ArchiveError("ARC_MEETINGS_SOURCE is not set")
        source_root = Path(source_env).resolve()
        workdir = Path(os.environ.get("ARC_WORKDIR", "."))
        archive_root = Path(
            os.environ.get("ARC_ARCHIVE_ROOT") or workdir / "crm" / "meetings" / "transcripts"
        )
        max_bytes = int(os.environ.get("ARC_ARCHIVE_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES))
        raws = sorted({_entry_path(entry) for entry in files})
    except (ArchiveError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    archived: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []
    for raw in raws:
        try:
            relative, copied = _archive_one(raw, source_root, archive_root, max_bytes)
        except (ArchiveError, OSError) as exc:
            failures.append(str(exc))
            continue
        (archived if copied else skipped).append(relative)

    if failures:
        sys.stderr.write("; ".join(failures)[:2000] + "\n")
        return 1
    result = {
        "status": "archived",
        "count": len(archived) + len(skipped),
        "archived": sorted(archived),
        "skipped": sorted(skipped),
    }
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
