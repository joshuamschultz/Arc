"""Tests for CircuitBreakerModule — state machine transitions and error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.modules.circuit_breaker import (
    CircuitBreakerModule,
    CircuitOpenError,
)
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
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


def _make_config(**overrides: object) -> dict:
    base: dict[str, object] = {
        "failure_threshold": 3,
        "cooldown_seconds": 10.0,
        "half_open_max_calls": 1,
    }
    base.update(overrides)
    return base


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role="user", content="hi")]


class TestCircuitBreakerValidation:
    def test_invalid_failure_threshold(self):
        with pytest.raises(ArcLLMConfigError, match="failure_threshold"):
            CircuitBreakerModule(_make_config(failure_threshold=0), _make_inner())

    def test_invalid_cooldown_seconds(self):
        with pytest.raises(ArcLLMConfigError, match="cooldown_seconds"):
            CircuitBreakerModule(_make_config(cooldown_seconds=0), _make_inner())

    def test_invalid_half_open_max(self):
        with pytest.raises(ArcLLMConfigError, match="half_open_max_calls"):
            CircuitBreakerModule(_make_config(half_open_max_calls=0), _make_inner())

    def test_unknown_config_key_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="Unknown"):
            CircuitBreakerModule(_make_config(bogus_key=True), _make_inner())

    def test_defaults_are_valid(self):
        module = CircuitBreakerModule({}, _make_inner())
        state = module.get_state()
        assert state["failure_threshold"] == 5
        assert state["cooldown_seconds"] == 30.0
        assert state["half_open_max_calls"] == 1


class TestCircuitBreakerStateMachine:
    async def test_starts_closed(self, messages: list[Message]):
        module = CircuitBreakerModule(_make_config(), _make_inner())
        assert module.get_state()["state"] == "closed"

    async def test_passes_through_when_closed(self, messages: list[Message]):
        inner = _make_inner()
        module = CircuitBreakerModule(_make_config(), inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        inner.invoke.assert_awaited_once()

    async def test_opens_after_threshold_failures(self, messages: list[Message]):
        inner = _make_inner()
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "server error", "test")
        )
        module = CircuitBreakerModule(_make_config(failure_threshold=3), inner)

        for _ in range(3):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        assert module.get_state()["state"] == "open"
        assert module.get_state()["consecutive_failures"] == 3

    async def test_rejects_calls_when_open(self, messages: list[Message]):
        inner = _make_inner()
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "server error", "test")
        )
        module = CircuitBreakerModule(
            _make_config(failure_threshold=2, cooldown_seconds=100.0), inner
        )

        # Trip the circuit
        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        assert module.get_state()["state"] == "open"

        # Next call should be rejected without reaching inner
        inner.invoke.reset_mock()
        with pytest.raises(CircuitOpenError, match="Circuit open"):
            await module.invoke(messages)
        inner.invoke.assert_not_awaited()

    @patch("arcllm.modules.circuit_breaker.time.monotonic")
    async def test_transitions_to_half_open_after_cooldown(
        self, mock_mono: MagicMock, messages: list[Message]
    ):
        inner = _make_inner()
        failure = ArcLLMAPIError(500, "error", "test")

        # Failures at t=0, t=1, t=2
        mock_mono.side_effect = [0.0, 0.0, 1.0, 1.0, 2.0, 2.0]
        inner.invoke = AsyncMock(side_effect=failure)
        module = CircuitBreakerModule(
            _make_config(failure_threshold=3, cooldown_seconds=10.0), inner
        )

        for _ in range(3):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)
        assert module.get_state()["state"] == "open"

        # At t=15 (after cooldown), should transition to half_open and allow probe
        mock_mono.return_value = 15.0
        mock_mono.side_effect = None
        inner.invoke = AsyncMock(return_value=_OK_RESPONSE)

        result = await module.invoke(messages)
        assert result.content == "ok"
        # Should close after successful probe
        assert module.get_state()["state"] == "closed"

    @patch("arcllm.modules.circuit_breaker.time.monotonic")
    async def test_half_open_failure_reopens(
        self, mock_mono: MagicMock, messages: list[Message]
    ):
        inner = _make_inner()
        failure = ArcLLMAPIError(500, "error", "test")

        # Trip circuit
        mock_mono.side_effect = [0.0, 0.0, 1.0, 1.0, 2.0, 2.0]
        inner.invoke = AsyncMock(side_effect=failure)
        module = CircuitBreakerModule(
            _make_config(failure_threshold=3, cooldown_seconds=10.0), inner
        )

        for _ in range(3):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        # After cooldown, probe fails → back to OPEN
        mock_mono.return_value = 15.0
        mock_mono.side_effect = None
        inner.invoke = AsyncMock(side_effect=failure)

        with pytest.raises(ArcLLMAPIError):
            await module.invoke(messages)

        assert module.get_state()["state"] == "open"

    async def test_success_resets_failure_count(self, messages: list[Message]):
        inner = _make_inner()
        failure = ArcLLMAPIError(500, "error", "test")
        module = CircuitBreakerModule(_make_config(failure_threshold=3), inner)

        # 2 failures, then success
        inner.invoke = AsyncMock(side_effect=failure)
        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
        await module.invoke(messages)

        assert module.get_state()["consecutive_failures"] == 0
        assert module.get_state()["state"] == "closed"

    async def test_on_state_change_callback(self, messages: list[Message]):
        transitions: list[tuple[str, str, dict]] = []

        def cb(old: str, new: str, info: dict) -> None:
            transitions.append((old, new, info))

        inner = _make_inner()
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "error", "test")
        )
        module = CircuitBreakerModule(
            _make_config(failure_threshold=2, on_state_change=cb), inner
        )

        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        assert len(transitions) == 1
        assert transitions[0][0] == "closed"
        assert transitions[0][1] == "open"
        assert transitions[0][2]["provider"] == "test-provider"
        assert transitions[0][2]["consecutive_failures"] == 2


class TestCircuitBreakerGetState:
    def test_get_state_returns_all_fields(self):
        module = CircuitBreakerModule(
            _make_config(failure_threshold=5, cooldown_seconds=30.0),
            _make_inner("anthropic"),
        )
        state = module.get_state()
        assert state["provider"] == "anthropic"
        assert state["model"] == "test-model"
        assert state["state"] == "closed"
        assert state["consecutive_failures"] == 0
        assert state["last_failure_time"] is None
        assert state["failure_threshold"] == 5
        assert state["cooldown_seconds"] == 30.0


class TestCircuitBreakerTraceEvents:
    """Task 2.3: CircuitBreaker emits TraceRecord on state transitions."""

    async def test_emits_circuit_change_on_open(self, messages: list[Message]):
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner("anthropic")
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "error", "anthropic")
        )
        module = CircuitBreakerModule(
            _make_config(failure_threshold=2, on_event=events.append), inner
        )

        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        assert len(events) == 1
        rec = events[0]
        assert isinstance(rec, TraceRecord)
        assert rec.event_type == "circuit_change"
        assert rec.provider == "anthropic"
        assert rec.event_data is not None
        assert rec.event_data["old_state"] == "closed"
        assert rec.event_data["new_state"] == "open"
        assert rec.event_data["consecutive_failures"] == 2

    @patch("arcllm.modules.circuit_breaker.time.monotonic")
    async def test_emits_circuit_change_on_half_open_and_close(
        self, mock_mono: MagicMock, messages: list[Message]
    ):
        from arcllm.trace_store import TraceRecord

        events: list[TraceRecord] = []
        inner = _make_inner()
        failure = ArcLLMAPIError(500, "error", "test")

        # Trip to OPEN
        mock_mono.side_effect = [0.0, 0.0, 1.0, 1.0]
        inner.invoke = AsyncMock(side_effect=failure)
        module = CircuitBreakerModule(
            _make_config(
                failure_threshold=2,
                cooldown_seconds=5.0,
                on_event=events.append,
            ),
            inner,
        )

        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        assert len(events) == 1  # closed → open

        # After cooldown, successful probe → half_open → closed
        mock_mono.return_value = 10.0
        mock_mono.side_effect = None
        inner.invoke = AsyncMock(return_value=_OK_RESPONSE)

        await module.invoke(messages)

        # Should have 3 events: closed→open, open→half_open, half_open→closed
        assert len(events) == 3
        assert events[1].event_data["old_state"] == "open"
        assert events[1].event_data["new_state"] == "half_open"
        assert events[2].event_data["old_state"] == "half_open"
        assert events[2].event_data["new_state"] == "closed"

    async def test_no_events_when_on_event_none(self, messages: list[Message]):
        """No crash when on_event is not configured."""
        inner = _make_inner()
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "error", "test")
        )
        module = CircuitBreakerModule(
            _make_config(failure_threshold=2), inner
        )

        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)

        # Should not raise; just no events emitted
        assert module.get_state()["state"] == "open"
