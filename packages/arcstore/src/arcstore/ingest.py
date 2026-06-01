"""StoreIngest — backfill + tail the durable files into the backend (FR-3).

arcstore is a **pure file-tailer**: it owns no sink in ``arctrust.audit.emit()``
and no live wire. It only reads the two durable files — the always-on spool
(operational telemetry) and the arctrust WORM (signed audit chain) — and mirrors
them into a queryable backend.

Crash-safety mirrors ``SessionIndex``: a per-file byte cursor is persisted in
the backend; on restart the scan resumes from the last cursor rather than
re-reading. Replay is harmless because every row is keyed by a content-derived
id (``INSERT OR IGNORE``), so at-least-once ingest never duplicates rows
(AC-3.3). The WORM is verified on ingest (``arctrust.verify_chain``) and each
mirrored row carries the ``verified`` result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from arctrust.audit import verify_chain

from arcstore.backends.base import AUDIT_TABLE, StorageBackend, table_for_kind
from arcstore.spool import read_complete_segments, read_from_offset

_logger = logging.getLogger("arcstore.ingest")

WORM_ACTIVE_FILENAME = "audit-chain.jsonl"
"""Filename of the active (non-rotated) WORM chain — single source for arccli + arcstore."""


class StoreIngest:
    """Backfills, then tails, the spool + WORM into a StorageBackend."""

    def __init__(
        self,
        backend: StorageBackend,
        *,
        spool_dir: Path,
        worm_dir: Path,
        worm_public_key: bytes | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self._backend = backend
        self._spool_dir = Path(spool_dir)
        self._worm_dir = Path(worm_dir)
        self._worm_public_key = worm_public_key
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def backfill(self) -> None:
        """One full scan from each file's persisted cursor (UC-1/UC-2 recovery)."""
        await self.scan_once()

    async def start(self) -> None:
        """Backfill, then start the background tail loop (managed task)."""
        await self.backfill()
        self._stop.clear()
        self._task = asyncio.create_task(self._tail_loop(), name="arcstore_ingest_tail")

    async def stop(self) -> None:
        """Stop the tail loop cleanly (no orphaned task)."""
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _tail_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop.wait()), timeout=self._poll_interval
                )
                break
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            try:
                await self.scan_once()
            except Exception:  # reason: fail-open — log + continue tailing
                _logger.exception("StoreIngest.scan_once raised; continuing")

    # -- scan --------------------------------------------------------------

    async def scan_once(self) -> None:
        """Ingest new lines from every spool + WORM file since the last cursor."""
        await self._scan_spool()
        await self._scan_worm()

    async def _scan_spool(self) -> None:
        for path in sorted(self._spool_dir.glob("operational-*.jsonl")):
            cursor = f"spool:{path.name}"
            offset = await self._backend.get_cursor(cursor)
            records, new_offset = read_from_offset(path, offset)
            if records:
                by_table: dict[str, list[tuple[str, dict[str, Any]]]] = {}
                for rec in records:
                    table = table_for_kind(rec.kind)
                    by_table.setdefault(table, []).append((rec.record_id, rec.model_dump()))
                for table, rows in by_table.items():
                    await self._backend.upsert_many(table, rows)
            if new_offset != offset:
                await self._backend.set_cursor(cursor, new_offset)

    async def _scan_worm(self) -> None:
        active = self._worm_dir / WORM_ACTIVE_FILENAME
        if not active.exists():
            return
        for path in sorted(self._worm_dir.glob("audit-chain*.jsonl")):
            # Verify each segment independently so the `verified` flag reflects
            # that segment's own integrity, not just the active file's verdict.
            seg_verified = (
                verify_chain(path, self._worm_public_key)
                if self._worm_public_key is not None
                else False
            )
            cursor = f"worm:{path.name}"
            offset = await self._backend.get_cursor(cursor)
            records, new_offset = _read_worm_from_offset(path, offset)
            if records:
                rows = [(_worm_key(r), _worm_row(r, seg_verified)) for r in records]
                await self._backend.upsert_many(AUDIT_TABLE, rows)
            if new_offset != offset:
                await self._backend.set_cursor(cursor, new_offset)


# ---------------------------------------------------------------------------
# WORM record reading + mapping
# ---------------------------------------------------------------------------


def _read_worm_from_offset(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read complete WORM records from ``offset``; leave a torn tail unconsumed."""
    chunks, new_offset = read_complete_segments(path, offset)
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        try:
            records.append(json.loads(chunk))
        except json.JSONDecodeError:
            _logger.warning("StoreIngest skipping corrupt WORM line in %s", path)
    return records, new_offset


def _worm_key(record: dict[str, Any]) -> str:
    # event_hash is unique + content-derived → idempotent ingest key.
    return str(record.get("event_hash", ""))


def _worm_row(record: dict[str, Any], verified: bool) -> dict[str, Any]:
    event = record.get("event", {})
    return {
        "seq": record.get("seq"),
        "ts": event.get("ts"),
        "actor_did": event.get("actor_did"),
        "action": event.get("action"),
        "target": event.get("target"),
        "outcome": event.get("outcome"),
        "event_hash": record.get("event_hash"),
        "prev_hash": record.get("prev_hash"),
        "signature": record.get("signature"),
        "verified": verified,
    }
