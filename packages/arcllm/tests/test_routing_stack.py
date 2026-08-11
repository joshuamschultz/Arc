"""Integration: the module stack sitting on top of the always-on router.

Telemetry wraps the router, so it sees a call *after* a lane was chosen but
knows nothing about the choosing. The route stamp on the response is what
carries that fact outward — without it a cheap lane is billed at the default
lane's rate and the whole point of routing vanishes from the ledger.
"""

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMBudgetError
from arcllm.modules.routing import Route, RoutingModule
from arcllm.modules.telemetry import TelemetryModule, clear_budgets
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_USAGE = Usage(input_tokens=100, output_tokens=50, total_tokens=150)


def _make_adapter(name: str = "test", model: str = "m") -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = model
    adapter.validate_config.return_value = True
    adapter.invoke = AsyncMock(
        return_value=LLMResponse(
            content="routed-ok", usage=_USAGE, model=model, stop_reason="end_turn"
        )
    )
    adapter.close = AsyncMock()
    return adapter


def _router(adapters: dict[str, MagicMock], **config: object) -> RoutingModule:
    routes = [Route(name=n, provider=a.name, model=a.model_name) for n, a in adapters.items()]
    settings: dict[str, object] = {"enforcement": "block", "default_route": next(iter(adapters))}
    settings.update(config)
    return RoutingModule(settings, routes, lambda route: adapters[route.name])


@pytest.fixture(autouse=True)
def _clean_budgets():
    clear_budgets()
    yield
    clear_budgets()


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


class TestTelemetryRouterStack:
    async def test_telemetry_wraps_router(self, messages: list[Message]) -> None:
        adapters = {
            "unclassified": _make_adapter("openai", "gpt"),
            "cui": _make_adapter("anthropic", "claude"),
        }
        telemetry = TelemetryModule(
            {"cost_input_per_1m": 3.00, "cost_output_per_1m": 15.00},
            _router(adapters),
        )

        result = await telemetry.invoke(messages, route="cui")

        assert result.content == "routed-ok"
        assert result.cost_usd is not None and result.cost_usd > 0
        adapters["cui"].invoke.assert_awaited_once()
        adapters["unclassified"].invoke.assert_not_awaited()

    async def test_pin_consumed_by_router_not_forwarded(self, messages: list[Message]) -> None:
        adapters = {
            "unclassified": _make_adapter("test", "model"),
            "other": _make_adapter("b", "m"),
        }
        telemetry = TelemetryModule(
            {"cost_input_per_1m": 0, "cost_output_per_1m": 0}, _router(adapters)
        )

        await telemetry.invoke(messages, route="unclassified", max_tokens=50)

        call_kwargs = adapters["unclassified"].invoke.call_args[1]
        assert "route" not in call_kwargs
        assert call_kwargs["max_tokens"] == 50

    async def test_no_pin_routes_to_default(self, messages: list[Message]) -> None:
        adapters = {
            "unclassified": _make_adapter("openai", "gpt"),
            "cui": _make_adapter("anthropic", "claude"),
        }
        telemetry = TelemetryModule(
            {"cost_input_per_1m": 0, "cost_output_per_1m": 0}, _router(adapters)
        )

        await telemetry.invoke(messages)

        adapters["unclassified"].invoke.assert_awaited_once()
        adapters["cui"].invoke.assert_not_awaited()


