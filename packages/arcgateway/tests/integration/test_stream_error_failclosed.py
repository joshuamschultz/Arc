"""Phase B (SPEC-027) — delivery error handling + the executor's absence of buffering.

AC-3.2: a failure while the message is being delivered, or while the turn it
opened is running, terminates the turn fail-closed — a single fail-closed Delta
is emitted, the done sentinel closes the turn, and no exception escapes to the
socket (no partial-success claim, and no raw exception text on the channel).

AC-3.3: the executor holds no buffer of its own. Since SPEC-065 it does not
stand between a producing agent and a consuming socket at all: it hands the
message over and awaits at most one reply, so there is no queue in this layer
that a slow consumer could let grow. The test below pins that property as it now
exists — exactly one reply per delivered message, and nothing carried across
messages — rather than the producer/consumer race it replaced.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcgateway.executor import AsyncioExecutor, InboundEvent


def _event(message: str = "go") -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did="did:arc:user:x",
        agent_did="did:arc:agent:y",
        session_key="sess-err",
        message=message,
    )


class _FailingDeliveryAgent:
    """Raises when the message is handed over."""

    def __init__(self, agent_did: str) -> None:
        self.agent_did = agent_did

    async def deliver_message(self, **_: Any) -> str:
        raise RuntimeError("model exploded at /secret/path with token hunter2")


class _FailingRunAgent:
    """Accepts the message, opens a turn, and that turn then fails."""

    def __init__(self, agent_did: str) -> None:
        self.agent_did = agent_did

    async def deliver_message(self, *, on_handle: Any | None = None, **_: Any) -> str:
        if on_handle is not None:
            on_handle(_ExplodingHandle())
        return "started"


class _ExplodingHandle:
    async def result(self) -> Any:
        raise RuntimeError("the run died at /secret/path")


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _Handle:
    def __init__(self, content: str) -> None:
        self._content = content

    async def result(self) -> _Reply:
        return _Reply(self._content)


class _CountingAgent:
    """Opens a turn per delivery, counting how many replies it has handed back."""

    def __init__(self, agent_did: str) -> None:
        self.agent_did = agent_did
        self.replies_handed_back = 0

    async def deliver_message(self, *, on_handle: Any | None = None, **_: Any) -> str:
        self.replies_handed_back += 1
        if on_handle is not None:
            on_handle(_Handle(f"reply {self.replies_handed_back}"))
        return "started"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [_FailingDeliveryAgent, _FailingRunAgent])
async def test_error_fails_closed(agent_cls: Any) -> None:
    """A raise on either side of the seam yields a fail-closed Delta + done sentinel."""

    async def _factory(agent_did: str) -> Any:
        return agent_cls(agent_did)

    executor = AsyncioExecutor(agent_factory=_factory)

    # The consuming iterator must NOT raise — the turn fails closed internally.
    deltas = [d async for d in await executor.run(_event())]

    assert any("[agent-error]" in d.content for d in deltas), "a fail-closed Delta is emitted"
    assert deltas[-1].kind == "done"
    assert deltas[-1].is_final is True, "the turn closes with the done sentinel"
    # No Delta claims success/finality before the done sentinel.
    assert all(not d.is_final for d in deltas[:-1])
    # The raw failure never reaches the channel (LLM02/LLM07) — it is in the log.
    combined = " ".join(d.content for d in deltas)
    assert "/secret/path" not in combined
    assert "hunter2" not in combined


@pytest.mark.asyncio
async def test_executor_buffers_nothing_across_messages() -> None:
    """AC-3.3: one delivered message yields exactly its own reply, and no more."""
    agent = _CountingAgent("did:arc:agent:y")

    async def _factory(agent_did: str) -> _CountingAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)

    first = [d async for d in await executor.run(_event("one"))]
    second = [d async for d in await executor.run(_event("two"))]

    assert [d.content for d in first if d.kind == "token"] == ["reply 1"]
    assert [d.content for d in second if d.kind == "token"] == ["reply 2"], (
        "the executor must not replay or accumulate a previous turn's reply"
    )
    assert agent.replies_handed_back == 2
