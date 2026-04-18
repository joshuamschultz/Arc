"""ProactiveEngine — SPEC-017 Phase 6 (R-040 through R-049).

Single asyncio task drives a min-heap priority queue keyed on
``next_run_monotonic``. On each tick the engine pops schedules that
are due, checks the per-schedule circuit breaker, skips if the
previous run is still in-flight, then dispatches the handler.

Drift-free: ``next_run = last_actual_run + interval - 0.010`` so
cumulative scheduling overhead does not accumulate.

Intentionally does NOT own the event loop start/stop — callers drive
it via :meth:`start_tick_loop` / :meth:`stop` so tests can pump
single ticks with a fake clock.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from arcagent.modules.proactive.circuit_breaker import CircuitBreaker

_logger = logging.getLogger("arcagent.proactive.engine")

_SCHED_OVERHEAD_SHIM = 0.010  # 10ms per-tick scheduling overhead (Celery Beat pattern)

MonotonicClock = Callable[[], float]
ScheduleHandler = Callable[["Schedule"], Awaitable[None]]
EventSink = Callable[[str, dict[str, Any]], None]

ScheduleKind = Literal["cron", "heartbeat"]


@dataclass(order=False)
class Schedule:
    """Runtime record for a scheduled task.

    ``next_run_monotonic`` is the timestamp (in the monotonic clock
    domain) at which the schedule is due. ``last_actual_run_monotonic``
    is updated after a tick dispatches the handler so rescheduling is
    drift-free.
    """

    id: str
    interval_seconds: float
    next_run_monotonic: float
    kind: ScheduleKind
    circuit_breaker: CircuitBreaker | None = None
    jitter_seconds: float = 0.0
    in_flight: bool = False
    last_actual_run_monotonic: float | None = None
    # Callers can stash arbitrary metadata (human-facing name, TZ,
    # cron expression, etc.) without the engine needing to know.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HeartbeatContext:
    """Minimal context for heartbeat decisions — R-044.

    Crucially carries NO session state, NO conversation history, NO
    tool results. Heartbeat runs as a stateless side channel; mixing
    its outputs with the main agent context causes behavioural
    inconsistencies (SDD §3.4).
    """

    now_iso: str
    idle_since_seconds: float


# ---------------------------------------------------------------------------
# Heartbeat driver — cheap-model invocation with strict decision boundary.
# ---------------------------------------------------------------------------

# Prompt shape is deliberately minimal: the model decides IDLE or
# NOT_IDLE. Anything more granular bleeds into the main agent's
# decision space, which SPEC-017 R-044 forbids.
_HEARTBEAT_PROMPT = """\
You are a silent watchdog. Decide whether the agent should wake up.

State:
- Current time (UTC): {now_iso}
- Seconds idle since last user interaction: {idle_since_seconds:.0f}

