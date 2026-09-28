"""Behavior of the shared embed()/classify() call-budget guard (SPEC-038 / SPEC-083).

Covers the limit paths the seam-level tests do not reach: the pre-flight
per-call estimate, the daily limit, warn-mode enforcement, and the telemetry
record shape when no request/response bodies are captured.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from arcstore.records import SpoolRecord

from arcllm.exceptions import ArcLLMBudgetError
from arcllm.modules.call_budget import budget_pre_check, emit_telemetry, parse_budget
from arcllm.modules.telemetry_budget import clear_budgets
from arcllm.types import Usage

PER_TOKEN = 1_000_000.0  # $1 per input token


@pytest.fixture(autouse=True)
def _fresh_budgets() -> Iterator[None]:
    clear_budgets()
    yield
    clear_budgets()


def test_parse_budget_without_scope_tracks_nothing() -> None:
    assert parse_budget({"monthly_limit_usd": 1.0}) is None


def test_per_call_estimate_over_max_blocks_before_the_call() -> None:
    plan = parse_budget({"budget_scope": "agent:per-call", "per_call_max_usd": 2.0})
    assert plan is not None

    with pytest.raises(ArcLLMBudgetError) as info:
        budget_pre_check(plan, pre_tokens=3, cost_input_per_1m=PER_TOKEN, call_kind="classify")

    assert info.value.limit_type == "per_call"
    assert info.value.estimated_usd == pytest.approx(3.0)


def test_per_call_estimate_within_max_passes() -> None:
    plan = parse_budget({"budget_scope": "agent:per-call-ok", "per_call_max_usd": 5.0})
    assert plan is not None

    budget_pre_check(plan, pre_tokens=3, cost_input_per_1m=PER_TOKEN, call_kind="classify")


def test_daily_limit_blocks_when_monthly_has_headroom() -> None:
    plan = parse_budget(
        {"budget_scope": "agent:daily", "monthly_limit_usd": 100.0, "daily_limit_usd": 1.0}
    )
    assert plan is not None
    plan.accumulator.deduct(1.5)

    with pytest.raises(ArcLLMBudgetError) as info:
        budget_pre_check(plan, pre_tokens=1, cost_input_per_1m=0.0, call_kind="embed")

    assert info.value.limit_type == "daily"


def test_warn_mode_logs_the_breach_and_lets_the_call_proceed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = parse_budget(
        {"budget_scope": "agent:warn", "monthly_limit_usd": 1.0, "enforcement": "warn"}
    )
    assert plan is not None
    plan.accumulator.deduct(2.0)

    with caplog.at_level(logging.WARNING, logger="arcllm.modules.call_budget"):
        budget_pre_check(plan, pre_tokens=1, cost_input_per_1m=0.0, call_kind="classify")

    assert "classify budget monthly limit exceeded for 'agent:warn'" in caplog.text


def test_telemetry_without_bodies_carries_only_the_operation() -> None:
    seen: list[SpoolRecord] = []

    emit_telemetry(
        {"arcstore_enabled": False},
        seen.append,
        provider_label="jev",
        model="jev-1",
        usage=Usage(input_tokens=4, output_tokens=0, total_tokens=4),
        cost=0.0,
        latency_ms=1.0,
        operation="memory_promotion",
    )

    assert len(seen) == 1
    event = seen[0]
    assert event.extra == {"operation": "memory_promotion"}
    assert event.actor_did == "did:arc:unknown"
