"""Integration tests: routing + module stack — classification flows through stack.

SDD deliverable: verifies that RoutingModule integrates with the module
stack (telemetry, retry, etc.) and that classification kwarg flows
correctly through the decorator chain.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMBudgetError
from arcllm.modules.routing import RoutingModule
from arcllm.modules.telemetry import TelemetryModule, clear_budgets
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="routed-ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)


def _make_adapter(name: str = "test", model: str = "m") -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = model
    adapter.validate_config.return_value = True
    adapter.invoke = AsyncMock(return_value=_OK_RESPONSE)
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture(autouse=True)
def _clean_budgets():
    clear_budgets()
    yield
    clear_budgets()


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# Telemetry → Router stack
# ---------------------------------------------------------------------------


class TestTelemetryRouterStack:
    """Verify TelemetryModule wrapping a RoutingModule works end-to-end."""

    async def test_telemetry_wraps_router(self, messages: list[Message]) -> None:
        """Telemetry → Router → Adapter: cost is tracked, routing works."""
        cui_adapter = _make_adapter("anthropic", "claude")
        unc_adapter = _make_adapter("openai", "gpt")
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"cui": cui_adapter, "unclassified": unc_adapter},
        )
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
            },
            router,
        )

        result = await telemetry.invoke(messages, classification="cui")
        assert result.content == "routed-ok"
        assert result.cost_usd is not None
        assert result.cost_usd > 0
        cui_adapter.invoke.assert_awaited_once()
        unc_adapter.invoke.assert_not_awaited()

    async def test_classification_consumed_by_router(self, messages: list[Message]) -> None:
        """classification kwarg is popped by router, not passed to adapter."""
        adapter = _make_adapter("test", "model")
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"unclassified": adapter},
        )
        telemetry = TelemetryModule(
            {"cost_input_per_1m": 0, "cost_output_per_1m": 0},
            router,
        )

        await telemetry.invoke(messages, classification="unclassified", max_tokens=50)
        call_kwargs = adapter.invoke.call_args[1]
        assert "classification" not in call_kwargs
        assert call_kwargs["max_tokens"] == 50

    async def test_default_classification_when_none_provided(
        self, messages: list[Message]
    ) -> None:
        """No classification kwarg → routes to default."""
        cui_adapter = _make_adapter("anthropic", "claude")
        unc_adapter = _make_adapter("openai", "gpt")
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"cui": cui_adapter, "unclassified": unc_adapter},
        )
        telemetry = TelemetryModule(
            {"cost_input_per_1m": 0, "cost_output_per_1m": 0},
            router,
        )

        await telemetry.invoke(messages)
        unc_adapter.invoke.assert_awaited_once()
        cui_adapter.invoke.assert_not_awaited()


# ---------------------------------------------------------------------------
# Budget + Router stack
# ---------------------------------------------------------------------------


class TestBudgetRouterStack:
    """Verify budget enforcement with a router as inner provider."""

    async def test_budget_blocks_before_routing(self, messages: list[Message]) -> None:
        """Budget pre-check blocks before the router ever runs."""
        adapter = _make_adapter("test", "model")
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"unclassified": adapter},
        )
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "monthly_limit_usd": 0.001,
                "enforcement": "block",
                "budget_scope": "agent:router-budget",
            },
            router,
        )

        # Seed accumulator past limit
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:router-budget")
        acc.deduct(0.01)

        with pytest.raises(ArcLLMBudgetError, match="monthly"):
            await telemetry.invoke(messages, classification="unclassified")
        # Adapter should NOT have been called
        adapter.invoke.assert_not_awaited()

    async def test_budget_deducts_routed_call_cost(self, messages: list[Message]) -> None:
        """Cost of routed call is deducted from the budget accumulator."""
        adapter = _make_adapter("test", "model")
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"unclassified": adapter},
        )
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "monthly_limit_usd": 1000.0,
                "per_call_max_usd": 100.0,
                "enforcement": "block",
                "budget_scope": "agent:router-deduct",
            },
            router,
        )

        await telemetry.invoke(messages, classification="unclassified", max_tokens=100)

        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:router-deduct")
        expected = (100 * 3.0 + 50 * 15.0) / 1_000_000
        assert acc.monthly_spend == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Router close() with stack
# ---------------------------------------------------------------------------


class TestRouterCloseStack:
    """Verify close propagates through telemetry → router → all adapters."""

    async def test_close_propagates_through_stack(self) -> None:
        """Closing the top module closes all router adapters."""
        cui_adapter = _make_adapter("anthropic", "claude")
        unc_adapter = _make_adapter("openai", "gpt")
        router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"cui": cui_adapter, "unclassified": unc_adapter},
        )
        telemetry = TelemetryModule(
            {"cost_input_per_1m": 0, "cost_output_per_1m": 0},
            router,
        )

        await telemetry.close()
        cui_adapter.close.assert_awaited_once()
        unc_adapter.close.assert_awaited_once()
