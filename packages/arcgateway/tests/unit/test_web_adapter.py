"""Unit tests for arcgateway.adapters.web.WebPlatformAdapter.

Per SDD §8.1 — covers the adapter's full surface in isolation:
register/unregister, ingest, send fan-out, drain loop, inactivity monitor,
limits, and tool-call routing. Uses a fake WebSocket so tests stay
in-process and deterministic.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from arcgateway.adapters.web import WebAdapterFull, WebPlatformAdapter
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, InboundEvent

pytestmark = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeWebSocket:
    """Minimal duck-typed WebSocket suitable for adapter unit tests.

    Records every send_json payload and every close call. Optionally raises
    on send_json to simulate disconnect.
    """

    def __init__(self, *, raise_on_send: BaseException | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed: bool = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._raise_on_send = raise_on_send

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        # Round-trip through JSON to verify payload is JSON-serialisable.
        json.dumps(payload)
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


class _AuditCapture:
    """Captures audit events emitted by the adapter."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, action: str, data: dict[str, Any]) -> None:
        self.events.append((action, data))

    def by_action(self, action: str) -> list[dict[str, Any]]:
        return [data for act, data in self.events if act == action]


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _noop_on_message(event: InboundEvent) -> None:
    return None


def _make_adapter(
    *,
    on_message: Any = None,
    audit: Any = None,
    max_connections: int = 50,
    idle_timeout_seconds: int = 3600,
    max_frame_bytes: int = 65536,
) -> WebPlatformAdapter:
    return WebPlatformAdapter(
        on_message=on_message or _noop_on_message,
        agent_did="did:arc:agent:test",
        max_connections=max_connections,
        idle_timeout_seconds=idle_timeout_seconds,
        max_frame_bytes=max_frame_bytes,
        audit_emitter=audit,
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def test_connect_and_disconnect_are_noops() -> None:
    """connect() opens no remote service; disconnect() closes registered sockets."""
    adapter = _make_adapter()
    await adapter.connect()  # must not raise

    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.disconnect()
    assert ws.closed is True


# ── Register / unregister ─────────────────────────────────────────────────────


async def test_register_socket_returns_stable_chat_id() -> None:
    """register_socket stores the socket under the given chat_id."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    assert ws in adapter._sockets["chat-1"]
    assert adapter._socket_meta[ws] == ("chat-1", "did:arc:agent:a", "did:arc:viewer:u")
    await adapter.disconnect()


async def test_register_socket_different_tokens_different_chat_ids() -> None:
    """Two distinct chat_ids produce two distinct internal entries.

    Signature compliance: chat_id is computed externally by the route from
    the viewer token; the adapter never sees the token. Two different
    viewer tokens → two different chat_ids → two distinct adapter entries.
    """
    adapter = _make_adapter()
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    adapter.register_socket(ws_a, "did:arc:agent:a", "did:arc:viewer:u1", "chat-1")
    adapter.register_socket(ws_b, "did:arc:agent:a", "did:arc:viewer:u2", "chat-2")
    assert "chat-1" in adapter._sockets
    assert "chat-2" in adapter._sockets
    assert adapter._sockets["chat-1"] != adapter._sockets["chat-2"]
    await adapter.disconnect()


async def test_register_socket_does_not_accept_viewer_token() -> None:
    """Signature compliance: adapter is secret-free; no viewer_token parameter."""
    sig = inspect.signature(WebPlatformAdapter.register_socket)
    assert "viewer_token" not in sig.parameters
    # Positional arg names match the SDD contract exactly.
    expected = ["self", "ws", "agent_did", "user_did", "chat_id"]
    assert list(sig.parameters.keys()) == expected


async def test_unregister_socket_removes_from_set() -> None:
    """unregister_socket removes the socket from internal maps."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    adapter.unregister_socket(ws)
    assert ws not in adapter._socket_meta
    assert ws not in adapter._socket_queues


async def test_unregister_last_socket_removes_chat_id_key() -> None:
    """Removing the only socket for a chat_id deletes the chat_id entry."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    adapter.unregister_socket(ws)
    assert "chat-1" not in adapter._sockets


# ── Ingest ────────────────────────────────────────────────────────────────────


async def test_ingest_builds_correct_inbound_event() -> None:
    """ingest constructs a fully-formed InboundEvent and forwards it."""
    received: list[InboundEvent] = []

    async def capture(event: InboundEvent) -> None:
        received.append(event)

    adapter = _make_adapter(on_message=capture)
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.ingest("chat-1", "hello", client_seq=1)

    assert len(received) == 1
    event = received[0]
    assert event.platform == "web"
    assert event.chat_id == "chat-1"
    assert event.message == "hello"
    assert event.user_did == "did:arc:viewer:u"
    assert event.agent_did == "did:arc:agent:a"
    # session_key is the build_session_key output (16 hex chars).
    assert len(event.session_key) == 16
    assert event.raw_payload.get("client_seq") == 1
    await adapter.disconnect()


async def test_ingest_empty_text_rejected() -> None:
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    with pytest.raises(ValueError):
        await adapter.ingest("chat-1", "")
    await adapter.disconnect()


async def test_ingest_oversized_text_rejected() -> None:
    """ingest enforces max_frame_bytes (UTF-8 byte length)."""
    adapter = _make_adapter(max_frame_bytes=8)
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    with pytest.raises(ValueError):
        await adapter.ingest("chat-1", "x" * 9)
    await adapter.disconnect()


async def test_client_seq_replay_rejected() -> None:
    """A non-monotonic client_seq is rejected."""
    received: list[InboundEvent] = []

    async def capture(event: InboundEvent) -> None:
        received.append(event)

    adapter = _make_adapter(on_message=capture)
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.ingest("chat-1", "first", client_seq=5)
    with pytest.raises(ValueError):
        await adapter.ingest("chat-1", "replay", client_seq=5)
    with pytest.raises(ValueError):
        await adapter.ingest("chat-1", "older", client_seq=3)
    # New higher seq is accepted.
    await adapter.ingest("chat-1", "next", client_seq=6)
    assert [e.message for e in received] == ["first", "next"]
    await adapter.disconnect()


# ── Send ──────────────────────────────────────────────────────────────────────


async def _drain_once(adapter: WebPlatformAdapter, ws: FakeWebSocket) -> None:
    """Yield control until the adapter's drain task delivers any pending frame.

    The drain loop is a background task; we need to give the event loop
    enough turns for it to pop the queued payload and call ws.send_json.
    """
    queue = adapter._socket_queues.get(ws)
    if queue is None:
        return
    # Two yields are sufficient for a single put → drain → send_json cycle.
    for _ in range(5):
        if queue.empty() and ws.sent:
            return
        await asyncio.sleep(0)


async def test_send_fans_out_to_all_sockets() -> None:
    """Two sockets registered for the same chat_id both receive the message."""
    adapter = _make_adapter()
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    adapter.register_socket(ws_a, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    adapter.register_socket(ws_b, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello world")
    await _drain_once(adapter, ws_a)
    await _drain_once(adapter, ws_b)
    assert any(p.get("text") == "hello world" for p in ws_a.sent)
    assert any(p.get("text") == "hello world" for p in ws_b.sent)
    await adapter.disconnect()


async def test_send_no_sockets_is_noop() -> None:
    """send for a chat_id with no sockets emits a 'dropped' audit and returns."""
    audit = _AuditCapture()
    adapter = _make_adapter(audit=audit)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello")
    dropped = audit.by_action("gateway.message.dropped")
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "no_socket"


async def test_send_audit_hash_in_payload() -> None:
    """The outbound message frame includes audit_hash."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello")
    await _drain_once(adapter, ws)
    assert ws.sent
    payload = ws.sent[-1]
    assert payload["type"] == "message"
    assert payload["from"] == "agent"
    assert "audit_hash" in payload
    assert payload["audit_hash"].startswith("sha256:")
    assert "ts" in payload
    await adapter.disconnect()