class TestPerRoutePricing:
    """A call is billed at the price of the lane it actually took."""

    _PRICING: ClassVar[dict[str, dict[str, float]]] = {
        "anthropic/claude": {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "cost_cache_read_per_1m": 0.30,
            "cost_cache_write_per_1m": 3.75,
        },
        "litellm/qwen": {
            "cost_input_per_1m": 0.0,
            "cost_output_per_1m": 0.0,
            "cost_cache_read_per_1m": 0.0,
            "cost_cache_write_per_1m": 0.0,
        },
    }

    def _stack(self) -> tuple[TelemetryModule, dict[str, MagicMock]]:
        adapters = {
            "default": _make_adapter("anthropic", "claude"),
            "local": _make_adapter("litellm", "qwen"),
        }
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "pricing": self._PRICING,
            },
            _router(adapters),
        )
        return telemetry, adapters

    async def test_local_route_is_not_billed_at_cloud_rates(self, messages) -> None:
        telemetry, _ = self._stack()

        result = await telemetry.invoke(messages, route="local")

        assert result.cost_usd == 0.0

    async def test_default_route_keeps_its_own_price(self, messages) -> None:
        telemetry, _ = self._stack()

        result = await telemetry.invoke(messages, route="default")

        assert result.cost_usd == pytest.approx((100 * 3.0 + 50 * 15.0) / 1_000_000)

    async def test_unpriced_route_falls_back_to_the_flat_rate(self, messages) -> None:
        """A route with no published metadata must not silently cost zero."""
        adapters = {
            "default": _make_adapter("anthropic", "claude"),
            "mystery": _make_adapter("x", "y"),
        }
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "pricing": self._PRICING,
            },
            _router(adapters),
        )

        result = await telemetry.invoke(messages, route="mystery")

        assert result.cost_usd == pytest.approx((100 * 3.0 + 50 * 15.0) / 1_000_000)

    async def test_budget_deducts_the_route_price(self, messages) -> None:
        """Routing to a free lane must not burn the cloud budget."""
        adapters = {
            "default": _make_adapter("anthropic", "claude"),
            "local": _make_adapter("litellm", "qwen"),
        }
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "pricing": self._PRICING,
                "monthly_limit_usd": 1000.0,
                "budget_scope": "agent:route-price",
            },
            _router(adapters),
        )

        await telemetry.invoke(messages, route="local")

        from arcllm.modules.telemetry_budget import get_or_create_accumulator

        assert get_or_create_accumulator("agent:route-price").monthly_spend == 0.0


class TestBudgetRouterStack:
    async def test_budget_blocks_before_routing(self, messages: list[Message]) -> None:
        adapters = {
            "unclassified": _make_adapter("test", "model"),
            "other": _make_adapter("b", "m"),
        }
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "monthly_limit_usd": 0.001,
                "enforcement": "block",
                "budget_scope": "agent:router-budget",
            },
            _router(adapters),
        )

        from arcllm.modules.telemetry_budget import get_or_create_accumulator

        get_or_create_accumulator("agent:router-budget").deduct(0.01)

        with pytest.raises(ArcLLMBudgetError, match="monthly"):
            await telemetry.invoke(messages, route="unclassified")
        adapters["unclassified"].invoke.assert_not_awaited()

    async def test_budget_deducts_routed_call_cost(self, messages: list[Message]) -> None:
        adapters = {
            "unclassified": _make_adapter("test", "model"),
            "other": _make_adapter("b", "m"),
        }
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "monthly_limit_usd": 1000.0,
                "per_call_max_usd": 100.0,
                "enforcement": "block",
                "budget_scope": "agent:router-deduct",
            },
            _router(adapters),
        )

        await telemetry.invoke(messages, route="unclassified", max_tokens=100)

        from arcllm.modules.telemetry_budget import get_or_create_accumulator

        acc = get_or_create_accumulator("agent:router-deduct")
        assert acc.monthly_spend == pytest.approx((100 * 3.0 + 50 * 15.0) / 1_000_000)


class TestRouterCloseStack:
    async def test_close_propagates_through_stack(self) -> None:
        adapters = {
            "unclassified": _make_adapter("openai", "gpt"),
            "cui": _make_adapter("anthropic", "claude"),
        }
        router = _router(adapters)
        router.adapter_for("cui")
        router.adapter_for("unclassified")
        telemetry = TelemetryModule({"cost_input_per_1m": 0, "cost_output_per_1m": 0}, router)

        await telemetry.close()

        adapters["cui"].close.assert_awaited_once()
        adapters["unclassified"].close.assert_awaited_once()
