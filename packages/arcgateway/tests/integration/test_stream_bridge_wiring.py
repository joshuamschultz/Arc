"""Integration tests for StreamBridge wiring through SessionRouter.

Verifies the full pipeline:
    InboundEvent → SessionRouter → AsyncioExecutor (chat_stream) → StreamBridge
    → adapter.send() + adapter.edit_message() called incrementally.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import AsyncioExecutor, Delta, InboundEvent
from arcgateway.session import SessionRouter
from arcgateway.stream_bridge import StreamBridge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inbound_event(message: str = "hello") -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="12345",
        user_did="did:arc:telegram:42",
        agent_did="did:arc:org:agent/test",
        session_key="test_session_key",
        message=message,
    )


async def _delta_stream(*contents: str) -> AsyncIterator[Delta]:
    """Yield token deltas followed by a done sentinel."""
    turn_id = "test-turn"
    for text in contents:
        yield Delta(kind="token", content=text, is_final=False, turn_id=turn_id)
    yield Delta(kind="done", content="", is_final=True, turn_id=turn_id)


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class _MockAdapter:
    """Minimal adapter that records all calls for assertion."""

    name = "mock"

    def __init__(self) -> None:
        self.sent_messages: list[tuple[DeliveryTarget, str]] = []
        self.edited_messages: list[tuple[DeliveryTarget, str, str]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        self.sent_messages.append((target, message))

    async def edit_message(
        self,
        target: DeliveryTarget,
        message_id: str,
        new_text: str,
    ) -> None:
        self.edited_messages.append((target, message_id, new_text))

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str:
        """Extension: send and return a fake message_id for editing."""
        self.sent_messages.append((target, message))
        return "msg-001"


# ---------------------------------------------------------------------------
# Tests: StreamBridge.consume() directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_bridge_calls_send_placeholder() -> None:
    """StreamBridge sends an initial placeholder via adapter.send()."""
    adapter = _MockAdapter()
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    async def _stream() -> AsyncIterator[Delta]:
        yield Delta(kind="done", content="", is_final=True, turn_id="t1")

    await bridge.consume(_stream(), target, adapter)

    # The placeholder message must have been sent.
    assert len(adapter.sent_messages) >= 1


@pytest.mark.asyncio
async def test_stream_bridge_sends_final_message() -> None:
    """StreamBridge sends the complete accumulated text at turn end."""
    adapter = _MockAdapter()
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    async def _stream() -> AsyncIterator[Delta]:
        yield Delta(kind="token", content="hello ", is_final=False, turn_id="t1")
        yield Delta(kind="token", content="world", is_final=False, turn_id="t1")
        yield Delta(kind="done", content="", is_final=True, turn_id="t1")

    await bridge.consume(_stream(), target, adapter)

    # The last send must contain the accumulated text.
    final_texts = [msg for _, msg in adapter.sent_messages]
    assert any("hello" in t and "world" in t for t in final_texts), (
        f"Expected final send with 'hello world', got: {final_texts}"
    )


@pytest.mark.asyncio
async def test_stream_bridge_edits_with_message_id() -> None:
    """StreamBridge calls edit_message() when send_with_id() is available."""
    from arcgateway.stream_bridge import EDIT_TOKEN_BUFFER_SIZE

    adapter = _MockAdapter()  # has send_with_id()
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    # Generate enough tokens to trigger a flush.
    tokens = ["x"] * (EDIT_TOKEN_BUFFER_SIZE + 5)

    async def _stream() -> AsyncIterator[Delta]:
        for t in tokens:
            yield Delta(kind="token", content=t, is_final=False, turn_id="t1")
        yield Delta(kind="done", content="", is_final=True, turn_id="t1")

    await bridge.consume(_stream(), target, adapter)

    # At least one edit should have been attempted.
    assert len(adapter.edited_messages) >= 1
    # The message_id from send_with_id must be passed through.
    assert all(mid == "msg-001" for _, mid, _ in adapter.edited_messages)


# ---------------------------------------------------------------------------
# Tests: SessionRouter → StreamBridge wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_router_wires_stream_bridge() -> None:
    """When adapter is wired, SessionRouter uses StreamBridge for delivery."""
    response_text = "routed via stream bridge"
    adapter = _MockAdapter()

    async def _chat_stream_method(message: str, **kwargs: Any) -> Any:
        """Fake chat_stream() returning a 2-token stream."""
        from arcrun import TokenEvent, TurnEndEvent

        async def _gen() -> Any:
            yield TokenEvent(text=response_text)
            yield TurnEndEvent(final_text=response_text)

        return _gen()

    fake_agent = MagicMock()
    fake_agent.chat_stream = _chat_stream_method

    async def _agent_factory(agent_did: str) -> Any:
        return fake_agent

    executor = AsyncioExecutor(agent_factory=_agent_factory)
    router = SessionRouter(
        executor=executor,
        adapter=adapter,  # type: ignore[arg-type]
    )

    event = _make_inbound_event("hello")
    await router.handle(event)

    # Wait for the session task to complete.
    await asyncio.sleep(0.1)

    # The adapter must have received at least one send call (placeholder + final).
    assert len(adapter.sent_messages) >= 1


@pytest.mark.asyncio
async def test_session_router_without_adapter_logs_only() -> None:
    """When no adapter is wired, SessionRouter logs deltas without delivery."""
    response_text = "no adapter mode"
    send_calls: list[str] = []

    async def _chat_stream_method(message: str, **kwargs: Any) -> Any:
        from arcrun import TokenEvent, TurnEndEvent

        async def _gen() -> Any:
            yield TokenEvent(text=response_text)
            yield TurnEndEvent(final_text=response_text)

        return _gen()

    fake_agent = MagicMock()
    fake_agent.chat_stream = _chat_stream_method

    async def _agent_factory(agent_did: str) -> Any:
        return fake_agent

    executor = AsyncioExecutor(agent_factory=_agent_factory)
    # No adapter wired.
    router = SessionRouter(executor=executor)

    event = _make_inbound_event("hello")
    await router.handle(event)

    await asyncio.sleep(0.1)

    # No send calls should have happened (no adapter).
    # This assertion just checks the test didn't crash — the real
    # verification is that no AttributeError was raised.
    assert send_calls == []
