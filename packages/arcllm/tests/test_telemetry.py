"""Tests for TelemetryModule — structured logging of timing, tokens, and cost."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.telemetry import TelemetryModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)

_CACHED_RESPONSE = LLMResponse(
    content="cached",
    usage=Usage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_read_tokens=80,
        cache_write_tokens=20,
    ),
    model="test-model",
    stop_reason="end_turn",
)

_ZERO_TOKEN_RESPONSE = LLMResponse(
    content="",
    usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
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


def _make_config(**overrides) -> dict:
    """Build a telemetry config dict with sensible defaults."""
    base = {
        "cost_input_per_1m": 3.00,
        "cost_output_per_1m": 15.00,
        "cost_cache_read_per_1m": 0.30,
        "cost_cache_write_per_1m": 3.75,
    }
    base.update(overrides)
    return base


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# TestTelemetryModule — core behavior
# ---------------------------------------------------------------------------


class TestTelemetryModule:
    async def test_invoke_delegates_to_inner(self, messages):
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        result = await module.invoke(messages)
        inner.invoke.assert_awaited_once_with(messages, None)
        assert result.content == "ok"

    async def test_invoke_passes_tools_and_kwargs(self, messages):
        inner = _make_inner()
        tools = [MagicMock()]
        module = TelemetryModule(_make_config(), inner)
        await module.invoke(messages, tools=tools, max_tokens=100)
        inner.invoke.assert_awaited_once_with(messages, tools, max_tokens=100)

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_logs_timing_and_usage(self, mock_mono, messages, caplog):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.5, 1000.5]  # 500ms llm_call
        inner = _make_inner("anthropic")
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        assert "provider=anthropic" in caplog.text
        assert "model=test-model" in caplog.text
        assert "duration_ms=500.0" in caplog.text
        assert "input_tokens=100" in caplog.text
        assert "output_tokens=50" in caplog.text
        assert "total_tokens=150" in caplog.text
        assert "stop_reason=end_turn" in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_logs_cost_calculation(self, mock_mono, messages, caplog):
        """Verify cost = (100 * 3.00 / 1e6) + (50 * 15.00 / 1e6) = 0.001050."""
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        assert "cost_usd=0.001050" in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_logs_cache_tokens_when_present(self, mock_mono, messages, caplog):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        inner.invoke = AsyncMock(return_value=_CACHED_RESPONSE)
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        assert "cache_read_tokens=80" in caplog.text
        assert "cache_write_tokens=20" in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_no_cache_fields_when_absent(self, mock_mono, messages, caplog):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        # Standard response has no cache tokens — fields should be omitted
        assert "cache_read_tokens" not in caplog.text
        assert "cache_write_tokens" not in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_cost_includes_cache_tokens(self, mock_mono, messages, caplog):
        """Cost should include cache read and write costs.

        cost = (100 * 3.00 / 1e6)   input
             + (50 * 15.00 / 1e6)    output
             + (80 * 0.30 / 1e6)     cache read
             + (20 * 3.75 / 1e6)     cache write
             = 0.000300 + 0.000750 + 0.000024 + 0.000075
             = 0.001149
        """
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        inner.invoke = AsyncMock(return_value=_CACHED_RESPONSE)
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        assert "cost_usd=0.001149" in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_cost_zero_when_zero_tokens(self, mock_mono, messages, caplog):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        inner.invoke = AsyncMock(return_value=_ZERO_TOKEN_RESPONSE)
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        assert "cost_usd=0.000000" in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_custom_log_level(self, mock_mono, messages, caplog):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        config = _make_config(log_level="DEBUG")
        module = TelemetryModule(config, inner)

        # Should NOT appear at INFO level
        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)
        assert caplog.text == ""

        # Should appear at DEBUG level
        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.telemetry"):
            mock_mono.side_effect = [2000.0, 2000.0, 2000.1, 2000.1]
            await module.invoke(messages)
        assert "provider=" in caplog.text

    async def test_provider_name_from_inner(self):
        inner = _make_inner("my-provider")
        module = TelemetryModule(_make_config(), inner)
        assert module.name == "my-provider"

    async def test_model_name_from_inner(self):
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        assert module.model_name == "test-model"

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_returns_response_unchanged(self, mock_mono, messages):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        result = await module.invoke(messages)
        # W10: model_copy() returns a new object, so check equality not identity
        assert result.content == _OK_RESPONSE.content
        assert result.model == _OK_RESPONSE.model
        assert result.stop_reason == _OK_RESPONSE.stop_reason


# ---------------------------------------------------------------------------
# TestTelemetryValidation
# ---------------------------------------------------------------------------


class TestTelemetryValidation:
    def test_negative_cost_input_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="cost_input_per_1m must be >= 0"):
            TelemetryModule(_make_config(cost_input_per_1m=-1.0), inner)

    def test_negative_cost_output_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="cost_output_per_1m must be >= 0"):
            TelemetryModule(_make_config(cost_output_per_1m=-1.0), inner)

    def test_missing_cost_fields_default_to_zero(self):
        inner = _make_inner()
        # No cost fields at all — should default to 0.0
        module = TelemetryModule({}, inner)
        assert module._cost_input == 0.0
        assert module._cost_output == 0.0
        assert module._cost_cache_read == 0.0
        assert module._cost_cache_write == 0.0

    def test_invalid_log_level_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="Invalid log_level"):
            TelemetryModule(_make_config(log_level="INVALID"), inner)


# ---------------------------------------------------------------------------
# TestTelemetryCostCalculation
# ---------------------------------------------------------------------------


class TestTelemetryCostCalculation:
    def test_basic_cost_no_cache(self):
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)
        cost = module._calculate_cost(usage)
        # (1000 * 3.00 / 1e6) + (500 * 15.00 / 1e6) = 0.003 + 0.0075 = 0.0105
        assert cost == pytest.approx(0.0105)

    def test_cost_with_cache_read(self):
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        usage = Usage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cache_read_tokens=800,
        )
        cost = module._calculate_cost(usage)
        # 0.003 + 0.0075 + (800 * 0.30 / 1e6) = 0.0105 + 0.00024 = 0.01074
        assert cost == pytest.approx(0.01074)

    def test_cost_with_all_token_types(self):
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        usage = Usage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cache_read_tokens=800,
            cache_write_tokens=200,
        )
        cost = module._calculate_cost(usage)
        # 0.003 + 0.0075 + 0.00024 + (200 * 3.75 / 1e6)
        # = 0.003 + 0.0075 + 0.00024 + 0.00075 = 0.01149
        assert cost == pytest.approx(0.01149)

    def test_cost_zero_when_no_pricing(self):
        inner = _make_inner()
        module = TelemetryModule({}, inner)
        usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)
        cost = module._calculate_cost(usage)
        assert cost == 0.0

    def test_cost_with_million_tokens(self):
        """Verify cost per 1M is applied correctly at exact 1M tokens."""
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        usage = Usage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        cost = module._calculate_cost(usage)
        # 3.00 + 15.00 = 18.00
        assert cost == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# TestTelemetryValidationExtended — cache cost + edge cases
# ---------------------------------------------------------------------------


class TestTelemetryValidationExtended:
    def test_negative_cost_cache_read_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="cost_cache_read_per_1m must be >= 0"):
            TelemetryModule(_make_config(cost_cache_read_per_1m=-1.0), inner)

    def test_negative_cost_cache_write_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="cost_cache_write_per_1m must be >= 0"):
            TelemetryModule(_make_config(cost_cache_write_per_1m=-1.0), inner)

    async def test_inner_exception_propagates(self, messages):
        """Exception from inner provider passes through — no audit log emitted."""
        inner = _make_inner()
        inner.invoke = AsyncMock(side_effect=ValueError("provider exploded"))
        module = TelemetryModule(_make_config(), inner)
        with pytest.raises(ValueError, match="provider exploded"):
            await module.invoke(messages)

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_cache_tokens_zero_logged_not_omitted(self, mock_mono, messages, caplog):
        """cache_read_tokens=0 (not None) should appear in log, unlike None which is omitted."""
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                usage=Usage(
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                ),
                model="test-model",
                stop_reason="end_turn",
            )
        )
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        assert "cache_read_tokens=0" in caplog.text
        assert "cache_write_tokens=0" in caplog.text

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_model_name_sanitized_in_log(self, mock_mono, messages, caplog):
        """Model names with newlines are escaped to prevent log injection."""
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
                model="evil\nINJECTED_LOG_LINE",
                stop_reason="end_turn",
            )
        )
        module = TelemetryModule(_make_config(), inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.telemetry"):
            await module.invoke(messages)

        # Newline should be escaped, not raw
        assert "evil\\nINJECTED_LOG_LINE" in caplog.text
        assert "evil\nINJECTED_LOG_LINE" not in caplog.text

    def test_unknown_config_key_rejected(self):
        """Typo'd config keys are caught at construction."""
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="Unknown TelemetryModule config keys"):
            TelemetryModule(_make_config(cost_imput_per_1m=5.0), inner)


