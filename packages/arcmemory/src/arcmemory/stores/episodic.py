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
            "(event_id, ts, scope, kind, text, hash, classification, refs, seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "SELECT event_id, ts, scope, kind, text, hash, classification, refs "
            "FROM episodic WHERE scope = ? ORDER BY seq",
            (scope_key,),
        ).fetchall()
        return [
            Event(
                event_id=r[0],
                ts=r[1],
                scope=r[2],
                kind=r[3],
                text=r[4],
                hash=r[5] or "",
                # Preserve an explicit empty label (fail-closed at federal); only a
                # legacy NULL falls back to the default.
                classification="unclassified" if r[6] is None else r[6],
                refs=json.loads(r[7]) if r[7] else [],
            )
            for r in rows
        ]

    def _next_seq(self, scope_key: str) -> int:
        """Next monotonic sequence number for ``scope_key`` (starts at 0)."""
        conn = self._db.connect()
        (current,) = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM episodic WHERE scope = ?", (scope_key,)
        ).fetchone()
        return int(current) + 1


__all__ = ["EpisodicStore"]
