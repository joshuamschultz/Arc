"""StorageBackend Protocol conformance — proven on both FakeBackend and SqliteBackend.

Running the same suite against an in-memory fake and the real SQLite backend
proves the Protocol is the only seam: no SQLite type leaks into the contract
(FR-3 AC-3.4 / 3.10). The Protocol is async with no ``begin()`` (research §11.3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from arcstore.backends.base import (
    OPERATIONAL_TABLES,
    StorageBackend,
    table_for_kind,
)
from arcstore.backends.memory import FakeBackend
from arcstore.backends.sqlite import SqliteBackend


def test_new_kinds_mapped() -> None:
    """Task 1.4 — tool_event/spawn_event map to their tables (SPEC-028 FR-1/FR-3)."""
    assert table_for_kind("tool_event") == "tool_events"
    assert table_for_kind("spawn_event") == "spawn_events"
    assert "tool_events" in OPERATIONAL_TABLES
    assert "spawn_events" in OPERATIONAL_TABLES


@pytest.fixture(params=["memory", "sqlite"])
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[StorageBackend]:
    if request.param == "memory":
        be: StorageBackend = FakeBackend()
        await be.start()
        yield be
        await be.stop()
    else:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        yield be
        await be.stop()


def _row(
    rid: str, *, model: str = "claude", outcome: str = "ok", ts: str = "2026-05-31T00:00:00"
) -> dict:
    return {
        "record_id": rid,
        "kind": "llm_call",
        "actor_did": "did:arc:test:exec/aabbccdd",
        "ts": ts,
        "request_id": rid,
        "model": model,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_usd": 0.001,
        "latency_ms": 12.5,
        "outcome": outcome,
        "name": None,
        "extra": {"k": "v"},
    }


class TestProtocolConformance:
    def test_fake_backend_conforms(self) -> None:
        assert isinstance(FakeBackend(), StorageBackend)

    def test_sqlite_backend_conforms(self, tmp_path: Path) -> None:
        assert isinstance(SqliteBackend(tmp_path / "s.db"), StorageBackend)

    def test_async_protocol_has_no_begin(self) -> None:
        # The transaction is an implementation detail of the backend, never the
        # Protocol surface (research §11.3 supersedes SDD §5.1 begin()).
        assert not hasattr(StorageBackend, "begin")

    async def test_query_runs_on_fake_and_sqlite(self, backend: StorageBackend) -> None:
        await backend.upsert("llm_calls", "r1", _row("r1", model="opus"))
        await backend.upsert("llm_calls", "r2", _row("r2", model="haiku"))
        rows = await backend.query("llm_calls", order_by="ts")
        assert {r["record_id"] for r in rows} == {"r1", "r2"}
        assert rows[0]["extra"] == {"k": "v"}  # round-trips structured extra

    async def test_query_where_filter(self, backend: StorageBackend) -> None:
        await backend.upsert("llm_calls", "r1", _row("r1", outcome="ok"))
        await backend.upsert("llm_calls", "r2", _row("r2", outcome="error"))
        rows = await backend.query("llm_calls", where={"outcome": "error"})
        assert [r["record_id"] for r in rows] == ["r2"]

    async def test_query_limit(self, backend: StorageBackend) -> None:
        for i in range(5):
            await backend.upsert("llm_calls", f"r{i}", _row(f"r{i}", ts=f"2026-05-31T00:00:0{i}"))
        rows = await backend.query("llm_calls", order_by="ts", limit=2)
        assert len(rows) == 2

    async def test_upsert_idempotent(self, backend: StorageBackend) -> None:
        """AC-3.3 — the same record twice produces exactly one row."""
        await backend.upsert("llm_calls", "r1", _row("r1"))
        await backend.upsert("llm_calls", "r1", _row("r1"))
        rows = await backend.query("llm_calls")
        assert len(rows) == 1

    async def test_upsert_many_batches(self, backend: StorageBackend) -> None:
        items = [(f"r{i}", _row(f"r{i}")) for i in range(10)]
        await backend.upsert_many("llm_calls", items)
        # Re-applying the batch is a no-op (idempotent content key).
        await backend.upsert_many("llm_calls", items)
        rows = await backend.query("llm_calls")
        assert len(rows) == 10

    async def test_cursor_roundtrip(self, backend: StorageBackend) -> None:
        assert await backend.get_cursor("file-a") == 0  # unknown cursor → 0
        await backend.set_cursor("file-a", 4096)
        assert await backend.get_cursor("file-a") == 4096