# ---------------------------------------------------------------------------
# TestTelemetryTraceRecording — on_event + trace_store (Tasks 1.5-1.7)
# ---------------------------------------------------------------------------


class TestTelemetryTraceRecording:
    """Tests for TraceRecord building, on_event callback, and trace_store."""

    async def test_on_event_fires_after_invoke(self, messages):
        """on_event callback receives a TraceRecord after each invoke."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner("anthropic")
        module = TelemetryModule(_make_config(on_event=events.append), inner)

        await module.invoke(messages)

        assert len(events) == 1
        rec = events[0]
        assert isinstance(rec, TraceRecord)
        assert rec.provider == "anthropic"
        assert rec.model == "test-model"
        assert rec.input_tokens == 100
        assert rec.output_tokens == 50
        assert rec.total_tokens == 150
        assert rec.stop_reason == "end_turn"
        assert rec.status == "success"

    async def test_on_event_not_called_when_none(self, messages):
        """No callback invocation when on_event is None."""
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)

        # Should not raise
        await module.invoke(messages)

    async def test_trace_store_receives_record(self, messages):
        """trace_store.append() called with TraceRecord after invoke."""
        from arcllm.trace_store import TraceRecord

        store = AsyncMock()
        inner = _make_inner("openai")
        module = TelemetryModule(_make_config(trace_store=store), inner)

        await module.invoke(messages)

        store.append.assert_awaited_once()
        rec = store.append.call_args[0][0]
        assert isinstance(rec, TraceRecord)
        assert rec.provider == "openai"

    async def test_cost_usd_in_trace_record(self, messages):
        """TraceRecord.cost_usd matches calculated cost."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(_make_config(on_event=events.append), inner)

        await module.invoke(messages)

        rec = events[0]
        # cost = (100 * 3.00 + 50 * 15.00) / 1_000_000 = 0.001050
        assert abs(rec.cost_usd - 0.001050) < 1e-9

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_phase_timings_present(self, mock_mono, messages):
        """TraceRecord contains phase timing breakdown."""
        from arcllm.trace_store import TraceRecord

        # t0=0, t_pre=10ms, t_llm=510ms, t_post=520ms
        mock_mono.side_effect = [0.0, 0.010, 0.510, 0.520]
        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(_make_config(on_event=events.append), inner)

        await module.invoke(messages)

        rec = events[0]
        assert "prompt_assembly_ms" in rec.phase_timings
        assert "llm_call_ms" in rec.phase_timings
        assert "post_processing_ms" in rec.phase_timings
        assert "total_ms" in rec.phase_timings
        assert rec.phase_timings["prompt_assembly_ms"] == 10.0
        assert rec.phase_timings["llm_call_ms"] == 500.0
        assert rec.phase_timings["post_processing_ms"] == 10.0
        assert rec.phase_timings["total_ms"] == 520.0
        assert rec.duration_ms == 520.0

    async def test_request_body_captured_when_store_raw_bodies_true(self, messages):
        """Request body stored when store_raw_bodies=True (default)."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(on_event=events.append, store_raw_bodies=True), inner
        )

        await module.invoke(messages)

        rec = events[0]
        assert rec.request_body is not None
        assert "messages" in rec.request_body
        assert rec.response_body is not None
        assert "content" in rec.response_body

    async def test_request_body_none_when_store_raw_bodies_false(self, messages):
        """Request/response bodies are None when store_raw_bodies=False."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(on_event=events.append, store_raw_bodies=False), inner
        )

        await module.invoke(messages)

        rec = events[0]
        assert rec.request_body is None
        assert rec.response_body is None

    async def test_store_raw_bodies_defaults_to_true(self, messages):
        """Default behavior stores raw bodies."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(_make_config(on_event=events.append), inner)

        await module.invoke(messages)

        rec = events[0]
        assert rec.request_body is not None

    async def test_agent_label_set_on_record(self, messages):
        """agent_label from config appears on TraceRecord."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(on_event=events.append, agent_label="agent-007"), inner
        )

        await module.invoke(messages)

        rec = events[0]
        assert rec.agent_label == "agent-007"

    async def test_budget_scope_set_on_record(self, messages):
        """budget_scope from config appears on TraceRecord."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(
                on_event=events.append,
                budget_scope="agent:test",
                monthly_limit_usd=100.0,
            ),
            inner,
        )

        await module.invoke(messages)

        rec = events[0]
        assert rec.budget_scope == "agent:test"

    async def test_both_on_event_and_trace_store(self, messages):
        """Both on_event and trace_store fire independently."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        store = AsyncMock()
        inner = _make_inner()
        module = TelemetryModule(_make_config(on_event=events.append, trace_store=store), inner)

        await module.invoke(messages)

        assert len(events) == 1
        store.append.assert_awaited_once()

    async def test_multiple_invokes_emit_multiple_records(self, messages):
        """Each invoke produces a separate TraceRecord."""
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        module = TelemetryModule(_make_config(on_event=events.append), inner)

        await module.invoke(messages)
        await module.invoke(messages)
        await module.invoke(messages)

        assert len(events) == 3
        # All should have distinct trace_ids
        ids = {e.trace_id for e in events}
        assert len(ids) == 3


