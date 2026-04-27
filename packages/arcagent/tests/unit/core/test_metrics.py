"""SPEC-017 Phase 8 Task 8.1-8.2 — Prometheus-compatible metric emitters.

The policy pipeline, proactive engine, and dynamic tool loader all
emit audit events. Metrics exporters convert those events into
counters / histograms / gauges that Prometheus can scrape. This file
verifies the core counters update correctly as events flow through.
"""

from __future__ import annotations


class TestMetricRegistry:
    def test_counter_increments(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment("arc_policy_decisions_total", labels={"layer": "global", "outcome": "deny"})
        registry.increment("arc_policy_decisions_total", labels={"layer": "global", "outcome": "deny"})
        registry.increment("arc_policy_decisions_total", labels={"layer": "agent", "outcome": "allow"})

        counters = registry.counters()
        assert counters[("arc_policy_decisions_total", (("layer", "global"), ("outcome", "deny")))] == 2
        assert counters[("arc_policy_decisions_total", (("layer", "agent"), ("outcome", "allow")))] == 1

    def test_histogram_records_observation(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        for value in (100, 200, 300, 400, 500):
            registry.observe("arc_policy_evaluation_duration_us", value, labels={"layer": "global"})

        hist = registry.histogram_stats(
            "arc_policy_evaluation_duration_us", labels={"layer": "global"}
        )
        assert hist["count"] == 5
        assert hist["sum"] == 1500.0
        # p50 median of 5 observations with linear interpolation → ≈300
        assert 250 < hist["p50"] < 350

    def test_gauge_set(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.set_gauge(
            "arc_schedule_circuit_breaker_state",
            value=1,
            labels={"schedule_id": "heartbeat", "state": "OPEN"},
        )
        gauges = registry.gauges()
        assert gauges[(
            "arc_schedule_circuit_breaker_state",
            (("schedule_id", "heartbeat"), ("state", "OPEN")),
        )] == 1


class TestPolicyMetricsSink:
    def test_policy_audit_event_increments_decisions_total(self) -> None:
        from arcagent.core.metrics import MetricRegistry, policy_audit_to_metrics

        registry = MetricRegistry()
        sink = policy_audit_to_metrics(registry)

        sink(
            "policy.evaluate",
            {
                "layer": "global",
                "decision": "deny",
                "evaluation_time_us": 125,
            },
        )
        counters = registry.counters()
        key = (
            "arc_policy_decisions_total",
            (("layer", "global"), ("outcome", "deny")),
        )
        assert counters[key] == 1

    def test_policy_audit_records_latency_histogram(self) -> None:
        from arcagent.core.metrics import MetricRegistry, policy_audit_to_metrics

        registry = MetricRegistry()
        sink = policy_audit_to_metrics(registry)

        for dur in (50, 100, 200, 400):
            sink(
                "policy.evaluate",
                {
                    "layer": "global",
                    "decision": "allow",
                    "evaluation_time_us": dur,
                },
            )
        stats = registry.histogram_stats(
            "arc_policy_evaluation_duration_us", labels={"layer": "global"}
        )
        assert stats["count"] == 4
        assert stats["sum"] == 750

    def test_cache_hit_tracked(self) -> None:
        from arcagent.core.metrics import MetricRegistry, policy_audit_to_metrics

        registry = MetricRegistry()
        sink = policy_audit_to_metrics(registry)

        sink(
            "policy.evaluate",
            {"layer": "global", "decision": "allow", "evaluation_time_us": 1, "cache_hit": True},
        )
        sink(
            "policy.evaluate",
            {"layer": "global", "decision": "allow", "evaluation_time_us": 80, "cache_hit": False},
        )
        counters = registry.counters()
        hit_key = ("arc_policy_cache_hits_total", (("layer", "global"),))
        miss_key = ("arc_policy_cache_misses_total", (("layer", "global"),))
        assert counters[hit_key] == 1
        assert counters[miss_key] == 1


class TestProactiveMetricsSink:
    def test_circuit_breaker_state_tracked_as_gauge(self) -> None:
        from arcagent.core.metrics import MetricRegistry, proactive_audit_to_metrics

        registry = MetricRegistry()
        sink = proactive_audit_to_metrics(registry)

        sink(
            "skipped_circuit_open",
            {"schedule_id": "heartbeat", "state": "OPEN"},
        )
        gauges = registry.gauges()
        key = (
            "arc_schedule_circuit_breaker_state",
            (("schedule_id", "heartbeat"), ("state", "OPEN")),
        )
        assert gauges[key] == 1

    def test_missed_concurrency_increments_counter(self) -> None:
        from arcagent.core.metrics import MetricRegistry, proactive_audit_to_metrics

        registry = MetricRegistry()
        sink = proactive_audit_to_metrics(registry)

        sink("missed_concurrency", {"schedule_id": "heartbeat"})
        sink("missed_concurrency", {"schedule_id": "heartbeat"})

        counters = registry.counters()
        key = (
            "arc_schedule_missed_concurrency_total",
            (("schedule_id", "heartbeat"),),
        )
        assert counters[key] == 2


class TestMetricRegistryEdgeCases:
    def test_histogram_stats_empty_returns_zeroes(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        stats = registry.histogram_stats("nonexistent_metric")
        assert stats == {"count": 0, "sum": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}

    def test_increment_without_labels_uses_empty_key(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment("arc_heartbeat_ticks_total")
        counters = registry.counters()
        assert counters[("arc_heartbeat_ticks_total", ())] == 1

    def test_increment_value_parameter(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment("arc_heartbeat_ticks_total", value=5.0)
        counters = registry.counters()
        assert counters[("arc_heartbeat_ticks_total", ())] == 5.0

    def test_observe_without_labels(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.observe("arc_tool_dispatch_parallelism", 3.0)
        stats = registry.histogram_stats("arc_tool_dispatch_parallelism")
        assert stats["count"] == 1
        assert stats["sum"] == 3.0

    def test_set_gauge_without_labels(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.set_gauge("arc_schedule_circuit_breaker_state", value=0)
        gauges = registry.gauges()
        assert gauges[("arc_schedule_circuit_breaker_state", ())] == 0


class TestPrometheusExposition:
    """Prometheus text format — enough for a /metrics endpoint scrape."""

    def test_exposition_contains_counters(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment("arc_policy_decisions_total", labels={"layer": "global", "outcome": "deny"})
        text = registry.render_prometheus()
        assert "arc_policy_decisions_total" in text
        assert 'layer="global"' in text
        assert 'outcome="deny"' in text
        assert " 1" in text or " 1.0" in text

    def test_exposition_contains_help_and_type(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment(
            "arc_policy_decisions_total", labels={"layer": "global", "outcome": "allow"}
        )
        text = registry.render_prometheus()
        assert "# TYPE arc_policy_decisions_total counter" in text
        assert "# HELP arc_policy_decisions_total" in text

    def test_exposition_contains_gauges(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.set_gauge(
            "arc_schedule_circuit_breaker_state",
            value=1,
            labels={"schedule_id": "s1", "state": "OPEN"},
        )
        text = registry.render_prometheus()
        assert "arc_schedule_circuit_breaker_state" in text
        assert "# TYPE arc_schedule_circuit_breaker_state gauge" in text
        assert 'schedule_id="s1"' in text

    def test_exposition_contains_histograms(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.observe(
            "arc_policy_evaluation_duration_us",
            100.0,
            labels={"layer": "global"},
        )
        text = registry.render_prometheus()
        assert "arc_policy_evaluation_duration_us_count" in text
        assert "arc_policy_evaluation_duration_us_sum" in text
        assert "# TYPE arc_policy_evaluation_duration_us histogram" in text

    def test_exposition_unknown_metric_uses_fallback_help(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment("arc_custom_unknown_metric")
        text = registry.render_prometheus()
        # Fallback help text: "{name} metric."
        assert "# HELP arc_custom_unknown_metric arc_custom_unknown_metric metric." in text

    def test_exposition_type_header_emitted_once_per_metric(self) -> None:
        from arcagent.core.metrics import MetricRegistry

        registry = MetricRegistry()
        registry.increment(
            "arc_policy_decisions_total", labels={"layer": "l1", "outcome": "allow"}
        )
        registry.increment(
            "arc_policy_decisions_total", labels={"layer": "l2", "outcome": "deny"}
        )
        text = registry.render_prometheus()
        # TYPE line must appear exactly once
        assert text.count("# TYPE arc_policy_decisions_total counter") == 1


class TestSinkIgnoresUnrelatedEvents:
    def test_policy_sink_ignores_non_policy_events(self) -> None:
        from arcagent.core.metrics import MetricRegistry, policy_audit_to_metrics

        registry = MetricRegistry()
        sink = policy_audit_to_metrics(registry)
        # Emit an event that's NOT "policy.evaluate"
        sink("tool.executed", {"layer": "global", "decision": "allow"})
        # Nothing should be incremented
        assert registry.counters() == {}

    def test_policy_sink_no_duration_skips_histogram(self) -> None:
        from arcagent.core.metrics import MetricRegistry, policy_audit_to_metrics

        registry = MetricRegistry()
        sink = policy_audit_to_metrics(registry)
        # Valid policy event but no evaluation_time_us
        sink("policy.evaluate", {"layer": "global", "decision": "allow"})
        # Counter incremented, but no histogram observation
        counters = registry.counters()
        assert counters[("arc_policy_decisions_total", (("layer", "global"), ("outcome", "allow")))] == 1
        stats = registry.histogram_stats(
            "arc_policy_evaluation_duration_us", labels={"layer": "global"}
        )
        assert stats["count"] == 0

    def test_proactive_sink_ignores_unknown_events(self) -> None:
        from arcagent.core.metrics import MetricRegistry, proactive_audit_to_metrics

        registry = MetricRegistry()
        sink = proactive_audit_to_metrics(registry)
        sink("some_other_event", {"schedule_id": "heartbeat"})
        assert registry.counters() == {}
        assert registry.gauges() == {}
