"""FederatedTraceStore — read-only fan-out across multiple TraceStores.

SPEC-019 T2.5 / FR-4. Distinct from JSONLTraceStore (writer-of-one). The
read-many-stores semantic is made explicit at the call site by wrapping a
list of stores; the routes (`/api/traces`) consume this transparently
through the TraceStore Protocol.

Design (Pillar 1 — simplicity, no clever abstractions):
  - query() runs the same filter args against every store, then merges by
    `timestamp` desc and truncates to `limit`. Cursors are compound — a
    base64-JSON envelope of per-store sub-cursors — so the next page picks
    up where this one left off in each underlying store.
  - get() probes every store; first hit wins (trace_id is a UUID4 hex,
    globally unique).
  - iter_records() defers to RollingAggregator's heap merge by exposing
    each store's iter_records() and chaining them.
  - close() forwards to every store. append() and verify_chain() are
    intentionally NOT implemented — this is read-only.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

from arcllm.trace_store import TraceRecord, TraceStore


class FederatedTraceStore:
    """Read-only TraceStore Protocol implementation that fans out across N stores."""

    def __init__(self, stores: list[TraceStore]) -> None:
        self._stores = stores

    async def append(self, record: TraceRecord) -> None:
        """Federated stores are read-only — appending is rejected.

        Writers must target a specific JSONLTraceStore directly. Surfacing
        this as an error rather than silently dropping the write avoids
        lost-data bugs (Pillar 3).
        """
        msg = "FederatedTraceStore is read-only; append to a specific JSONLTraceStore"
        raise NotImplementedError(msg)

    async def query(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        provider: str | None = None,
        agent: str | None = None,
        status: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[list[TraceRecord], str | None]:
        """Fan out the query to every store, merge newest-first, paginate.

        Cursor uses a global timestamp watermark rather than per-store
        sub-cursors. Per-store sub-cursors silently dropped records that
        were truncated by the merge but that the store itself had no
        further pages of (review S-2): the store's cursor advances past
        records the federation never emitted, so the next page skipped
        them entirely. The watermark scheme is correct *and* simpler:

          - Page 1: each store returns its `limit` newest records.
            Merge global top-N. Cursor = (last_ts, [trace_ids at last_ts]).
          - Page N: each store queries with `end=last_ts`; the federation
            skips any record whose trace_id is in `skip_ids`. New cursor
            advances `last_ts` and rebuilds `skip_ids`.

        `skip_ids` exists because timestamps can collide; without it,
        records sharing the boundary timestamp are emitted twice.
        """
        if not self._stores:
            return [], None

        watermark, skip_ids = self._decode_cursor(cursor)
        # If the caller passed an explicit `end`, intersect with our
        # watermark — both upper-bound the next page; the tighter one wins.
        effective_end = self._tighter_end(end, watermark)

        per_store = await self._query_each_store(
            limit=limit,
            provider=provider,
            agent=agent,
            status=status,
            start=start,
            end=effective_end,
        )

        merged = self._merge_top_n(per_store, limit, skip_ids=skip_ids)
        # A store that returned exactly `limit` records may have more
        # behind it; the federation can't tell from this fetch alone, so
        # we MUST emit a cursor so the next page advances. Without this
        # check, the last record in any store with > limit records gets
        # silently dropped at end-of-walk (off-by-one).
        any_store_at_limit = any(len(recs) >= limit for recs, _ in per_store)
        next_cursor = self._build_next_cursor(
            merged, requested=limit, force_cursor=any_store_at_limit
        )
        return [rec for _, _, rec in merged], next_cursor

    async def _query_each_store(
        self,
        *,
        limit: int,
        provider: str | None,
        agent: str | None,
        status: str | None,
        start: str | None,
        end: str | None,
    ) -> list[tuple[list[TraceRecord], int]]:
        """Fan-out: run the same query against every backing store concurrently.

        Per-store I/O is independent — JSONL reads, no shared mutable state.
        `asyncio.gather` runs all K stores in parallel; page latency is
        max(per-store) instead of sum(per-store). Wave 2 review fix:
        at K=100 federal agents this is the difference between sub-second
        and 100x serial overhead.
        """
        per_store_calls = [
            store.query(
                limit=limit,
                cursor=None,
                provider=provider,
                agent=agent,
                status=status,
                start=start,
                end=end,
            )
            for store in self._stores
        ]
        results = await asyncio.gather(*per_store_calls)
        return [(recs, idx) for idx, (recs, _next_sub) in enumerate(results)]

    @staticmethod
    def _merge_top_n(
        per_store: list[tuple[list[TraceRecord], int]],
        limit: int,
        *,
        skip_ids: frozenset[str],
    ) -> list[tuple[str, int, TraceRecord]]:
        """Newest-first global top-`limit` after dedupe by trace_id."""
        flat: list[tuple[str, int, TraceRecord]] = []
        for recs, idx in per_store:
            for rec in recs:
                if rec.trace_id in skip_ids:
                    continue
                flat.append((rec.timestamp, idx, rec))
        flat.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return flat[:limit]

    def _build_next_cursor(
        self,
        merged: list[tuple[str, int, TraceRecord]],
        *,
        requested: int,
        force_cursor: bool = False,
    ) -> str | None:
        """Return cursor pointing at the watermark + records-at-watermark.

        Returns None when the merge produced fewer than `requested` records
        AND no underlying store hit its per-page cap — that's the
        end-of-stream signal. If any store hit its cap, more records may
        live behind it; emit a cursor so the next page can fetch them.
        """
        if not merged:
            return None
        if len(merged) < requested and not force_cursor:
            return None
        last_ts = merged[-1][0]
        # Records sharing the watermark timestamp must be remembered so
        # the next page does not re-emit them when it requests `end=last_ts`.
        skip_ids = [
            rec.trace_id for ts, _idx, rec in merged if ts == last_ts
        ]
        return self._encode_cursor(last_ts, skip_ids)

    @staticmethod
    def _tighter_end(
        caller_end: str | None, watermark_end: str | None
    ) -> str | None:
        """Intersect caller-supplied `end` with our pagination watermark."""
        if caller_end is None:
            return watermark_end
        if watermark_end is None:
            return caller_end
        return min(caller_end, watermark_end)

    async def get(self, trace_id: str) -> TraceRecord | None:
        """Try each store in order; UUIDs are globally unique so first hit wins."""
        for store in self._stores:
            rec = await store.get(trace_id)
            if rec is not None:
                return rec
        return None

    async def verify_chain(self, start_seq: int = 0) -> bool:
        """Federated chains are not single-chain — verify each store individually."""
        for store in self._stores:
            if not await store.verify_chain(start_seq=start_seq):
                return False
        return True

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        """Yield records from every store in chronological merge order.

        Delegates to `arcui.aggregator.merge_by_timestamp` (public) so the
        aggregator's `warm_start_multi` and the federation share one merge
        implementation — see Pillar 2: one writer-of-truth for ordering
        semantics.
        """
        from arcui.aggregator import merge_by_timestamp

        async for record in merge_by_timestamp(self._stores):
            yield record

    async def close(self) -> None:
        for store in self._stores:
            await store.close()

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
    ) -> tuple[str | None, frozenset[str]]:
        """Parse cursor → (watermark_timestamp, skip_trace_ids).

        Malformed input is silently treated as start-of-stream. The cursor
        is opaque to clients; we never raise.
        """
        if not cursor:
            return None, frozenset()
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None, frozenset()
        if not isinstance(parsed, dict):
            return None, frozenset()
        watermark = parsed.get("ts")
        skip_raw = parsed.get("skip", [])
        skip_ids = frozenset(
            str(s) for s in skip_raw if isinstance(s, str)
        ) if isinstance(skip_raw, list) else frozenset()
        return (str(watermark) if watermark else None), skip_ids

    @staticmethod
    def _encode_cursor(watermark: str, skip_ids: list[str]) -> str:
        payload = {"ts": watermark, "skip": list(skip_ids)}
        return base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).decode("ascii")
