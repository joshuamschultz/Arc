"""Mutable directory plane (SPEC-056 Phase 0A / SPEC-032 slice) — RED.

``mutable_records(collection, key, value JSON, updated_at, PK(collection,key))``
on the sqlite backend, mirroring the WAL/busy_timeout discipline of the
insert-once spool plane (sqlite.py:252-301) but supporting real overwrite —
unlike ``OPERATIONAL_TABLES``, which are insert-once (``INSERT OR IGNORE``).

None of ``mutable_write``/``mutable_read``/``mutable_delete``/``mutable_query``
exist yet — every test here fails with ``AttributeError`` (feature absent),
not an import/syntax error.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from arcstore.backends.sqlite import SqliteBackend

_ACTOR = "did:arc:test:exec/aabbccdd"


class TestMutableRecordsSchema:
    async def test_table_exists_with_collection_key_pk(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        be = SqliteBackend(db)
        await be.start()
        try:
            conn = sqlite3.connect(str(db))
            try:
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "mutable_records" in names
                cols = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(mutable_records)")}
                assert set(cols) >= {"collection", "key", "value", "updated_at"}
                # composite PK — both collection and key carry a nonzero pk ordinal
                assert cols["collection"] > 0
                assert cols["key"] > 0
            finally:
                conn.close()
        finally:
            await be.stop()

    async def test_wal_and_busy_timeout_applied(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        be = SqliteBackend(db)
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"title": "x"}, actor_did=_ACTOR)
        finally:
            await be.stop()
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
        finally:
            conn.close()


class TestMutableRecordsRoundtrip:
    async def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write(
                "tasks", "t1", {"title": "Fix bug", "status": "todo"}, actor_did=_ACTOR
            )
            got = await be.mutable_read("tasks", "t1")
            assert got is not None
            assert got["title"] == "Fix bug"
            assert got["status"] == "todo"
            assert "updated_at" in got
            assert isinstance(got["updated_at"], str)
        finally:
            await be.stop()

    async def test_read_missing_key_returns_none(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            assert await be.mutable_read("tasks", "does-not-exist") is None
        finally:
            await be.stop()

    async def test_write_overwrites_existing_value(self, tmp_path: Path) -> None:
        """Unlike the insert-once spool plane, a repeated key mutates in place."""
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"status": "todo"}, actor_did=_ACTOR)
            await be.mutable_write("tasks", "t1", {"status": "done"}, actor_did=_ACTOR)
            got = await be.mutable_read("tasks", "t1")
            assert got is not None
            assert got["status"] == "done"
            assert len(await be.mutable_query("tasks")) == 1
        finally:
            await be.stop()

    async def test_write_bumps_updated_at(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"status": "todo"}, actor_did=_ACTOR)
            first = (await be.mutable_read("tasks", "t1"))["updated_at"]  # type: ignore[index]
            await asyncio.sleep(0.01)
            await be.mutable_write("tasks", "t1", {"status": "done"}, actor_did=_ACTOR)
            second = (await be.mutable_read("tasks", "t1"))["updated_at"]  # type: ignore[index]
            assert second > first
        finally:
            await be.stop()

    async def test_delete_removes_row(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"status": "todo"}, actor_did=_ACTOR)
            deleted = await be.mutable_delete("tasks", "t1", actor_did=_ACTOR)
            assert deleted is True
            assert await be.mutable_read("tasks", "t1") is None
        finally:
            await be.stop()

    async def test_delete_missing_key_returns_false(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            assert await be.mutable_delete("tasks", "does-not-exist", actor_did=_ACTOR) is False
        finally:
            await be.stop()


class TestMutableRecordsQuery:
    async def test_query_scoped_to_collection(self, tmp_path: Path) -> None:
        """Two collections sharing a key never leak into each other's query."""
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"status": "todo"}, actor_did=_ACTOR)
            await be.mutable_write("teams", "t1", {"name": "alpha"}, actor_did=_ACTOR)
            rows = await be.mutable_query("tasks")
            assert len(rows) == 1
            assert rows[0]["status"] == "todo"
        finally:
            await be.stop()

    async def test_query_filters_by_where(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"status": "todo"}, actor_did=_ACTOR)
            await be.mutable_write("tasks", "t2", {"status": "done"}, actor_did=_ACTOR)
            rows = await be.mutable_query("tasks", where={"status": "done"})
            assert len(rows) == 1
            assert rows[0]["status"] == "done"
        finally:
            await be.stop()

    async def test_query_empty_collection_returns_empty_list(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            assert await be.mutable_query("tasks") == []
        finally:
            await be.stop()
