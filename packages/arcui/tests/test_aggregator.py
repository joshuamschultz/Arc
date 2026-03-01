"""Tests for RollingAggregator — time-bucketed aggregation."""

from arcui.aggregator import Bucket, BucketedWindow, RollingAggregator


def _make_record(**overrides) -> dict:
    """Build a test trace record dict."""
    base = {
        "total_tokens": 150,
        "cost_usd": 0.001,
        "duration_ms": 500.0,
        "model": "claude-sonnet-4",
        "provider": "anthropic",
        "agent_label": "test-agent",
    }
    base.update(overrides)
    return base


class TestBucket:
    def test_ingest_updates_counts(self):
        b = Bucket()
        b.ingest(_make_record())
        assert b.request_count == 1
        assert b.total_tokens == 150
        assert b.total_cost == 0.001

    def test_ingest_tracks_latency(self):
        b = Bucket()
        b.ingest(_make_record(duration_ms=100.0))
        b.ingest(_make_record(duration_ms=500.0))
        assert b.latency_min == 100.0
        assert b.latency_max == 500.0
        assert len(b.latency_samples) == 2

    def test_ingest_tracks_model_stats(self):
        b = Bucket()
        b.ingest(_make_record(model="gpt-4o", cost_usd=0.01, total_tokens=1000))
        b.ingest(_make_record(model="claude-sonnet-4", cost_usd=0.005, total_tokens=500))
        assert "gpt-4o" in b.model_stats
        assert "claude-sonnet-4" in b.model_stats
        assert b.model_stats["gpt-4o"]["request_count"] == 1

    def test_ingest_tracks_provider_counts(self):
        b = Bucket()
        b.ingest(_make_record(provider="anthropic"))
        b.ingest(_make_record(provider="openai"))
        b.ingest(_make_record(provider="anthropic"))
        assert b.provider_counts["anthropic"] == 2
        assert b.provider_counts["openai"] == 1

    def test_reset_clears_all(self):
        b = Bucket()
        b.ingest(_make_record())
        b.reset()
        assert b.request_count == 0
        assert b.total_tokens == 0
        assert len(b.latency_samples) == 0


class TestBucketedWindow:
    def test_ingest_and_snapshot(self):
        w = BucketedWindow(60, 60)
        w.ingest(_make_record(), now=1000.0)
        w.ingest(_make_record(cost_usd=0.002), now=1000.0)

        snap = w.snapshot()
        assert snap["request_count"] == 2
        assert snap["total_tokens"] == 300
        assert snap["total_cost"] == 0.003

    def test_snapshot_percentiles(self):
        w = BucketedWindow(60, 60)
        for lat in [100.0, 200.0, 300.0, 400.0, 500.0]:
            w.ingest(_make_record(duration_ms=lat), now=1000.0)

        snap = w.snapshot()
        assert snap["latency_p50"] == 300.0
        assert snap["latency_min"] == 100.0
        assert snap["latency_max"] == 500.0

    def test_stale_buckets_reset_on_advance(self):
        w = BucketedWindow(3, 60)  # 3 buckets, 60s each
        w.ingest(_make_record(), now=0.0)
        assert w.snapshot()["request_count"] == 1

        # Advance past all buckets
        w.ingest(_make_record(), now=200.0)
        # Old bucket should be cleared, only new one counts
        assert w.snapshot()["request_count"] == 1

    def test_empty_snapshot(self):
        w = BucketedWindow(60, 60)
        snap = w.snapshot()
        assert snap["request_count"] == 0
        assert snap["latency_p50"] == 0.0
        assert snap["total_cost"] == 0.0


class TestRollingAggregator:
    def test_ingest_populates_all_windows(self):
        agg = RollingAggregator()
        agg.ingest(_make_record())

        for window in ("1h", "24h", "7d"):
            s = agg.stats(window)
            assert s["request_count"] == 1

    def test_stats_invalid_window(self):
        agg = RollingAggregator()
        result = agg.stats("30d")
        assert "error" in result

    def test_cost_efficiency_single_model(self):
        agg = RollingAggregator()
        agg.ingest(_make_record(model="claude-sonnet-4", cost_usd=0.01, total_tokens=1000))

        result = agg.cost_efficiency("1h")
        assert len(result["models"]) == 1
        assert result["cheapest_model"] == "claude-sonnet-4"
        assert result["most_used_model"] == "claude-sonnet-4"
        assert result["potential_savings_usd"] == 0.0

    def test_cost_efficiency_multiple_models(self):
        agg = RollingAggregator()
        # Cheap model: 0.001/1000 = 0.000001 per token
        agg.ingest(_make_record(model="cheap", cost_usd=0.001, total_tokens=1000))
        # Expensive model: 0.01/1000 = 0.00001 per token
        agg.ingest(_make_record(model="expensive", cost_usd=0.01, total_tokens=1000))

        result = agg.cost_efficiency("1h")
        assert result["cheapest_model"] == "cheap"
        assert result["potential_savings_usd"] > 0

    def test_cost_efficiency_savings_calculation(self):
        agg = RollingAggregator()
        # Cheap: $0.001 / 1000 tokens = $0.000001/token
        agg.ingest(_make_record(model="cheap", cost_usd=0.001, total_tokens=1000))
        # Expensive: $0.010 / 1000 tokens = $0.000010/token
        agg.ingest(_make_record(model="expensive", cost_usd=0.010, total_tokens=1000))

        result = agg.cost_efficiency("1h")
        # If expensive used cheap model: 1000 * 0.000001 = 0.001
        # Savings = 0.010 - 0.001 = 0.009
        assert abs(result["potential_savings_usd"] - 0.009) < 1e-6