async def test_send_under_backpressure_drops_oldest() -> None:
    """When per-socket queue is full, send drops the oldest frame."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    queue = adapter._socket_queues[ws]
    # Fill the queue to capacity without letting drain task run.
    for i in range(queue.maxsize):
        queue.put_nowait({"type": "filler", "i": i})
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "newest")
    # The drop-oldest path consumed the first filler and added our message.
    items: list[dict[str, Any]] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert any(p.get("text") == "newest" for p in items)
    # Original oldest filler (i=0) was dropped.
    assert not any(p.get("i") == 0 for p in items)
    await adapter.disconnect()


async def test_audit_emit_breakdown_under_partial_fanout() -> None:
    """One audit event per send call, with a per-outcome breakdown."""
    audit = _AuditCapture()
    adapter = _make_adapter(audit=audit)
    ws_ok = FakeWebSocket()
    ws_dead = FakeWebSocket()
    adapter.register_socket(ws_ok, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    adapter.register_socket(ws_dead, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    # Force ws_dead's queue to be missing so it counts as "dead".
    adapter._socket_queues.pop(ws_dead)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello")
    delivered = audit.by_action("gateway.message.delivered")
    assert len(delivered) == 1, "exactly one audit event per turn"
    breakdown = delivered[0]["breakdown"]
    assert breakdown["delivered"] == 1
    assert breakdown["dead"] == 1
    assert breakdown["dropped_backpressure"] == 0
    await adapter.disconnect()


# ── Drain loop ────────────────────────────────────────────────────────────────


async def test_drain_loop_handles_websocket_disconnect_cleanly() -> None:
    """A disconnect raised by send_json triggers unregister, no exception escapes."""
    adapter = _make_adapter()
    ws = FakeWebSocket(raise_on_send=RuntimeError("websocket disconnect"))
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello")
    # Yield until drain task encounters the error and unregisters.
    for _ in range(20):
        await asyncio.sleep(0)
        if ws not in adapter._socket_meta:
            break
    assert ws not in adapter._socket_meta


async def test_send_removes_dead_socket() -> None:
    """A socket whose queue is gone is reported as 'dead' in audit but not crashing."""
    audit = _AuditCapture()
    adapter = _make_adapter(audit=audit)
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    # Simulate the queue having been removed but the socket still in the map.
    adapter._socket_queues.pop(ws)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello")
    delivered = audit.by_action("gateway.message.delivered")
    assert delivered[0]["breakdown"]["dead"] == 1
    await adapter.disconnect()


# ── Inactivity ────────────────────────────────────────────────────────────────


async def test_inactivity_monitor_closes_idle_socket() -> None:
    """idle_timeout=0 with a manually backdated last_activity triggers immediate close."""
    adapter = _make_adapter(idle_timeout_seconds=0)
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    # Backdate to ensure idle threshold is exceeded.
    adapter._last_activity[ws] = adapter._last_activity[ws] - 1.0
    # Yield until the inactivity monitor closes the socket.
    for _ in range(50):
        await asyncio.sleep(0)
        if ws.closed:
            break
    assert ws.closed is True
    assert ws.close_reason == "idle"
    await adapter.disconnect()


# ── Limits ────────────────────────────────────────────────────────────────────


async def test_max_connections_rejects_overflow() -> None:
    adapter = _make_adapter(max_connections=1)
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    adapter.register_socket(ws_a, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    with pytest.raises(WebAdapterFull):
        adapter.register_socket(ws_b, "did:arc:agent:a", "did:arc:viewer:u", "chat-2")
    await adapter.disconnect()


# ── Tool-call frames ──────────────────────────────────────────────────────────


async def test_tool_call_delta_sends_tool_call_frame() -> None:
    """Delta(kind='tool_call') routes to a 'tool_call' frame, not a 'message' frame."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    delta = Delta(kind="tool_call", content="read_file path=/x", turn_id="t1")
    await adapter.dispatch_delta(target, delta)
    await _drain_once(adapter, ws)
    assert ws.sent
    payload = ws.sent[-1]
    assert payload["type"] == "tool_call"
    assert payload["turn_id"] == "t1"
    assert "ts" in payload
    await adapter.disconnect()


