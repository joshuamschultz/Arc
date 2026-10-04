"""A chat turn that cannot start must fail visibly, never freeze the channel.

Production (2026-10-03): a message to Olivia produced no reply, no error and no
log. Whatever the blocking await is, the interactive delivery path must bound
it: the caller gets a terminal ``failed`` event with a plain reason, and the
session is usable again afterwards. These tests force real interleaving with
``asyncio.Event`` holders, not instant mocks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun import StreamEvent, TokenEvent, TurnEndEvent

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    SessionConfig,
    TelemetryConfig,
)
from arcagent.core.session_internal.manager import SessionManager
from arcagent.streaming import DeliveryStreamEvent, DeliveryTerminalEvent, DeliveryTextEvent

_START_BOUND = 0.3


@pytest.fixture()
def agent_config(tmp_path: Path) -> ArcAgentConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ArcAgentConfig(
        agent=AgentConfig(name="bound-agent", org="o", type="executor", workspace=str(workspace)),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        session=SessionConfig(turn_start_timeout_seconds=_START_BOUND),
    )


async def _reply(*args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
    async def stream() -> AsyncIterator[StreamEvent]:
        yield TokenEvent(text="pong")
        yield TurnEndEvent(final_text="pong")

    return stream()


async def _deliver(agent: ArcAgent, session_key: str) -> list[DeliveryStreamEvent]:
    return [
        event
        async for event in agent.stream_delivered_message(
            caller_did="did:arc:user/1", message="ping", session_key=session_key
        )
    ]


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_stuck_turn_start_yields_failed_frame_within_bound(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig
) -> None:
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(config=agent_config)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold_session_turn() -> None:
        # Something else (a background run, a stuck earlier turn) owns the
        # session's turn lock and does not let go.
        async with agent._run_coordinator.turn("web:chat-1"):
            holding.set()
            await release.wait()

    trace: list[Any] = []
    with (
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_reply),
        patch("arcagent.core.context_prep.spool_record", side_effect=trace.append),
    ):
        await agent.startup()
        holder = asyncio.create_task(hold_session_turn())
        try:
            await holding.wait()
            async with asyncio.timeout(_START_BOUND * 10):
                events = await _deliver(agent, "web:chat-1")

            assert isinstance(events[-1], DeliveryTerminalEvent)
            assert events[-1].status == "failed"
            assert "did not start" in events[-1].reason
            # The abandoned run is closed on its own trace, never left "Running".
            closed = [r for r in trace if r.name == "run.not_started"]
            assert len(closed) == 1
            assert closed[0].outcome == "failed"
            assert "did not start within" in closed[0].extra["reason"]
            assert closed[0].request_id

            # The bound released the delivery lock: once the holder is gone the
            # same session answers normally.
            release.set()
            await holder
            async with asyncio.timeout(5):
                events = await _deliver(agent, "web:chat-1")
            assert any(isinstance(e, DeliveryTextEvent) and e.text == "pong" for e in events)
            assert isinstance(events[-1], DeliveryTerminalEvent)
            assert events[-1].status == "completed"
        finally:
            release.set()
            await asyncio.gather(holder, return_exceptions=True)
            await agent.shutdown()


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_slow_session_open_never_blocks_another_session(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig
) -> None:
    """One session's open (a large history read) must not stall every chat."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(config=agent_config)
    opening = asyncio.Event()
    release = asyncio.Event()
    real_open = SessionManager.open_or_resume

    async def gated_open(self: SessionManager, key: str) -> list[dict[str, Any]]:
        if key == "slow":
            opening.set()
            await release.wait()
        return await real_open(self, key)

    with (
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_reply),
        patch.object(SessionManager, "open_or_resume", gated_open),
    ):
        await agent.startup()
        slow = asyncio.create_task(agent.session("slow"))
        try:
            await opening.wait()
            async with asyncio.timeout(5):
                events = await _deliver(agent, "fast")
            assert isinstance(events[-1], DeliveryTerminalEvent)
            assert events[-1].status == "completed"
            assert not slow.done()
        finally:
            release.set()
            await asyncio.gather(slow, return_exceptions=True)
            await agent.shutdown()


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_concurrent_opens_of_one_key_share_one_manager(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig
) -> None:
    """Per-key locking keeps the get-or-create single-flight for one key."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(config=agent_config)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_open = SessionManager.open_or_resume

    async def gated_open(self: SessionManager, key: str) -> list[dict[str, Any]]:
        entered.set()
        await release.wait()
        return await real_open(self, key)

    with patch.object(SessionManager, "open_or_resume", gated_open):
        await agent.startup()
        try:
            first = asyncio.create_task(agent.session("same"))
            await entered.wait()
            second = asyncio.create_task(agent.session("same"))
            await asyncio.sleep(0)
            release.set()
            one, two = await asyncio.gather(first, second)
            assert one is two
        finally:
            await agent.shutdown()


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_connector_reconcile_fast_path_is_bounded(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig
) -> None:
    """A reconcile stuck behind a long sync reports pending and finishes later.

    The operator's Remove awaited it inline for 24 minutes while the browser
    gave up at 60 seconds.
    """
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(config=agent_config)
    release = asyncio.Event()
    finished = asyncio.Event()

    class _SlowConnectors:
        async def reconcile(self) -> Any:
            await release.wait()
            finished.set()
            from arcagent.connector_control import ConnectorReconcileResult

            return ConnectorReconcileResult(status="applied")

    entry = MagicMock(setup_done=True, instance=_SlowConnectors())
    await agent.startup()
    try:
        with patch.object(
            agent._capability_registry, "get_capability", AsyncMock(return_value=entry)
        ):
            async with asyncio.timeout(2):
                result = await agent.reconcile_connectors(wait_seconds=0.2)
            assert result.status == "activation_pending"
            release.set()
            async with asyncio.timeout(2):
                await finished.wait()
    finally:
        release.set()
        await agent.shutdown()
