"""WebPlatformAdapter — browser chat platform adapter for arcgateway.

Implements ``BasePlatformAdapter`` so ArcUI is just another chat
platform peered with Slack and Telegram. Inbound traffic arrives as
WebSocket connections handed in by ``arcui.routes.chat_ws``; outbound
traffic streams back as JSON frames per per-socket bounded queues.

Design (SDD §3.2 / brainstorm §8):
  - The adapter owns no remote service; ``connect()`` is a no-op.
  - One bounded ``asyncio.Queue`` per socket isolates a slow consumer
    so it never blocks the agent's turn or other browsers.
  - ``send()`` returns immediately after enqueueing — the per-socket
    drain task is responsible for actual delivery — and emits exactly
    one ``gateway.message.delivered`` audit event per call carrying a
    fan-out breakdown (delivered / dropped_backpressure / dead).
  - The adapter never sees the viewer token; the route derives
    ``user_did`` and ``chat_id`` and passes them in via
    ``register_socket``. (NFR-5 — secret-free adapter.)
  - Backpressure: queue full ⇒ drop oldest enqueued frame, retry put.

Threat model coverage (SDD §7):
  - DoS via too-many connections → ``max_connections`` cap raises
    ``WebAdapterFull`` from ``register_socket`` so the route can return
    HTTP 429 to the client.
  - DoS via oversized frames → ``max_frame_bytes`` cap on inbound
    text (UTF-8 byte length, not character count).
  - Replay → strictly monotonic ``client_seq`` enforced in ``ingest``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import build_session_key

_logger = logging.getLogger("arcgateway.adapters.web")

_QUEUE_MAXSIZE = 100
_INACTIVITY_CHECK_SECONDS = 60.0

_DEFAULT_MAX_CONNECTIONS = 50
_DEFAULT_IDLE_TIMEOUT_SECONDS = 3600
_DEFAULT_MAX_FRAME_BYTES = 65_536


class WebAdapterFull(RuntimeError):  # noqa: N818 — name predates ruff naming check
    """Raised by ``register_socket`` when ``max_connections`` is reached.

    Routes catch this and return HTTP 429 so the client backs off.
    """


@runtime_checkable
class WebSocketLike(Protocol):
    """Minimum surface the adapter relies on from a WebSocket.

    Defined here so arcgateway does not depend on Starlette / FastAPI at
    import time — keeps the adapter package light and the dependency
    direction clean (arcui → arcgateway, never the reverse).
    """

    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


def _utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp, millisecond precision, ``Z`` suffix."""
    return (
        datetime.now(tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _audit_hash(turn_id: str, message: str) -> str:
    """Deterministic audit hash for an outbound message frame."""
    return "sha256:" + hashlib.sha256(f"{turn_id}:{message}".encode()).hexdigest()


class WebPlatformAdapter:
    """Browser chat platform adapter — implements ``BasePlatformAdapter``.

    State is per-socket and per-chat_id: the same logical chat may have
    multiple browser tabs open simultaneously, and ``send`` fans out to
    each. ``unregister_socket`` is the single cleanup entry point — it
    removes the socket from every internal map and cancels its tasks.
    """

    name: str = "web"

    def __init__(
        self,
        *,
        on_message: Callable[[InboundEvent], Awaitable[None]],
        agent_did: str = "",
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        idle_timeout_seconds: int = _DEFAULT_IDLE_TIMEOUT_SECONDS,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        audit_emitter: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._on_message = on_message
        self._default_agent_did = agent_did
        self.max_connections = max_connections
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_frame_bytes = max_frame_bytes
        self._audit_emitter = audit_emitter

        self._sockets: dict[str, set[Any]] = {}
        self._socket_meta: dict[Any, tuple[str, str, str]] = {}
        self._socket_queues: dict[Any, asyncio.Queue[dict[str, Any]]] = {}
        self._socket_tasks: dict[Any, list[asyncio.Task[None]]] = {}
        self._last_activity: dict[Any, float] = {}
        self._inbound_seq: dict[str, int] = {}
        self._n_connections = 0

    # ── BasePlatformAdapter Protocol ──────────────────────────────────────────

    async def connect(self) -> None:
        """No remote service to dial — return promptly."""
        _logger.info("WebPlatformAdapter: connect (in-process, no remote dial)")

    async def disconnect(self) -> None:
        """Cancel all per-socket tasks and close every registered socket."""
        sockets = list(self._socket_meta.keys())
        for ws in sockets:
            self.unregister_socket(ws)
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="shutdown")

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Fan out a ``message`` frame to every socket for ``target.chat_id``.

        Returns without waiting for socket I/O. Emits exactly one audit
        event per call. Drop-oldest under backpressure preserves liveness.
        """
        sockets = list(self._sockets.get(target.chat_id, set()))
        turn_id = reply_to or hashlib.sha256(
            f"{target.chat_id}:{message}".encode()
        ).hexdigest()[:16]
        audit_hash = _audit_hash(turn_id, message)

        if not sockets:
            self._audit(
                "gateway.message.dropped",
                {
                    "chat_id": target.chat_id,
                    "reason": "no_socket",
                    "audit_hash": audit_hash,
                },
            )
            return

        payload = {
            "type": "message",
            "from": "agent",
            "text": message,
            "turn_id": turn_id,
            "audit_hash": audit_hash,
            "ts": _utcnow_iso(),
        }
        breakdown = self._fan_out(sockets, payload)
        self._audit(
            "gateway.message.delivered",
            {
                "chat_id": target.chat_id,
                "audit_hash": audit_hash,
                "breakdown": breakdown,
            },
        )

    async def send_with_id(
        self,
        target: DeliveryTarget,
        message: str,
    ) -> str | None:
        """No platform-assigned message ID for browser frames; default to None."""
        await self.send(target, message)
        return None

    # ── Web-specific surface ──────────────────────────────────────────────────

    def register_socket(
        self,
        ws: WebSocketLike,
        agent_did: str,
        user_did: str,
        chat_id: str,
    ) -> None:
        """Register a freshly-accepted WebSocket for the given identities.

        Raises:
            WebAdapterFull: When ``max_connections`` has been reached.
        """
        if self._n_connections >= self.max_connections:
            raise WebAdapterFull(
                f"max_connections ({self.max_connections}) reached"
            )

        self._sockets.setdefault(chat_id, set()).add(ws)
        self._socket_meta[ws] = (chat_id, agent_did, user_did)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._socket_queues[ws] = queue
        self._last_activity[ws] = time.monotonic()
        self._n_connections += 1

        drain_task = asyncio.create_task(
            self._drain_loop(ws, queue),
            name=f"web:drain:{chat_id}",
        )
        idle_task = asyncio.create_task(
            self._inactivity_monitor(ws),
            name=f"web:idle:{chat_id}",
        )
        self._socket_tasks[ws] = [drain_task, idle_task]

        self._audit(
            "gateway.adapter.register",
            {
                "platform": "web",
                "chat_id": chat_id,
                "agent_did": agent_did,
                "user_did": user_did,
            },
        )

    def unregister_socket(self, ws: WebSocketLike) -> None:
        """Remove a socket from every internal map and cancel its tasks.

        Idempotent — calling on an already-removed socket is a no-op.
        """
        meta = self._socket_meta.pop(ws, None)
        if meta is None:
            return
        chat_id, _, _ = meta

        sockets_for_chat = self._sockets.get(chat_id)
        if sockets_for_chat is not None:
            sockets_for_chat.discard(ws)
            if not sockets_for_chat:
                del self._sockets[chat_id]

        tasks = self._socket_tasks.pop(ws, [])
        for task in tasks:
            task.cancel()

        self._socket_queues.pop(ws, None)
        self._last_activity.pop(ws, None)
        self._n_connections = max(0, self._n_connections - 1)

        self._audit(
            "gateway.adapter.unregister",
            {"platform": "web", "chat_id": chat_id},
        )

    async def ingest(
        self,
        chat_id: str,
        text: str,
        client_seq: int | None = None,
    ) -> None:
        """Build an InboundEvent from a browser frame and forward it.

        Validation precedes any ``await`` — a malformed frame never
        reaches the SessionRouter.
        """
        if not text:
            msg = "empty text"
            raise ValueError(msg)
        if len(text.encode("utf-8")) > self.max_frame_bytes:
            msg = "frame too large"
            raise ValueError(msg)

        if client_seq is not None:
            last = self._inbound_seq.get(chat_id, -1)
            if client_seq <= last:
                msg = "replay"
                raise ValueError(msg)
            self._inbound_seq[chat_id] = client_seq

        meta = self._meta_for_chat_id(chat_id)
        if meta is None:
            return  # socket already unregistered — drop silently
        agent_did, user_did = meta

        raw_payload: dict[str, Any] = (
            {"client_seq": client_seq} if client_seq is not None else {}
        )
        event = InboundEvent(
            platform="web",
            chat_id=chat_id,
            thread_id=None,
            user_did=user_did,
            agent_did=agent_did,
            session_key=build_session_key(agent_did, user_did),
            message=text,
            raw_payload=raw_payload,
        )
        await self._on_message(event)

    async def dispatch_delta(
        self,
        target: DeliveryTarget,
        delta: Delta,
    ) -> None:
        """Route a single Delta into the matching outbound frame type.

        - ``kind="tool_call"`` → ``tool_call`` frame (per SDD §5.1).
        - ``kind="token"`` / ``"done"`` → ``message`` frame (the bridge
          accumulates tokens; this is the single-frame escape hatch).
        """
        if delta.kind == "tool_call":
            sockets = list(self._sockets.get(target.chat_id, set()))
            if not sockets:
                return
            payload = {
                "type": "tool_call",
                "tool": delta.content,
                "args": "",
                "turn_id": delta.turn_id,
                "ts": _utcnow_iso(),
            }
            self._fan_out(sockets, payload)
            return
        await self.send(target, delta.content, reply_to=delta.turn_id or None)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fan_out(
        self,
        sockets: list[Any],
        payload: dict[str, Any],
    ) -> dict[str, int]:
        """Enqueue ``payload`` on every socket queue, with drop-oldest fallback.

        Returns the per-outcome breakdown for audit emission.
        """
        breakdown = {"delivered": 0, "dropped_backpressure": 0, "dead": 0}
        for ws in sockets:
            queue = self._socket_queues.get(ws)
            if queue is None:
                breakdown["dead"] += 1
                continue
            try:
                queue.put_nowait(payload)
                breakdown["delivered"] += 1
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                    queue.put_nowait(payload)
                    breakdown["dropped_backpressure"] += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    breakdown["dead"] += 1
        return breakdown

    def _meta_for_chat_id(self, chat_id: str) -> tuple[str, str] | None:
        """Look up (agent_did, user_did) for any socket on ``chat_id``."""
        sockets = self._sockets.get(chat_id)
        if not sockets:
            return None
        ws = next(iter(sockets))
        meta = self._socket_meta.get(ws)
        if meta is None:
            return None
        _, agent_did, user_did = meta
        return agent_did, user_did

    async def _drain_loop(
        self,
        ws: WebSocketLike,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        """Forward each queued payload to ``ws.send_json``.

        On any disconnect / runtime error from ``send_json``, unregister
        the socket and exit cleanly.
        """
        try:
            while True:
                payload = await queue.get()
                try:
                    await ws.send_json(payload)
                    self._last_activity[ws] = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.unregister_socket(ws)
                    return
        except asyncio.CancelledError:
            return

    async def _inactivity_monitor(self, ws: WebSocketLike) -> None:
        """Close the socket if it has been silent for ``idle_timeout_seconds``."""
        try:
            while ws in self._socket_queues:
                idle = time.monotonic() - self._last_activity.get(ws, 0.0)
                if idle > self.idle_timeout_seconds:
                    with contextlib.suppress(Exception):
                        await ws.close(code=1000, reason="idle")
                    self.unregister_socket(ws)
                    return
                await asyncio.sleep(_INACTIVITY_CHECK_SECONDS)
        except asyncio.CancelledError:
            return

    def _audit(self, action: str, data: dict[str, Any]) -> None:
        """Emit a structured audit event.

        Uses the test-injected emitter when present; otherwise routes to
        the canonical ``arcgateway.audit.emit_event``. Errors are swallowed
        per AU-5 so the audit pipeline never interrupts the audited path.
        """
        if self._audit_emitter is not None:
            try:
                self._audit_emitter(action, data)
            except Exception:
                _logger.exception("WebPlatformAdapter: test audit emitter raised")
            return
        try:
            from arcgateway.audit import emit_event as _arc_emit

            _arc_emit(
                action=action,
                target=str(data.get("chat_id", "web")),
                outcome=data.get("outcome", "allow"),
                extra=data,
            )
        except Exception:
            _logger.exception("WebPlatformAdapter: audit emission failed")
