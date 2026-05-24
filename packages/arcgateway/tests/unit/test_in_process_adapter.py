"""Tests for arcgateway.adapters.in_process — PythonAdapter + DeltaStream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from arcgateway.adapters.in_process import DeltaStream, PythonAdapter
from arcgateway.executor import Delta, InboundEvent


def _make_event(
    *,
    chat_id: str = "12345",
    session_key: str = "sess",
    user_did: str = "did:arc:user:alice",
    agent_did: str = "did:arc:agent:bot",
    message: str = "hi",
) -> InboundEvent:
    return InboundEvent(
        platform="python",
        chat_id=chat_id,
        user_did=user_did,
        agent_did=agent_did,
        session_key=session_key,
        message=message,
    )


class _EchoExecutor:
    """Yields N token deltas then a terminal done delta."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        for c in self._chunks:
            yield Delta(kind="token", content=c, is_final=False, turn_id=event.chat_id)
        yield Delta(kind="done", content="", is_final=True, turn_id=event.chat_id)


@pytest.mark.asyncio
async def test_delta_stream_terminates_on_done() -> None:
    queue: asyncio.Queue[Delta] = asyncio.Queue()
    queue.put_nowait(Delta(kind="token", content="a"))
    queue.put_nowait(Delta(kind="done", is_final=True))
    queue.put_nowait(Delta(kind="token", content="leak"))  # must not be seen

    stream = DeltaStream(queue, session_key="sk", timeout=1.0)
    seen = [d async for d in stream]
    assert [d.content for d in seen] == ["a", ""]
    assert seen[-1].is_final is True


@pytest.mark.asyncio
async def test_delta_stream_timeout_raises() -> None:
    queue: asyncio.Queue[Delta] = asyncio.Queue()
    stream = DeltaStream(queue, session_key="sk", timeout=0.05)
    with pytest.raises(asyncio.TimeoutError):
        async for _ in stream:
            pass


@pytest.mark.asyncio
async def test_python_adapter_satisfies_protocol() -> None:
    """The Protocol is runtime_checkable — confirm structural conformance."""
    from arcgateway.adapters.base import BasePlatformAdapter

    adapter = PythonAdapter()
    assert isinstance(adapter, BasePlatformAdapter)
    assert adapter.name == "python"


@pytest.mark.asyncio
async def test_send_pushes_token_delta_into_queue() -> None:
    """The adapter's ``send`` is invoked by StreamBridge as it flushes
    edits; the result must land on the dispatching call's queue as a
    token delta so the DeltaStream consumer sees the text."""
    from arcgateway.delivery import DeliveryTarget

    adapter = PythonAdapter()
    queue: asyncio.Queue[Delta] = asyncio.Queue()
    adapter._streams["chat-1"] = queue

    target = DeliveryTarget(platform="python", chat_id="chat-1", thread_id=None)
    await adapter.send(target, "hello")

    delta = queue.get_nowait()
    assert delta.kind == "token"
    assert delta.content == "hello"
    assert delta.is_final is False


@pytest.mark.asyncio
async def test_send_to_unknown_chat_id_is_noop() -> None:
    """If no dispatch is in flight for the chat_id, send drops silently."""
    from arcgateway.delivery import DeliveryTarget

    adapter = PythonAdapter()
    target = DeliveryTarget(platform="python", chat_id="never-dispatched", thread_id=None)
    # Must not raise; nothing to put it on.
    await adapter.send(target, "orphan")


@pytest.mark.asyncio
async def test_disconnect_drains_open_streams() -> None:
    """``disconnect`` must release any open DeltaStream consumers."""
    adapter = PythonAdapter()
    # Manually populate a stream as if a dispatch were in flight.
    queue: asyncio.Queue[Delta] = asyncio.Queue()
    adapter._streams["12345"] = queue

    await adapter.disconnect()
    assert adapter._streams == {}
    drained = queue.get_nowait()
    assert drained.is_final is True
