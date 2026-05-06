"""DashboardEventBus — topic-keyed pub-sub for dashboard widgets.

SPEC-025 Track E (SDD §C5).

Subscribers are per-socket ``asyncio.Queue`` instances; publishers are
state aggregators that already exist on the server (queue depth,
circuit-breaker state, roster, …). The bus is the single fan-out point
so arcui routes never need to know about aggregator internals.

Design pillars:
    Simplicity  — one class, ~80 LOC, subscribe() + publish() surface.
    Scalability — per-socket Queue(maxsize=100) with drop-oldest
                  backpressure, identical to web.py's per-socket pattern.
                  N subscribers fan out concurrently; a slow subscriber
                  never stalls publish().
    Security    — auth is enforced by the route (dashboard_ws.py) before
                  subscribe() is called; the bus itself is policy-blind.

Topic registry (9 topics matching the 9 polling endpoints replaced):
    stats               /api/stats
    stats.timeseries    /api/stats/timeseries
    circuit_breakers    /api/circuit-breakers
    budget              /api/budget
    performance         /api/performance
    queue               /api/queue
    cost_efficiency     /api/cost-efficiency
    roster              /api/team/roster
    schedule_history    /api/schedule-history

Replay on subscribe: the bus stores the last published value per topic
and replays it immediately on subscribe so the UI doesn't blink empty
on first connect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_logger = logging.getLogger("arcgateway.dashboard_events")

_QUEUE_MAXSIZE = 100

# SPEC-025 §M-1 — replay-on-subscribe TTL. The bus stores the last value
# per topic so a fresh widget paints immediately, but cached values older
# than this are skipped. Without a TTL, a viewer who connects an hour
# after the last operator-emitted update would receive stale data with no
# indication of its age.
_DEFAULT_LAST_VALUE_TTL_SECONDS = 60.0


class DashboardEventBus:
    """Topic-keyed pub-sub bus for dashboard state pushed to browser sockets.

    Thread-safety: intended for use within a single asyncio event loop.
    All methods are non-blocking at the call site; queue I/O is buffered.
    """

    def __init__(
        self,
        *,
        last_value_ttl_seconds: float = _DEFAULT_LAST_VALUE_TTL_SECONDS,
        audit_emitter: Any | None = None,
    ) -> None:
        # topic → set of per-socket queues
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        # topic → (payload, monotonic_ts) for replay-on-subscribe with TTL
        self._last_value: dict[str, tuple[Any, float]] = {}
        self._last_value_ttl = last_value_ttl_seconds
        # Optional audit emitter for tests; falls back to arcgateway.audit on
        # None.  Same pattern as ``WebPlatformAdapter._audit_emitter``.
        self._audit_emitter = audit_emitter

    def subscribe(
        self,
        socket_queue: asyncio.Queue[dict[str, Any]],
        topics: list[str],
    ) -> None:
        """Register ``socket_queue`` for each topic in ``topics``.

        Replays the last published value for every topic that already has
        one AND whose timestamp is within the TTL window. Stale or unknown
        topics produce no replay frame.

        Args:
            socket_queue: The per-socket bounded queue owned by the route.
            topics: List of topic strings from the subscribe frame.
        """
        now = time.monotonic()
        for topic in topics:
            self._subscribers.setdefault(topic, set()).add(socket_queue)
            entry = self._last_value.get(topic)
            if entry is None:
                continue
            payload, ts = entry
            if now - ts > self._last_value_ttl:
                continue
            frame = {"topic": topic, "payload": payload}
            self._enqueue(topic, socket_queue, frame)

    def unsubscribe(self, socket_queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove ``socket_queue`` from every topic it was subscribed to.

        Idempotent — safe to call after the socket queue has already been
        removed or the topic has never been subscribed.
        """
        for queues in self._subscribers.values():
            queues.discard(socket_queue)

    async def publish(self, topic: str, payload: Any) -> None:
        """Publish ``payload`` under ``topic`` to all current subscribers.

        Stores the value with a monotonic timestamp for TTL-bounded replay
        and fans out to each subscriber queue with drop-oldest backpressure.
        ``await`` completes immediately after enqueueing (no socket I/O on
        the publish path).

        Args:
            topic: One of the 9 registered topic strings.
            payload: JSON-serialisable dict produced by the aggregator.
        """
        self._last_value[topic] = (payload, time.monotonic())
        frame = {"topic": topic, "payload": payload}
        for queue in list(self._subscribers.get(topic, set())):
            self._enqueue(topic, queue, frame)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _enqueue(
        self,
        topic: str,
        queue: asyncio.Queue[dict[str, Any]],
        frame: dict[str, Any],
    ) -> None:
        """Best-effort enqueue with drop-oldest fallback on full queue.

        Same pattern as ``web.py``'s ``_fan_out`` — a slow subscriber never
        stalls the publish path, and the oldest buffered frame is silently
        dropped rather than blocking.

        SPEC-025 §M-2 — emits ``gateway.dashboard.dropped_backpressure``
        on every drop so a compliance auditor can see slow-subscriber
        events instead of relying on a debug log only.
        """
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                _ = queue.get_nowait()  # drop oldest
                queue.put_nowait(frame)
                self._audit(
                    "gateway.dashboard.dropped_backpressure",
                    {"topic": topic, "reason": "queue_full"},
                )
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                _logger.debug(
                    "dashboard_bus: drop frame for topic=%s (queue unusable)", topic
                )
                self._audit(
                    "gateway.dashboard.dropped_dead",
                    {"topic": topic, "reason": "queue_unusable"},
                )

    def _audit(self, action: str, data: dict[str, Any]) -> None:
        """Emit a structured audit event.

        Test hook ``audit_emitter`` (constructor arg) takes precedence;
        otherwise routes through ``arcgateway.audit.emit_event``. Errors
        are swallowed per AU-5 so the audit pipeline never interrupts
        the audited path.
        """
        if self._audit_emitter is not None:
            try:
                self._audit_emitter(action, data)
            except Exception:
                _logger.exception("dashboard_bus: test audit emitter raised")
            return
        try:
            from arcgateway.audit import emit_event as _arc_emit

            _arc_emit(
                action=action,
                target=str(data.get("topic", "dashboard")),
                outcome="dropped",
                extra=data,
            )
        except Exception:
            _logger.debug("dashboard_bus: audit emission failed", exc_info=True)

    def last_value(self, topic: str) -> Any:
        """Return the last published payload for ``topic``, or ``None``.

        Ignores TTL — this is for the test-helper / introspection path.
        Subscribe-side replay uses TTL filtering.
        """
        entry = self._last_value.get(topic)
        return entry[0] if entry is not None else None
