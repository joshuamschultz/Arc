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

import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from arcokf import IndexEntry, OKFValidationError
from pydantic import BaseModel

from arcmemory.collection_index import memory_maintainer, routing_text
from arcmemory.mdfile import card_files, parse_document
from arcmemory.security import dominating_classification
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

#: Longest text handed to the EMBEDDER per chunk. FTS keeps the full (≤60 KB)
#: window; an embedding model only reads its first few hundred to few thousand
#: tokens, so sending more is wasted spend (and some providers reject it).
#: ~2,000 tokens of English covers every embedding model Arc wires today.
EMBED_TEXT_MAX_CHARS = 8_000

#: Entity types that are connector bookkeeping, not knowledge: connected-source
#: routing (``mapping``), source registrations (``source``), blob folder
#: inventories and DB table schemas. Their only readers are the operator/ingest
#: code via ``store.read`` — they carry no recall value and must never be
#: searchable or embedded. The ONE list both the incremental index and the
#: deterministic rebuild filter on (both walk ``iter_source_chunks``).
BOOKKEEPING_ENTITY_TYPES = frozenset({"mapping", "source", "blob_folder", "db_table"})

_FOLDER_COUNT_RE = re.compile(r" - \d+ docs?$")
_LINK_TARGET_RE = re.compile(r"\]\(([^)]+)\)")

# Human-readable frontmatter fields worth embedding alongside the body.
_HEADER_FIELDS = ("name", "aliases", "summary", "description", "tags")


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
    excluded: set[str] = set()
    for subdir in _SOURCE_SUBDIRS:
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in card_files(directory):
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
                fm, body = parse_document(text)
            except OKFValidationError:
                classification = ""
            else:
                if fm.get("entity_type") in BOOKKEEPING_ENTITY_TYPES:
                    excluded.add(path.relative_to(mem_dir).as_posix())
                    continue
                classification = str(fm.get("classification") or "")
                text = render_index_text(fm, body)
            yield from bounded_chunks(chunk_id, rel, text, classification, mtime)
    yield from _routing_chunks(mem_dir, workspace, excluded)
    for event in events:
        yield from bounded_chunks(
            f"event:{event.event_id}",
            "episodic",
            event.text,
            # The stored stream label — NOT a literal — so a classified capture is
            # gated on the raw-stream channel too (empty => fail-closed).
            event.classification,
            _iso_epoch(event.ts),
        )


def _routing_chunks(mem_dir: Path, workspace: Path, excluded: set[str]) -> Iterator[SourceChunk]:
    """One chunk per verified folder ``index.md``: its routing lines and nothing else.

    A folder index is a derived routing artifact, never part of the inventory it
    describes. A reader may use it only after the owning maintainer produced a
    canonical file whose digest sidecar still matches (O(1) per folder, never a
    re-hash of the documents); tampering therefore degrades to ordinary document
    recall instead of becoming trusted instructions. Only headings and listing
    lines are embedded: never the root's ``okf_version`` frontmatter, the digest
    sidecar, or the dirty journal. Lines linking a bookkeeping card (``excluded``)
    are dropped, and folder document counts are stripped so a new card does not
    re-embed the root listing.
    """
    maintainer = memory_maintainer(mem_dir)
    for sub in ("", *_SOURCE_SUBDIRS):
        folder = mem_dir / sub if sub else mem_dir
        validation = maintainer.validate(folder)
        if not validation.valid:
            continue
        index_path = folder / "index.md"
        rel = index_path.relative_to(workspace).as_posix()
        lines = [
            _FOLDER_COUNT_RE.sub("", line)
            for line in routing_text(index_path.read_text(encoding="utf-8")).splitlines()
            if not _links_excluded(line, excluded, sub)
        ]
        yield from bounded_chunks(
            f"file:{rel}",
            rel,
            "\n".join(lines),
            _routing_label(validation.entries, excluded, sub),
            index_path.stat().st_mtime,
        )


def _routing_label(entries: tuple[IndexEntry, ...], excluded: set[str], folder: str) -> str:
    """The label a folder's routing chunk is gated on: its most restrictive listed document.

    A listed document with no label makes the chunk unlabeled (""), which the
    no-read-up gate fails closed on at federal, exactly as for an unlabeled card.
    Bookkeeping cards are not listed in the chunk, so they do not count.
    """
    listed = [
        entry
        for entry in entries
        if not entry.is_folder and f"{folder}/{entry.path}".lstrip("/") not in excluded
    ]
    labels = [entry.classification for entry in listed]
    if any(not label for label in labels):
        return ""
    return dominating_classification(labels)


def _links_excluded(line: str, excluded: set[str], folder: str) -> bool:
    match = _LINK_TARGET_RE.search(line)
    if match is None:
        return False
    target = unquote(match.group(1))
    return (f"{folder}/{target}" if folder else target) in excluded


def render_index_text(frontmatter: dict[str, Any], body: str) -> str:
    """The text a card contributes to the index: one human-field header + the body.

    Machine fields (``last_updated``, ``links_to``, ``entity_type``, ids, hashes,
    confidence) are dropped, so they are never embedded and a write that only
    bumps ``last_updated`` leaves the text — and therefore ``content_hash`` — the
    same. Only ``name``/``aliases``/``summary``/``description``/``tags`` survive.
    """
    parts: list[str] = []
    for key in _HEADER_FIELDS:
        value = frontmatter.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value:
            parts.append(f"{key}: {value}")
    header = " | ".join(parts)
    body = body.strip()
    return f"{header}\n\n{body}" if header and body else header or body


def embed_text(text: str) -> str:
    """``text`` capped at the embedder window; FTS keeps the full chunk text."""
    return text[:EMBED_TEXT_MAX_CHARS]


def bounded_chunks(
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
            units.extend(hard_split(para))
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


def hard_split(unit: str, *, max_bytes: int = MAX_CHUNK_BYTES) -> list[str]:
    """Split one over-budget paragraph into ``max_bytes`` pieces, char-aligned."""
    pieces: list[str] = []
    current = ""
    current_bytes = 0
    for ch in unit:
        ch_bytes = len(ch.encode("utf-8"))
        if current and current_bytes + ch_bytes > max_bytes:
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


__all__ = [
    "BOOKKEEPING_ENTITY_TYPES",
    "EMBED_TEXT_MAX_CHARS",
    "MAX_CHUNK_BYTES",
    "SourceChunk",
    "bounded_chunks",
    "embed_text",
    "hard_split",
    "iter_source_chunks",
    "render_index_text",
]
