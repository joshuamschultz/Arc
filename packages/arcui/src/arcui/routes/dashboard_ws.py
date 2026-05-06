"""Dashboard WebSocket route — ``/ws/dashboard``.

SPEC-025 Track E (SDD §C5).

Thin proxy between browser dashboard widgets and
``arcgateway.dashboard_events.DashboardEventBus``.  The browser sends one
subscribe frame, and the server pushes events as aggregators publish state
changes.  No polling; no ``setInterval``.

Protocol:

  Browser → server (subscribe frame):
      {"type": "subscribe", "topics": ["stats", "queue", ...]}

  Server → browser (event frame):
      {"type": "event", "topic": "stats", "payload": {...}, "ts": "..."}

  Server → browser (on subscribe, if topic has a cached value):
      {"type": "event", "topic": "stats", "payload": {...}, "ts": "..."}
      — replayed immediately so the widget doesn't start blank.

Auth:
    Same first-message token as /ws/chat/{agent_id}: ``authenticate_ws``
    validates a ``{"token": "..."}`` first frame.  Only ``viewer`` and
    ``operator`` roles may subscribe; ``agent`` tokens are rejected (ASI03).
    An audit event is emitted on subscribe (NIST AU-2).

Backpressure:
    Per-socket ``asyncio.Queue(maxsize=100)`` with drop-oldest fallback,
    identical to ``web.py``.  A slow browser never stalls publish().
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from arcui.ws_helpers import (
    CLOSE_AUTH_INVALID,
    authenticate_ws,
)

if TYPE_CHECKING:
    from arcgateway.dashboard_events import DashboardEventBus

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 100

# Valid topic names — kept as a frozenset to guard against arbitrary-string
# subscriptions without blocking on unknown topics gracefully.
_VALID_TOPICS = frozenset(
    {
        "stats",
        "stats.timeseries",
        "circuit_breakers",
        "budget",
        "performance",
        "queue",
        "cost_efficiency",
        "roster",
        "schedule_history",
    }
)

# SPEC-025 §H-3 — minimum role required to subscribe to each topic.
# Topics absent from this map default to ``viewer`` (lowest-privilege).
# Operator-tier topics carry financial / capacity-planning data that
# viewers should not see at federal tiers.
_TOPIC_MIN_ROLE: dict[str, str] = {
    "budget": "operator",
    "cost_efficiency": "operator",
    "circuit_breakers": "operator",
    "schedule_history": "operator",
}

# Role hierarchy — index encodes privilege (higher = more privilege).
# A role at index N satisfies any minimum role at index <= N.
_ROLE_HIERARCHY: list[str] = ["viewer", "operator"]


def _role_satisfies(actual: str, minimum: str) -> bool:
    """Return True iff ``actual`` is at least ``minimum`` in the hierarchy."""
    try:
        return _ROLE_HIERARCHY.index(actual) >= _ROLE_HIERARCHY.index(minimum)
    except ValueError:
        # Unknown role — fail closed.
        return False


def _filter_topics_by_role(topics: list[str], role: str) -> tuple[list[str], list[str]]:
    """Split ``topics`` into ``(allowed, denied)`` per the role hierarchy.

    SPEC-025 §H-3 — viewer-role subscribers must not receive ``budget`` /
    ``cost_efficiency`` / ``circuit_breakers`` / ``schedule_history``
    payloads. Returning the denied list lets the caller emit a single
    audit event covering the dropped topics.
    """
    allowed: list[str] = []
    denied: list[str] = []
    for topic in topics:
        required = _TOPIC_MIN_ROLE.get(topic, "viewer")
        if _role_satisfies(role, required):
            allowed.append(topic)
        else:
            denied.append(topic)
    return allowed, denied


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp, millisecond precision."""
    return (
        datetime.now(tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


async def _drain(ws: WebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    """Forward each queued event frame to the WebSocket as a typed event envelope.

    Wraps the raw bus payload in the wire envelope so callers get:
        {"type": "event", "topic": "...", "payload": {...}, "ts": "..."}
    """
    try:
        while True:
            frame = await queue.get()
            envelope = {
                "type": "event",
                "topic": frame["topic"],
                "payload": frame["payload"],
                "ts": _utcnow_iso(),
            }
            try:
                await ws.send_json(envelope)
            except Exception:
                # Socket closed or errored — stop draining.
                return
    except asyncio.CancelledError:
        return


async def dashboard_ws_endpoint(ws: WebSocket) -> None:
    """Accept a browser WebSocket and stream dashboard events.

    Flow:
      1. Accept the connection.
      2. Authenticate via first-message token.
      3. Expect a subscribe frame with a list of topics.
      4. Register a per-socket queue with the bus; bus replays last values.
      5. Drain queue → WebSocket in a background task.
      6. Loop on incoming frames (none expected in v1 — close signals stop).
      7. On exit: cancel drain task, unsubscribe queue from bus.
    """
    await ws.accept()

    auth_config = ws.app.state.auth_config
    role, _ = await authenticate_ws(ws, auth_config)
    if role is None:
        return  # authenticate_ws already closed the WS

    if role not in ("viewer", "operator"):
        await ws.send_json({"error": "Agent tokens cannot subscribe to dashboard"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return

    bus: DashboardEventBus | None = getattr(ws.app.state, "dashboard_bus", None)
    if bus is None:
        # Bus not wired (e.g. tests without gateway config).  Return a
        # minimal "no data" response rather than crashing.
        await ws.send_json({"type": "error", "code": "bus_unavailable"})
        await ws.close(code=1011)
        return

    # Expect the first real payload to be a subscribe frame.
    try:
        msg = await ws.receive_json()
    except (WebSocketDisconnect, RuntimeError):
        return

    if not isinstance(msg, dict) or msg.get("type") != "subscribe":
        await ws.send_json(
            {"type": "error", "code": "protocol", "message": "first frame must be subscribe"}
        )
        await ws.close(code=1003)
        return

    raw_topics: object = msg.get("topics", [])
    if not isinstance(raw_topics, list):
        await ws.send_json(
            {"type": "error", "code": "protocol", "message": "topics must be a list"}
        )
        await ws.close(code=1003)
        return

    # Accept only known topics; silently drop unknown ones to avoid
    # information-leakage via error enumeration (SPEC-025 §Security).
    known_topics = [t for t in raw_topics if isinstance(t, str) and t in _VALID_TOPICS]
    unknown_topics = [
        t for t in raw_topics if isinstance(t, str) and t not in _VALID_TOPICS
    ]

    # SPEC-025 §H-3 — viewer cannot subscribe to operator-tier topics.
    topics, role_denied = _filter_topics_by_role(known_topics, role)

    # Emit audit event (NIST AU-2 — every significant operation is logged).
    # SPEC-025 §L-5 — also include the dropped topics so an operator
    # debugging an empty widget can see why it never received an event.
    try:
        from arcgateway.audit import emit_event as _arc_emit

        _arc_emit(
            action="gateway.dashboard.subscribe",
            target="dashboard",
            outcome="allow" if not role_denied else "partial",
            extra={
                "role": role,
                "topics": topics,
                "denied_by_role": role_denied,
                "dropped_unknown": unknown_topics,
            },
        )
    except Exception:
        logger.debug("dashboard_ws: audit emission failed", exc_info=True)

    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    # Subscribe; bus replays last-known value for each topic into queue.
    bus.subscribe(queue, topics)

    drain_task = asyncio.create_task(_drain(ws, queue), name="dashboard:drain")
    try:
        # v1: client sends nothing after subscribe; loop exits on disconnect.
        async for _ in ws.iter_text():
            pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        drain_task.cancel()
        bus.unsubscribe(queue)


routes = [
    WebSocketRoute("/ws/dashboard", dashboard_ws_endpoint),
]
