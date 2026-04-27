"""Integration tests for ArcAgent.chat_stream().

Verifies that chat_stream() returns an AsyncIterator[StreamEvent] that
yields incremental tokens and terminates with TurnEndEvent.

Follows the same pattern as test_agent_integration.py — only external
entry points (load_eval_model, arcrun_run, arcrun_run_stream) are mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun import StreamEvent, TokenEvent, TurnEndEvent
from arcrun.types import LoopResult

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="stream-test-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        ),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


def _make_loop_result(text: str) -> LoopResult:
    return LoopResult(
        content=text,
        turns=1,
        tool_calls_made=0,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )


async def _fake_stream(*tokens: str) -> AsyncIterator[StreamEvent]:
    """Yield TokenEvents for each token string, then TurnEndEvent."""
    for t in tokens:
        yield TokenEvent(text=t)
    yield TurnEndEvent(final_text="".join(tokens))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("arcagent.core.agent.load_eval_model")
async def test_chat_stream_returns_async_iterator(
    mock_load_model: MagicMock,
    agent_config: ArcAgentConfig,
) -> None:
    """chat_stream() must return an async-iterable object."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return _fake_stream("hello")

    agent = ArcAgent(config=agent_config)
    with patch("arcagent.core.agent.arcrun_run_stream", side_effect=_fake_run_stream):
        await agent.startup()
        try:
            stream = await agent.chat_stream("say hello")
            assert hasattr(stream, "__aiter__"), "chat_stream must return an async iterable"
        finally:
            await agent.shutdown()


@pytest.mark.asyncio
@patch("arcagent.core.agent.load_eval_model")
async def test_chat_stream_yields_tokens_then_turn_end(
    mock_load_model: MagicMock,
    agent_config: ArcAgentConfig,
) -> None:
    """chat_stream() yields TokenEvent(s) followed by TurnEndEvent."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    response_text = "per token streaming works"

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return _fake_stream(*response_text.split())

    agent = ArcAgent(config=agent_config)
    events: list[Any] = []

    with patch("arcagent.core.agent.arcrun_run_stream", side_effect=_fake_run_stream):
        await agent.startup()
        try:
            stream = await agent.chat_stream("task")
            async for ev in stream:
                events.append(ev)
        finally:
            await agent.shutdown()

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    turn_end_events = [e for e in events if isinstance(e, TurnEndEvent)]

    assert len(token_events) >= 1, "Expected at least one TokenEvent"
    assert len(turn_end_events) == 1, "Expected exactly one TurnEndEvent"
    assert isinstance(events[-1], TurnEndEvent), "TurnEndEvent must be last"

    # Joined token text must match the response text.
    joined = " ".join(e.text for e in token_events)
    assert joined == response_text


@pytest.mark.asyncio
@patch("arcagent.core.agent.load_eval_model")
async def test_chat_stream_final_text_matches_response(
    mock_load_model: MagicMock,
    agent_config: ArcAgentConfig,
) -> None:
    """TurnEndEvent.final_text must equal the full response text."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    response_text = "the final answer"

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return _fake_stream(response_text)

    agent = ArcAgent(config=agent_config)

    with patch("arcagent.core.agent.arcrun_run_stream", side_effect=_fake_run_stream):
        await agent.startup()
        try:
            stream = await agent.chat_stream("question")
            turn_end: TurnEndEvent | None = None
            async for ev in stream:
                if isinstance(ev, TurnEndEvent):
                    turn_end = ev
        finally:
            await agent.shutdown()

    assert turn_end is not None
    assert turn_end.final_text == response_text


@pytest.mark.asyncio
@patch("arcagent.core.agent.load_eval_model")
@patch("arcagent.core.agent.arcrun_run")
async def test_chat_stream_back_compat_run_unchanged(
    mock_arcrun_run: AsyncMock,
    mock_load_model: MagicMock,
    agent_config: ArcAgentConfig,
) -> None:
    """ArcAgent.run() must still work and return a LoopResult (back-compat)."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    response_text = "run result"
    loop_result = _make_loop_result(response_text)
    mock_arcrun_run.return_value = loop_result

    agent = ArcAgent(config=agent_config)
    await agent.startup()
    try:
        result = await agent.run("task")
    finally:
        await agent.shutdown()

    assert result.content == response_text


@pytest.mark.asyncio
@patch("arcagent.core.agent.load_eval_model")
async def test_chat_stream_bus_events_emitted(
    mock_load_model: MagicMock,
    agent_config: ArcAgentConfig,
) -> None:
    """chat_stream() emits agent:pre_respond and agent:post_respond bus events."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    emitted_events: list[str] = []

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return _fake_stream("response")

    agent = ArcAgent(config=agent_config)

    with patch("arcagent.core.agent.arcrun_run_stream", side_effect=_fake_run_stream):
        await agent.startup()
        try:
            assert agent._bus is not None

            async def _capture(ctx: Any) -> None:
                emitted_events.append(ctx.event)

            agent._bus.subscribe("agent:pre_respond", _capture, priority=999)
            agent._bus.subscribe("agent:post_respond", _capture, priority=999)

            stream = await agent.chat_stream("task")
            async for _ in stream:
                pass
        finally:
            await agent.shutdown()

    assert "agent:pre_respond" in emitted_events
    assert "agent:post_respond" in emitted_events
