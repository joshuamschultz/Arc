"""CircuitBreakerModule — per-provider circuit breaker with state machine.

States: CLOSED → OPEN → HALF_OPEN → CLOSED (or back to OPEN).

Wraps inner LLMProvider. Prevents calls to unhealthy providers entirely.
Positioned between RetryModule (handles transient retries) and
TelemetryModule (records all calls including rejections).
"""

import logging
import threading
import time
from enum import StrEnum
from typing import Any

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.trace_store import TraceRecord
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_VALID_CONFIG_KEYS = {
    "failure_threshold",
    "cooldown_seconds",
    "half_open_max_calls",
    "on_state_change",
    "on_event",
    "enabled",
}


class CircuitState(StrEnum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when circuit is open and call is rejected."""

    def __init__(self, provider: str, cooldown_remaining: float) -> None:
        self.provider = provider
        self.cooldown_remaining = cooldown_remaining
        super().__init__(
            f"Circuit open for '{provider}': {cooldown_remaining:.1f}s remaining in cooldown"
        )


class CircuitBreakerModule(BaseModule):
    """Per-provider circuit breaker.

    Config keys:
        failure_threshold: Consecutive failures before opening (default: 5).
        cooldown_seconds: Seconds to wait before half-open probe (default: 30).
        half_open_max_calls: Max probe calls in half-open state (default: 1).
        on_state_change: Optional callback(old_state, new_state, info_dict).
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        validate_config_keys(config, _VALID_CONFIG_KEYS, "CircuitBreakerModule")

        self._failure_threshold: int = config.get("failure_threshold", 5)
        self._cooldown_seconds: float = config.get("cooldown_seconds", 30.0)
        self._half_open_max: int = config.get("half_open_max_calls", 1)
        self._on_state_change: Any | None = config.get("on_state_change")
        self._on_event: Any | None = config.get("on_event")

        if self._failure_threshold < 1:
            raise ArcLLMConfigError("failure_threshold must be >= 1")
        if self._cooldown_seconds <= 0:
            raise ArcLLMConfigError("cooldown_seconds must be > 0")
        if self._half_open_max < 1:
            raise ArcLLMConfigError("half_open_max_calls must be >= 1")

        self._lock = threading.Lock()
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._last_failure_time: float | None = None
        self._half_open_calls: int = 0

    def _transition(self, new_state: CircuitState) -> None:
        """Transition to a new state. Caller must hold self._lock."""
        old_state = self._state
        if old_state == new_state:
            return
        self._state = new_state
        logger.info(
            "Circuit breaker %s: %s → %s (failures=%d)",
            self._inner.name,
            old_state.value,
            new_state.value,
            self._consecutive_failures,
        )
        if self._on_state_change is not None:
            self._on_state_change(
                old_state.value,
                new_state.value,
                {
                    "provider": self._inner.name,
                    "consecutive_failures": self._consecutive_failures,
                },
            )
        # Emit circuit_change TraceRecord via on_event callback
        if self._on_event is not None:
            record = TraceRecord(
                provider=self._inner.name,
                model=self.model_name,
                event_type="circuit_change",
                event_data={
                    "provider": self._inner.name,
                    "old_state": old_state.value,
                    "new_state": new_state.value,
                    "consecutive_failures": self._consecutive_failures,
                },
            )
            self._on_event(record)

    def _record_success(self) -> None:
        """Record a successful call. Caller must hold self._lock."""
        if self._state == CircuitState.HALF_OPEN:
            self._consecutive_failures = 0
            self._half_open_calls = 0
            self._transition(CircuitState.CLOSED)
        elif self._state == CircuitState.CLOSED:
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Record a failed call. Caller must hold self._lock."""
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._transition(CircuitState.OPEN)
        elif self._state == CircuitState.CLOSED:
            if self._consecutive_failures >= self._failure_threshold:
                self._transition(CircuitState.OPEN)

    def _check_state(self) -> None:
        """Check if circuit should allow a call. Caller must hold self._lock.

        Raises CircuitOpenError if call should be rejected.
        """
        if self._state == CircuitState.CLOSED:
            return

        if self._state == CircuitState.OPEN:
            if self._last_failure_time is None:
                return
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._cooldown_seconds:
                self._half_open_calls = 0
                self._transition(CircuitState.HALF_OPEN)
            else:
                remaining = self._cooldown_seconds - elapsed
                raise CircuitOpenError(self._inner.name, remaining)

        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_calls >= self._half_open_max:
                raise CircuitOpenError(self._inner.name, 0.0)
            self._half_open_calls += 1

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.circuit_breaker") as cb_span:
            with self._lock:
                self._check_state()
            cb_span.set_attribute("arcllm.circuit_breaker.state", self._state.value)

            try:
                response = await self._inner.invoke(messages, tools, **kwargs)
            except (ArcLLMAPIError, Exception):
                with self._lock:
                    self._record_failure()
                raise

            with self._lock:
                self._record_success()
            return response

    def get_state(self) -> dict[str, Any]:
        """Return queryable state dict for REST API."""
        with self._lock:
            return {
                "provider": self._inner.name,
                "model": self.model_name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "last_failure_time": self._last_failure_time,
                "failure_threshold": self._failure_threshold,
                "cooldown_seconds": self._cooldown_seconds,
                "half_open_max_calls": self._half_open_max,
            }
