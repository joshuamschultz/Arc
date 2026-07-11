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
        "cache_read_tokens": None,
        "cache_write_tokens": None,
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

    async def test_start_reconciles_columns_on_legacy_schema(self, tmp_path: Path) -> None:
        """A DB created by an earlier (pre-SPEC-028) schema is missing the
        tool/spawn columns; ``start()`` must ALTER them in so queries that list
        every allowlisted column don't fail with ``no such column``."""
        db = tmp_path / "legacy.db"
        # Simulate an old DB: llm_calls without the SPEC-028 columns.
        conn = sqlite3.connect(str(db))
        conn.executescript(
            "CREATE TABLE llm_calls(record_id TEXT PRIMARY KEY, kind TEXT, "
            "agent_label TEXT, ts TEXT, prompt_tokens INTEGER, extra TEXT);"
        )
        conn.commit()
        conn.close()

        be = SqliteBackend(db)
        await be.start()
        try:
            cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(llm_calls)")}
            assert "tool_name" in cols
            assert "parent_did" in cols
            # The full-column SELECT that used to raise now succeeds.
            assert await be.query("llm_calls") == []
        finally:
            await be.stop()

    async def test_cache_token_columns_round_trip(self, tmp_path: Path) -> None:
        """cache_read/write_tokens persist as INTEGER columns and read back intact."""
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            row = _row("r1")
            row["cache_read_tokens"] = 1500
            row["cache_write_tokens"] = 300
            await be.upsert("llm_calls", "r1", row)
            got = await be.query("llm_calls")
            assert got[0]["cache_read_tokens"] == 1500
            assert got[0]["cache_write_tokens"] == 300
        finally:
            await be.stop()

    async def test_reconciles_cache_columns_on_legacy_schema(self, tmp_path: Path) -> None:
        """A pre-cache-accounting DB gains the two cache columns via ALTER on start()."""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            "CREATE TABLE llm_calls(record_id TEXT PRIMARY KEY, kind TEXT, "
            "prompt_tokens INTEGER, completion_tokens INTEGER, extra TEXT);"
        )
        conn.commit()
        conn.close()
        be = SqliteBackend(db)
        await be.start()
        try:
            cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(llm_calls)")}
            assert "cache_read_tokens" in cols
            assert "cache_write_tokens" in cols
        finally:
            await be.stop()
