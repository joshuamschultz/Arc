"""Tests for FederatedTraceStore (SPEC-019 T2.5).

Read-only fan-out across N JSONLTraceStore instances. Validates:
  - empty store list passthrough
  - single-store equivalence (legacy compatibility)
  - three-store query with `agent=` filter combines correctly
  - cursor pagination round-trip across stores
  - get() first-hit-wins
  - close() propagates to every store
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcui.federated_store import FederatedTraceStore


def _record(agent: str, *, ts: datetime | None = None, provider: str = "anthropic") -> TraceRecord:
    return TraceRecord(
        provider=provider,
        model="claude-sonnet-4",
        agent_label=agent,
        timestamp=(ts or datetime.now(UTC)).isoformat(),
    )


async def _seed_store(agent_root: Path, records: list[TraceRecord]) -> JSONLTraceStore:
    store = JSONLTraceStore(agent_root)
    for rec in records:
        await store.append(rec)
    return store


class TestFederatedEmpty:
    async def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        store = FederatedTraceStore([])
        recs, cursor = await store.query(limit=10)
        assert recs == []
        assert cursor is None

    async def test_empty_get_returns_none(self) -> None:
        store = FederatedTraceStore([])
        assert await store.get("nonexistent") is None


class TestFederatedSingleStore:
    """A federation of one store passes through results identically."""

    async def test_single_store_passthrough(self, tmp_path: Path) -> None:
        rec = _record("a")
        s = await _seed_store(tmp_path / "agent_a", [rec])

        federated = FederatedTraceStore([s])
        recs, _ = await federated.query(limit=10)
        assert len(recs) == 1
        assert recs[0].agent_label == "a"


class TestFederatedThreeStores:
    """Per-agent filter applied per store; results merge."""

    async def test_agent_filter_only_returns_matching(self, tmp_path: Path) -> None:
        # Three stores, only the middle one has agent "target".
        s1 = await _seed_store(tmp_path / "ws1", [_record("other1")])
        s2 = await _seed_store(tmp_path / "ws2", [_record("target"), _record("noise")])
        s3 = await _seed_store(tmp_path / "ws3", [_record("other2")])

        federated = FederatedTraceStore([s1, s2, s3])
        recs, _ = await federated.query(limit=10, agent="target")
        assert len(recs) == 1
        assert recs[0].agent_label == "target"

    async def test_combined_count_no_filter(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        s1 = await _seed_store(
            tmp_path / "ws1",
            [_record("a", ts=now - timedelta(seconds=10))],
        )
        s2 = await _seed_store(
            tmp_path / "ws2",
            [_record("b", ts=now - timedelta(seconds=5))],
        )
        s3 = await _seed_store(
            tmp_path / "ws3",
            [_record("c", ts=now - timedelta(seconds=1))],
        )

        federated = FederatedTraceStore([s1, s2, s3])
        recs, _ = await federated.query(limit=10)
        assert len(recs) == 3


class TestFederatedGetFirstHitWins:
    """trace_id is globally unique (UUID4); first store with a hit wins."""

    async def test_get_finds_in_any_store(self, tmp_path: Path) -> None:
        rec = _record("only")
        s1 = await _seed_store(tmp_path / "ws1", [_record("other")])
        s2 = await _seed_store(tmp_path / "ws2", [rec])

        federated = FederatedTraceStore([s1, s2])
        result = await federated.get(rec.trace_id)
        assert result is not None
        assert result.agent_label == "only"


class TestFederatedClosePropagates:
    """close() must propagate to every wrapped store."""

    async def test_close_calls_each(self) -> None:
        called: list[int] = []

        class _Spy:
            async def close(self) -> None:
                called.append(1)

        federated = FederatedTraceStore([_Spy(), _Spy(), _Spy()])
        await federated.close()
        assert len(called) == 3


class TestFederatedCursorPagination:
    """Compound cursor must round-trip across pages without duplicates.

    Review BLOCKER #12: the prior test suite walked one page; the whole
    point of compound cursors is multi-page resume. These tests fan out
    multiple stores with more records than `limit` and walk every page.
    """

    async def test_paginates_across_three_stores_in_strict_timestamp_order(
        self, tmp_path: Path
    ) -> None:
        # Three stores × 30 records each (90 total). Distinct timestamps so
        # ordering is unambiguous. Limit=10 → 9 pages expected.
        # Records are appended in chronological order (oldest first) — that
        # is the production agent flow, and JSONLTraceStore's query iterates
        # files newest-first then lines newest-last, which assumes append
        # order matches timestamp order.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        stores = []
        for i in range(3):
            ws = tmp_path / f"ws{i}"
            ws.mkdir()
            recs = [
                _record(
                    agent=f"a{i}",
                    ts=base + timedelta(seconds=10 * i + n),
                )
                for n in range(30)
            ]
            stores.append(await _seed_store(ws, recs))

        federated = FederatedTraceStore(stores)

        all_emitted: list[TraceRecord] = []
        cursor: str | None = None
        page_count = 0
        while True:
            page, cursor = await federated.query(limit=10, cursor=cursor)
            page_count += 1
            assert page_count <= 20, "pagination not making progress"
            all_emitted.extend(page)
            if cursor is None:
                break

        # Every record reached us exactly once.
        assert len(all_emitted) == 90
        ids = [r.trace_id for r in all_emitted]
        assert len(set(ids)) == 90, "duplicate trace_id across pages"

        # Strict timestamp-desc order across the full walk.
        timestamps = [r.timestamp for r in all_emitted]
        assert timestamps == sorted(timestamps, reverse=True), (
            "federated query must yield newest-first across page boundaries"
        )

    async def test_filter_applies_across_pages(self, tmp_path: Path) -> None:
        """`agent=` filter must isolate one store across pagination."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        stores = []
        for i in range(3):
            ws = tmp_path / f"ws{i}"
            ws.mkdir()
            agent = "target" if i == 1 else f"noise{i}"
            # Append oldest-first; JSONLTraceStore relies on append order
            # mirroring chronological order for newest-first iteration.
            recs = [
                _record(agent=agent, ts=base + timedelta(seconds=10 * i + n))
                for n in range(20)
            ]
            stores.append(await _seed_store(ws, recs))

        federated = FederatedTraceStore(stores)

        all_target: list[TraceRecord] = []
        cursor: str | None = None
        while True:
            page, cursor = await federated.query(
                limit=5, cursor=cursor, agent="target"
            )
            all_target.extend(page)
            if cursor is None:
                break

        assert len(all_target) == 20
        for rec in all_target:
            assert rec.agent_label == "target", (
                "agent filter leaked another workspace's records across "
                "pagination — federation collapses isolation"
            )

    async def test_malformed_cursor_decodes_to_empty(self) -> None:
        """A garbage cursor must not crash; treat as start-of-stream."""
        watermark, skip_ids = FederatedTraceStore._decode_cursor(
            "not-base64!@#"
        )
        assert watermark is None
        assert skip_ids == frozenset()

        # Round-trip via public query() with bad cursor: should not raise.
        federated = FederatedTraceStore([])
        recs, cursor = await federated.query(
            limit=10, cursor="garbage-cursor"
        )
        assert recs == []
        assert cursor is None

    async def test_cursor_round_trip_via_encode_decode(self) -> None:
        """encode → decode must yield the original (watermark, skip_ids)."""
        ts = "2026-01-01T00:00:30+00:00"
        skip = ["abc123", "def456"]
        encoded = FederatedTraceStore._encode_cursor(ts, skip)
        decoded_ts, decoded_skip = FederatedTraceStore._decode_cursor(encoded)
        assert decoded_ts == ts
        assert decoded_skip == frozenset(skip)
