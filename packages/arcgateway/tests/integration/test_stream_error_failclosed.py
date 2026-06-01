"""Phase B (SPEC-027) — streaming error handling + backpressure.

AC-3.2: an error mid-stream terminates the turn fail-closed — the iterator is
abandoned, a single fail-closed Delta is emitted, the done sentinel closes the
turn, and no exception escapes to the socket (no partial-success claim).

AC-3.3: the streaming path is pull-based — a slow consumer does not let the
producing agent run unbounded ahead (no hidden buffer in the executor; the
per-socket queue bound lives in the adapters).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from arcrun import StreamEvent, TokenEvent, TurnEndEvent

from arcgateway.executor import AsyncioExecutor, InboundEvent


def _event() -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did="did:arc:user:x",
        agent_did="did:arc:agent:y",
        session_key="sess-err",
        message="go",
    )


class _FailingAgent:
    """Streams one token, then raises mid-stream."""

    def __init__(self, agent_did: str) -> None:
        self.agent_did = agent_did

    async def session(self, key: str) -> str:
        return key

    async def run(self, input_text: str, *, session: Any) -> AsyncIterator[StreamEvent]:
        yield TokenEvent(text="partial")
        raise RuntimeError("model exploded")


class _CountingAgent:
    """Streams many tokens, recording how many it has produced."""

    def __init__(self, agent_did: str) -> None:
        self.agent_did = agent_did
        self.produced = 0

    async def session(self, key: str) -> str:
        return key

    async def run(self, input_text: str, *, session: Any) -> AsyncIterator[StreamEvent]:
        for i in range(100):
            self.produced += 1
            yield TokenEvent(text=str(i))
        yield TurnEndEvent(final_text="done")


@pytest.mark.asyncio
async def test_error_midstream_fails_closed() -> None:
    """A raise mid-stream yields a fail-closed Delta + done sentinel; nothing escapes."""
    async def _factory(agent_did: str) -> _FailingAgent:
        return _FailingAgent(agent_did)

    executor = AsyncioExecutor(agent_factory=_factory)

    # The consuming iterator must NOT raise — the turn fails closed internally.
    deltas = [d async for d in await executor.run(_event())]

    assert any(d.content == "partial" for d in deltas), "tokens before the error still stream"
    assert any("[agent-error]" in d.content for d in deltas), "a fail-closed Delta is emitted"
    assert deltas[-1].kind == "done"
    assert deltas[-1].is_final is True, "the turn closes with the done sentinel"
    # No Delta claims success/finality before the done sentinel.
    assert all(not d.is_final for d in deltas[:-1])


@pytest.mark.asyncio
async def test_slow_consumer_does_not_run_producer_ahead() -> None:
    """Pull-based streaming: the agent never produces far beyond what's consumed."""
    agent = _CountingAgent("did:arc:agent:y")

    async def _factory(agent_did: str) -> _CountingAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)

    consumed = 0
    async for delta in await executor.run(_event()):
        if delta.kind == "token":
            consumed += 1
            # The producer is at most one token ahead of the consumer —
            # proof there is no unbounded buffer in the executor path.
            assert agent.produced <= consumed + 1
        if consumed >= 5:
            break
