"""Store-backed telemetry aggregation (SPEC-026 FR-5).

The old push pipeline (``RollingAggregator`` fed by a live ``/ws`` stream) is
gone. The database *is* the aggregate now: ``/api/stats``,
``/api/stats/timeseries``, ``/api/performance`` and ``/api/cost-efficiency``
recompute on read from arcstore ``llm_calls`` rows in a single pass. Read-on-
demand is cheap for the single-operator scale this serves (ADR-022, §11.6).

These are pure functions over plain dicts (one ``llm_calls`` row each) so they
are trivially testable and carry no backend coupling. The window cutoff is
applied by the caller via the ISO-8601 ``ts`` column; bucketing for the
timeseries is wall-clock aligned to the same boundaries the front-end charts
expect.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

# bucket_count, bucket_duration_seconds — mirrors the chart granularity the
# front-end renders for each window selector value.
_TIMESERIES_SHAPE: dict[str, tuple[int, int]] = {
    "1h": (60, 60),  # 60 x 1 min
    "24h": (24, 3600),  # 24 x 1 hr
    "7d": (7, 86_400),  # 7 x 1 day
    "30d": (30, 86_400),  # 30 x 1 day
}


def _percentile(samples_sorted: list[float], p: float) -> float:
    """Nearest-rank percentile from an ascending sample list (matches legacy)."""
    n = len(samples_sorted)
    if n == 0:
        return 0.0
    idx = min(math.ceil(p / 100 * n) - 1, n - 1)
    return samples_sorted[max(0, idx)]


def _tokens(row: dict[str, Any]) -> int:
    return (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)


def _is_error(row: dict[str, Any]) -> bool:
    # The producer records ``ok``/``error`` outcomes; treat anything non-ok as
    # a failure for reliability counting.
    return (row.get("outcome") or "ok") != "ok"


def _agent_of(row: dict[str, Any]) -> str:
    return row.get("agent_label") or row.get("actor_did") or "unknown"


def _perf_entry() -> dict[str, Any]:
    return {
        "total_cost": 0.0,
        "total_tokens": 0,
        "request_count": 0,
        "error_count": 0,
        "latency_samples": [],
    }


def _accumulate(entry: dict[str, Any], row: dict[str, Any]) -> None:
    entry["total_cost"] += row.get("cost_usd") or 0.0
    entry["total_tokens"] += _tokens(row)
    entry["request_count"] += 1
    if _is_error(row):
        entry["error_count"] += 1
    latency = row.get("latency_ms")
    if latency is not None:
        entry["latency_samples"].append(float(latency))


def compute_stats(rows: list[dict[str, Any]], *, window: str) -> dict[str, Any]:
    """Aggregate LLM telemetry over ``rows`` into the ``/api/stats`` shape."""
    total_cost = 0.0
    total_tokens = 0
    error_count = 0
    latencies: list[float] = []
    model_stats: dict[str, dict[str, Any]] = {}
    provider_counts: dict[str, int] = {}
    provider_costs: dict[str, float] = {}
    agent_counts: dict[str, int] = {}
    agent_costs: dict[str, float] = {}
    agent_perf: dict[str, dict[str, Any]] = {}

    for row in rows:
        cost = row.get("cost_usd") or 0.0
        total_cost += cost
        total_tokens += _tokens(row)
        if _is_error(row):
            error_count += 1
        latency = row.get("latency_ms")
        if latency is not None:
            latencies.append(float(latency))

        model = row.get("model") or "unknown"
        _accumulate(model_stats.setdefault(model, _perf_entry()), row)

        provider = row.get("provider") or "unknown"
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        provider_costs[provider] = provider_costs.get(provider, 0.0) + cost

        agent = _agent_of(row)
        agent_counts[agent] = agent_counts.get(agent, 0) + 1
        agent_costs[agent] = agent_costs.get(agent, 0.0) + cost
        _accumulate(agent_perf.setdefault(agent, _perf_entry()), row)

    latencies.sort()
    n = len(latencies)

    def _strip(d: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            k: {kk: vv for kk, vv in v.items() if kk != "latency_samples"}
            for k, v in d.items()
        }

    return {
        "window": window,
        "request_count": len(rows),
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
        "error_count": error_count,
        "retry_count": 0,
        "latency_avg": round(sum(latencies) / n, 1) if n else 0.0,
        "latency_p50": round(_percentile(latencies, 50), 1),
        "latency_p95": round(_percentile(latencies, 95), 1),
        "latency_p99": round(_percentile(latencies, 99), 1),
        "model_stats": _strip(model_stats),
        "provider_counts": provider_counts,
        "provider_costs": {k: round(v, 6) for k, v in provider_costs.items()},
        "agent_counts": agent_counts,
        "agent_costs": {k: round(v, 6) for k, v in agent_costs.items()},
        "agent_perf": _strip(agent_perf),
    }


def compute_llm_by_identity(rows: list[dict[str, Any]], *, window: str) -> dict[str, Any]:
    """Per-identity LLM rollup (SPEC-028 FR-4 / UC-3) — parent vs each child.

    Groups ``llm_calls`` by ``agent_label`` (falling back to ``actor_did``), so a
    spawned child carrying a distinct label separates cleanly from its parent.
    Cost lives at the leaf and is summed per identity on read — a parent total
    never absorbs its children's spend (no double-count by construction).
    """
    identities: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = identities.setdefault(_agent_of(row), _perf_entry())
        _accumulate(entry, row)
    return {
        "window": window,
        "identities": [
            {
                "identity": name,
                "request_count": int(e["request_count"]),
                "error_count": int(e["error_count"]),
                "total_tokens": int(e["total_tokens"]),
                "total_cost": round(e["total_cost"], 6),
            }
            for name, e in sorted(identities.items(), key=lambda kv: -kv[1]["total_cost"])
        ],
    }


def compute_cost_efficiency(rows: list[dict[str, Any]], *, window: str) -> dict[str, Any]:
    """Per-model cost-efficiency ranking + potential single-model savings."""
    stats = compute_stats(rows, window=window)
    models = []
    for model, data in stats["model_stats"].items():
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
    models.sort(key=lambda m: m["cost_per_token"])

    cheapest = models[0]["model"] if models else None
    most_used = max(models, key=lambda m: m["request_count"])["model"] if models else None

    potential_savings = 0.0
    if len(models) > 1:
        cheapest_cpt = models[0]["cost_per_token"]
        for m in models[1:]:
            if m["total_tokens"] > 0:
                potential_savings += m["total_cost"] - m["total_tokens"] * cheapest_cpt

    total_cost = stats["total_cost"]
    savings_pct = (potential_savings / total_cost * 100) if total_cost > 0 else 0.0
    return {
        "window": window,
        "models": models,
        "cheapest_model": cheapest,
        "most_used_model": most_used,
        "potential_savings_usd": round(potential_savings, 6),
        "potential_savings_pct": round(savings_pct, 1),
    }


def compute_performance(rows: list[dict[str, Any]], *, window: str) -> dict[str, Any]:
    """Per-model and per-agent performance with success rate + percentiles."""
    model_agg: dict[str, dict[str, Any]] = {}
    agent_agg: dict[str, dict[str, Any]] = {}
    for row in rows:
        _accumulate(model_agg.setdefault(row.get("model") or "unknown", _perf_entry()), row)
        _accumulate(agent_agg.setdefault(_agent_of(row), _perf_entry()), row)

    def _rows(agg: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for name, data in agg.items():
            samples = sorted(data["latency_samples"])
            n = len(samples)
            req = data["request_count"]
            err = data["error_count"]
            out.append(
                {
                    "name": name,
                    "request_count": req,
                    "error_count": err,
                    "retry_count": 0,
                    "success_rate": round((req - err) / req * 100, 1) if req > 0 else 0.0,
                    "total_cost": round(data["total_cost"], 6),
                    "total_tokens": int(data["total_tokens"]),
                    "latency_avg": round(sum(samples) / n, 1) if n else 0.0,
                    "latency_p50": round(_percentile(samples, 50), 1),
                    "latency_p95": round(_percentile(samples, 95), 1),
                }
            )
        out.sort(key=lambda r: r["request_count"], reverse=True)
        return out

    return {"window": window, "models": _rows(model_agg), "agents": _rows(agent_agg)}


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def compute_timeseries(rows: list[dict[str, Any]], *, window: str) -> dict[str, Any]:
    """Per-bucket request/token/cost/latency series (oldest to newest)."""
    bucket_count, duration = _TIMESERIES_SHAPE.get(window, _TIMESERIES_SHAPE["24h"])
    now = datetime.now(UTC).timestamp()
    newest_start = int(now) // duration * duration
    # Bucket index 0 == oldest; the newest bucket covers [newest_start, now].
    oldest_start = newest_start - duration * (bucket_count - 1)

    buckets = [
        {"request_count": 0, "total_tokens": 0, "total_cost": 0.0, "_latency_sum": 0.0}
        for _ in range(bucket_count)
    ]
    for row in rows:
        t = _epoch(row.get("ts"))
        if t is None:
            continue
        bucket_start = int(t) // duration * duration
        idx = (bucket_start - oldest_start) // duration
        if 0 <= idx < bucket_count:
            b = buckets[idx]
            b["request_count"] += 1
            b["total_tokens"] += _tokens(row)
            b["total_cost"] += row.get("cost_usd") or 0.0
            b["_latency_sum"] += float(row.get("latency_ms") or 0.0)

    out = []
    for b in buckets:
        req = b["request_count"]
        out.append(
            {
                "request_count": req,
                "total_tokens": b["total_tokens"],
                "total_cost": round(b["total_cost"], 6),
                "latency_avg": round(b["_latency_sum"] / req, 1) if req else 0.0,
            }
        )
    return {"window": window, "buckets": out}
