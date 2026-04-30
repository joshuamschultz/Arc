"""RollingAggregator — time-bucketed aggregation of TraceRecords.

Four windows: 1h (60 x 1min), 24h (24 x 1hr), 7d (7 x 1day), 30d (30 x 1day).
Uses simple sorted-sample percentiles (no ddsketch dependency).
"""

from __future__ import annotations

import heapq
import itertools
import math
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


async def merge_by_timestamp(
    stores: list[Any],
) -> AsyncIterator[dict[str, Any]]:
    """Async heap-merge of `iter_records()` from each store, ordered by timestamp.

    Public: also imported by `arcui.federated_store` for `iter_records()`
    fan-out. Pillar 2 — federated_store and aggregator share the same merge
    semantics; centralizing it here avoids drift between writer and reader.

    Returns an async iterator that yields records in non-decreasing
    `timestamp` order. Ties are broken by store index, then by per-store
    arrival order — a stable, deterministic interleaving (Pillar 1).

    Memory: one buffered record per active store. With K stores and N total
    records, the heap holds at most K entries, so peak overhead is O(K).
    """
    iterators = [s.iter_records().__aiter__() for s in stores]
    counter = itertools.count()
    heap: list[tuple[str, int, int, dict[str, Any]]] = []

    async def _push_next(idx: int) -> None:
        try:
            record = await iterators[idx].__anext__()
        except StopAsyncIteration:
            return
        heapq.heappush(
            heap,
            (record.get("timestamp", ""), idx, next(counter), record),
        )

    for i in range(len(iterators)):
        await _push_next(i)

    while heap:
        _ts, idx, _seq, record = heapq.heappop(heap)
        yield record
        await _push_next(idx)


# Memory cap: max latency samples stored per time bucket.
# 1000 float64s ≈ 8KB per bucket — keeps memory bounded while providing
# accurate percentile estimates for typical request volumes.
_MAX_LATENCY_SAMPLES_PER_BUCKET = 1000


def _percentile(samples: list[float], p: float) -> float:
    """Nearest-rank percentile from a sorted-ascending sample list.

    Module-level (Wave 2 simplification): two callers — `RollingAggregator.stats`
    and `RollingAggregator.performance` — used to define their own
    closure variants, the latter using a default-arg trick to capture
    loop-state. Sharing one helper keeps the math in one place and lets
    a future percentile fix (e.g. ddsketch) replace one function instead
    of two near-identical closures.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    idx = min(math.ceil(p / 100 * n) - 1, n - 1)
    return samples[max(0, idx)]


# Max records to replay from TraceStore during warm_start().
# Limits startup time while recovering recent history.
_WARM_START_LIMIT = 500


def _perf_entry() -> dict[str, Any]:
    """Default per-entity performance stats dict."""
    return {
        "total_cost": 0.0,
        "total_tokens": 0,
        "request_count": 0,
        "error_count": 0,
        "retry_count": 0,
        "latency_samples": [],
    }


@dataclass
class Bucket:
    """Single time bucket accumulating trace metrics."""

    request_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    latency_sum: float = 0.0
    latency_min: float = float("inf")
    latency_max: float = 0.0
    latency_samples: list[float] = field(default_factory=list)
    # Reliability counters
    error_count: int = 0
    retry_count: int = 0
    # Per-model tracking for cost efficiency + performance
    model_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per-provider tracking (count + cost)
    provider_counts: dict[str, int] = field(default_factory=dict)
    provider_costs: dict[str, float] = field(default_factory=dict)
    # Per-agent tracking (count + cost + performance)
    agent_counts: dict[str, int] = field(default_factory=dict)
    agent_costs: dict[str, float] = field(default_factory=dict)
    agent_perf: dict[str, dict[str, Any]] = field(default_factory=dict)

    def ingest(self, record_data: dict[str, Any]) -> None:
        """Add a trace record's data to this bucket."""
        self.request_count += 1
        tokens = record_data.get("total_tokens", 0)
        cost = record_data.get("cost_usd", 0.0)
        latency = record_data.get("duration_ms", 0.0)
        status = record_data.get("status", "success")
        attempt = record_data.get("attempt_number", 0)

        self.total_tokens += tokens
        self.total_cost += cost
        self.latency_sum += latency
        self.latency_min = min(self.latency_min, latency)
        self.latency_max = max(self.latency_max, latency)

        if len(self.latency_samples) < _MAX_LATENCY_SAMPLES_PER_BUCKET:
            self.latency_samples.append(latency)

        # Reliability tracking
        if status in ("error", "timeout"):
            self.error_count += 1
        if attempt > 0:
            self.retry_count += 1

        # Per-model stats (with error/retry/latency tracking)
        model = record_data.get("model", "unknown")
        if model not in self.model_stats:
            self.model_stats[model] = _perf_entry()
        ms = self.model_stats[model]
        ms["total_cost"] += cost
        ms["total_tokens"] += tokens
        ms["request_count"] += 1
        if status in ("error", "timeout"):
            ms["error_count"] += 1
        if attempt > 0:
            ms["retry_count"] += 1
        if len(ms["latency_samples"]) < _MAX_LATENCY_SAMPLES_PER_BUCKET:
            ms["latency_samples"].append(latency)

        # Per-provider
        provider = record_data.get("provider", "unknown")
        self.provider_counts[provider] = self.provider_counts.get(provider, 0) + 1
        self.provider_costs[provider] = self.provider_costs.get(provider, 0.0) + cost

        # Per-agent (with performance tracking)
        agent = record_data.get("agent_label") or record_data.get("agent_name") or "unknown"
        if agent:
            self.agent_counts[agent] = self.agent_counts.get(agent, 0) + 1
            self.agent_costs[agent] = self.agent_costs.get(agent, 0.0) + cost
            if agent not in self.agent_perf:
                self.agent_perf[agent] = _perf_entry()
            ap = self.agent_perf[agent]
            ap["total_cost"] += cost
            ap["total_tokens"] += tokens
            ap["request_count"] += 1
            if status in ("error", "timeout"):
                ap["error_count"] += 1
            if attempt > 0:
                ap["retry_count"] += 1
            if len(ap["latency_samples"]) < _MAX_LATENCY_SAMPLES_PER_BUCKET:
                ap["latency_samples"].append(latency)

    def reset(self) -> None:
        """Clear all data in this bucket."""
        self.request_count = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.latency_sum = 0.0
        self.latency_min = float("inf")
        self.latency_max = 0.0
        self.latency_samples.clear()
        self.error_count = 0
        self.retry_count = 0
        self.model_stats.clear()
        self.provider_counts.clear()
        self.provider_costs.clear()
        self.agent_counts.clear()
        self.agent_costs.clear()
        self.agent_perf.clear()


