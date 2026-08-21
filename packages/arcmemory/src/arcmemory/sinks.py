"""Router sinks — wire connected sources onto existing memory paths (SPEC-073).

No new memory machinery lives here: each sink adapts a connected source's
normalized records onto an already-audited, already-tested capture path.
"""

from __future__ import annotations

from arcmemory.capture import FastCapture
from arcmemory.types import SourceRecord


class MemorySink:
    """Routes a source's records into the memory home via ``FastCapture``.

    Each record becomes an episodic event through the EXISTING capture
    pipeline, so it is recallable through the normal Retriever/SurfaceIndex
    channel — no parallel ingestion path to keep in sync.
    """

    def __init__(self, capture: FastCapture) -> None:
        self._capture = capture

    async def __call__(self, source_id: str, records: list[SourceRecord]) -> None:
        """Capture every record in ``records`` (``source_id`` identifies the caller)."""
        for record in records:
            self._capture.capture(
                record.text,
                kind=record.kind,
                salience=record.salience,
                classification=record.classification,
            )


__all__ = ["MemorySink"]
