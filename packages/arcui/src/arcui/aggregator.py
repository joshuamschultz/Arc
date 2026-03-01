"""RollingAggregator — time-bucketed aggregation of TraceRecords.

Three windows: 1h (60 x 1min), 24h (24 x 1hr), 7d (7 x 1day).
Uses simple sorted-sample percentiles (no ddsketch dependency).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Memory cap: max latency samples stored per time bucket.
# 1000 float64s ≈ 8KB per bucket — keeps memory bounded while providing
# accurate percentile estimates for typical request volumes.
_MAX_LATENCY_SAMPLES_PER_BUCKET = 1000

# Max records to replay from TraceStore during warm_start().
# Limits startup time while recovering recent history.
_WARM_START_LIMIT = 500


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
    # Per-model tracking for cost efficiency
    model_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    # Per-provider tracking
    provider_counts: dict[str, int] = field(default_factory=dict)
    # Per-agent tracking
    agent_counts: dict[str, int] = field(default_factory=dict)

    def ingest(self, record_data: dict[str, Any]) -> None:
        """Add a trace record's data to this bucket."""
        self.request_count += 1
        tokens = record_data.get("total_tokens", 0)
        cost = record_data.get("cost_usd", 0.0)
        latency = record_data.get("duration_ms", 0.0)

        self.total_tokens += tokens
        self.total_cost += cost
        self.latency_sum += latency
        self.latency_min = min(self.latency_min, latency)
        self.latency_max = max(self.latency_max, latency)

        if len(self.latency_samples) < _MAX_LATENCY_SAMPLES_PER_BUCKET:
            self.latency_samples.append(latency)

        # Per-model stats
        model = record_data.get("model", "unknown")
        if model not in self.model_stats:
            self.model_stats[model] = {
                "total_cost": 0.0,
                "total_tokens": 0,
                "request_count": 0,
            }
        self.model_stats[model]["total_cost"] += cost
        self.model_stats[model]["total_tokens"] += tokens
        self.model_stats[model]["request_count"] += 1

        # Per-provider
        provider = record_data.get("provider", "unknown")
        self.provider_counts[provider] = self.provider_counts.get(provider, 0) + 1

        # Per-agent
        agent = record_data.get("agent_label")
        if agent:
            self.agent_counts[agent] = self.agent_counts.get(agent, 0) + 1

    def reset(self) -> None:
        """Clear all data in this bucket."""
        self.request_count = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.latency_sum = 0.0
        self.latency_min = float("inf")
        self.latency_max = 0.0
        self.latency_samples.clear()
        self.model_stats.clear()
        self.provider_counts.clear()
        self.agent_counts.clear()


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

        elapsed_buckets = (
            bucket_start - self._current_bucket_start
        ) // self._bucket_duration

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

        self._current_index = (
            self._current_index + elapsed_buckets
        ) % self._bucket_count
        self._current_bucket_start = bucket_start

    def ingest(self, record_data: dict[str, Any], now: float | None = None) -> None:
        """Add record data to the current bucket."""
        t = now if now is not None else time.monotonic()
        self._advance_to(t)
        self._buckets[self._current_index].ingest(record_data)

    def snapshot(self) -> dict[str, Any]:
        """Return aggregated stats across all active buckets."""
        total_requests = 0
        total_tokens = 0
        total_cost = 0.0
        all_latencies: list[float] = []
        latency_min = float("inf")
        latency_max = 0.0
        model_stats: dict[str, dict[str, float]] = {}
        provider_counts: dict[str, int] = {}
        agent_counts: dict[str, int] = {}

        for bucket in self._buckets:
            if bucket.request_count == 0:
                continue
            total_requests += bucket.request_count
            total_tokens += bucket.total_tokens
            total_cost += bucket.total_cost
            latency_min = min(latency_min, bucket.latency_min)
            latency_max = max(latency_max, bucket.latency_max)
            all_latencies.extend(bucket.latency_samples)

            for model, stats in bucket.model_stats.items():
                if model not in model_stats:
                    model_stats[model] = {
                        "total_cost": 0.0,
                        "total_tokens": 0,
                        "request_count": 0,
                    }
                model_stats[model]["total_cost"] += stats["total_cost"]
                model_stats[model]["total_tokens"] += stats["total_tokens"]
                model_stats[model]["request_count"] += stats["request_count"]

            for prov, cnt in bucket.provider_counts.items():
                provider_counts[prov] = provider_counts.get(prov, 0) + cnt

            for ag, cnt in bucket.agent_counts.items():
                agent_counts[ag] = agent_counts.get(ag, 0) + cnt

        # Compute percentiles from sorted samples
        all_latencies.sort()
        n = len(all_latencies)

        def _percentile(p: float) -> float:
            if n == 0:
                return 0.0
            idx = min(math.ceil(p / 100 * n) - 1, n - 1)
            return all_latencies[max(0, idx)]

        return {
            "request_count": total_requests,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "latency_min": latency_min if total_requests > 0 else 0.0,
            "latency_max": latency_max if total_requests > 0 else 0.0,
            "latency_avg": (
                round(sum(all_latencies) / n, 1) if n > 0 else 0.0
            ),
            "latency_p50": round(_percentile(50), 1),
            "latency_p95": round(_percentile(95), 1),
            "latency_p99": round(_percentile(99), 1),
            "model_stats": model_stats,
            "provider_counts": provider_counts,
            "agent_counts": agent_counts,
        }


class RollingAggregator:
    """Three rolling windows: 1h, 24h, 7d.

    Thread-safe. All mutations go through ingest() which acquires the lock.
    stats() returns a snapshot for the requested window.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, BucketedWindow] = {
            "1h": BucketedWindow(60, 60),       # 60 x 1min
            "24h": BucketedWindow(24, 3600),     # 24 x 1hr
            "7d": BucketedWindow(7, 86400),      # 7 x 1day
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
            return {"error": f"Unknown window: {window}. Use 1h, 24h, or 7d."}
        with self._lock:
            snap = w.snapshot()
        snap["window"] = window
        return snap

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
            models.append({
                "model": model,
                "total_cost": round(total_cost, 6),
                "total_tokens": int(total_tokens),
                "cost_per_token": round(cost_per_token, 10),
                "request_count": int(data["request_count"]),
            })

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
        savings_pct = (
            (potential_savings_usd / total_cost * 100)
            if total_cost > 0
            else 0.0
        )

        return {
            "window": window,
            "models": models,
            "cheapest_model": cheapest,
            "most_used_model": most_used,
            "potential_savings_usd": round(potential_savings_usd, 6),
            "potential_savings_pct": round(savings_pct, 1),
        }

    async def warm_start(self, store: Any) -> None:
        """Replay today's traces from store into aggregator."""
        records, _ = await store.query(limit=_WARM_START_LIMIT)
        for rec in reversed(records):  # oldest first
            data = rec.model_dump() if hasattr(rec, "model_dump") else rec
            self.ingest(data)
