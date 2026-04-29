"""SPEC-017 Phase 6 — Circuit breaker state machine.

Resilience4j pattern:
  CLOSED → (N consecutive failures) → OPEN
  OPEN   → (waitDuration elapsed)   → HALF_OPEN
  HALF_OPEN → (probe success)       → CLOSED
  HALF_OPEN → (probe failure)       → OPEN (open_count++)

waitDuration = min(base * 2 ** open_count, max_wait) — exponential backoff.

Every transition is observable via ``state``; tests drive a fake
monotonic clock to make timing deterministic.
"""

from __future__ import annotations

import pytest


class TestStateTransitions:
    """Core state machine: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def test_starts_closed(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, base_wait_seconds=1)
        assert cb.state == "CLOSED"
        assert cb.allow_request() is True

    def test_opens_after_threshold_failures(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, base_wait_seconds=1)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.allow_request() is False

    def test_opens_only_on_consecutive_failures(self) -> None:
        """Mixed success/failure does not trip the breaker."""
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, base_wait_seconds=1)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # resets consecutive counter
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "CLOSED"

    def test_half_open_after_wait_elapses(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        clock = _FakeClock()
        cb = CircuitBreaker(
            failure_threshold=2,
            base_wait_seconds=10,
            max_wait_seconds=60,
            monotonic=clock,
        )
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "OPEN"

        # Before wait elapses: stays OPEN, blocks requests
        clock.advance(5)
        assert cb.state == "OPEN"
        assert cb.allow_request() is False

        # After wait elapses: allow_request triggers probe → HALF_OPEN
        clock.advance(10)
        assert cb.allow_request() is True
        assert cb.state == "HALF_OPEN"

    def test_half_open_success_closes(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        clock = _FakeClock()
        cb = CircuitBreaker(failure_threshold=2, base_wait_seconds=1, monotonic=clock)
        cb.record_failure()
        cb.record_failure()
        clock.advance(2)

        assert cb.allow_request() is True  # enters HALF_OPEN
        cb.record_success()
        assert cb.state == "CLOSED"
        assert cb.allow_request() is True

    def test_half_open_failure_reopens(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        clock = _FakeClock()
        cb = CircuitBreaker(
            failure_threshold=2,
            base_wait_seconds=1,
            max_wait_seconds=60,
            monotonic=clock,
        )
        cb.record_failure()
        cb.record_failure()
        clock.advance(2)
        cb.allow_request()  # HALF_OPEN
        cb.record_failure()
        assert cb.state == "OPEN"
        # open_count incremented so next wait is longer


class TestExponentialBackoff:
    """Wait duration grows exponentially with consecutive opens."""

    def test_wait_doubles_each_reopening(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        clock = _FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1,
            base_wait_seconds=2,
            max_wait_seconds=100,
            monotonic=clock,
        )

        # First open: wait = 2 * 2^0 = 2s
        cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.current_wait_seconds() == 2.0

        # Probe after 2s → HALF_OPEN → fail → OPEN again (open_count=1)
        clock.advance(2)
        cb.allow_request()
        cb.record_failure()
        # wait = 2 * 2^1 = 4s
        assert cb.current_wait_seconds() == 4.0

        # Again: open_count=2 → wait = 2 * 2^2 = 8s
        clock.advance(4)
        cb.allow_request()
        cb.record_failure()
        assert cb.current_wait_seconds() == 8.0

    def test_wait_caps_at_max(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        clock = _FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1,
            base_wait_seconds=2,
            max_wait_seconds=5,  # cap very low
            monotonic=clock,
        )
        for _ in range(10):
            cb.record_failure()
            clock.advance(10)
            cb.allow_request()
        assert cb.current_wait_seconds() == 5.0


class TestManualOverrides:
    """``force_open`` / ``force_close`` — operator escape hatches."""

    def test_force_open_blocks_requests(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=5, base_wait_seconds=1)
        assert cb.allow_request() is True
        cb.force_open()
        assert cb.state == "OPEN"
        assert cb.allow_request() is False

    def test_force_close_resets(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, base_wait_seconds=1)
        cb.record_failure()
        assert cb.state == "OPEN"
        cb.force_close()
        assert cb.state == "CLOSED"
        assert cb.allow_request() is True


class TestConstructorValidation:
    def test_negative_threshold_raises(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=0, base_wait_seconds=1)

    def test_negative_wait_raises(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=1, base_wait_seconds=0)


class _FakeClock:
    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
