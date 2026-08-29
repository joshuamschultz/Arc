"""Source-chunk enumeration — the single walk of everything that gets indexed.

Both the incremental surface index (``SurfaceIndex._collect_chunks``, hash-gated)
and the bulk rebuild (``IndexRebuilder._rebuild_chunks``, deterministic) must chunk
the *same* things the *same* way: every curated markdown file under the fixed source
subdirs, then every raw episodic event. The classification-labelling rule below is
security-relevant (SDD §8) — a genuinely missing label passes through **empty** so
the no-read-up gate, never the index, decides fail-closed vs default. Defining that
walk once means the two callers cannot drift on it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path

from arcokf import validate_collection_index
from pydantic import BaseModel

from arcmemory.mdfile import parse_document
from arcmemory.types import Event

# Curated markdown source directories, in a fixed order (determinism).
_SOURCE_SUBDIRS = ("entities", "insights", "procedures", "events", "daily-log")


class SourceChunk(BaseModel):
    """One indexable unit — a source file or a raw event — before gate-hashing.

    ``mtime`` is the file/event modification time; the incremental index keeps it,
    the deterministic rebuild deliberately discards it (writes ``None``) so a
    byte-identical rebuild does not depend on wall-clock file stats.
    """

    chunk_id: str
    source_path: str
    text: str
    classification: str
    mtime: float


def iter_source_chunks(
    mem_dir: Path,
    workspace: Path,
    events: Iterable[Event],
    *,
    skip_file: Callable[[str, float], bool] | None = None,
) -> Iterator[SourceChunk]:
    """Yield every curated file chunk (fixed order) then every raw-event chunk.

    ``skip_file`` (turn-path bound, H-REG-1) lets a caller avoid reading a
    markdown card's full body at all: called with ``(chunk_id, on-disk mtime)``
    — a cheap ``stat()``, already needed either way — right before the file
    would be opened. When it returns ``True`` the file is skipped entirely
    (never read, never yielded), which only ``SurfaceIndex._collect_chunks``
    uses, and only for a chunk it can already PROVE is unchanged (and, when
    relevant, already embedded) from data it has independent of this file's
    body. ``None`` (the default, including every call from the deterministic
    ``IndexRebuilder``) preserves the full, unconditional walk exactly as
    before.
    """
    # ``index.md`` is a derived routing artifact, never part of the inventory it
    # describes.  A reader may use it only after the owning collection service has
    # produced a canonical, digest-verified file; tampering therefore degrades to
    # ordinary document recall instead of becoming trusted instructions.
    collection_index = mem_dir / "index.md"
    if validate_collection_index(collection_index, mem_dir).valid:
        index_text = collection_index.read_text(encoding="utf-8")
        yield SourceChunk(
            chunk_id="file:" + collection_index.relative_to(workspace).as_posix(),
            source_path=collection_index.relative_to(workspace).as_posix(),
            # Keep the machine comments on disk for verification, but index the
            # compact human routing lines so one large collection cannot consume
            # the entire bounded recall budget as a single chunk.
            text="\n".join(
                line for line in index_text.splitlines() if line.startswith(("# ", "- ["))
            ),
            classification="",
            mtime=collection_index.stat().st_mtime,
        )
    for subdir in _SOURCE_SUBDIRS:
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            # as_posix() keeps source identifiers stable across OSes — on Windows
            # str() would emit backslashes and fork the chunk_id from Unix.
            rel = path.relative_to(workspace).as_posix()
            chunk_id = f"file:{rel}"
            mtime = path.stat().st_mtime
            if skip_file is not None and skip_file(chunk_id, mtime):
                continue
            text = path.read_text(encoding="utf-8")
            fm, _ = parse_document(text)
            yield SourceChunk(
                chunk_id=chunk_id,
                source_path=rel,
                text=text,
                # A genuinely missing label passes through empty — the no-read-up
                # gate decides fail-closed (federal) vs default (personal), never
                # the index (SDD §8).
                classification=str(fm.get("classification") or ""),
                mtime=mtime,
            )
    for event in events:
        yield SourceChunk(
            chunk_id=f"event:{event.event_id}",
            source_path="episodic",
            text=event.text,
            # The stored stream label — NOT a literal — so a classified capture is
            # gated on the raw-stream channel too (empty => fail-closed).
            classification=event.classification,
            mtime=_iso_epoch(event.ts),
        )


def _iso_epoch(ts: str) -> float:
    """Best-effort epoch seconds from an ISO timestamp (0.0 when unparseable)."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


__all__ = ["SourceChunk", "iter_source_chunks"]
