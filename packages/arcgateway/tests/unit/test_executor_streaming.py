"""The executor hands every message to the agent and streams back only its own turn.

SPEC-065 COMP-006. The executor used to drive ``agent.run(...)`` itself, which
always opened a fresh turn. It now calls the agent's delivery entry point and
adapts the reply of a turn that delivery *opened* into a Delta. A message that
joined a run already in flight streams nothing here — its answer rides the
stream of the turn it joined, so the user gets one reply, not two.

The fake agent below deliberately declares **no** ``interrupt`` parameter: an
executor that ever passed a run decision across this seam would raise
``TypeError`` instead of quietly working (REQ-312).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from arcgateway.executor import AsyncioExecutor, InboundEvent


def _event(message: str = "hi", *, session_key: str = "sess-1") -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did="did:arc:user:x",
        agent_did="did:arc:agent:y",
        session_key=session_key,
        message=message,
    )


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content


class _Handle:
    """Stand-in for the arcrun RunHandle delivery hands back for a started turn."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def result(self) -> _Result:
        return _Result(self._content)


@dataclass
class _Delivery:
    caller_did: str
    message: str
    session_key: str
    reply_target: str | None
    reply_label: str | None


class _DeliveryAgent:
    """Fake agent on the delivery contract: ``deliver_message(...) -> outcome``."""

    def __init__(self, agent_did: str, *, reply: str = "", outcome: str = "started") -> None:
        self.agent_did = agent_did
        self._reply = reply
        self._outcome = outcome
        self.deliveries: list[_Delivery] = []

    async def deliver_message(
        self,
        *,
        caller_did: str,
        message: str,
        session_key: str,
        reply_target: str | None = None,
        reply_label: str | None = None,
        on_handle: Any | None = None,
    ) -> str:
        self.deliveries.append(
            _Delivery(
                caller_did=caller_did,
                message=message,
                session_key=session_key,
                reply_target=reply_target,
                reply_label=reply_label,
            )
        )
        if self._outcome == "started" and on_handle is not None:
            on_handle(_Handle(self._reply))
        return self._outcome


@pytest.mark.asyncio
async def test_message_is_delivered_to_the_agent_with_its_channel() -> None:
    """Every inbound message reaches the delivery entry point, channel included."""
    agent = _DeliveryAgent("did:arc:agent:y", reply="hello world")

    async def _factory(agent_did: str) -> _DeliveryAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)
    deltas = [d async for d in await executor.run(_event("greet"))]

    assert len(agent.deliveries) == 1, "the message must be handed to the agent exactly once"
    delivered = agent.deliveries[0]
    assert delivered.message == "greet"
    assert delivered.caller_did == "did:arc:user:x"
    # The executor binds the event's session key — the agent derives none of its own.
    assert delivered.session_key == "sess-1"
    # The inbound channel is threaded so a mid-chat schedule can default delivery.
    assert delivered.reply_target == "telegram:1"
    # A friendly label is threaded so the channel can appear in arcui's dropdown.
    assert delivered.reply_label == "Telegram (chat 1)"

    token_deltas = [d for d in deltas if d.kind == "token"]
    assert "".join(d.content for d in token_deltas) == "hello world"
    assert all(not d.is_final for d in token_deltas), "token deltas must not be final"
    assert deltas[-1].kind == "done"
    assert deltas[-1].is_final is True, "last delta is the done sentinel"


@pytest.mark.asyncio
async def test_session_bound_per_event() -> None:
    """The executor delivers against the event's session_key, whatever it is."""
    agent = _DeliveryAgent("did:arc:agent:y", reply="x")

    async def _factory(agent_did: str) -> _DeliveryAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)
    _ = [d async for d in await executor.run(_event("m", session_key="channel-42"))]

    assert [d.session_key for d in agent.deliveries] == ["channel-42"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["followed_up", "steered"])
async def test_message_that_joined_a_live_run_streams_no_reply(outcome: str) -> None:
    """No second reply for a message the agent injected into a turn in flight.

    The injected message is answered by the run it joined, and that run's reply
    leaves on the stream of the turn that opened it. Emitting anything here would
    send the user two messages for one answer.
    """
    agent = _DeliveryAgent("did:arc:agent:y", reply="unused", outcome=outcome)

    async def _factory(agent_did: str) -> _DeliveryAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)
    deltas = [d async for d in await executor.run(_event("and another thing"))]

    assert agent.deliveries, "the message still reaches the agent"
    assert [d.kind for d in deltas] == ["done"], f"expected only the done sentinel, got {deltas!r}"
    assert deltas[-1].is_final is True
