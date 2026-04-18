"""Circuit breaker state machine — SPEC-017 R-045 (Resilience4j pattern).

State transitions::

    CLOSED  --(failure_threshold consecutive failures)-->  OPEN
    OPEN    --(wait elapsed, next allow_request())     -->  HALF_OPEN
    HALF_OPEN  --(record_success)                      -->  CLOSED
    HALF_OPEN  --(record_failure)                      -->  OPEN (++open_count)

Wait is exponential: ``base_wait * 2 ** open_count``, capped at
``max_wait``. ``open_count`` increments every time the breaker
reopens from HALF_OPEN, so a schedule that keeps failing backs off
further each cycle.

Intentionally non-async — pure state transitions driven by the caller.
The caller is responsible for invoking ``allow_request()`` BEFORE
dispatching work and ``record_success`` / ``record_failure`` AFTER.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

CircuitState = Literal["CLOSED", "OPEN", "HALF_OPEN"]

MonotonicClock = Callable[[], float]


class CircuitBreaker:
    """Per-schedule breaker. Not thread-safe — one instance per owner.

    Parameters
    ----------
    failure_threshold:
        Consecutive failures that trip the breaker. Must be >= 1.
    base_wait_seconds:
        Base of the exponential backoff. Must be > 0.
    max_wait_seconds:
        Ceiling on wait duration. Defaults to 1800 (30 minutes).
    monotonic:
        Injectable clock for tests. Defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        base_wait_seconds: float,
        max_wait_seconds: float = 1800.0,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        if failure_threshold < 1:
            msg = f"failure_threshold must be >= 1, got {failure_threshold}"
            raise ValueError(msg)
        if base_wait_seconds <= 0:
            msg = f"base_wait_seconds must be > 0, got {base_wait_seconds}"
            raise ValueError(msg)
        if max_wait_seconds <= 0:
            msg = f"max_wait_seconds must be > 0, got {max_wait_seconds}"
            raise ValueError(msg)

        self._threshold = failure_threshold
        self._base_wait = float(base_wait_seconds)
        self._max_wait = float(max_wait_seconds)
        self._monotonic = monotonic or time.monotonic

        self._state: CircuitState = "CLOSED"
        self._consecutive_failures = 0
        self._open_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        """Current state — visible to callers for telemetry/metrics."""
        return self._state

    def current_wait_seconds(self) -> float:
        """Wait duration that will apply next time the breaker opens.

        Useful for operator tooling; also exposes exponential backoff
        in tests without relying on clock advance.
        """
        return float(min(self._base_wait * (2**self._open_count), self._max_wait))

    def allow_request(self) -> bool:
        """Return True if the caller may proceed.

        Side effect: when the breaker is OPEN and the wait has elapsed,
        the first call to ``allow_request`` transitions the state to
        HALF_OPEN and returns True. The caller must then invoke either
        ``record_success`` or ``record_failure`` to close the probe.
        """
        if self._state == "CLOSED":
            return True
        if self._state == "HALF_OPEN":
            return True
        # OPEN — check whether wait has elapsed
        if self._opened_at is None:
            return False
        elapsed = self._monotonic() - self._opened_at
        if elapsed >= self.current_wait_seconds():
            self._state = "HALF_OPEN"
            return True
        return False

    def record_success(self) -> None:
        """Acknowledge a successful execution.

        In CLOSED: resets the consecutive-failure counter.
        In HALF_OPEN: transitions to CLOSED and resets ``open_count``
        so subsequent trips restart the exponential backoff from base.
        """
        self._consecutive_failures = 0
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
            self._open_count = 0
            self._opened_at = None

    def record_failure(self) -> None:
        """Acknowledge a failed execution.

        In CLOSED: increments consecutive-failure counter; opens the
        breaker when threshold is crossed.
        In HALF_OPEN: immediately reopens with open_count++. The
        schedule's next wait will be longer.
        """
        self._consecutive_failures += 1
        if self._state == "HALF_OPEN":
            self._state = "OPEN"
            self._opened_at = self._monotonic()
            self._open_count += 1
            return
        if self._state == "CLOSED" and self._consecutive_failures >= self._threshold:
            self._state = "OPEN"
            self._opened_at = self._monotonic()
            # open_count stays at 0 for the first trip — intended
            # behaviour so initial wait equals ``base_wait_seconds``.

    def force_open(self) -> None:
        """Operator escape hatch — forcibly OPEN the breaker."""
        self._state = "OPEN"
        self._opened_at = self._monotonic()

    def force_close(self) -> None:
        """Operator escape hatch — reset to CLOSED state."""
        self._state = "CLOSED"
        self._consecutive_failures = 0
        self._open_count = 0
        self._opened_at = None


__all__ = ["CircuitBreaker", "CircuitState"]
