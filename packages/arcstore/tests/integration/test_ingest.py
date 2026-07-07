"""StoreIngest — backfill from offline files + tail live appends (FR-3, UC-1/2/3).

arcstore is a pure file-tailer: it owns no sink in emit(), only reads the
durable spool + WORM files. These tests prove the structural guarantee — a
record written while the store is down is recovered on startup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from arcstore import query
from arcstore.backends.sqlite import SqliteBackend
from arcstore.ingest import StoreIngest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record


def _spool_dir(data_dir: Path) -> Path:
    d = data_dir / "spool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_call(path: Path, rid: str, outcome: str = "ok") -> None:
    spool_record(
        SpoolRecord(
            kind="llm_call",
            actor_did="did:arc:test:exec/aabbccdd",
            request_id=rid,
            model="claude",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.001,
            latency_ms=12.0,
            outcome=outcome,
        ),
        path=path,
    )


async def _make_ingest(tmp_path: Path) -> tuple[StoreIngest, SqliteBackend, Path]:
    data_dir = tmp_path / "data"
    spool = _spool_dir(data_dir)
    backend = SqliteBackend(data_dir / "store" / "inst.db")
    await backend.start()
    ingest = StoreIngest(backend, spool_dir=spool, worm_dir=data_dir / "worm")
    return ingest, backend, spool


class TestBackfill:
    async def test_backfill_recovers_offline_records(self, tmp_path: Path) -> None:
        """AC-3.1 / UC-1 — records written while the store was down appear after backfill."""
        ingest, backend, spool = await _make_ingest(tmp_path)
        try:
            f = spool / "operational-2026-05-31.jsonl"
            for i in range(3):
                _write_call(f, f"r{i}")
            await ingest.backfill()
            rows = await backend.query("llm_calls", order_by="ts")
            assert len(rows) == 3
            assert rows[0]["model"] == "claude"
        finally:
            await backend.stop()

    async def test_backfill_twice_no_duplicates(self, tmp_path: Path) -> None:
        """AC-3.3 — running backfill twice over the same files produces no dup rows."""
        ingest, backend, spool = await _make_ingest(tmp_path)
        try:
            f = spool / "operational-2026-05-31.jsonl"
            for i in range(4):
                _write_call(f, f"r{i}")
            await ingest.backfill()
            await ingest.backfill()
            assert len(await backend.query("llm_calls")) == 4
        finally:
            await backend.stop()


class TestTail:
    async def test_tail_follows_and_resumes_from_offset(self, tmp_path: Path) -> None:
        """AC-3.2 — appended lines appear via scan; the byte offset persists across restart."""
        ingest, backend, spool = await _make_ingest(tmp_path)
        f = spool / "operational-2026-05-31.jsonl"
        try:
            _write_call(f, "r0")
            await ingest.scan_once()
            assert len(await backend.query("llm_calls")) == 1
            # Append more, scan again — only the new line is ingested.
            _write_call(f, "r1")
            await ingest.scan_once()
            assert len(await backend.query("llm_calls")) == 2
        finally:
            await backend.stop()

        # A fresh store over the same DB + files resumes from the persisted
        # offset and does NOT re-ingest already-consumed lines.
        backend2 = SqliteBackend(tmp_path / "data" / "store" / "inst.db")
        await backend2.start()
        ingest2 = StoreIngest(backend2, spool_dir=spool, worm_dir=tmp_path / "data" / "worm")
        try:
            _write_call(f, "r2")
            await ingest2.scan_once()
            assert len(await backend2.query("llm_calls")) == 3
        finally:
            await backend2.stop()


class TestTailLoop:
    async def test_start_backfills_then_tails_and_stops_clean(self, tmp_path: Path) -> None:
        """AC-6.2 precursor — start() backfills + tails as a managed task; stop() is clean."""
        ingest, backend, spool = await _make_ingest(tmp_path)
        f = spool / "operational-2026-05-31.jsonl"
        try:
            _write_call(f, "r0")
            ingest._poll_interval = 0.02  # fast tail for the test
            await ingest.start()  # backfills r0
            assert len(await backend.query("llm_calls")) == 1
            # A line appended after start() is picked up by the tail loop.
            _write_call(f, "r1")
            for _ in range(50):
                await asyncio.sleep(0.02)
                if len(await backend.query("llm_calls")) == 2:
                    break
            assert len(await backend.query("llm_calls")) == 2
            await ingest.stop()
            assert ingest._task is None  # no orphaned task
        finally:
            await backend.stop()


class TestQueryApi:
    async def test_recent_and_audit_reads(self, tmp_path: Path) -> None:
        ingest, backend, spool = await _make_ingest(tmp_path)
        try:
            f = spool / "operational-2026-05-31.jsonl"
            for i in range(3):
                _write_call(f, f"r{i}")
            await ingest.backfill()
            calls = await query.recent(backend, "llm_call", limit=2)
            assert len(calls) == 2
            assert await query.recent(backend, "run_event") == []
            assert await query.audit_records(backend) == []
        finally:
            await backend.stop()


class TestToolAndSpawnIngest:
    async def test_tool_and_spawn_ingest(self, tmp_path: Path) -> None:
        """Task 1.6 — tool_event + spawn_event round-trip spool → query, idempotent on replay."""
        ingest, backend, spool = await _make_ingest(tmp_path)
        f = spool / "operational-2026-05-31.jsonl"
        try:
            spool_record(
                SpoolRecord(
                    kind="tool_event",
                    actor_did="did:arc:test:exec/aabbccdd",
                    request_id="run-1",
                    tool_name="web.fetch",
                    phase="end",
                    outcome="ok",
                    latency_ms=42.0,
                    args_digest="a" * 64,
                    args_size=12,
                    result_digest="b" * 64,
                    result_size=99,
                ),
                path=f,
            )
            spool_record(
                SpoolRecord(
                    kind="spawn_event",
                    actor_did="did:arc:test:agent:child",
                    request_id="run-1",
                    parent_did="did:arc:test:agent:parent",
                    child_did="did:arc:test:agent:child",
                    role="researcher",
                    depth=1,
                    outcome="ok",
                ),
                path=f,
            )
            await ingest.backfill()
            await ingest.backfill()  # replay must not duplicate (idempotent)

            tool_rows = await backend.query("tool_events")
            assert len(tool_rows) == 1
            assert tool_rows[0]["tool_name"] == "web.fetch"
            assert tool_rows[0]["result_digest"] == "b" * 64
            assert tool_rows[0]["result_size"] == 99

            spawn_rows = await backend.query("spawn_events")
            assert len(spawn_rows) == 1
            assert spawn_rows[0]["parent_did"] == "did:arc:test:agent:parent"
            assert spawn_rows[0]["child_did"] == "did:arc:test:agent:child"
            assert spawn_rows[0]["role"] == "researcher"
            assert spawn_rows[0]["depth"] == 1
        finally:
            await backend.stop()


class TestWormIngest:
    async def test_worm_ingest_flags_tamper(self, tmp_path: Path) -> None:
        """AC — a verifiable WORM ingests as verified; a tampered one flags rows unverified."""
        from arctrust.audit import WormSink
        from arctrust.keypair import generate_keypair
        from arctrust.signer import InProcessSigner

        from arcstore.records import SpoolRecord  # noqa: F401  (kept for symmetry)

        data_dir = tmp_path / "data"
        worm_dir = data_dir / "worm"
        worm_dir.mkdir(parents=True, exist_ok=True)
        worm_path = worm_dir / "audit-chain.jsonl"
        kp = generate_keypair()
        sink = WormSink(worm_path, InProcessSigner(kp.private_key))
        from arctrust.audit import AuditEvent

        for i in range(3):
            sink.write(
                AuditEvent(
                    actor_did=f"did:arc:test:exec/{i:08x}",
                    action="tool.call",
                    target="read_file",
                    outcome="allow",
                )
            )
        sink.close()

        backend = SqliteBackend(data_dir / "store" / "inst.db")
        await backend.start()
        ingest = StoreIngest(
            backend,
            spool_dir=data_dir / "spool",
            worm_dir=worm_dir,
            worm_public_key=kp.public_key,
        )
        try:
            await ingest.backfill()
            rows = await backend.query("audit_chain")
            assert len(rows) == 3
            assert all(r["verified"] for r in rows)

            # Tamper a byte on disk, re-ingest a fresh store → rows flagged unverified.
            text = worm_path.read_text().splitlines()
            import json

            rec = json.loads(text[1])
            rec["event"]["action"] = "tampered"
            text[1] = json.dumps(rec)
            worm_path.write_text("\n".join(text) + "\n")

            backend2 = SqliteBackend(tmp_path / "store2.db")
            await backend2.start()
            ingest2 = StoreIngest(
                backend2,
                spool_dir=data_dir / "spool",
                worm_dir=worm_dir,
                worm_public_key=kp.public_key,
            )
            await ingest2.backfill()
            rows2 = await backend2.query("audit_chain")
            assert rows2  # rows still ingested
            assert not any(r["verified"] for r in rows2)  # but flagged unverified
            await backend2.stop()
        finally:
            await backend.stop()
