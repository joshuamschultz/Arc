"""Security tests for budget control — adversarial inputs and bypass prevention."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.telemetry import (
    TelemetryModule,
    _validate_budget_scope,
    clear_budgets,
)
from arcllm.types import LLMProvider, LLMResponse, Message, Usage


def _make_inner() -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(
        return_value=LLMResponse(
            content="ok",
            usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            model="test-model",
            stop_reason="end_turn",
        )
    )
    return inner


def _make_budget_config(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cost_input_per_1m": 3.00,
        "cost_output_per_1m": 15.00,
        "monthly_limit_usd": 100.0,
        "daily_limit_usd": 50.0,
        "per_call_max_usd": 5.0,
        "enforcement": "block",
        "budget_scope": "agent:test",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_budgets():
    clear_budgets()
    yield
    clear_budgets()


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# Scope Injection Attacks
# ---------------------------------------------------------------------------


class TestScopeInjection:
    """Verify scope validation blocks injection attacks."""

    def test_sql_injection_in_scope(self):
        with pytest.raises(ArcLLMConfigError):
            _validate_budget_scope("'; DROP TABLE agents; --")

    def test_path_traversal_in_scope(self):
        with pytest.raises(ArcLLMConfigError):
            _validate_budget_scope("../../etc/passwd")

    def test_null_byte_in_scope(self):
        with pytest.raises(ArcLLMConfigError):
            _validate_budget_scope("agent\x00:evil")

    def test_unicode_homoglyph_cyrillic(self):
        """Cyrillic U+0430 looks like Latin 'a' -- must be rejected."""
        with pytest.raises(ArcLLMConfigError):
            _validate_budget_scope("\u0430gent:test")

    def test_unicode_fullwidth_characters(self):
        """Fullwidth U+FF41 NFKC-normalizes to 'a' but the original
        string differs from the normalized form, so it's rejected."""
        with pytest.raises(ArcLLMConfigError):
            _validate_budget_scope("\uff41gent:test")

    def test_newline_injection(self):
        with pytest.raises(ArcLLMConfigError):
            _validate_budget_scope("agent:test\nINJECTED_LINE")


# ---------------------------------------------------------------------------
# Negative Cost Injection
# ---------------------------------------------------------------------------


class TestNegativeCostInjection:
    """Verify that negative token counts cannot reduce accumulated spend."""

    async def test_negative_output_tokens_clamped(self, messages):
        """Adapter returns negative output_tokens — cost must be clamped to 0."""
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                usage=Usage(input_tokens=100, output_tokens=-1000, total_tokens=-900),
                model="test-model",
                stop_reason="end_turn",
            )
        )
        config = _make_budget_config(per_call_max_usd=100.0)
        module = TelemetryModule(config, inner)
        await module.invoke(messages, max_tokens=100)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        # Spend should NOT decrease (cost clamped to 0.0)
        assert acc.monthly_spend >= 0.0

    async def test_multiple_negative_calls_never_reduce_spend(self, messages):
        """Multiple calls with negative costs should not decrease accumulator."""
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                usage=Usage(input_tokens=0, output_tokens=-5000, total_tokens=-5000),
                model="test-model",
                stop_reason="end_turn",
            )
        )
        config = _make_budget_config(per_call_max_usd=100.0)
        module = TelemetryModule(config, inner)
        # First real call to establish spend
        inner.invoke.return_value = LLMResponse(
            content="ok",
            usage=Usage(input_tokens=1000, output_tokens=500, total_tokens=1500),
            model="test-model",
            stop_reason="end_turn",
        )
        await module.invoke(messages, max_tokens=100)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        spend_after_real = acc.monthly_spend
        # Now negative tokens call
        inner.invoke.return_value = LLMResponse(
            content="ok",
            usage=Usage(input_tokens=0, output_tokens=-5000, total_tokens=-5000),
            model="test-model",
            stop_reason="end_turn",
        )
        await module.invoke(messages, max_tokens=100)
        assert acc.monthly_spend >= spend_after_real


# ---------------------------------------------------------------------------
# Accumulator Isolation
# ---------------------------------------------------------------------------


class TestAccumulatorIsolation:
    """Verify per-scope independence — one agent cannot affect another's budget."""

    async def test_scopes_do_not_leak(self, messages):
        inner1 = _make_inner()
        inner2 = _make_inner()
        config1 = _make_budget_config(budget_scope="agent:one", per_call_max_usd=100.0)
        config2 = _make_budget_config(budget_scope="agent:two", per_call_max_usd=100.0)
        module1 = TelemetryModule(config1, inner1)
        TelemetryModule(config2, inner2)  # Initialize scope "agent:two" accumulator

        # Agent one spends a lot
        for _ in range(10):
            await module1.invoke(messages, max_tokens=100)

        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc1 = _get_or_create_accumulator("agent:one")
        acc2 = _get_or_create_accumulator("agent:two")
        assert acc1.monthly_spend > 0
        assert acc2.monthly_spend == 0.0


# ---------------------------------------------------------------------------
# Float Overflow
# ---------------------------------------------------------------------------


class TestFloatOverflow:
    """Verify budget handles extremely large token counts safely."""

    async def test_massive_tokens_do_not_crash(self, messages):
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                usage=Usage(
                    input_tokens=2**53,
                    output_tokens=2**53,
                    total_tokens=2**54,
                ),
                model="test-model",
                stop_reason="end_turn",
            )
        )
        # Use very high per_call_max to not block on pre-flight
        config = _make_budget_config(
            per_call_max_usd=float("inf"),
            monthly_limit_usd=float("inf"),
            daily_limit_usd=float("inf"),
        )
        module = TelemetryModule(config, inner)
        result = await module.invoke(messages, max_tokens=100)
        assert result.cost_usd is not None
        assert result.cost_usd > 0
