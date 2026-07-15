"""GAP-A — a streaming/chat run is cancellable by the operator kill-switch.

These tests drive ``agent.run`` (the single dispatch/stream entry) with the REAL
arcrun loop — no ``arcrun_run_stream`` patch. They prove the streaming run now
registers a live ``RunHandle`` the runcontrol watcher can resolve (``_find_handle``)
and cooperatively cancel, terminating the stream with an operator-attributed
structured result — the same seam the tracked path already had. And that the
handle is unregistered when the stream ends (no leak).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcrun import TokenEvent, TurnEndEvent
from arcstore.cancellations import CancelRequest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.modules.runcontrol.capabilities import _find_handle

pytestmark = pytest.mark.asyncio

_OPERATOR_DID = "did:arc:ui:operator"


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 60
        self.output_tokens = 40
        self.total_tokens = 100


class _ToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = call_id
        self.name = name
        self.arguments = arguments


class _Resp:
    def __init__(self, tool_calls: list[_ToolCall]) -> None:
        self.content = "working"
        self.stop_reason = "tool_use"
        self.tool_calls = tool_calls
        self.cost_usd = 0.0
        self.usage = _Usage()


class _SlowLoopingModel:
    """Never ends on its own — every turn calls a tool, with a small delay so a
    concurrent canceller can catch the run mid-flight. Only a cancel/breaker
    stops it before ``max_turns``."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, _messages: Any, tools: Any = None, **_: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(0.03)
        return _Resp([_ToolCall(f"tc{self.calls}", "nonexistent_tool", {})])

    async def close(self) -> None:
        return None


def _config(workspace: Path, tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="cancel-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


async def _wait_for_active_run(agent: ArcAgent, timeout: float = 2.0) -> str:
    """Poll the agent's tracked-run map until the streaming handle registers."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if agent._active_runs:
            return next(iter(agent._active_runs))
        await asyncio.sleep(0.01)
    raise AssertionError("streaming run never registered a handle in _active_runs")


async def test_streaming_run_cancelled_via_watcher_seam(tmp_path: Path) -> None:
    """A chat/streaming run is resolvable by the watcher and stops cooperatively.

    The run is driven through ``agent.run`` (real loop). Once it registers, we
    resolve it exactly as the watcher does — ``_find_handle`` against a
    ``CancelRequest`` naming the session — and call ``handle.cancel`` with the
    operator DID. The stream terminates with the attributed cancelled payload.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = ArcAgent(config=_config(workspace, tmp_path))
    await agent.startup()
    agent._model = _SlowLoopingModel()

    session = await agent.session("chat:web:alice")
    events: list[Any] = []

    async def _consume() -> None:
        async for ev in agent.run("go forever", session=session):
            events.append(ev)

    consumer = asyncio.ensure_future(_consume())
    try:
        session_key = await _wait_for_active_run(agent)
        assert session_key == session.session_id

        # Resolve via the REAL watcher seam (by session_key, as the operator
        # names a chat run) and cancel with the operator identity.
        req = CancelRequest(
            id="c1", session_key=session_key, requested_by=_OPERATOR_DID, reason="stop it"
        )
        match = _find_handle(agent, req)
        assert match is not None, "watcher could not resolve the streaming handle"
        _, handle = match
        await handle.cancel(req.requested_by, req.reason)

        await asyncio.wait_for(consumer, timeout=2.0)
    finally:
        if not consumer.done():
            consumer.cancel()
        await agent.shutdown()

    turn_end = events[-1]
    assert isinstance(turn_end, TurnEndEvent)
    assert turn_end.completion_payload is not None
    assert turn_end.completion_payload["error"] == "cancelled"
    assert _OPERATOR_DID in turn_end.final_text
    assert "stop it" in turn_end.final_text


async def test_streaming_run_resolvable_by_run_id(tmp_path: Path) -> None:
    """The watcher can also name the run by ``run_id`` (the timeline identifier)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = ArcAgent(config=_config(workspace, tmp_path))
    await agent.startup()
    agent._model = _SlowLoopingModel()

    session = await agent.session("chat:web:bob")

    async def _consume() -> None:
        async for _ in agent.run("go forever", session=session, run_id="run-xyz"):
            pass

    consumer = asyncio.ensure_future(_consume())
    try:
        await _wait_for_active_run(agent)
        req = CancelRequest(id="c2", run_id="run-xyz", requested_by=_OPERATOR_DID, reason="stop")
        match = _find_handle(agent, req)
        assert match is not None
        _, handle = match
        assert handle.state.run_id == "run-xyz"
        await handle.cancel(req.requested_by, req.reason)
        await asyncio.wait_for(consumer, timeout=2.0)
    finally:
        if not consumer.done():
            consumer.cancel()
        await agent.shutdown()


async def test_streaming_handle_unregistered_after_completion(tmp_path: Path) -> None:
    """No leak: a normally-completing streaming run leaves ``_active_runs`` empty."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = ArcAgent(config=_config(workspace, tmp_path))
    await agent.startup()

    class _EndTurnModel:
        async def invoke(self, _messages: Any, tools: Any = None, **_: Any) -> Any:
            resp = _Resp([])
            resp.stop_reason = "end_turn"
            resp.content = "done"
            return resp

        async def close(self) -> None:
            return None

    agent._model = _EndTurnModel()
    session = await agent.session("chat:web:carol")

    events = [ev async for ev in agent.run("hello", session=session)]

    assert isinstance(events[-1], TurnEndEvent)
    assert any(isinstance(e, TokenEvent) for e in events)
    # The handle was removed when the stream finished — nothing left tracked.
    assert session.session_id not in agent._active_runs
    assert agent._active_runs == {}
    await agent.shutdown()
