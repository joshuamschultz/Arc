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

from arcokf import OKFValidationError, validate_collection_index
from pydantic import BaseModel

from arcmemory.mdfile import parse_document
from arcmemory.types import Event

# Curated markdown source directories, in a fixed order (determinism).
_SOURCE_SUBDIRS = ("entities", "insights", "procedures", "events", "daily-log")

#: Per-chunk byte ceiling. An append-only daily-log (or a giant ingested event)
#: grows without bound, and the Postgres index backend feeds each chunk's text to
#: ``to_tsvector``, which rejects any single string over 1,048,575 bytes with
#: ``ProgramLimitExceededError`` — one such chunk aborted the whole refresh pass
#: and left every OTHER chunk unembedded, so the next poll re-embedded the entire
#: corpus, forever. Splitting every source into windows this far under the hard
#: limit means no single chunk can ever overflow the tsvector, keeps the vector
#: (whose embedder truncates a huge blob to garbage anyway) meaningful, and bounds
#: what one changed window costs to re-embed. Window 0 keeps the plain ``file:``/
#: ``event:`` id — a small file is the one-window case, so existing ids never move.
MAX_CHUNK_BYTES = 60_000


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
        rel = collection_index.relative_to(workspace).as_posix()
        # Keep the machine comments on disk for verification, but index the compact
        # human routing lines only. A large inventory's routing lines can STILL run
        # to megabytes, though (a fleet with thousands of memory files), so this too
        # goes through the size bound — one collection index must never become a
        # single chunk that overflows the Postgres tsvector limit.
        routing_text = "\n".join(
            line for line in index_text.splitlines() if line.startswith(("# ", "- ["))
        )
        yield from _bounded_chunks(
            f"file:{rel}", rel, routing_text, "", collection_index.stat().st_mtime
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
            # A file OKF rejects — most importantly one past ``MAX_DOCUMENT_BYTES``,
            # which an append-only daily-log crosses as it grows — must NOT abort
            # the whole walk (and with it all of the agent's indexing). Degrade to
            # raw text with an EMPTY classification, which the no-read-up gate reads
            # as fail-closed (federal) / default (personal), and still window it so
            # the content stays searchable. Same blast-radius rule as the size split.
            try:
                fm, _ = parse_document(text)
                classification = str(fm.get("classification") or "")
            except OKFValidationError:
                classification = ""
            yield from _bounded_chunks(chunk_id, rel, text, classification, mtime)
    for event in events:
        yield from _bounded_chunks(
            f"event:{event.event_id}",
            "episodic",
            event.text,
            # The stored stream label — NOT a literal — so a classified capture is
            # gated on the raw-stream channel too (empty => fail-closed).
            event.classification,
            _iso_epoch(event.ts),
        )


def _bounded_chunks(
    base_id: str, source_path: str, text: str, classification: str, mtime: float
) -> Iterator[SourceChunk]:
    """Yield ``text`` as one or more ``SourceChunk`` windows, each ≤ ``MAX_CHUNK_BYTES``.

    Window 0 carries ``base_id`` unchanged (the common one-window case, so a small
    card's id never gains a suffix); further windows are ``base_id#1``, ``base_id#2``…
    The split is deterministic (a pure function of ``text``), so the byte-identical
    rebuild path yields identical chunks. An empty file still yields its single
    window, so window 0 always exists for the mtime-skip sentinel.
    """
    for i, window in enumerate(_split_windows(text)):
        yield SourceChunk(
            chunk_id=base_id if i == 0 else f"{base_id}#{i}",
            source_path=source_path,
            text=window,
            classification=classification,
            mtime=mtime,
        )


def _split_windows(text: str) -> list[str]:
    """Pack paragraphs into ``\\n\\n``-joined windows, each ≤ ``MAX_CHUNK_BYTES`` bytes.

    A paragraph that alone exceeds the budget is hard-split on character
    boundaries (never mid-multibyte-char). Text at or under the budget returns as
    a single window, so the overwhelming common case is a one-element list.
    """
    if len(text.encode("utf-8")) <= MAX_CHUNK_BYTES:
        return [text]
    units: list[str] = []
    for para in text.split("\n\n"):
        if len(para.encode("utf-8")) > MAX_CHUNK_BYTES:
            units.extend(_hard_split(para))
        else:
            units.append(para)
    windows: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for unit in units:
        unit_bytes = len(unit.encode("utf-8")) + (2 if current else 0)  # "\n\n" join cost
        if current and current_bytes + unit_bytes > MAX_CHUNK_BYTES:
            windows.append("\n\n".join(current))
            current, current_bytes = [], 0
            unit_bytes = len(unit.encode("utf-8"))
        current.append(unit)
        current_bytes += unit_bytes
    if current:
        windows.append("\n\n".join(current))
    return windows or [""]


def _hard_split(unit: str) -> list[str]:
    """Split one over-budget paragraph into byte-bounded pieces, char-aligned."""
    pieces: list[str] = []
    current = ""
    current_bytes = 0
    for ch in unit:
        ch_bytes = len(ch.encode("utf-8"))
        if current and current_bytes + ch_bytes > MAX_CHUNK_BYTES:
            pieces.append(current)
            current, current_bytes = "", 0
        current += ch
        current_bytes += ch_bytes
    if current:
        pieces.append(current)
    return pieces


def _iso_epoch(ts: str) -> float:
    """Best-effort epoch seconds from an ISO timestamp (0.0 when unparseable)."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


__all__ = ["MAX_CHUNK_BYTES", "SourceChunk", "iter_source_chunks"]
