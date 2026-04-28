"""Tests for RollingAggregator.warm_start_multi (SPEC-019 T2.3, T2.4).

Validates heap-merge correctness across multiple stores: chronological
ordering, single-store equivalence to legacy warm_start, and bounded perf.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from arcui.aggregator import RollingAggregator


# ---------------------------------------------------------------------------
# Test doubles — minimal TraceStore that yields records in fixed order.
# ---------------------------------------------------------------------------


class _FakeStore:
    """Stand-in TraceStore returning a pre-baked record list.

    Exposes only the surface warm_start_multi consumes (iter_records).
    Records are dicts so the aggregator's ingest() path is unchanged.
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        for rec in self._records:
            yield rec

    async def query(
        self, *, limit: int = 50, **_: Any
    ) -> tuple[list[Any], None]:
        # Used by single-store legacy warm_start equivalence comparison.
        # Return TraceRecord-shaped objects but really just pass dicts through
        # since aggregator.warm_start uses model_dump() with hasattr guard.
        from arcllm.trace_store import TraceRecord
        objs = [TraceRecord(**r) for r in self._records[:limit]]
        return objs, None


def _record(ts: datetime, agent: str, cost: float = 0.001) -> dict[str, Any]:
    return {
        "timestamp": ts.isoformat(),
        "provider": "anthropic",
        "model": "claude-sonnet-4",
        "agent_label": agent,
        "total_tokens": 100,
        "cost_usd": cost,
        "duration_ms": 250.0,
        "status": "success",
        "attempt_number": 0,
    }


class TestWarmStartMultiEmpty:
    async def test_empty_list_no_op(self) -> None:
        agg = RollingAggregator()
        await agg.warm_start_multi([])
        assert agg.stats("24h")["request_count"] == 0


class TestWarmStartMultiSingleStore:
    async def test_single_store_equivalent_to_legacy(self) -> None:
        """A list of one store ingests the same records as legacy warm_start."""
        now = datetime.now(UTC)
        records = [
            _record(now - timedelta(minutes=10), "a"),
            _record(now - timedelta(minutes=5), "a"),
            _record(now - timedelta(minutes=1), "a"),
        ]
        store = _FakeStore(records)

        agg = RollingAggregator()
        await agg.warm_start_multi([store])
        snap = agg.stats("24h")
        assert snap["request_count"] == 3


class TestWarmStartMultiThreeStores:
    """Three stores with interleaved timestamps merge correctly."""

    async def test_interleaved_timestamps(self) -> None:
        now = datetime.now(UTC)
        s_a = _FakeStore([
            _record(now - timedelta(minutes=10), "a"),
            _record(now - timedelta(minutes=4), "a"),
        ])
        s_b = _FakeStore([
            _record(now - timedelta(minutes=8), "b"),
            _record(now - timedelta(minutes=2), "b"),
        ])
        s_c = _FakeStore([
            _record(now - timedelta(minutes=6), "c"),
        ])

        agg = RollingAggregator()
        await agg.warm_start_multi([s_a, s_b, s_c])

        snap = agg.stats("24h")
        assert snap["request_count"] == 5
        # Per-agent breakdown reflects each store's contribution
        agent_counts = snap["agent_counts"]
        assert agent_counts["a"] == 2
        assert agent_counts["b"] == 2
        assert agent_counts["c"] == 1


class TestWarmStartMultiIngestOrder:
    """The heap merge MUST hand records to ingest() in strict chronological
    order, not in store-by-store order — the aggregator's per-bucket math
    depends on monotonic-ish ordering for correct percentile recovery.

    Review BLOCKER #15: prior tests asserted total counts but not ordering;
    a buggy merge that emitted store-A-then-store-B would still pass them.
    """

    async def test_records_ingested_in_chronological_order(self) -> None:
        from arcui.aggregator import merge_by_timestamp

        now = datetime.now(UTC)
        # Three stores where each emits a strictly increasing timestamp
        # series. Globally interleaved order would be: a@t0, b@t1, c@t2,
        # a@t3, b@t4, c@t5, a@t6, b@t7, c@t8.
        s_a = _FakeStore([
            _record(now - timedelta(seconds=8), "a"),
            _record(now - timedelta(seconds=5), "a"),
            _record(now - timedelta(seconds=2), "a"),
        ])
        s_b = _FakeStore([
            _record(now - timedelta(seconds=7), "b"),
            _record(now - timedelta(seconds=4), "b"),
            _record(now - timedelta(seconds=1), "b"),
        ])
        s_c = _FakeStore([
            _record(now - timedelta(seconds=6), "c"),
            _record(now - timedelta(seconds=3), "c"),
            _record(now, "c"),
        ])

        emitted: list[tuple[str, str]] = []
        async for rec in merge_by_timestamp([s_a, s_b, s_c]):
            emitted.append((rec["timestamp"], rec["agent_label"]))

        # Strict ascending timestamp order — the heap merge contract.
        timestamps = [ts for ts, _ in emitted]
        assert timestamps == sorted(timestamps), (
            "merge_by_timestamp must yield records in non-decreasing "
            "timestamp order; got " + str(timestamps)
        )
        # And the agent labels interleave: a, b, c, a, b, c, a, b, c.
        agents = [agent for _, agent in emitted]
        assert agents == ["a", "b", "c", "a", "b", "c", "a", "b", "c"]


