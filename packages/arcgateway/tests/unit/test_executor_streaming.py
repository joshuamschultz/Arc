"""Phase B (SPEC-027) — the executor streams real per-token Deltas.

Finishes the M2 TODO: the executor consumes the agent's arcrun StreamEvent
iterator and emits a Delta per token (is_final only on the terminal done
sentinel) — no more wrapping the whole reply as one fake token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from arcrun import StreamEvent, TokenEvent, TurnEndEvent

from arcgateway.executor import AsyncioExecutor, InboundEvent


def _event(message: str = "hi") -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did="did:arc:user:x",
        agent_did="did:arc:agent:y",
        session_key="sess-1",
        message=message,
    )


class _StreamingAgent:
    """Fake agent on the new SPEC-027 contract: session(key) + streaming run()."""

    def __init__(self, agent_did: str, tokens: list[str]) -> None:
        self.agent_did = agent_did
        self._tokens = tokens
        self.session_keys: list[str] = []

    async def session(self, key: str) -> str:
        self.session_keys.append(key)
        return key

    async def run(self, input_text: str, *, session: Any) -> AsyncIterator[StreamEvent]:
        for tok in self._tokens:
            yield TokenEvent(text=tok)
        yield TurnEndEvent(final_text="".join(self._tokens))


@pytest.mark.asyncio
async def test_streams_real_token_deltas() -> None:
    """A multi-token reply yields >=2 token Deltas; is_final only on the done sentinel."""
    agent = _StreamingAgent("did:arc:agent:y", ["hello", " ", "world"])

    async def _factory(agent_did: str) -> _StreamingAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)
    deltas = [d async for d in await executor.run(_event("greet"))]

    token_deltas = [d for d in deltas if d.kind == "token"]
    assert len(token_deltas) >= 2, "expected real per-token streaming, not one wrapped chunk"
    assert all(not d.is_final for d in token_deltas), "token deltas must not be final"
    assert deltas[-1].is_final is True, "last delta is the done sentinel"
    assert deltas[-1].kind == "done"
    # Reassembled token text is the full reply.
    assert "".join(d.content for d in token_deltas) == "hello world"
    # The executor bound the event's session_key to a real agent session.
    assert agent.session_keys == ["sess-1"]


@pytest.mark.asyncio
async def test_session_bound_per_event() -> None:
    """The executor opens the agent session for the event's session_key."""
    agent = _StreamingAgent("did:arc:agent:y", ["x"])

    async def _factory(agent_did: str) -> _StreamingAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=_factory)
    ev = InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did="did:arc:user:x",
        agent_did="did:arc:agent:y",
        session_key="channel-42",
        message="m",
    )
    _ = [d async for d in await executor.run(ev)]
    assert agent.session_keys == ["channel-42"]
