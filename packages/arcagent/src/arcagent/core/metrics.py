"""Prometheus-compatible metric registry — SPEC-017 Phase 8 R-061.

Lightweight in-process registry that collects counters, gauges, and
histograms. Exports the Prometheus text exposition format for a
``/metrics`` endpoint. Deliberately not a dependency on
``prometheus_client`` — the registry ships with the agent and has
no external deps.

Provides adapters that turn policy / proactive audit events into
metric updates. Callers wire these adapters as the ``audit_sink``
parameter of the pipeline / engine.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

LabelTuple = tuple[tuple[str, str], ...]
MetricKey = tuple[str, LabelTuple]


_HELP: dict[str, str] = {
    "arc_policy_decisions_total": (
        "Policy-pipeline decisions counted by layer and outcome."
    ),
    "arc_policy_evaluation_duration_us": (
        "Policy evaluation latency in microseconds."
    ),
    "arc_policy_cache_hits_total": "Policy decision cache hits by layer.",
    "arc_policy_cache_misses_total": "Policy decision cache misses by layer.",
    "arc_policy_exceptions_total": (
        "Exceptions raised inside policy layers (MUST be zero in healthy state)."
    ),
    "arc_schedule_circuit_breaker_state": (
        "Current circuit-breaker state as a gauge (1=active)."
    ),
    "arc_schedule_missed_concurrency_total": (
        "Schedule ticks skipped because prior run was still in-flight."
    ),
    "arc_schedule_circuit_skipped_total": (
        "Schedule ticks skipped because circuit breaker was open."
    ),
    "arc_heartbeat_ticks_total": "Heartbeat tick outcomes.",
    "arc_heartbeat_silent_suppressions_total": (
        "Heartbeat ticks suppressed by cheap-model idle signal."
    ),
    "arc_tool_dispatch_parallelism": (
        "Observed concurrency when dispatching tool batches."
    ),
    "arc_dynamic_tool_creations_total": (
        "Dynamic tool creations counted by tier and outcome."
    ),
}


class MetricRegistry:
    """In-process counters / gauges / histograms with Prometheus export."""

    def __init__(self) -> None:
        self._counters: dict[MetricKey, float] = defaultdict(float)
        self._gauges: dict[MetricKey, float] = {}
        self._histograms: dict[MetricKey, list[float]] = defaultdict(list)

    # --- Primitive operations --------------------------------------------

    def increment(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        value: float = 1.0,
    ) -> None:
        self._counters[_key(name, labels)] += value

    def set_gauge(
        self,
        name: str,
        *,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._gauges[_key(name, labels)] = value

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._histograms[_key(name, labels)].append(value)

    # --- Read-side --------------------------------------------------------

    def counters(self) -> dict[MetricKey, float]:
        return dict(self._counters)

    def gauges(self) -> dict[MetricKey, float]:
        return dict(self._gauges)

    def histogram_stats(
        self, name: str, *, labels: dict[str, str] | None = None
    ) -> dict[str, float]:
        """Summary statistics for a histogram series."""
        samples = self._histograms.get(_key(name, labels), [])
        if not samples:
            return {"count": 0, "sum": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
        samples_sorted = sorted(samples)
        total = float(sum(samples_sorted))
        p50 = float(statistics.median(samples_sorted))
        p95 = _percentile(samples_sorted, 95)
        p99 = _percentile(samples_sorted, 99)
        return {
            "count": len(samples_sorted),
            "sum": total,
            "p50": p50,
            "p95": p95,
            "p99": p99,
        }

    # --- Prometheus text exposition --------------------------------------

    def render_prometheus(self) -> str:
        """Emit the Prometheus text format for a ``/metrics`` scrape."""
        lines: list[str] = []
        emitted: set[str] = set()

        for (name, labels), value in sorted(self._counters.items()):
            _ensure_header(lines, emitted, name, "counter")
            lines.append(f"{name}{_format_labels(labels)} {value}")

        for (name, labels), value in sorted(self._gauges.items()):
            _ensure_header(lines, emitted, name, "gauge")
            lines.append(f"{name}{_format_labels(labels)} {value}")

        for (name, labels), _samples in sorted(self._histograms.items()):
            _ensure_header(lines, emitted, name, "histogram")
            stats = self.histogram_stats(name, labels=dict(labels))
            lines.append(f"{name}_count{_format_labels(labels)} {stats['count']}")
            lines.append(f"{name}_sum{_format_labels(labels)} {stats['sum']}")

        return "\n".join(lines) + "\n"


# --- Audit-sink adapters --------------------------------------------------


def policy_audit_to_metrics(
    registry: MetricRegistry,
) -> Callable[[str, dict[str, Any]], None]:
    """Turn ``policy.evaluate`` audit events into metric updates."""

    def sink(event: str, data: dict[str, Any]) -> None:
        if event != "policy.evaluate":
            return
        layer = str(data.get("layer") or "pipeline")
        outcome = str(data.get("decision") or "allow")
        registry.increment(
            "arc_policy_decisions_total",
            labels={"layer": layer, "outcome": outcome},
        )
        duration = data.get("evaluation_time_us")
        if isinstance(duration, int | float):
            registry.observe(
                "arc_policy_evaluation_duration_us",
                float(duration),
                labels={"layer": layer},
            )
        if "cache_hit" in data:
            counter = (
                "arc_policy_cache_hits_total"
                if data["cache_hit"]
                else "arc_policy_cache_misses_total"
            )
            registry.increment(counter, labels={"layer": layer})

    return sink


def proactive_audit_to_metrics(
    registry: MetricRegistry,
) -> Callable[[str, dict[str, Any]], None]:
    """Turn ProactiveEngine events into metric updates."""

    def sink(event: str, data: dict[str, Any]) -> None:
        if event == "skipped_circuit_open":
            sched = str(data.get("schedule_id", ""))
            state = str(data.get("state", ""))
            registry.set_gauge(
                "arc_schedule_circuit_breaker_state",
                value=1,
                labels={"schedule_id": sched, "state": state},
            )
            registry.increment(
                "arc_schedule_circuit_skipped_total",
                labels={"schedule_id": sched},
            )
        elif event == "missed_concurrency":
            sched = str(data.get("schedule_id", ""))
            registry.increment(
                "arc_schedule_missed_concurrency_total",
                labels={"schedule_id": sched},
            )

    return sink


# --- Helpers --------------------------------------------------------------


def _key(name: str, labels: dict[str, str] | None) -> MetricKey:
    """Build a stable key for a metric name + label set."""
    if not labels:
        return (name, ())
    return (name, tuple(sorted(labels.items())))


def _percentile(samples_sorted: list[float], pct: int) -> float:
    if not samples_sorted:
        return 0.0
    idx = max(0, min(len(samples_sorted) - 1, int(len(samples_sorted) * pct / 100)))
    return float(samples_sorted[idx])


def _format_labels(labels: LabelTuple) -> str:
    if not labels:
        return ""
    pairs = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + pairs + "}"


def _ensure_header(
    lines: list[str], emitted: set[str], name: str, mtype: str
) -> None:
    if name in emitted:
        return
    help_text = _HELP.get(name, f"{name} metric.")
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {mtype}")
    emitted.add(name)


__all__ = [
    "MetricRegistry",
    "policy_audit_to_metrics",
    "proactive_audit_to_metrics",
]
