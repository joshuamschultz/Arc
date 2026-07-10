"""Episodic store — the raw event stream (SQLite) + daily-log bullets (markdown).

Two writes per event, both append-only and order-preserving:

* the raw row goes to the per-agent ``episodic`` table with a per-scope monotonic
  ``seq`` (so adjacency for enrichment survives even if timestamps collide);
* a human-readable bullet goes to ``memory/daily-log/YYYY-MM-DD.md`` (glass-box,
  the curated truth a human can read/edit).

Absorbs the old ``bio_memory`` daily-notes / ``working.md`` behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcmemory.db import MemoryDB
from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.security import dominating_classification
from arcmemory.types import Event


class EpisodicStore:
    """Append raw events + daily-log bullets for one scope."""

    def __init__(self, db: MemoryDB, workspace: Path) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._daily_dir = self._workspace / "memory" / "daily-log"

    def append(self, event: Event) -> None:
        """Persist one raw event to the stream with a per-scope monotonic seq."""
        conn = self._db.connect()
        seq = self._next_seq(event.scope)
        conn.execute(
            "INSERT OR REPLACE INTO episodic "
            "(event_id, ts, scope, kind, text, hash, classification, refs, seq, "
            "salience, entities) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.ts,
                event.scope,
                event.kind,
                event.text,
                event.hash,
                event.classification,
                json.dumps(event.refs),
                seq,
                event.salience,
                json.dumps(event.entities),
            ),
        )
        conn.commit()

    def append_bullet(self, event: Event) -> Path:
        """Append a bullet for ``event`` to today's daily-log; return the file path.

        The day-file carries a frontmatter ``classification`` = the dominating label of
        every bullet written to it, so the glass-box file channel is gated exactly like
        the raw stream (no unclassified-plaintext leak of a classified capture).
        """
        day = event.ts[:10]  # YYYY-MM-DD prefix of the ISO timestamp
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        path = self._daily_dir / f"{day}.md"
        prior_label, body = "", ""
        if path.exists():
            fm, body = parse_document(path.read_text(encoding="utf-8"))
            prior_label = str(fm.get("classification", ""))
        label = dominating_classification([prior_label, event.classification])
        bullet = f"- {event.ts} [{event.kind}] {event.text}"
        new_body = f"{body.rstrip()}\n{bullet}" if body.strip() else bullet
        atomic_write_text(path, render_document({"classification": label}, new_body))
        return path

    def events(self, scope_key: str) -> list[Event]:
        """Return all events for a scope, in stream (seq) order."""
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT event_id, ts, scope, kind, text, hash, classification, refs, "
            "salience, entities FROM episodic WHERE scope = ? ORDER BY seq",
            (scope_key,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def page(self, scope_key: str, *, limit: int, offset: int) -> list[Event]:
        """Return one page of a scope's events, newest first (for the operator view)."""
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT event_id, ts, scope, kind, text, hash, classification, refs, "
            "salience, entities FROM episodic WHERE scope = ? "
            "ORDER BY seq DESC LIMIT ? OFFSET ?",
            (scope_key, limit, offset),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count(self, scope_key: str) -> int:
        """Total number of events stored for a scope."""
        conn = self._db.connect()
        (total,) = conn.execute(
            "SELECT COUNT(*) FROM episodic WHERE scope = ?", (scope_key,)
        ).fetchone()
        return int(total)

    def get(self, scope_key: str, event_id: str) -> Event | None:
        """Fetch a single event by id within a scope (None if absent)."""
        conn = self._db.connect()
        row = conn.execute(
            "SELECT event_id, ts, scope, kind, text, hash, classification, refs, "
            "salience, entities FROM episodic WHERE scope = ? AND event_id = ?",
            (scope_key, event_id),
        ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def update_text(self, scope_key: str, event_id: str, text: str) -> bool:
        """Replace an event's text; return whether a row was affected."""
        conn = self._db.connect()
        cursor = conn.execute(
            "UPDATE episodic SET text = ? WHERE scope = ? AND event_id = ?",
            (text, scope_key, event_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def update_salience(self, scope_key: str, event_id: str, salience: float) -> bool:
        """Set an event's salience (the decay-slowing / importance field)."""
        conn = self._db.connect()
        cursor = conn.execute(
            "UPDATE episodic SET salience = ? WHERE scope = ? AND event_id = ?",
            (salience, scope_key, event_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def delete(self, scope_key: str, event_id: str) -> bool:
        """Remove an event by id; return whether a row was affected."""
        conn = self._db.connect()
        cursor = conn.execute(
            "DELETE FROM episodic WHERE scope = ? AND event_id = ?",
            (scope_key, event_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_event(r: tuple[Any, ...]) -> Event:
        """Hydrate one episodic row into an ``Event`` (column order matches the SELECTs)."""
        return Event(
            event_id=str(r[0]),
            ts=str(r[1]),
            scope=str(r[2]),
            kind=str(r[3]),
            text=str(r[4]),
            hash=str(r[5]) if r[5] else "",
            # Preserve an explicit empty label (fail-closed at federal); only a
            # legacy NULL falls back to the default.
            classification="unclassified" if r[6] is None else str(r[6]),
            refs=json.loads(r[7]) if r[7] else [],
            salience=float(r[8]) if r[8] is not None else 0.0,
            entities=json.loads(r[9]) if r[9] else [],
        )

    def _next_seq(self, scope_key: str) -> int:
        """Next monotonic sequence number for ``scope_key`` (starts at 0)."""
        conn = self._db.connect()
        (current,) = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM episodic WHERE scope = ?", (scope_key,)
        ).fetchone()
        return int(current) + 1


__all__ = ["EpisodicStore"]
