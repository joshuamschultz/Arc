"""SqliteBackend specifics — PRAGMA stack, per-instance file, content-keyed idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arcstore.backends.sqlite import SqliteBackend


def _row(rid: str) -> dict:
    return {
        "record_id": rid,
        "kind": "llm_call",
        "actor_did": "did:arc:test:exec/aabbccdd",
        "ts": "2026-05-31T00:00:00",
        "request_id": rid,
        "model": "claude",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "cost_usd": 0.0,
        "latency_ms": 1.0,
        "outcome": "ok",
        "name": None,
        "extra": {},
    }


class TestSqliteBackend:
    async def test_upsert_idempotent(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.upsert("llm_calls", "r1", _row("r1"))
            await be.upsert("llm_calls", "r1", _row("r1"))
            assert len(await be.query("llm_calls")) == 1
        finally:
            await be.stop()

    async def test_pragmas_applied(self, tmp_path: Path) -> None:
        """C5 — WAL + NORMAL + busy_timeout set on every connection."""
        db = tmp_path / "store.db"
        be = SqliteBackend(db)
        await be.start()
        try:
            await be.upsert("llm_calls", "r1", _row("r1"))
        finally:
            await be.stop()
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
        finally:
            conn.close()

    async def test_per_instance_file_shared_nothing(self, tmp_path: Path) -> None:
        """NFR-8 — two instances own two distinct files (no shared DB)."""
        a = SqliteBackend(tmp_path / "a.db")
        b = SqliteBackend(tmp_path / "b.db")
        await a.start()
        await b.start()
        try:
            await a.upsert("llm_calls", "r1", _row("r1"))
            assert len(await a.query("llm_calls")) == 1
            assert len(await b.query("llm_calls")) == 0  # isolated
        finally:
            await a.stop()
            await b.stop()

    async def test_content_key_dedups_across_restart(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        be = SqliteBackend(db)
        await be.start()
        await be.upsert("llm_calls", "r1", _row("r1"))
        await be.stop()
        # Reopen the same file and re-apply — content key prevents a dup row.
        be2 = SqliteBackend(db)
        await be2.start()
        try:
            await be2.upsert("llm_calls", "r1", _row("r1"))
            assert len(await be2.query("llm_calls")) == 1
        finally:
            await be2.stop()