async def test_dispatch_delta_token_routes_to_message_frame() -> None:
    """A non-tool_call delta routes through send() and produces a message frame."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    delta = Delta(kind="token", content="hello", turn_id="turn-x")
    await adapter.dispatch_delta(target, delta)
    await _drain_once(adapter, ws)
    assert ws.sent
    payload = ws.sent[-1]
    assert payload["type"] == "message"
    assert payload["turn_id"] == "turn-x"
    await adapter.disconnect()


async def test_dispatch_delta_tool_call_no_sockets_is_noop() -> None:
    """tool_call with no sockets registered for the chat_id is a silent no-op."""
    adapter = _make_adapter()
    target = DeliveryTarget(platform="web", chat_id="chat-empty")
    await adapter.dispatch_delta(
        target, Delta(kind="tool_call", content="read_file", turn_id="t")
    )
    # Nothing to assert — just verify no exception.


# ── Auxiliary surface ─────────────────────────────────────────────────────────


async def test_send_with_id_returns_none() -> None:
    """The default send_with_id always returns None (no platform message ID)."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    result = await adapter.send_with_id(target, "hi")
    assert result is None
    await adapter.disconnect()


async def test_unregister_socket_idempotent_when_already_removed() -> None:
    """Calling unregister_socket on an already-removed socket is a no-op."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    adapter.unregister_socket(ws)
    # Second call must not raise.
    adapter.unregister_socket(ws)


async def test_ingest_silently_drops_when_chat_unregistered() -> None:
    """ingest after the route closed the socket must not raise.

    The chat_id may have been unregistered between the inbound frame
    arriving on the route and ingest being called. Drop silently.
    """
    received: list[InboundEvent] = []

    async def capture(event: InboundEvent) -> None:
        received.append(event)

    adapter = _make_adapter(on_message=capture)
    # No socket ever registered — meta lookup returns None.
    await adapter.ingest("ghost-chat", "hello")
    assert received == []


async def test_audit_emitter_exception_is_swallowed() -> None:
    """A raising audit emitter must never break the audited operation."""

    def boom(_action: str, _data: dict[str, Any]) -> None:
        raise RuntimeError("audit sink down")

    adapter = _make_adapter(audit=boom)
    ws = FakeWebSocket()
    # register_socket emits an audit event; if the emitter raises and the
    # adapter doesn't swallow it, we'll see it here.
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "hello")  # also emits audit
    await _drain_once(adapter, ws)
    assert ws.sent  # delivery still happened
    await adapter.disconnect()
