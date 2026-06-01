"""Store-backed stats computation (SPEC-026 FR-5).

The RollingAggregator push pipeline is gone; ``/api/stats``,
``/api/stats/timeseries``, ``/api/performance`` and ``/api/cost-efficiency``
are now computed on read from arcstore ``llm_calls`` rows. These tests pin the
JSON contract the front-end consumes so the cutover preserves it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arcui.observe_stats import (
    compute_cost_efficiency,
    compute_performance,
    compute_runs,
    compute_stats,
    compute_timeseries,
)


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "record_id": "r",
        "ts": datetime.now(UTC).isoformat(),
        "model": "gpt-4",
        "provider": "openai",
        "agent_label": "alice",
        "actor_did": "did:arc:alice",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": 0.01,
        "latency_ms": 200.0,
        "outcome": "ok",
    }
    base.update(kw)
    return base


class TestComputeStats:
    def test_totals_and_groupings(self) -> None:
        rows = [
            _row(model="gpt-4", provider="openai", agent_label="alice", cost_usd=0.02),
            _row(model="haiku", provider="anthropic", agent_label="bob", cost_usd=0.001),
        ]
        stats = compute_stats(rows, window="24h")
        assert stats["window"] == "24h"
        assert stats["request_count"] == 2
        assert stats["total_tokens"] == 300  # (100+50) * 2
        assert round(stats["total_cost"], 6) == 0.021
        assert stats["model_stats"]["gpt-4"]["request_count"] == 1
        assert stats["provider_counts"] == {"openai": 1, "anthropic": 1}
        assert stats["agent_counts"] == {"alice": 1, "bob": 1}

    def test_error_outcome_counts_as_error(self) -> None:
        rows = [_row(outcome="ok"), _row(outcome="error")]
        stats = compute_stats(rows, window="24h")
        assert stats["error_count"] == 1

    def test_latency_percentiles_present(self) -> None:
        rows = [_row(latency_ms=float(x)) for x in range(1, 101)]
        stats = compute_stats(rows, window="24h")
        assert stats["latency_p95"] >= stats["latency_p50"]
        assert stats["latency_avg"] > 0

    def test_empty_rows_are_well_formed(self) -> None:
        stats = compute_stats([], window="1h")
        assert stats["request_count"] == 0
        assert stats["total_cost"] == 0.0
        assert stats["model_stats"] == {}


class TestCostEfficiency:
    def test_ranks_cheapest_first(self) -> None:
        rows = [
            _row(model="expensive", cost_usd=1.0, prompt_tokens=10, completion_tokens=0),
            _row(model="cheap", cost_usd=0.001, prompt_tokens=10, completion_tokens=0),
        ]
        eff = compute_cost_efficiency(rows, window="24h")
        assert eff["cheapest_model"] == "cheap"
        assert eff["models"][0]["model"] == "cheap"
        assert eff["potential_savings_usd"] >= 0.0


class TestPerformance:
    def test_success_rate_and_percentiles(self) -> None:
        rows = [_row(outcome="ok", latency_ms=10.0) for _ in range(9)]
        rows.append(_row(outcome="error", latency_ms=10.0))
        perf = compute_performance(rows, window="24h")
        model_row = next(m for m in perf["models"] if m["name"] == "gpt-4")
        assert model_row["request_count"] == 10
        assert model_row["error_count"] == 1
        assert model_row["success_rate"] == 90.0
        assert "latency_p95" in model_row


class TestTimeseries:
    def test_buckets_count_matches_window(self) -> None:
        ts = compute_timeseries([_row()], window="24h")
        assert ts["window"] == "24h"
        assert len(ts["buckets"]) == 24  # 24 x 1h buckets

    def test_old_rows_fall_outside_recent_buckets(self) -> None:
        old = _row(ts=(datetime.now(UTC) - timedelta(days=2)).isoformat())
        ts = compute_timeseries([old], window="24h")
        # A 2-day-old row contributes to no 24h bucket.
        assert sum(b["request_count"] for b in ts["buckets"]) == 0


class TestLLMByIdentity:
    def test_llm_by_identity(self) -> None:
        """Task 4.3 — separate parent vs child LLM spend by agent_label."""
        from arcui.observe_stats import compute_llm_by_identity

        rows = [
            _row(agent_label="parent", cost_usd=0.02, prompt_tokens=100, completion_tokens=50),
            _row(agent_label="researcher:d1", cost_usd=0.01, prompt_tokens=40, completion_tokens=10),
            _row(agent_label="researcher:d1", cost_usd=0.03, prompt_tokens=60, completion_tokens=20),
        ]
        result = compute_llm_by_identity(rows, window="24h")
        by = {r["identity"]: r for r in result["identities"]}
        assert set(by) == {"parent", "researcher:d1"}
        assert by["parent"]["request_count"] == 1
        assert by["researcher:d1"]["request_count"] == 2
        assert abs(by["researcher:d1"]["total_cost"] - 0.04) < 1e-9
        # Parent total does NOT absorb the child's spend (UC-3).
        assert abs(by["parent"]["total_cost"] - 0.02) < 1e-9


class TestComputeRuns:
    """A run = one user-question→final-response cycle, keyed by request_id.
    compute_runs folds a run's run/tool/llm spool rows into one summary row."""

    def _events(self) -> list[dict[str, object]]:
        # One run with: 2 turns, 1 tool call, 2 llm calls; one stray other run.
        return [
            {"kind": "run_event", "request_id": "run-1", "actor_did": "did:a",
             "agent_label": "alice", "name": "turn.start", "ts": "2026-05-31T00:00:01+00:00"},
            {"kind": "llm_call", "request_id": "run-1", "actor_did": "did:a",
             "agent_label": "alice", "model": "claude", "prompt_tokens": 100,
             "completion_tokens": 50, "cost_usd": 0.002, "outcome": "ok",
             "ts": "2026-05-31T00:00:02+00:00"},
            {"kind": "tool_event", "request_id": "run-1", "actor_did": "did:a",
             "tool_name": "web.fetch", "phase": "start", "ts": "2026-05-31T00:00:03+00:00"},
            {"kind": "tool_event", "request_id": "run-1", "actor_did": "did:a",
             "tool_name": "web.fetch", "phase": "end", "outcome": "ok",
             "ts": "2026-05-31T00:00:04+00:00"},
            {"kind": "run_event", "request_id": "run-1", "actor_did": "did:a",
             "name": "turn.start", "ts": "2026-05-31T00:00:05+00:00"},
            {"kind": "llm_call", "request_id": "run-1", "actor_did": "did:a",
             "agent_label": "alice", "model": "claude", "prompt_tokens": 80,
             "completion_tokens": 20, "cost_usd": 0.001, "outcome": "ok",
             "ts": "2026-05-31T00:00:06+00:00"},
            {"kind": "run_event", "request_id": "run-1", "actor_did": "did:a",
             "name": "loop.complete", "ts": "2026-05-31T00:00:07+00:00"},
            # Older, separate run.
            {"kind": "run_event", "request_id": "run-0", "actor_did": "did:a",
             "name": "turn.start", "ts": "2026-05-30T00:00:01+00:00"},
        ]

    def test_groups_and_summarizes(self) -> None:
        runs = compute_runs(self._events())
        assert [r["run_id"] for r in runs] == ["run-1", "run-0"]  # newest first
        r = runs[0]
        assert r["agent"] == "alice"
        assert r["turns"] == 2
        assert r["tool_calls"] == 1  # one tool invocation (counted at phase=start)
        assert r["llm_calls"] == 2
        assert r["total_tokens"] == 250
        assert abs(r["cost_usd"] - 0.003) < 1e-9
        assert r["status"] == "completed"
        assert r["started_at"] == "2026-05-31T00:00:01+00:00"
        assert r["ended_at"] == "2026-05-31T00:00:07+00:00"
        assert round(r["duration_ms"]) == 6000

    def test_status_error_when_any_failure(self) -> None:
        events = self._events()
        events.append({"kind": "tool_event", "request_id": "run-1", "actor_did": "did:a",
                       "tool_name": "web.fetch", "phase": "error", "outcome": "error",
                       "ts": "2026-05-31T00:00:08+00:00"})
        runs = compute_runs(events)
        assert runs[0]["status"] == "error"

    def test_status_running_without_completion(self) -> None:
        runs = compute_runs([
            {"kind": "run_event", "request_id": "r", "actor_did": "did:a",
             "name": "turn.start", "ts": "2026-05-31T00:00:01+00:00"},
        ])
        assert runs[0]["status"] == "running"

    def test_agent_falls_back_to_did(self) -> None:
        runs = compute_runs([
            {"kind": "run_event", "request_id": "r", "actor_did": "did:x",
             "name": "turn.start", "ts": "2026-05-31T00:00:01+00:00"},
        ])
        assert runs[0]["agent"] == "did:x"

    def test_rows_without_request_id_ignored(self) -> None:
        assert compute_runs([{"kind": "llm_call", "actor_did": "did:a"}]) == []

    def test_limit_caps_run_count(self) -> None:
        events = [
            {"kind": "run_event", "request_id": f"run-{i}", "actor_did": "did:a",
             "name": "turn.start", "ts": f"2026-05-31T00:00:0{i}+00:00"}
            for i in range(5)
        ]
        assert len(compute_runs(events, limit=2)) == 2

    def test_loop_complete_also_marks_done(self) -> None:
        # The universal terminal `loop.complete` (every run exit) marks completion.
        runs = compute_runs([
            {"kind": "run_event", "request_id": "r", "actor_did": "did:a",
             "name": "turn.start", "ts": "2026-05-31T00:00:01+00:00"},
            {"kind": "run_event", "request_id": "r", "actor_did": "did:a",
             "name": "loop.complete", "ts": "2026-05-31T00:00:02+00:00"},
        ])
        assert runs[0]["status"] == "completed"
