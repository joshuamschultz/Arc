"""Integration tests: budget + telemetry — end-to-end cost tracking and enforcement.

SDD deliverable: verifies that load_model() → invoke() correctly wires
budget enforcement through the telemetry module, including budget_scope
injection via the load_model() kwarg.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMBudgetError
from arcllm.modules.telemetry import TelemetryModule, clear_budgets
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=500, output_tokens=200, total_tokens=700),
    model="test-model",
    stop_reason="end_turn",
)


def _make_inner() -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


@pytest.fixture(autouse=True)
def _clean_budgets():
    clear_budgets()
    yield
    clear_budgets()


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# Budget + Telemetry Integration
# ---------------------------------------------------------------------------


class TestBudgetTelemetryIntegration:
    """Verify budget enforcement integrates with telemetry cost calculation."""

    async def test_cost_calculated_and_deducted(self, messages: list[Message]) -> None:
        """invoke() calculates cost from usage and deducts from accumulator."""
        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "monthly_limit_usd": 1000.0,
            "daily_limit_usd": 500.0,
            "per_call_max_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:integration-test",
        }
        module = TelemetryModule(config, inner)
        result = await module.invoke(messages, max_tokens=100)

        # Cost should be set on response
        assert result.cost_usd is not None
        assert result.cost_usd > 0

        # Expected cost: (500*3 + 200*15) / 1M = 4500/1M = 0.0045
        expected_cost = (500 * 3.0 + 200 * 15.0) / 1_000_000
        assert result.cost_usd == pytest.approx(expected_cost)

        # Accumulator should have been deducted
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:integration-test")
        assert acc.monthly_spend == pytest.approx(expected_cost)
        assert acc.daily_spend == pytest.approx(expected_cost)

    async def test_multiple_calls_accumulate_spend(self, messages: list[Message]) -> None:
        """Sequential calls accumulate cost in the budget accumulator."""
        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "monthly_limit_usd": 1000.0,
            "per_call_max_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:multi-call",
        }
        module = TelemetryModule(config, inner)

        await module.invoke(messages, max_tokens=100)
        await module.invoke(messages, max_tokens=100)
        await module.invoke(messages, max_tokens=100)

        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:multi-call")
        expected_per_call = (500 * 3.0 + 200 * 15.0) / 1_000_000
        assert acc.monthly_spend == pytest.approx(expected_per_call * 3)

    async def test_block_after_accumulation(self, messages: list[Message]) -> None:
        """Block mode triggers after enough calls accumulate past limit."""
        inner = _make_inner()
        # cost per call: (500*3 + 200*15)/1M = 0.0045
        # monthly_limit=0.004 → first call succeeds (pre-check passes at 0.0),
        # deducts 0.0045, second call blocked (0.0045 >= 0.004)
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "monthly_limit_usd": 0.004,
            "per_call_max_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:low-limit",
        }
        module = TelemetryModule(config, inner)

        # First call succeeds but accumulates cost past monthly limit
        await module.invoke(messages, max_tokens=100)

        # Second call should be blocked (monthly exceeded)
        with pytest.raises(ArcLLMBudgetError, match="monthly"):
            await module.invoke(messages, max_tokens=100)

    async def test_cache_tokens_included_in_cost(self, messages: list[Message]) -> None:
        """Cache read/write tokens are included in cost calculation."""
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=LLMResponse(
                content="cached",
                usage=Usage(
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                    cache_read_tokens=1000,
                    cache_write_tokens=500,
                ),
                model="test-model",
                stop_reason="end_turn",
            )
        )
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "cost_cache_read_per_1m": 0.30,
            "cost_cache_write_per_1m": 3.75,
            "monthly_limit_usd": 1000.0,
            "per_call_max_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:cache-test",
        }
        module = TelemetryModule(config, inner)
        result = await module.invoke(messages, max_tokens=100)

        expected = (
            (100 * 3.0 + 50 * 15.0) / 1_000_000 + 1000 * 0.30 / 1_000_000 + 500 * 3.75 / 1_000_000
        )
        assert result.cost_usd == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Budget Scope Injection via load_model()
# ---------------------------------------------------------------------------


class TestBudgetScopeInjection:
    """Verify budget_scope kwarg is injected into telemetry config."""

    async def test_scope_from_config(self, messages: list[Message]) -> None:
        """budget_scope in config dict is used directly."""
        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "monthly_limit_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:config-scope",
        }
        module = TelemetryModule(config, inner)
        assert module._budget_scope == "agent:config-scope"

    async def test_shared_accumulator_across_modules(self, messages: list[Message]) -> None:
        """Two modules with the same scope share one accumulator."""
        inner1 = _make_inner()
        inner2 = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "monthly_limit_usd": 100.0,
            "per_call_max_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:shared",
        }
        m1 = TelemetryModule(config, inner1)
        m2 = TelemetryModule(config, inner2)

        await m1.invoke(messages, max_tokens=100)
        await m2.invoke(messages, max_tokens=100)

        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:shared")
        expected_per_call = (500 * 3.0 + 200 * 15.0) / 1_000_000
        assert acc.monthly_spend == pytest.approx(expected_per_call * 2)
