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

from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from arcmemory.mdfile import parse_document
from arcmemory.types import Event

# Curated markdown source directories, in a fixed order (determinism).
_SOURCE_SUBDIRS = ("entities", "insights", "procedures", "daily-log")


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
    mem_dir: Path, workspace: Path, events: Iterable[Event]
) -> Iterator[SourceChunk]:
    """Yield every curated file chunk (fixed order) then every raw-event chunk."""
    for subdir in _SOURCE_SUBDIRS:
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            fm, _ = parse_document(text)
            rel = str(path.relative_to(workspace))
            yield SourceChunk(
                chunk_id=f"file:{rel}",
                source_path=rel,
                text=text,
                # A genuinely missing label passes through empty — the no-read-up
                # gate decides fail-closed (federal) vs default (personal), never
                # the index (SDD §8).
                classification=str(fm.get("classification") or ""),
                mtime=path.stat().st_mtime,
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