class BucketedWindow:
    """Ring buffer of time buckets for a specific time window."""

    def __init__(self, bucket_count: int, bucket_duration_seconds: int) -> None:
        self._bucket_count = bucket_count
        self._bucket_duration = bucket_duration_seconds
        self._buckets = [Bucket() for _ in range(bucket_count)]
        self._current_index = 0
        self._current_bucket_start: int | None = None

    def _bucket_time(self, t: float) -> int:
        """Quantize time to bucket boundary."""
        return int(t) // self._bucket_duration * self._bucket_duration

    def _advance_to(self, now: float) -> None:
        """Advance the ring buffer to the current time, resetting stale buckets."""
        bucket_start = self._bucket_time(now)

        if self._current_bucket_start is None:
            self._current_bucket_start = bucket_start
            return

        elapsed_buckets = (bucket_start - self._current_bucket_start) // self._bucket_duration

        if elapsed_buckets <= 0:
            return

        if elapsed_buckets >= self._bucket_count:
            # Entire window is stale — reset everything
            for bucket in self._buckets:
                bucket.reset()
        else:
            # Reset only the buckets we're advancing past
            for i in range(1, elapsed_buckets + 1):
                idx = (self._current_index + i) % self._bucket_count
                self._buckets[idx].reset()

        self._current_index = (self._current_index + elapsed_buckets) % self._bucket_count
        self._current_bucket_start = bucket_start

    def ingest(self, record_data: dict[str, Any], now: float | None = None) -> None:
        """Add record data to the current bucket."""
        t = now if now is not None else time.monotonic()
        self._advance_to(t)
        self._buckets[self._current_index].ingest(record_data)

    def timeseries(self) -> list[dict[str, Any]]:
        """Return per-bucket data for chart rendering (oldest to newest)."""
        results: list[dict[str, Any]] = []
        for i in range(self._bucket_count):
            # Walk from oldest to newest
            idx = (self._current_index + 1 + i) % self._bucket_count
            bucket = self._buckets[idx]
            results.append(
                {
                    "request_count": bucket.request_count,
                    "total_tokens": bucket.total_tokens,
                    "total_cost": round(bucket.total_cost, 6),
                    "latency_avg": (
                        round(bucket.latency_sum / bucket.request_count, 1)
                        if bucket.request_count > 0
                        else 0.0
                    ),
                }
            )
        return results

    def snapshot(self) -> dict[str, Any]:
        """Return aggregated stats across all active buckets."""
        total_requests = 0
        total_tokens = 0
        total_cost = 0.0
        total_errors = 0
        total_retries = 0
        all_latencies: list[float] = []
        latency_min = float("inf")
        latency_max = 0.0
        model_stats: dict[str, dict[str, Any]] = {}
        provider_counts: dict[str, int] = {}
        provider_costs: dict[str, float] = {}
        agent_counts: dict[str, int] = {}
        agent_costs: dict[str, float] = {}
        agent_perf: dict[str, dict[str, Any]] = {}

        for bucket in self._buckets:
            if bucket.request_count == 0:
                continue
            total_requests += bucket.request_count
            total_tokens += bucket.total_tokens
            total_cost += bucket.total_cost
            total_errors += bucket.error_count
            total_retries += bucket.retry_count
            latency_min = min(latency_min, bucket.latency_min)
            latency_max = max(latency_max, bucket.latency_max)
            all_latencies.extend(bucket.latency_samples)

            for model, stats in bucket.model_stats.items():
                if model not in model_stats:
                    model_stats[model] = _perf_entry()
                ms = model_stats[model]
                ms["total_cost"] += stats["total_cost"]
                ms["total_tokens"] += stats["total_tokens"]
                ms["request_count"] += stats["request_count"]
                ms["error_count"] += stats.get("error_count", 0)
                ms["retry_count"] += stats.get("retry_count", 0)
                ms["latency_samples"].extend(stats.get("latency_samples", []))

            for prov, cnt in bucket.provider_counts.items():
                provider_counts[prov] = provider_counts.get(prov, 0) + cnt
            for prov, cost in bucket.provider_costs.items():
                provider_costs[prov] = provider_costs.get(prov, 0.0) + cost

            for ag, cnt in bucket.agent_counts.items():
                agent_counts[ag] = agent_counts.get(ag, 0) + cnt
            for ag, cost in bucket.agent_costs.items():
                agent_costs[ag] = agent_costs.get(ag, 0.0) + cost
            for ag, perf in bucket.agent_perf.items():
                if ag not in agent_perf:
                    agent_perf[ag] = _perf_entry()
                ap = agent_perf[ag]
                ap["total_cost"] += perf["total_cost"]
                ap["total_tokens"] += perf["total_tokens"]
                ap["request_count"] += perf["request_count"]
                ap["error_count"] += perf.get("error_count", 0)
                ap["retry_count"] += perf.get("retry_count", 0)
                ap["latency_samples"].extend(perf.get("latency_samples", []))

        # Compute percentiles from sorted samples
        all_latencies.sort()
        n = len(all_latencies)

        return {
            "request_count": total_requests,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "error_count": total_errors,
            "retry_count": total_retries,
            "latency_min": latency_min if total_requests > 0 else 0.0,
            "latency_max": latency_max if total_requests > 0 else 0.0,
            "latency_avg": (round(sum(all_latencies) / n, 1) if n > 0 else 0.0),
            "latency_p50": round(_percentile(all_latencies, 50), 1),
            "latency_p95": round(_percentile(all_latencies, 95), 1),
            "latency_p99": round(_percentile(all_latencies, 99), 1),
            "model_stats": {
                k: {kk: vv for kk, vv in v.items() if kk != "latency_samples"}
                for k, v in model_stats.items()
            },
            "provider_counts": provider_counts,
            "provider_costs": {k: round(v, 6) for k, v in provider_costs.items()},
            "agent_counts": agent_counts,
            "agent_costs": {k: round(v, 6) for k, v in agent_costs.items()},
            "agent_perf": {
                k: {kk: vv for kk, vv in v.items() if kk != "latency_samples"}
                for k, v in agent_perf.items()
            },
        }