# ---------------------------------------------------------------------------
# TestGetBudgetState — Task 2.6
# ---------------------------------------------------------------------------


class TestGetBudgetState:
    """get_budget_state() returns BudgetAccumulator state for REST API."""

    def test_returns_none_when_budget_disabled(self):
        inner = _make_inner()
        module = TelemetryModule(_make_config(), inner)
        assert module.get_budget_state() is None

    def test_returns_state_when_budget_enabled(self):
        from arcllm.modules.telemetry import clear_budgets

        clear_budgets()
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(
                budget_scope="agent:budget-state-test",
                monthly_limit_usd=100.0,
                daily_limit_usd=10.0,
                per_call_max_usd=1.0,
                enforcement="block",
                alert_threshold_pct=80,
            ),
            inner,
        )
        state = module.get_budget_state()
        assert state is not None
        assert state["scope"] == "agent:budget-state-test"
        assert state["monthly_limit"] == 100.0
        assert state["daily_limit"] == 10.0
        assert state["per_call_max"] == 1.0
        assert state["enforcement"] == "block"
        assert state["alert_threshold_pct"] == 80
        assert state["monthly_spend"] == 0.0
        assert state["daily_spend"] == 0.0

    @patch("arcllm.modules.telemetry.time.monotonic")
    async def test_spend_reflected_after_invoke(self, mock_mono, messages):
        mock_mono.side_effect = [1000.0, 1000.0, 1000.1, 1000.1]
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(
                budget_scope="agent:spend-test",
                monthly_limit_usd=100.0,
            ),
            inner,
        )

        await module.invoke(messages)

        state = module.get_budget_state()
        assert state is not None
        # cost = (100 * 3.00 + 50 * 15.00) / 1_000_000 = 0.001050
        assert state["monthly_spend"] > 0.0
        assert abs(state["monthly_spend"] - 0.001050) < 1e-9
        assert abs(state["daily_spend"] - 0.001050) < 1e-9

    def test_state_includes_defaults_when_partial_config(self):
        inner = _make_inner()
        module = TelemetryModule(
            _make_config(
                budget_scope="agent:partial",
                monthly_limit_usd=50.0,
            ),
            inner,
        )
        state = module.get_budget_state()
        assert state is not None
        assert state["daily_limit"] is None
        assert state["per_call_max"] is None
        assert state["enforcement"] == "block"
        assert state["alert_threshold_pct"] == 80
