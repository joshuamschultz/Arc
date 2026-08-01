"""SqliteBackend specifics — PRAGMA stack, per-instance file, content-keyed idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcstore.backends.sqlite import SqliteBackend

_SYSTEM = "did:arc:test:system"


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


class TestMutableCreateBatch:
    """SPEC-061 COMP-007 — ``mutable_create_batch`` atomicity and idempotency."""

    async def test_all_rows_land_in_one_call(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            entries = [(f"k{i}", {"v": i}) for i in range(5)]
            rows = await be.mutable_create_batch("tasks", entries, actor_did=_SYSTEM)
            assert [r["v"] for r in rows] == [0, 1, 2, 3, 4]
            for i in range(5):
                got = await be.mutable_read("tasks", f"k{i}")
                assert got is not None
                assert got["v"] == i
        finally:
            await be.stop()

    async def test_existing_key_is_returned_unmodified_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "k1", {"v": "original"}, actor_did=_SYSTEM)
            rows = await be.mutable_create_batch(
                "tasks", [("k1", {"v": "clobbered"}), ("k2", {"v": "new"})], actor_did=_SYSTEM
            )
            by_key = {r0: r1 for r0, r1 in zip(("k1", "k2"), rows, strict=True)}
            assert by_key["k1"]["v"] == "original"
            assert by_key["k2"]["v"] == "new"
        finally:
            await be.stop()

    async def test_replaying_the_same_batch_is_idempotent(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            entries = [("k1", {"v": 1}), ("k2", {"v": 2})]
            first = await be.mutable_create_batch("tasks", entries, actor_did=_SYSTEM)
            replay = await be.mutable_create_batch("tasks", entries, actor_did=_SYSTEM)
            assert first == replay
            rows = await be.mutable_query("tasks")
            assert len(rows) == 2
        finally:
            await be.stop()

    async def test_a_mid_batch_failure_leaves_no_partial_rows(self, tmp_path: Path) -> None:
        """A crash after the third INSERT (before COMMIT) must roll back the
        whole batch — SQLite never persists an uncommitted transaction — so a
        partial write never becomes visible to any other connection.

        ``sqlite3.Connection`` is a C-implemented type and refuses attribute
        patching directly, so the flaky ``execute`` lives on a
        ``sqlite3.connect(..., factory=...)`` subclass instead — the
        documented way to intercept a connection's behavior.
        """

        class _BoomError(Exception):
            pass

        call_count = {"n": 0}

        class _FlakyConnection(sqlite3.Connection):
            def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:  # type: ignore[override]
                if sql.startswith("INSERT OR IGNORE INTO mutable_records"):
                    call_count["n"] += 1
                    if call_count["n"] == 3:
                        raise _BoomError("simulated crash mid-batch")
                return super().execute(sql, params)

        class _FlakyBackend(SqliteBackend):
            def _connect(self) -> sqlite3.Connection:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=True,
                    timeout=5.0,
                    factory=_FlakyConnection,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                return conn

        db = tmp_path / "store.db"
        be = _FlakyBackend(db)
        await be.start()
        try:
            entries = [(f"k{i}", {"v": i}) for i in range(5)]
            with pytest.raises(_BoomError):
                await be.mutable_create_batch("tasks", entries, actor_did=_SYSTEM)
        finally:
            await be.stop()

        # Reopen with a clean (non-flaky) connection and confirm nothing landed.
        be2 = SqliteBackend(db)
        await be2.start()
        try:
            rows = await be2.mutable_query("tasks")
            assert rows == [], "a mid-batch failure must not leave partial rows committed"
        finally:
            await be2.stop()


class TestMutableIncrement:
    """SPEC-061 COMP-006 — ``mutable_increment`` atomic arithmetic."""

    async def test_increments_a_fresh_never_set_counter_from_zero(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("runs", "r1", {"budget": {}}, actor_did=_SYSTEM)
            won = await be.mutable_increment(
                "runs", "r1", {"budget.tokens_reserved": 100}, actor_did=_SYSTEM
            )
            assert won is True
            got = await be.mutable_read("runs", "r1")
            assert got is not None
            assert got["budget"]["tokens_reserved"] == 100
        finally:
            await be.stop()

    async def test_two_disjoint_paths_incremented_in_one_call_both_land(
        self, tmp_path: Path
    ) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write(
                "runs",
                "r1",
                {"budget": {"tokens_reserved": 10, "wall_clock_seconds_reserved": 5}},
                actor_did=_SYSTEM,
            )
            await be.mutable_increment(
                "runs",
                "r1",
                {"budget.tokens_reserved": 90, "budget.wall_clock_seconds_reserved": 15},
                actor_did=_SYSTEM,
            )
            got = await be.mutable_read("runs", "r1")
            assert got is not None
            assert got["budget"]["tokens_reserved"] == 100
            assert got["budget"]["wall_clock_seconds_reserved"] == 20
        finally:
            await be.stop()

    async def test_negative_delta_decrements(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write(
                "runs", "r1", {"budget": {"tokens_reserved": 500}}, actor_did=_SYSTEM
            )
            await be.mutable_increment(
                "runs", "r1", {"budget.tokens_reserved": -200}, actor_did=_SYSTEM
            )
            got = await be.mutable_read("runs", "r1")
            assert got is not None
            assert got["budget"]["tokens_reserved"] == 300
        finally:
            await be.stop()

    async def test_missing_key_returns_false(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            won = await be.mutable_increment(
                "runs", "does-not-exist", {"budget.tokens_reserved": 1}, actor_did=_SYSTEM
            )
            assert won is False
        finally:
            await be.stop()

    async def test_concurrent_increments_on_the_same_counter_both_land(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write(
                "runs", "r1", {"budget": {"tokens_reserved": 0}}, actor_did=_SYSTEM
            )
            barrier = asyncio.Barrier(2)

            async def bump(amount: int) -> None:
                await barrier.wait()
                await be.mutable_increment(
                    "runs", "r1", {"budget.tokens_reserved": amount}, actor_did=_SYSTEM
                )

            await asyncio.gather(bump(300), bump(700))
            got = await be.mutable_read("runs", "r1")
            assert got is not None
            assert got["budget"]["tokens_reserved"] == 1000
        finally:
            await be.stop()