class RollingAggregator:
    """Four rolling windows: 1h, 24h, 7d, 30d.

    Thread-safe. All mutations go through ingest() which acquires the lock.
    stats() returns a snapshot for the requested window.

    The 30d window was added so the global Telemetry page shows historical
    activity for agents that have not pushed traces recently — without it,
    a workspace whose newest trace is N>7 days old looks identical to one
    with no traces at all.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, BucketedWindow] = {
            "1h": BucketedWindow(60, 60),       # 60 x 1min
            "24h": BucketedWindow(24, 3600),    # 24 x 1hr
            "7d": BucketedWindow(7, 86400),     # 7 x 1day
            "30d": BucketedWindow(30, 86400),   # 30 x 1day
        }

    def ingest(self, record_data: dict[str, Any]) -> None:
        """Ingest a trace record into all windows."""
        now = time.monotonic()
        with self._lock:
            for window in self._windows.values():
                window.ingest(record_data, now=now)

    def stats(self, window: str = "24h") -> dict[str, Any]:
        """Return aggregated stats for the specified window."""
        w = self._windows.get(window)
        if w is None:
            return {"error": f"Unknown window: {window}. Use 1h, 24h, 7d, or 30d."}
        with self._lock:
            snap = w.snapshot()
        snap["window"] = window
        return snap

    def timeseries(self, window: str = "24h") -> dict[str, Any]:
        """Return per-bucket timeseries data for chart rendering."""
        w = self._windows.get(window)
        if w is None:
            return {"error": f"Unknown window: {window}. Use 1h, 24h, 7d, or 30d."}
        with self._lock:
            buckets = w.timeseries()
        return {"window": window, "buckets": buckets}

    def cost_efficiency(self, window: str = "24h") -> dict[str, Any]:
        """Compute per-model cost efficiency from aggregated trace data."""
        snap = self.stats(window)
        if "error" in snap:
            return snap

        model_stats = snap.get("model_stats", {})
        models = []
        for model, data in model_stats.items():
            total_tokens = data["total_tokens"]
            total_cost = data["total_cost"]
            cost_per_token = total_cost / total_tokens if total_tokens > 0 else 0.0
            models.append(
                {
                    "model": model,
                    "total_cost": round(total_cost, 6),
                    "total_tokens": int(total_tokens),
                    "cost_per_token": round(cost_per_token, 10),
                    "request_count": int(data["request_count"]),
                }
            )

        # Sort by cost_per_token ascending (cheapest first)
        models.sort(key=lambda m: m["cost_per_token"])

        cheapest = models[0]["model"] if models else None
        most_used = max(models, key=lambda m: m["request_count"])["model"] if models else None

        # Calculate potential savings: if all requests used cheapest model
        potential_savings_usd = 0.0
        if len(models) > 1 and cheapest:
            cheapest_cpt = models[0]["cost_per_token"]
            for m in models[1:]:
                if m["total_tokens"] > 0:
                    actual = m["total_cost"]
                    hypothetical = m["total_tokens"] * cheapest_cpt
                    potential_savings_usd += actual - hypothetical

        total_cost = snap.get("total_cost", 0.0)
        savings_pct = (potential_savings_usd / total_cost * 100) if total_cost > 0 else 0.0

        return {
            "window": window,
            "models": models,
            "cheapest_model": cheapest,
            "most_used_model": most_used,
            "potential_savings_usd": round(potential_savings_usd, 6),
            "potential_savings_pct": round(savings_pct, 1),
        }

    def performance(self, window: str = "24h") -> dict[str, Any]:
        """Compute per-model and per-agent performance with percentiles."""
        w = self._windows.get(window)
        if w is None:
            return {"error": f"Unknown window: {window}. Use 1h, 24h, 7d, or 30d."}

        # Collect raw latency samples per model and per agent across buckets
        model_agg: dict[str, dict[str, Any]] = {}
        agent_agg: dict[str, dict[str, Any]] = {}

        with self._lock:
            for bucket in w._buckets:
                for model, stats in bucket.model_stats.items():
                    if model not in model_agg:
                        model_agg[model] = _perf_entry()
                    ma = model_agg[model]
                    ma["total_cost"] += stats["total_cost"]
                    ma["total_tokens"] += stats["total_tokens"]
                    ma["request_count"] += stats["request_count"]
                    ma["error_count"] += stats.get("error_count", 0)
                    ma["retry_count"] += stats.get("retry_count", 0)
                    ma["latency_samples"].extend(stats.get("latency_samples", []))

                for agent, perf in bucket.agent_perf.items():
                    if agent not in agent_agg:
                        agent_agg[agent] = _perf_entry()
                    aa = agent_agg[agent]
                    aa["total_cost"] += perf["total_cost"]
                    aa["total_tokens"] += perf["total_tokens"]
                    aa["request_count"] += perf["request_count"]
                    aa["error_count"] += perf.get("error_count", 0)
                    aa["retry_count"] += perf.get("retry_count", 0)
                    aa["latency_samples"].extend(perf.get("latency_samples", []))

        def _build_rows(agg: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
            rows = []
            for name, data in agg.items():
                samples = sorted(data["latency_samples"])
                n = len(samples)
                req = data["request_count"]
                err = data["error_count"]
                success_rate = round((req - err) / req * 100, 1) if req > 0 else 0.0

                rows.append(
                    {
                        "name": name,
                        "request_count": req,
                        "error_count": err,
                        "retry_count": data["retry_count"],
                        "success_rate": success_rate,
                        "total_cost": round(data["total_cost"], 6),
                        "total_tokens": int(data["total_tokens"]),
                        "latency_avg": round(sum(samples) / n, 1) if n > 0 else 0.0,
                        "latency_p50": round(_percentile(samples, 50), 1),
                        "latency_p95": round(_percentile(samples, 95), 1),
                    }
                )
            rows.sort(key=lambda r: r["request_count"], reverse=True)
            return rows

        return {
            "window": window,
            "models": _build_rows(model_agg),
            "agents": _build_rows(agent_agg),
        }

    async def warm_start_multi(self, stores: list[Any]) -> None:
        """Replay all records from N TraceStores in chronological order.

        SPEC-019 T2.3, FR-5. Heap-merges by `timestamp` across stores so the
        earliest record from any store is ingested first. Uses each store's
        `iter_records()` so memory stays bounded (one buffered record per
        store, not full files).

        Wave 2 perf fix: skips records older than the largest window
        (7 days) BEFORE acquiring the aggregator lock. Records past that
        horizon contribute to nothing and were previously being walked
        through every window's per-bucket math (3 lock takes per record)
        only to be filtered out individually. At 100 agents x 10K records
        with most records older than 7d, this prefilter eliminates ~99%
        of the lock contention during cold start.

        For an empty list, returns without touching the aggregator.
        """
        if not stores:
            return

        now_mono = time.monotonic()
        now_wall = time.time()

        # Pre-advance every window so historical bucket math is correct
        # before any ingest call.
        with self._lock:
            for window in self._windows.values():
                window._advance_to(now_mono)

        max_window_seconds = max(
            w._bucket_count * w._bucket_duration for w in self._windows.values()
        )

        async for record in merge_by_timestamp(stores):
            ts_str = record.get("timestamp", "")
            age_seconds = self._timestamp_age(ts_str, now_wall)
            if age_seconds >= max_window_seconds:
                # Older than every window; nothing to ingest. Cheaper
                # to short-circuit here than to walk three windows under
                # the lock per record.
                continue
            self._ingest_historical(record, age_seconds, now_mono)

    async def warm_start(self, store: Any) -> None:
        """Replay recent traces from store into aggregator.

        Places each trace into the correct time bucket based on its age
        relative to now. Uses direct bucket placement instead of sequential
        replay to handle large time gaps correctly.
        """
        records, _ = await store.query(limit=_WARM_START_LIMIT)
        if not records:
            return

        now_mono = time.monotonic()
        now_wall = time.time()

        # First, advance all windows to current time so bucket indices are set
        with self._lock:
            for window in self._windows.values():
                window._advance_to(now_mono)

        for rec in records:
            data = rec.model_dump() if hasattr(rec, "model_dump") else rec
            ts_str = data.get("timestamp", "")
            age_seconds = self._timestamp_age(ts_str, now_wall)
            self._ingest_historical(data, age_seconds, now_mono)

    @staticmethod
    def _timestamp_age(ts_str: str, now_wall: float) -> float:
        """Return age in seconds of an ISO 8601 timestamp. 0 if unparseable."""
        if not ts_str:
            return 0.0
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0.0, now_wall - dt.timestamp())
        except (ValueError, OSError):
            return 0.0

    def _ingest_historical(
        self,
        record_data: dict[str, Any],
        age_seconds: float,
        now_mono: float,
    ) -> None:
        """Place a historical record into the correct bucket by age.

        Calculates how many buckets back from current the record belongs
        and ingests directly into that bucket. Skips records older than
        the window's total span.
        """
        with self._lock:
            for window in self._windows.values():
                total_span = window._bucket_count * window._bucket_duration
                if age_seconds >= total_span:
                    continue  # Too old for this window

                buckets_back = int(age_seconds / window._bucket_duration)
                if buckets_back >= window._bucket_count:
                    continue

                idx = (window._current_index - buckets_back) % window._bucket_count
                window._buckets[idx].ingest(record_data)