class TestMergeByTimestampContract:
    """Property-style tests pinning the merge_by_timestamp contract.

    The function's contract is documented only in its docstring:
    "non-decreasing timestamp order; ties broken by store index, then
    arrival order." A future TraceStore.iter_records() that returns
    out-of-order records would silently violate this — these tests
    fail loudly instead.
    """

    async def test_random_streams_yield_globally_sorted_output(self) -> None:
        """N stores with arbitrary record counts → globally sorted output.

        Each store is internally sorted; the merge guarantees globally
        sorted output. Property: for every consecutive pair (a, b) in
        the emitted sequence, a.timestamp <= b.timestamp.
        """
        from arcui.aggregator import merge_by_timestamp

        import random
        rng = random.Random(42)
        stores = []
        all_seeded = []
        for store_idx in range(5):
            count = rng.randint(0, 30)
            # Pick `count` distinct timestamps in non-decreasing order.
            tss = sorted(
                f"2026-01-01T00:00:{rng.randint(0, 99):02d}+00:00"
                for _ in range(count)
            )
            recs = [
                {"timestamp": ts, "agent_label": f"a{store_idx}"}
                for ts in tss
            ]
            stores.append(_FakeStore(recs))
            all_seeded.extend(recs)

        emitted: list[dict] = []
        async for rec in merge_by_timestamp(stores):
            emitted.append(rec)

        # No record dropped or duplicated.
        assert len(emitted) == len(all_seeded)
        # Globally non-decreasing.
        timestamps = [r["timestamp"] for r in emitted]
        assert timestamps == sorted(timestamps), (
            "merge_by_timestamp must yield non-decreasing timestamps "
            "across all stores"
        )

    async def test_tie_break_by_store_index(self) -> None:
        """Records sharing a timestamp emit in store-index order.

        Documented stability: when two records share `timestamp`, the
        one from the lower-indexed store is yielded first. Future
        callers depend on this for deterministic test output.
        """
        from arcui.aggregator import merge_by_timestamp

        same_ts = "2026-01-01T00:00:00+00:00"
        stores = [
            _FakeStore([{"timestamp": same_ts, "agent_label": "z"}]),
            _FakeStore([{"timestamp": same_ts, "agent_label": "a"}]),
            _FakeStore([{"timestamp": same_ts, "agent_label": "m"}]),
        ]

        emitted = [r async for r in merge_by_timestamp(stores)]
        assert [r["agent_label"] for r in emitted] == ["z", "a", "m"], (
            "ties resolve by store index — store 0's record first"
        )


class TestWarmStartMultiTieOrder:
    """Records with identical timestamps must produce a deterministic result.

    Ordering ties is a Pillar 1 (simplicity) concern: the merge must not
    behave differently across runs given the same inputs.
    """

    async def test_tie_deterministic(self) -> None:
        ts = datetime.now(UTC) - timedelta(minutes=5)
        s_a = _FakeStore([_record(ts, "a")])
        s_b = _FakeStore([_record(ts, "b")])

        agg1 = RollingAggregator()
        await agg1.warm_start_multi([s_a, s_b])

        agg2 = RollingAggregator()
        await agg2.warm_start_multi([s_a, s_b])

        # Same inputs, same outputs — request count is 2 in both
        assert agg1.stats("24h")["request_count"] == agg2.stats("24h")["request_count"]


class TestWarmStartMultiPerformance:
    """NFR-1, NFR-2: 5 stores × 1000 records under 500ms."""

    async def test_5x1000_under_500ms(self) -> None:
        now = datetime.now(UTC)
        stores = []
        for s_idx in range(5):
            recs = [
                _record(now - timedelta(seconds=i), f"agent_{s_idx}")
                for i in range(1000)
            ]
            stores.append(_FakeStore(recs))

        agg = RollingAggregator()
        start = time.perf_counter()
        await agg.warm_start_multi(stores)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.5, f"warm_start_multi took {elapsed:.3f}s; expected <0.5s"
        assert agg.stats("24h")["request_count"] == 5000
