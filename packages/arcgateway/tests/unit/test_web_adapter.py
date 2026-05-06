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
    """Signature compliance: adapter is secret-free; no viewer_token parameter.

    The contract is: required positional args ``ws, agent_did, user_did, chat_id``
    plus keyword-only optional ``since_seq`` for replay on reconnect (SPEC-025
    Track A). The adapter never accepts the viewer token directly.
    """
    sig = inspect.signature(WebPlatformAdapter.register_socket)
    assert "viewer_token" not in sig.parameters
    expected = ["self", "ws", "agent_did", "user_did", "chat_id", "since_seq"]
    assert list(sig.parameters.keys()) == expected
    # since_seq is keyword-only with a None default
    assert sig.parameters["since_seq"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["since_seq"].default is None


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


# ── SPEC-025 Track A — Sequence-gap detection + replay buffer ────────────────


async def test_send_stamps_monotonic_seq_per_chat_id() -> None:
    """Every outbound frame for a chat_id carries a monotonic 'seq' starting at 0."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.send(target, "first")
    await adapter.send(target, "second")
    await adapter.send(target, "third")
    await _drain_once(adapter, ws)
    seqs = [p.get("seq") for p in ws.sent if p.get("type") == "message"]
    assert seqs == [0, 1, 2]
    await adapter.disconnect()


async def test_seq_counters_are_independent_per_chat_id() -> None:
    """Two chat_ids have independent seq counters."""
    adapter = _make_adapter()
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    adapter.register_socket(ws_a, "did:arc:agent:a", "did:arc:viewer:u1", "chat-A")
    adapter.register_socket(ws_b, "did:arc:agent:a", "did:arc:viewer:u2", "chat-B")
    await adapter.send(DeliveryTarget(platform="web", chat_id="chat-A"), "a1")
    await adapter.send(DeliveryTarget(platform="web", chat_id="chat-A"), "a2")
    await adapter.send(DeliveryTarget(platform="web", chat_id="chat-B"), "b1")
    await _drain_once(adapter, ws_a)
    await _drain_once(adapter, ws_b)
    a_seqs = [p.get("seq") for p in ws_a.sent if p.get("type") == "message"]
    b_seqs = [p.get("seq") for p in ws_b.sent if p.get("type") == "message"]
    assert a_seqs == [0, 1]
    assert b_seqs == [0]
    await adapter.disconnect()


async def test_tool_call_frame_also_carries_seq() -> None:
    """Tool-call frames stamp seq from the same per-chat counter as message frames."""
    adapter = _make_adapter()
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    await adapter.dispatch_delta(target, Delta(kind="tool_call", content="read_file", turn_id="t1"))
    await adapter.send(target, "result text")
    await _drain_once(adapter, ws)
    seqs = [p.get("seq") for p in ws.sent]
    assert seqs == [0, 1]
    await adapter.disconnect()


async def test_register_with_since_seq_replays_missed_frames() -> None:
    """Reconnecting with since_seq=N replays frames whose seq > N to the new socket only."""
    adapter = _make_adapter()
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    # Original socket receives the first three frames (seq 0, 1, 2).
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await adapter.send(target, "msg-1")
    await adapter.send(target, "msg-2")
    await _drain_once(adapter, ws_old)
    # Disconnect the old socket. Server-side ring still holds frames 0..2.
    adapter.unregister_socket(ws_old)
    # New socket reconnects, claims it last saw seq=0 — wants 1 and 2 replayed.
    ws_new = FakeWebSocket()
    adapter.register_socket(
        ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1", since_seq=0,
    )
    await _drain_once(adapter, ws_new)
    replayed_seqs = [p.get("seq") for p in ws_new.sent if p.get("type") == "message"]
    assert replayed_seqs == [1, 2]
    replayed_texts = [p.get("text") for p in ws_new.sent if p.get("type") == "message"]
    assert replayed_texts == ["msg-1", "msg-2"]
    await adapter.disconnect()


async def test_register_with_since_seq_replays_only_to_new_socket() -> None:
    """Replay does not fan out to other sockets on the same chat_id."""
    adapter = _make_adapter()
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_existing = FakeWebSocket()
    adapter.register_socket(ws_existing, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await _drain_once(adapter, ws_existing)
    n_seen_before = len(ws_existing.sent)
    # New socket joins with since_seq=-1 — wants the full ring replayed.
    ws_new = FakeWebSocket()
    adapter.register_socket(
        ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1", since_seq=-1,
    )
    await _drain_once(adapter, ws_new)
    await _drain_once(adapter, ws_existing)
    # Existing socket received nothing extra; new socket got the replay.
    assert len(ws_existing.sent) == n_seen_before
    assert any(p.get("text") == "msg-0" for p in ws_new.sent)
    await adapter.disconnect()


async def test_replay_emits_recovery_banner_when_ring_overran() -> None:
    """If since_seq is below the oldest frame in the ring, prepend a recovery_banner."""
    adapter = _make_adapter()
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    # Send more frames than the ring can hold to force overrun (ring max = 50 per SDD §C1).
    for i in range(60):
        await adapter.send(target, f"msg-{i}")
    await _drain_once(adapter, ws_old)
    adapter.unregister_socket(ws_old)
    # Reconnect claiming last seen seq=0 — but ring only holds frames 10..59.
    ws_new = FakeWebSocket()
    adapter.register_socket(
        ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1", since_seq=0,
    )
    await _drain_once(adapter, ws_new)
    # First payload must be the recovery banner; client uses it to flag a UX warning.
    banners = [p for p in ws_new.sent if p.get("type") == "recovery_banner"]
    assert len(banners) == 1
    assert banners[0]["lost_below_seq"] > 1, "banner must say which seq was the floor"
    await adapter.disconnect()


async def test_replay_with_since_seq_at_or_above_latest_is_noop() -> None:
    """If client claims it's already at or past latest seq, replay sends nothing extra."""
    adapter = _make_adapter()
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await adapter.send(target, "msg-1")
    await _drain_once(adapter, ws_old)
    adapter.unregister_socket(ws_old)
    ws_new = FakeWebSocket()
    adapter.register_socket(
        ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1", since_seq=1,
    )
    await _drain_once(adapter, ws_new)
    # No replay frames (since_seq=1 means client already has up to seq 1).
    msg_frames = [p for p in ws_new.sent if p.get("type") == "message"]
    banners = [p for p in ws_new.sent if p.get("type") == "recovery_banner"]
    assert msg_frames == []
    assert banners == []
    await adapter.disconnect()


# ── SPEC-025 §TD-1 — TTL eviction of replay state ────────────────────────────


def _make_adapter_with_eviction(*, ttl: float = 0.05, audit: Any = None) -> WebPlatformAdapter:
    """Build an adapter with a tiny TTL so eviction tests stay fast."""
    return WebPlatformAdapter(
        on_message=_noop_on_message,
        agent_did="did:arc:agent:test",
        max_connections=50,
        idle_timeout_seconds=3600,
        max_frame_bytes=65536,
        replay_ttl_seconds=ttl,
        audit_emitter=audit,
    )


async def test_replay_buffer_survives_within_ttl_window() -> None:
    """A reconnect inside the TTL window cancels eviction and gets full replay."""
    adapter = _make_adapter_with_eviction(ttl=10.0)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await adapter.send(target, "msg-1")
    await _drain_once(adapter, ws_old)
    adapter.unregister_socket(ws_old)
    # Reconnect fast — TTL hasn't expired.
    ws_new = FakeWebSocket()
    adapter.register_socket(
        ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1", since_seq=-1,
    )
    await _drain_once(adapter, ws_new)
    replayed = [p["text"] for p in ws_new.sent if p.get("type") == "message"]
    assert replayed == ["msg-0", "msg-1"]
    await adapter.disconnect()


async def test_replay_buffer_evicted_after_ttl_elapses() -> None:
    """After last unregister + TTL, _replay_buffers and _outbound_seq drop."""
    audit = _AuditCapture()
    adapter = _make_adapter_with_eviction(ttl=0.01, audit=audit)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await _drain_once(adapter, ws_old)
    adapter.unregister_socket(ws_old)
    assert "chat-1" in adapter._replay_buffers
    # Wait for the eviction task to fire.
    await asyncio.sleep(0.1)
    assert "chat-1" not in adapter._replay_buffers
    assert "chat-1" not in adapter._outbound_seq
    # Audit emits an evicted event.
    evicted = audit.by_action("gateway.replay.evicted")
    assert len(evicted) == 1
    assert evicted[0]["chat_id"] == "chat-1"
    await adapter.disconnect()


async def test_register_within_ttl_cancels_pending_eviction() -> None:
    """Reconnect cancels the eviction task; next unregister schedules a new one."""
    adapter = _make_adapter_with_eviction(ttl=10.0)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await _drain_once(adapter, ws_old)
    adapter.unregister_socket(ws_old)
    assert "chat-1" in adapter._eviction_tasks
    pending_task = adapter._eviction_tasks["chat-1"]
    # Reconnect — must cancel the pending task.
    ws_new = FakeWebSocket()
    adapter.register_socket(ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    # Yield to let the cancellation propagate.
    for _ in range(5):
        await asyncio.sleep(0)
        if pending_task.done():
            break
    assert pending_task.cancelled() or pending_task.done()
    assert "chat-1" not in adapter._eviction_tasks
    await adapter.disconnect()


async def test_disconnect_cancels_pending_evictions() -> None:
    """adapter.disconnect() must not leave eviction tasks running."""
    adapter = _make_adapter_with_eviction(ttl=10.0)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws = FakeWebSocket()
    adapter.register_socket(ws, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    await adapter.send(target, "msg-0")
    await _drain_once(adapter, ws)
    adapter.unregister_socket(ws)
    pending = adapter._eviction_tasks.get("chat-1")
    assert pending is not None
    await adapter.disconnect()
    # Yield to let cancellation finish.
    for _ in range(5):
        await asyncio.sleep(0)
        if pending.done():
            break
    assert adapter._eviction_tasks == {}
    assert pending.cancelled() or pending.done()


# ── SPEC-025 §TD-6 — replay drop-oldest emits audit ──────────────────────────


async def test_replay_drop_oldest_emits_audit_event() -> None:
    """When the per-socket queue is full during replay, the drop is audited."""
    audit = _AuditCapture()
    adapter = _make_adapter_with_eviction(ttl=10.0, audit=audit)
    target = DeliveryTarget(platform="web", chat_id="chat-1")
    ws_old = FakeWebSocket()
    adapter.register_socket(ws_old, "did:arc:agent:a", "did:arc:viewer:u", "chat-1")
    # Push frames into the ring without draining.
    for i in range(5):
        await adapter.send(target, f"msg-{i}")
    adapter.unregister_socket(ws_old)
    # New socket — fill its queue to capacity, then trigger replay.
    ws_new = FakeWebSocket()
    adapter.register_socket(
        ws_new, "did:arc:agent:a", "did:arc:viewer:u", "chat-1",
    )
    queue = adapter._socket_queues[ws_new]
    # Fill to maxsize - 0 so any further put_nowait without get_nowait fails.
    while not queue.full():
        queue.put_nowait({"type": "filler"})
    # Now manually replay — every frame should hit the QueueFull → drop branch.
    adapter._replay_to_socket(ws_new, "chat-1", since_seq=-1)
    # At least one drop event should have been audited.
    drops = audit.by_action("gateway.replay.dropped_backpressure")
    assert len(drops) >= 1
    assert drops[0]["chat_id"] == "chat-1"
    await adapter.disconnect()
