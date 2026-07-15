"""arcstore ``cancellations`` domain — CancelRequest + CancelStore.

The shared directory the operator surfaces (``arc stop`` / arcui) and the
per-agent watcher meet on: create a ``pending`` row naming a run, resolve it once
(race-safe) to ``applied`` / ``not_found`` / ``expired``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arcstore.backends.sqlite import SqliteBackend
from arcstore.cancellations import CancelRequest, CancelStore

_OPERATOR = "did:arc:test:human/operator"


async def _backend(tmp_path: Path) -> SqliteBackend:
    be = SqliteBackend(tmp_path / "store.db")
    await be.start()
    return be


def _request(rid: str = "c1", *, run_id: str = "run-abc") -> CancelRequest:
    return CancelRequest(
        id=rid,
        run_id=run_id,
        agent_label="josh_agent",
        requested_by=_OPERATOR,
        reason="taking too long",
    )


class TestCancelRequest:
    def test_requires_a_target(self) -> None:
        with pytest.raises(ValueError, match="run_id or a session_key"):
            CancelRequest(id="c1", requested_by=_OPERATOR)

    def test_session_key_alone_is_a_valid_target(self) -> None:
        req = CancelRequest(id="c1", session_key="cli:main", requested_by=_OPERATOR)
        assert req.session_key == "cli:main"


class TestCancelStore:
    async def test_create_then_get_roundtrips(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            created = await store.create(_request())
            assert created.created_at is not None
            got = await store.get("c1")
            assert got is not None
            assert got.status == "pending"
            assert got.run_id == "run-abc"
            assert got.requested_by == _OPERATOR
            assert got.reason == "taking too long"
        finally:
            await be.stop()

    async def test_list_filters_by_status(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            await store.create(_request("a"))
            await store.create(_request("b"))
            await store.resolve("b", status="applied", actor_did=_OPERATOR)
            pending = await store.list(status="pending")
            assert [r.id for r in pending] == ["a"]
        finally:
            await be.stop()

    async def test_resolve_applied_stamps_terminal_fields(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            await store.create(_request())
            resolved = await store.resolve(
                "c1", status="applied", actor_did=_OPERATOR, resolved_by=_OPERATOR, note="stopped"
            )
            assert resolved is not None
            assert resolved.status == "applied"
            assert resolved.resolved_by == _OPERATOR
            assert resolved.resolved_at is not None
            assert resolved.note == "stopped"
        finally:
            await be.stop()

    async def test_resolve_missing_returns_none(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            assert await store.resolve("nope", status="applied", actor_did=_OPERATOR) is None
        finally:
            await be.stop()

    async def test_double_resolve_exactly_one_wins(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            await store.create(_request())
            barrier = asyncio.Barrier(2)

            async def resolve(status: str) -> CancelRequest | None:
                await barrier.wait()
                return await store.resolve(
                    "c1", status=status, actor_did=_OPERATOR  # type: ignore[arg-type]
                )

            results = await asyncio.gather(resolve("applied"), resolve("not_found"))
            winners = [r for r in results if r is not None]
            assert len(winners) == 1
        finally:
            await be.stop()


class TestExpireStale:
    async def test_ages_out_pending_older_than_ttl(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            created = await store.create(_request())
            # A wall-clock 301s past creation with a 300s TTL — the request never
            # matched a live run, so it is swept to ``expired``.
            future = datetime.fromisoformat(created.created_at or _now()) + timedelta(seconds=301)
            expired = await store.expire_stale(
                ttl_seconds=300, actor_did=_OPERATOR, now=future
            )
            assert [r.id for r in expired] == ["c1"]
            assert expired[0].status == "expired"
            assert expired[0].resolved_at is not None
            got = await store.get("c1")
            assert got is not None and got.status == "expired"
        finally:
            await be.stop()

    async def test_leaves_fresh_pending_untouched(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            await store.create(_request())
            # Evaluated at creation time: well within the TTL, so nothing ages out.
            expired = await store.expire_stale(ttl_seconds=300, actor_did=_OPERATOR)
            assert expired == []
            got = await store.get("c1")
            assert got is not None and got.status == "pending"
        finally:
            await be.stop()

    async def test_ignores_already_resolved(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = CancelStore(be)
            created = await store.create(_request())
            await store.resolve("c1", status="applied", actor_did=_OPERATOR)
            future = datetime.fromisoformat(created.created_at or _now()) + timedelta(seconds=301)
            expired = await store.expire_stale(
                ttl_seconds=300, actor_did=_OPERATOR, now=future
            )
            # Only pending rows are candidates — a terminal request is never touched.
            assert expired == []
            got = await store.get("c1")
            assert got is not None and got.status == "applied"
        finally:
            await be.stop()


def _now() -> str:
    return datetime.now(UTC).isoformat()
