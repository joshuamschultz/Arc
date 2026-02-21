"""Tests for budget control — accumulator, enforcement, scope validation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcllm.exceptions import ArcLLMBudgetError, ArcLLMConfigError, ArcLLMError
from arcllm.modules.telemetry import (
    BudgetAccumulator,
    TelemetryModule,
    _validate_budget_scope,
    clear_budgets,
)
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)


def _make_inner(name: str = "test-provider") -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = name
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


def _make_budget_config(**overrides: object) -> dict[str, object]:
    """Build a telemetry+budget config with sensible defaults."""
    base: dict[str, object] = {
        "cost_input_per_1m": 3.00,
        "cost_output_per_1m": 15.00,
        "monthly_limit_usd": 100.0,
        "daily_limit_usd": 50.0,
        "per_call_max_usd": 5.0,
        "alert_threshold_pct": 80,
        "enforcement": "block",
        "budget_scope": "agent:test",
    }
    base.update(overrides)
    return base


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# TestArcLLMBudgetError
# ---------------------------------------------------------------------------


class TestArcLLMBudgetError:
    def test_inherits_from_arcllm_error(self):
        err = ArcLLMBudgetError(
            scope="agent:test",
            limit_type="monthly",
            limit_usd=100.0,
            current_usd=99.0,
            estimated_usd=5.0,
        )
        assert isinstance(err, ArcLLMError)

    def test_stores_all_attributes(self):
        err = ArcLLMBudgetError(
            scope="agent:agent-007",
            limit_type="daily",
            limit_usd=50.0,
            current_usd=48.0,
            estimated_usd=3.0,
        )
        assert err.scope == "agent:agent-007"
        assert err.limit_type == "daily"
        assert err.limit_usd == 50.0
        assert err.current_usd == 48.0
        assert err.estimated_usd == 3.0

    def test_str_includes_scope_and_limit(self):
        err = ArcLLMBudgetError(
            scope="agent:test",
            limit_type="monthly",
            limit_usd=100.0,
            current_usd=99.0,
            estimated_usd=None,
        )
        msg = str(err)
        assert "agent:test" in msg
        assert "monthly" in msg
        assert "100.0" in msg

    def test_estimated_usd_can_be_none(self):
        err = ArcLLMBudgetError(
            scope="agent:test",
            limit_type="per_call",
            limit_usd=5.0,
            current_usd=0.0,
            estimated_usd=None,
        )
        assert err.estimated_usd is None


# ---------------------------------------------------------------------------
# TestBudgetAccumulator
# ---------------------------------------------------------------------------


class TestBudgetAccumulator:
    def test_initial_spend_is_zero(self):
        acc = BudgetAccumulator()
        assert acc.monthly_spend == 0.0
        assert acc.daily_spend == 0.0

    def test_deduct_adds_to_accumulators(self):
        acc = BudgetAccumulator()
        acc.deduct(1.50)
        assert acc.monthly_spend == pytest.approx(1.50)
        assert acc.daily_spend == pytest.approx(1.50)

    def test_deduct_accumulates_multiple_calls(self):
        acc = BudgetAccumulator()
        acc.deduct(0.01)
        acc.deduct(0.02)
        acc.deduct(0.03)
        assert acc.monthly_spend == pytest.approx(0.06)
        assert acc.daily_spend == pytest.approx(0.06)

    def test_check_limits_returns_none_when_under(self):
        acc = BudgetAccumulator()
        acc.deduct(10.0)
        result = acc.check_limits(monthly_limit=100.0, daily_limit=50.0)
        assert result is None

    def test_check_limits_returns_monthly_when_exceeded(self):
        acc = BudgetAccumulator()
        acc.deduct(101.0)
        result = acc.check_limits(monthly_limit=100.0, daily_limit=200.0)
        assert result == "monthly"

    def test_check_limits_returns_daily_when_exceeded(self):
        acc = BudgetAccumulator()
        acc.deduct(51.0)
        result = acc.check_limits(monthly_limit=200.0, daily_limit=50.0)
        assert result == "daily"

    def test_check_limits_monthly_takes_priority(self):
        """When both limits exceeded, monthly is returned first."""
        acc = BudgetAccumulator()
        acc.deduct(101.0)
        result = acc.check_limits(monthly_limit=100.0, daily_limit=50.0)
        assert result == "monthly"


class TestBudgetPreFlight:
    def test_under_max_returns_false(self):
        acc = BudgetAccumulator()
        assert acc.check_pre_flight(estimated=2.0, per_call_max=5.0) is False

    def test_over_max_returns_true(self):
        acc = BudgetAccumulator()
        assert acc.check_pre_flight(estimated=10.0, per_call_max=5.0) is True

    def test_exact_max_returns_false(self):
        """At exactly the limit, allow (<=, not <)."""
        acc = BudgetAccumulator()
        assert acc.check_pre_flight(estimated=5.0, per_call_max=5.0) is False


class TestBudgetCostClamping:
    def test_negative_cost_clamped_to_zero(self):
        acc = BudgetAccumulator()
        acc.deduct(-5.0)
        assert acc.monthly_spend == 0.0
        assert acc.daily_spend == 0.0

    def test_zero_cost_accepted(self):
        acc = BudgetAccumulator()
        acc.deduct(0.0)
        assert acc.monthly_spend == 0.0


class TestBudgetPeriodBoundary:
    @patch("arcllm.modules.telemetry._utc_month_key")
    @patch("arcllm.modules.telemetry._utc_day_key")
    def test_monthly_reset_on_new_month(self, mock_day, mock_month):
        """Accumulator resets monthly spend when month changes."""
        mock_month.return_value = 202601
        mock_day.return_value = 20260101
        acc = BudgetAccumulator()
        acc.deduct(50.0)
        assert acc.monthly_spend == pytest.approx(50.0)

        # Advance to next month
        mock_month.return_value = 202602
        mock_day.return_value = 20260201
        acc.deduct(1.0)
        assert acc.monthly_spend == pytest.approx(1.0)

    @patch("arcllm.modules.telemetry._utc_month_key")
    @patch("arcllm.modules.telemetry._utc_day_key")
    def test_daily_reset_on_new_day(self, mock_day, mock_month):
        """Accumulator resets daily spend when day changes (month unchanged)."""
        mock_month.return_value = 202601
        mock_day.return_value = 20260115
        acc = BudgetAccumulator()
        acc.deduct(10.0)
        assert acc.daily_spend == pytest.approx(10.0)

        # Advance to next day, same month
        mock_day.return_value = 20260116
        acc.deduct(2.0)
        assert acc.daily_spend == pytest.approx(2.0)
        # Monthly should still accumulate
        assert acc.monthly_spend == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# TestBudgetScopeValidation
# ---------------------------------------------------------------------------


class TestBudgetScopeValidation:
    def test_valid_simple_scope(self):
        _validate_budget_scope("agent:agent-007")

    def test_valid_dot_scope(self):
        _validate_budget_scope("agent:test.scope")

    def test_valid_single_char(self):
        _validate_budget_scope("a")

    def test_valid_with_colons_and_hyphens(self):
        _validate_budget_scope("org:team:agent-id")

    def test_empty_scope_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("")

    def test_uppercase_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("Agent:Test")

    def test_spaces_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("agent test")

    def test_unicode_homoglyph_rejected(self):
        """Unicode lookalike characters must be rejected after NFKC normalization."""
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("agent:\u0430gent")  # Cyrillic U+0430

    def test_path_traversal_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("../etc/passwd")

    def test_sql_injection_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("agent'; DROP TABLE--")

    def test_over_128_chars_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            _validate_budget_scope("a" * 129)

    def test_exactly_128_chars_accepted(self):
        _validate_budget_scope("a" * 128)


# ---------------------------------------------------------------------------
# TestBudgetRegistry
# ---------------------------------------------------------------------------


class TestBudgetRegistry:
    def setup_method(self):
        clear_budgets()

    def test_clear_budgets_empties_registry(self):
        from arcllm.modules.telemetry import _get_or_create_accumulator

        _get_or_create_accumulator("scope-a")
        clear_budgets()
        # After clear, a new get should create a fresh accumulator
        acc = _get_or_create_accumulator("scope-a")
        assert acc.monthly_spend == 0.0

    def test_same_scope_returns_same_accumulator(self):
        from arcllm.modules.telemetry import _get_or_create_accumulator

        a1 = _get_or_create_accumulator("agent:test")
        a2 = _get_or_create_accumulator("agent:test")
        assert a1 is a2

    def test_different_scopes_are_isolated(self):
        from arcllm.modules.telemetry import _get_or_create_accumulator

        a1 = _get_or_create_accumulator("agent:one")
        a2 = _get_or_create_accumulator("agent:two")
        a1.deduct(100.0)
        assert a2.monthly_spend == 0.0


# ---------------------------------------------------------------------------
# TestBudgetEnforcement — TelemetryModule with budget
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def setup_method(self) -> None:
        clear_budgets()

    async def test_block_mode_raises_when_monthly_exceeded(self, messages: list[Message]) -> None:
        inner = _make_inner()
        config = _make_budget_config(monthly_limit_usd=1.0, enforcement="block")
        module = TelemetryModule(config, inner)
        # Seed the accumulator past the limit
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        acc.deduct(1.01)
        with pytest.raises(ArcLLMBudgetError, match="monthly"):
            await module.invoke(messages)
        # Inner should NOT have been called
        inner.invoke.assert_not_awaited()

    async def test_warn_mode_allows_when_exceeded(self, messages: list[Message]) -> None:
        inner = _make_inner()
        config = _make_budget_config(monthly_limit_usd=1.0, enforcement="warn")
        module = TelemetryModule(config, inner)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        acc.deduct(1.01)
        result = await module.invoke(messages)
        assert result.metadata is not None
        assert result.metadata.get("budget_warning") is True
        inner.invoke.assert_awaited_once()

    async def test_block_mode_raises_when_daily_exceeded(self, messages: list[Message]) -> None:
        inner = _make_inner()
        config = _make_budget_config(
            monthly_limit_usd=1000.0,
            daily_limit_usd=1.0,
            enforcement="block",
        )
        module = TelemetryModule(config, inner)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        acc.deduct(1.01)
        with pytest.raises(ArcLLMBudgetError, match="daily"):
            await module.invoke(messages)

    async def test_per_call_max_blocks(self, messages: list[Message]) -> None:
        """Per-call pre-flight check blocks when estimated cost exceeds max."""
        inner = _make_inner()
        # cost_output = 15.00/1M, max_tokens=4096 → estimated = 4096*15/1M ≈ 0.06
        # per_call_max = 0.01 → should block
        config = _make_budget_config(per_call_max_usd=0.01, enforcement="block")
        module = TelemetryModule(config, inner)
        with pytest.raises(ArcLLMBudgetError, match="per_call"):
            await module.invoke(messages, max_tokens=4096)

    async def test_budget_deducts_after_successful_call(self, messages: list[Message]) -> None:
        inner = _make_inner()
        config = _make_budget_config(per_call_max_usd=100.0)
        module = TelemetryModule(config, inner)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        assert acc.monthly_spend == 0.0
        await module.invoke(messages, max_tokens=100)
        assert acc.monthly_spend > 0.0

    async def test_no_budget_when_no_limits(self, messages: list[Message]) -> None:
        """TelemetryModule without budget limits behaves as before."""
        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
        }
        module = TelemetryModule(config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        assert result.cost_usd is not None
        # No budget metadata
        assert result.metadata is None


class TestBudgetValidation:
    def test_invalid_enforcement_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="enforcement"):
            TelemetryModule(_make_budget_config(enforcement="ignore"), inner)

    def test_negative_monthly_limit_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="monthly_limit_usd"):
            TelemetryModule(_make_budget_config(monthly_limit_usd=-1.0), inner)

    def test_scope_required_when_budget_enabled(self) -> None:
        inner = _make_inner()
        config = _make_budget_config()
        del config["budget_scope"]
        with pytest.raises(ArcLLMConfigError, match="budget_scope"):
            TelemetryModule(config, inner)

    def test_alert_threshold_zero_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="alert_threshold_pct"):
            TelemetryModule(_make_budget_config(alert_threshold_pct=0), inner)

    def test_alert_threshold_negative_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="alert_threshold_pct"):
            TelemetryModule(_make_budget_config(alert_threshold_pct=-10), inner)

    def test_alert_threshold_over_100_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="alert_threshold_pct"):
            TelemetryModule(_make_budget_config(alert_threshold_pct=101), inner)

    def test_alert_threshold_exactly_100_accepted(self) -> None:
        inner = _make_inner()
        clear_budgets()
        module = TelemetryModule(_make_budget_config(alert_threshold_pct=100), inner)
        assert module._alert_pct == 100

    def test_negative_daily_limit_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="daily_limit_usd"):
            TelemetryModule(_make_budget_config(daily_limit_usd=-1.0), inner)

    def test_negative_per_call_max_rejected(self) -> None:
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="per_call_max_usd"):
            TelemetryModule(_make_budget_config(per_call_max_usd=-0.5), inner)


# ---------------------------------------------------------------------------
# TestBudgetWarnMode — warn enforcement for all limit types
# ---------------------------------------------------------------------------


class TestBudgetWarnMode:
    """Verify warn mode allows calls but sets metadata for all limit types."""

    def setup_method(self) -> None:
        clear_budgets()

    async def test_warn_mode_per_call_exceeded(self, messages: list[Message]) -> None:
        """Per-call pre-flight in warn mode sets warning instead of blocking."""
        inner = _make_inner()
        config = _make_budget_config(per_call_max_usd=0.001, enforcement="warn")
        module = TelemetryModule(config, inner)
        result = await module.invoke(messages, max_tokens=4096)
        assert result.metadata is not None
        assert result.metadata.get("budget_warning") is True
        inner.invoke.assert_awaited_once()

    async def test_warn_mode_daily_exceeded(self, messages: list[Message]) -> None:
        """Daily limit in warn mode sets warning instead of blocking."""
        inner = _make_inner()
        config = _make_budget_config(
            monthly_limit_usd=1000.0,
            daily_limit_usd=0.001,
            enforcement="warn",
        )
        module = TelemetryModule(config, inner)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        acc.deduct(0.01)
        result = await module.invoke(messages)
        assert result.metadata is not None
        assert result.metadata.get("budget_warning") is True


# ---------------------------------------------------------------------------
# TestBudgetPerCallOnly — budget with only per_call_max
# ---------------------------------------------------------------------------


class TestBudgetPerCallOnly:
    """Budget enabled with only per_call_max (no monthly/daily limits)."""

    def setup_method(self) -> None:
        clear_budgets()

    async def test_per_call_only_allows_under_max(self, messages: list[Message]) -> None:
        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "per_call_max_usd": 100.0,
            "enforcement": "block",
            "budget_scope": "agent:per-call-only",
        }
        module = TelemetryModule(config, inner)
        result = await module.invoke(messages, max_tokens=100)
        assert result.content == "ok"

    async def test_per_call_only_blocks_over_max(self, messages: list[Message]) -> None:
        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "per_call_max_usd": 0.001,
            "enforcement": "block",
            "budget_scope": "agent:per-call-only",
        }
        module = TelemetryModule(config, inner)
        with pytest.raises(ArcLLMBudgetError, match="per_call"):
            await module.invoke(messages, max_tokens=4096)


# ---------------------------------------------------------------------------
# TestBudgetAllThreeLimits — all limits exceeded simultaneously
# ---------------------------------------------------------------------------


class TestBudgetAllThreeLimits:
    """Budget with all three limits configured — monthly takes priority."""

    def setup_method(self) -> None:
        clear_budgets()

    async def test_monthly_priority_when_all_exceeded(self, messages: list[Message]) -> None:
        """When monthly + daily are both exceeded, monthly reported first."""
        inner = _make_inner()
        config = _make_budget_config(
            monthly_limit_usd=1.0,
            daily_limit_usd=0.5,
            per_call_max_usd=100.0,
            enforcement="block",
        )
        module = TelemetryModule(config, inner)
        from arcllm.modules.telemetry import _get_or_create_accumulator

        acc = _get_or_create_accumulator("agent:test")
        acc.deduct(2.0)  # Exceeds both monthly (1.0) and daily (0.5)
        with pytest.raises(ArcLLMBudgetError, match="monthly"):
            await module.invoke(messages)


# ---------------------------------------------------------------------------
# TestBudgetMaxTokensDefault — configurable default_max_tokens
# ---------------------------------------------------------------------------


class TestBudgetMaxTokensDefault:
    """Verify default_max_tokens flows from config to pre-flight estimate."""

    def setup_method(self) -> None:
        clear_budgets()

    async def test_custom_default_max_tokens(self, messages: list[Message]) -> None:
        """Custom default_max_tokens changes pre-flight cost estimate."""
        inner = _make_inner()
        # With default_max_tokens=100, cost_output=15/1M:
        # estimated = 100 * 15 / 1M = 0.0015, per_call_max = 0.001 → blocks
        config = _make_budget_config(
            per_call_max_usd=0.001,
            default_max_tokens=100,
            enforcement="block",
        )
        module = TelemetryModule(config, inner)
        with pytest.raises(ArcLLMBudgetError, match="per_call"):
            await module.invoke(messages)  # No max_tokens kwarg — uses default

    async def test_max_tokens_kwarg_overrides_default(self, messages: list[Message]) -> None:
        """Explicit max_tokens kwarg overrides the config default."""
        inner = _make_inner()
        # default_max_tokens=100000 would block, but max_tokens=1 won't
        config = _make_budget_config(
            per_call_max_usd=0.001,
            default_max_tokens=100000,
            enforcement="block",
        )
        module = TelemetryModule(config, inner)
        # max_tokens=1: estimated = 1 * 15 / 1M ≈ 0.000015 < 0.001 → allowed
        result = await module.invoke(messages, max_tokens=1)
        assert result.content == "ok"


# ---------------------------------------------------------------------------
# TestClassificationValidation — routing classification format validation
# ---------------------------------------------------------------------------


class TestClassificationFormatValidation:
    """Verify RoutingModule rejects invalid classification formats."""

    def setup_method(self) -> None:
        from arcllm.modules.routing import RoutingModule

        self._adapter = MagicMock(spec=LLMProvider)
        self._adapter.name = "test"
        self._adapter.model_name = "m"
        self._adapter.validate_config.return_value = True
        self._adapter.invoke = AsyncMock(return_value=_OK_RESPONSE)
        self._adapter.close = AsyncMock()
        self._router = RoutingModule(
            {"enforcement": "block", "default_classification": "unclassified"},
            {"unclassified": self._adapter},
        )
        self._messages = [Message(role="user", content="hi")]

    async def test_uppercase_classification_rejected(self) -> None:
        from arcllm.exceptions import ArcLLMConfigError

        with pytest.raises(ArcLLMConfigError, match="Invalid classification format"):
            await self._router.invoke(self._messages, classification="CUI")

    async def test_spaces_in_classification_rejected(self) -> None:
        from arcllm.exceptions import ArcLLMConfigError

        with pytest.raises(ArcLLMConfigError, match="Invalid classification format"):
            await self._router.invoke(self._messages, classification="my data")

    async def test_sql_injection_classification_rejected(self) -> None:
        from arcllm.exceptions import ArcLLMConfigError

        with pytest.raises(ArcLLMConfigError, match="Invalid classification format"):
            await self._router.invoke(
                self._messages, classification="'; DROP TABLE--"
            )

    async def test_valid_classification_accepted(self) -> None:
        result = await self._router.invoke(
            self._messages, classification="unclassified"
        )
        assert result.content == "ok"
