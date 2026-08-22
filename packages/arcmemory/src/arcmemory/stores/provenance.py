"""Canonical item dedup + per-provenance classification gate (SPEC-073 COMP-011).

Same bytes arriving from two sources dedup into ONE
canonical item keyed by content hash, but each source's provenance carries
its own classification. Retrieval must gate PER-PROVENANCE, never on the
item's highest label, so a stricter copy from one source can never suppress
a looser copy of the same bytes from another.
"""

from __future__ import annotations

from datetime import UTC, datetime

from arctrust.classification import Classification, dominates, parse_classification

from arcmemory.db import MemoryDB
from arcmemory.types import Provenance


class ProvenanceStore:
    """Upsert canonical items and gate their provenances per-source."""

    def __init__(self, db: MemoryDB) -> None:
        self._db = db

    def record(self, content_hash: str, provenance: Provenance) -> str:
        """Upsert the canonical item (item_id == content_hash) and append this provenance.

        Idempotent on ``(item_id, source, external_id)`` -- recording the same
        provenance twice is a no-op. Returns the (stable) item_id.
        """
        conn = self._db.connect()
        conn.execute(
            "INSERT OR IGNORE INTO items (item_id, content_hash, first_seen) VALUES (?, ?, ?)",
            (content_hash, content_hash, datetime.now(UTC).isoformat()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO item_provenances "
            "(item_id, source, external_id, classification) VALUES (?, ?, ?, ?)",
            (content_hash, provenance.source, provenance.external_id, provenance.classification),
        )
        conn.commit()
        return content_hash

    def provenances(self, item_id: str) -> list[Provenance]:
        """Every provenance recorded against ``item_id``, unfiltered."""
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT source, external_id, classification FROM item_provenances WHERE item_id = ?",
            (item_id,),
        ).fetchall()
        return [
            Provenance(source=row[0], external_id=row[1], classification=row[2]) for row in rows
        ]

    def readable_provenances(
        self, item_id: str, *, clearance: Classification, strict: bool
    ) -> list[Provenance]:
        """The subset of provenances whose classification ``clearance`` dominates.

        The per-provenance no-read-up gate (reuses ``arctrust`` ``dominates`` /
        ``parse_classification`` -- no comparator defined here). An unparseable
        label fails closed (excluded), never defaults to readable.
        """
        readable: list[Provenance] = []
        for provenance in self.provenances(item_id):
            try:
                resource = parse_classification(provenance.classification, strict=strict)
            except ValueError:
                continue
            if dominates(clearance, resource):
                readable.append(provenance)
        return readable


__all__ = ["ProvenanceStore"]