Respond with exactly one token: ``IDLE`` or ``NOT_IDLE``.
Nothing else. No punctuation, no explanation, no prose.
"""


async def evaluate_heartbeat(
    model: Any,
    ctx: HeartbeatContext,
    *,
    max_output_tokens: int = 4,
) -> bool:
    """Run a single heartbeat decision. Returns True if NOT_IDLE.

    The model's output is strictly parsed — anything other than the
    literal tokens ``IDLE`` / ``NOT_IDLE`` is treated as IDLE
    (conservative). This prevents model hallucination from triggering
    spurious wake events.

    Parameters
    ----------
    model:
        Any object with an async ``invoke(messages, ...)``-shaped call
        returning an object whose ``content`` attribute is the model's
        reply. Matches the arcllm provider contract.
    ctx:
        The heartbeat context (no session state).
    max_output_tokens:
        Bound on model output — defense against runaway generation.
    """
    from arcllm import Message  # late import; avoids startup cost

    prompt = _HEARTBEAT_PROMPT.format(
        now_iso=ctx.now_iso, idle_since_seconds=ctx.idle_since_seconds
    )
    try:
        response = await model.invoke(
            [Message(role="user", content=prompt)],
            max_tokens=max_output_tokens,
        )
    except Exception:
        _logger.exception("Heartbeat model invocation failed; treating as IDLE")
        return False

    content = str(getattr(response, "content", "") or "").strip()
    # Normalize — accept uppercase / title / with trailing chars.
    token = content.split()[0].upper() if content else "IDLE"
    return token == "NOT_IDLE"


class ProactiveEngine:
    """Unified heartbeat + cron scheduler. Replaces pulse + scheduler.

    Parameters
    ----------
    handler:
        Async callable invoked for each due schedule. Implementations
        dispatch cron-type schedules to the agent and heartbeat-type
        schedules to the side-channel path.
    monotonic:
        Injectable clock. Defaults to :func:`time.monotonic`.
    poll_interval_seconds:
        How long :meth:`start_tick_loop` sleeps between ticks. Unit
        tests drive :meth:`tick` directly and ignore this.
    event_sink:
        Callback for structured audit / telemetry events. Safe to
        leave as ``None``; engine degrades quietly.
    clock_warp_threshold_seconds:
        Delta between wall clock and monotonic deltas that triggers a
        ``clock_warp`` event. Defaults to 5s.
    """

    def __init__(
        self,
        *,
        handler: ScheduleHandler,
        monotonic: MonotonicClock | None = None,
        poll_interval_seconds: float = 1.0,
        event_sink: EventSink | None = None,
        clock_warp_threshold_seconds: float = 5.0,
    ) -> None:
        self._handler = handler
        self._monotonic = monotonic or time.monotonic
        self._poll_interval = poll_interval_seconds
        self._event_sink = event_sink
        self._clock_warp_threshold = clock_warp_threshold_seconds

        # Min-heap of (next_run, seq, schedule_id) so heap entries are
        # cheap and stable under equal next_run values. ``seq`` is a
        # tie-breaker so Python's heap operations never attempt to
        # compare Schedule dataclasses (which have no __lt__).
        self._heap: list[tuple[float, int, str]] = []
        self._by_id: dict[str, Schedule] = {}
        self._heap_seq = 0

        self._last_wake_us: int = -1
        self._running = False
        # Strong refs to in-flight handler tasks so asyncio's weak-ref
        # task set does not drop them before completion.
        self._inflight_tasks: set[asyncio.Task[None]] = set()

    def add(self, schedule: Schedule) -> None:
        """Register a schedule. Idempotent — readding replaces the entry."""
        self._by_id[schedule.id] = schedule
        self._push(schedule)

    def remove(self, schedule_id: str) -> None:
        """Drop a schedule. Heap entry is left lazy (filtered on pop)."""
        self._by_id.pop(schedule_id, None)

    def get(self, schedule_id: str) -> Schedule | None:
        return self._by_id.get(schedule_id)

    async def tick(self) -> None:
        """Process all schedules that are due right now.

        Exposed for tests that drive a fake clock. Production callers
        go through :meth:`start_tick_loop` which wraps this in an
        asyncio task with sleep between ticks.
        """
        now = self._monotonic()
        while self._heap and self._heap[0][0] <= now:
            _next_run, _seq, sched_id = heapq.heappop(self._heap)
            sched = self._by_id.get(sched_id)
            if sched is None:
                continue  # lazily dropped
            await self._dispatch(sched, now)

    def handle_wake(self, *, timestamp_us: int) -> bool:
        """Handle an external wake event idempotently (R-047).

        Returns ``True`` if this wake was accepted, ``False`` if it
        is stale or a duplicate. The caller uses this signal to avoid
        replaying work on redelivery.
        """
        if timestamp_us <= self._last_wake_us:
            return False
        self._last_wake_us = timestamp_us
        return True

    def check_clock_warp(
        self, *, monotonic_delta: float, wall_delta: float
    ) -> None:
        """Emit ``clock_warp`` event when wall/monotonic deltas diverge.

        Does not refuse to run — production systems want the engine
        to keep ticking even across suspend/resume. Operators
        correlate the warp event with observed schedule behaviour.
        """
        delta = abs(wall_delta - monotonic_delta)
        if delta >= self._clock_warp_threshold:
            self._emit("clock_warp", {"delta_seconds": delta})

    async def start_tick_loop(self) -> None:
        """Drive ticks until :meth:`stop` is called. Single task.

        Not exercised by unit tests; they use :meth:`tick` directly.
        """
        self._running = True
        while self._running:
            try:
                await self.tick()
            except Exception:
                _logger.exception("ProactiveEngine tick failed — continuing")
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False

    async def drain(self) -> None:
        """Await all in-flight handler tasks. Caller's orderly shutdown.

        Tests use this to guarantee every task finishes before the
        event loop closes; production ``stop()`` + ``drain()`` gives
        a graceful shutdown path.
        """
        if not self._inflight_tasks:
            return
        await asyncio.gather(*list(self._inflight_tasks), return_exceptions=True)

    # --- Internals --------------------------------------------------------

    def _push(self, schedule: Schedule) -> None:
        self._heap_seq += 1
        heapq.heappush(
            self._heap, (schedule.next_run_monotonic, self._heap_seq, schedule.id)
        )

    async def _dispatch(self, schedule: Schedule, now: float) -> None:
        """Run one schedule tick — circuit, concurrency, handler, reschedule."""
        # 1. Circuit breaker
        breaker = schedule.circuit_breaker
        if breaker is not None and not breaker.allow_request():
            self._emit(
                "skipped_circuit_open",
                {"schedule_id": schedule.id, "state": breaker.state},
            )
            # On circuit-skip advance past ``now`` — otherwise the heap
            # keeps replaying the same due timestamp and tick() spins.
            self._reschedule_from_now(schedule, now)
            return

        # 2. Concurrency — Kubernetes CronJob ``Forbid`` semantics
        if schedule.in_flight:
            self._emit("missed_concurrency", {"schedule_id": schedule.id})
            # Same rationale as above: advance past ``now`` so the next
            # tick is some interval later, not immediately re-firing.
            self._reschedule_from_now(schedule, now)
            return

        schedule.in_flight = True
        schedule.last_actual_run_monotonic = now
        # Reschedule BEFORE awaiting handler so subsequent ticks see
        # the correct next_run even if handler is slow.
        self._reschedule(schedule, now)

        async def _run() -> None:
            try:
                await self._handler(schedule)
                if breaker is not None:
                    breaker.record_success()
            except Exception:
                _logger.exception("Schedule %r handler failed", schedule.id)
                if breaker is not None:
                    breaker.record_failure()
                self._emit("handler_error", {"schedule_id": schedule.id})
            finally:
                schedule.in_flight = False

        # Fire-and-forget — the engine's tick must not block on handler.
        # Track the task so tests/callers can gracefully drain.
        task = asyncio.get_running_loop().create_task(_run())
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    def _reschedule(self, schedule: Schedule, now: float) -> None:
        """Drift-free reschedule — next_run = last_actual_run + interval.

        Used after a successful dispatch so cumulative scheduling
        overhead does not accumulate. If ``last_actual_run`` is unset
        (first run) fall back to ``now``.
        """
        base = schedule.last_actual_run_monotonic or now
        next_run = base + schedule.interval_seconds - _SCHED_OVERHEAD_SHIM
        self._apply_jitter_and_push(schedule, next_run)

    def _reschedule_from_now(self, schedule: Schedule, now: float) -> None:
        """Reschedule relative to ``now`` — used when a tick was skipped.

        Skipped ticks (circuit open, in-flight) must advance past
        ``now`` or the heap will replay the same due timestamp and
        :meth:`tick` will spin. Does NOT update
        ``last_actual_run_monotonic`` because the handler did not run.
        """
        next_run = now + schedule.interval_seconds - _SCHED_OVERHEAD_SHIM
        self._apply_jitter_and_push(schedule, next_run)

    def _apply_jitter_and_push(self, schedule: Schedule, next_run: float) -> None:
        if schedule.jitter_seconds > 0:
            # Deterministic jitter would undermine herd-prevention; use
            # the system RNG for cryptographic randomness without
            # interfering with ``random.seed()`` in tests.
            import secrets

            next_run += secrets.SystemRandom().random() * schedule.jitter_seconds
        schedule.next_run_monotonic = next_run
        self._push(schedule)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """Emit a structured event to the optional sink."""
        if self._event_sink is None:
            return
        try:
            self._event_sink(event, payload)
        except Exception:
            _logger.exception("Event sink raised; continuing")


__all__ = [
    "HeartbeatContext",
    "ProactiveEngine",
    "Schedule",
    "ScheduleKind",
]
