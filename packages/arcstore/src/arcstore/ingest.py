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
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctrust.audit import verify_chain

from arcstore.backends.base import (
    AUDIT_TABLE,
    SKILL_BODIES_TABLE,
    SKILL_CANDIDATES_TABLE,
    StorageBackend,
    table_for_kind,
)
from arcstore.spool import read_complete_segments, read_from_offset

_logger = logging.getLogger("arcstore.ingest")

WORM_ACTIVE_FILENAME = "audit-chain.jsonl"
"""Filename of the active (non-rotated) WORM chain — single source for arccli + arcstore."""

SKILLS_WORM_RELPATH = Path("..") / ".audit" / "skills.worm"
"""Skills WORM chain relative to the agent workspace (written by arcagent skills runtime)."""

# Path-safe candidate ids, mirroring arcskill.improver.candidate_store (ASI-02/ASI06
# defense): a poisoned manifest id must never drive a read outside candidates/.
_SAFE_CANDIDATE_ID_RE = re.compile(r"^[a-f0-9-]{1,40}$|^seed$")


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
        workspace_dir: Path | None = None,
    ) -> None:
        self._backend = backend
        self._spool_dir = Path(spool_dir)
        self._worm_dir = Path(worm_dir)
        self._worm_public_key = worm_public_key
        self._poll_interval = poll_interval
        # Agent workspace root — enables the arcskill candidate-store scan
        # (<workspace>/skill_traces/) and the skills WORM chain (SPEC-054 REQ-120).
        self._workspace_dir = Path(workspace_dir) if workspace_dir is not None else None
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
        await self._scan_skills()

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

    def _worm_chains(self) -> list[Path]:
        """Every WORM chain to mirror: the audit-chain segments + the skills chain."""
        chains: list[Path] = []
        # Glob every audit-chain segment unconditionally — a fleet writes
        # per-agent chains (``audit-chain-<agent>.jsonl``) and the bare
        # ``audit-chain.jsonl`` may never exist, so gating on it skipped them all.
        if self._worm_dir.exists():
            chains.extend(sorted(self._worm_dir.glob("audit-chain*.jsonl")))
        if self._workspace_dir is not None:
            skills_chain = self._workspace_dir / SKILLS_WORM_RELPATH
            if skills_chain.exists():
                chains.append(skills_chain)
        return chains

    async def _scan_worm(self) -> None:
        for path in self._worm_chains():
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

    # -- skill candidate store (SPEC-054 REQ-120) ---------------------------

    async def _scan_skills(self) -> None:
        """Mirror the arcskill candidate store into the backend.

        Layout owner: ``arcskill.improver.candidate_store.CandidateStore`` —
        ``<workspace>/skill_traces/<skill>/candidates/{<id>.md, manifest.json}``.
        The manifest mtime is the per-skill change cursor: an unchanged manifest
        skips the skill entirely, and bodies are read once per candidate id
        (content-hash keyed), so steady-state scans touch no candidate files.
        """
        if self._workspace_dir is None:
            return
        traces = self._workspace_dir / "skill_traces"
        if not traces.is_dir():
            return
        for skill_dir in sorted(p for p in traces.iterdir() if p.is_dir()):
            await self._scan_skill(skill_dir)

    async def _scan_skill(self, skill_dir: Path) -> None:
        manifest_path = skill_dir / "candidates" / "manifest.json"
        if not manifest_path.exists():
            return
        cursor = f"skillmf:{skill_dir.name}"
        mtime_ns = manifest_path.stat().st_mtime_ns
        if mtime_ns == await self._backend.get_cursor(cursor):
            return
        manifest = _read_manifest(manifest_path)
        if manifest is not None:
            await self._ingest_candidates(skill_dir, manifest, mtime_ns)
        # Advance even on a corrupt manifest — re-parsing the same bytes cannot
        # succeed; the next rewrite bumps the mtime and is reprocessed.
        await self._backend.set_cursor(cursor, mtime_ns)

    async def _ingest_candidates(
        self, skill_dir: Path, manifest: dict[str, Any], mtime_ns: int
    ) -> None:
        candidates = manifest.get("candidates")
        if not isinstance(candidates, dict):
            return
        skill_name = skill_dir.name
        active_id = manifest.get("active_candidate_id")
        latest = await self._latest_rows(skill_name)
        meta_rows: list[tuple[str, dict[str, Any]]] = []
        body_rows: list[tuple[str, dict[str, Any]]] = []
        for cid, meta in candidates.items():
            if not isinstance(cid, str) or not _SAFE_CANDIDATE_ID_RE.match(cid):
                _logger.warning("StoreIngest skipping unsafe candidate id in %s", skill_dir)
                continue
            if not isinstance(meta, dict):
                continue
            body_hash = (latest.get(cid) or {}).get("body_hash")
            if body_hash is None:
                body = _read_body(skill_dir / "candidates" / f"{cid}.md")
                if body is not None:
                    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
                    body_rows.append((body_hash, {"body": body}))
            row = _candidate_row(
                skill_name, cid, meta, active=cid == active_id, body_hash=body_hash
            )
            # Only a state change inserts a new row version. A row can revert to
            # a *previous* state (rollback flips ``active`` back), so the key is
            # salted with the manifest file version — a pure content key would
            # collide with the historical row and INSERT OR IGNORE would drop
            # the current state.
            if _candidate_state(row) == _candidate_state(latest.get(cid) or {}):
                continue
            meta_rows.append((_candidate_key(row, mtime_ns), row))
        if body_rows:
            await self._backend.upsert_many(SKILL_BODIES_TABLE, body_rows)
        if meta_rows:
            await self._backend.upsert_many(SKILL_CANDIDATES_TABLE, meta_rows)

    async def _latest_rows(self, skill_name: str) -> dict[str, dict[str, Any]]:
        """Latest stored row per candidate id — bodies already stored are never re-read."""
        rows = await self._backend.query(
            SKILL_CANDIDATES_TABLE, where={"skill_name": skill_name}, order_by="ts"
        )
        return {r["candidate_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# Candidate-store reading + mapping
# ---------------------------------------------------------------------------


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _logger.warning("StoreIngest skipping unreadable skill manifest %s", path)
        return None
    return data if isinstance(data, dict) else None


def _read_body(path: Path) -> str | None:
    """Candidate body text, or ``None`` when pending/pruned (tombstone row)."""
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        _logger.warning("StoreIngest skipping unreadable candidate body %s", path)
        return None


def _candidate_row(
    skill_name: str,
    candidate_id: str,
    meta: dict[str, Any],
    *,
    active: bool,
    body_hash: str | None,
) -> dict[str, Any]:
    generation = meta.get("generation")
    parent_id = meta.get("parent_id")
    scores = meta.get("scores")
    return {
        "skill_name": skill_name,
        "candidate_id": candidate_id,
        "generation": generation if isinstance(generation, int) else 0,
        "parent_id": parent_id if isinstance(parent_id, str) else None,
        "scores": scores if isinstance(scores, dict) else {},
        "active": active,
        "body_hash": body_hash,
        "ts": datetime.now(UTC).isoformat(),
    }


def _candidate_state(row: dict[str, Any]) -> tuple[Any, ...]:
    """The comparable candidate state — a re-read that changes none of it is a no-op."""
    return (
        row.get("generation"),
        row.get("parent_id"),
        json.dumps(row.get("scores"), sort_keys=True, separators=(",", ":")),
        bool(row.get("active")),
        row.get("body_hash"),
    )


def _candidate_key(row: dict[str, Any], mtime_ns: int) -> str:
    """Stable content-derived key, salted with the manifest file version.

    Deterministic for one manifest state, so replaying the same file (fresh DB,
    lost cursor) dedupes on INSERT OR IGNORE — never a byte offset or row id.
    """
    raw = "|".join(
        [
            row["skill_name"],
            row["candidate_id"],
            str(mtime_ns),
            *map(str, _candidate_state(row)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
