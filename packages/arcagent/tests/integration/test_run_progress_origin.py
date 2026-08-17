"""The origin channel reaches the progress stream through the real dispatch path.

The bridge and the narrator are each unit-tested. This drives the actual
``agent.run`` path instead, takes the ``on_event`` callback arcrun is really
handed, and pushes a dynamic-run event through it — so a wire left unconnected
between the turn's ``reply_target`` and the bridge fails here rather than
shipping as a feature that silently narrates nothing.

The two cases are the same case twice: a turn that came in on a channel, and a
turn that came in on none. The second must produce ``None``, never a leftover
value from the first — two agents share one process, and a stale origin is the
bug that answers one person's question on another person's phone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from arcrun import Event, TurnEndEvent

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.module_bus import EventContext


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("Agent: progress-agent")
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="progress-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
    )


@patch("arcagent.core.model_manager.load_eval_model")
async def test_the_turns_own_origin_reaches_the_progress_stream(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig
) -> None:
    mock_load_model.return_value = MagicMock()
    bridges: list[Any] = []

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> Any:
        bridges.append(kwargs["on_event"])

        async def _gen() -> Any:
            yield TurnEndEvent(final_text="done", tool_calls_made=0)

        return _gen()

    agent = ArcAgent(config=agent_config)
    await agent.startup()

    progress: list[dict[str, Any]] = []

    async def _collect(ctx: EventContext) -> None:
        progress.append(ctx.data)

    assert agent._bus is not None
    agent._bus.subscribe("agent:run_progress", _collect, module_name="test")

    with patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_fake_run_stream):
        session = await agent.session("channel-turn")
        async for _ in agent.run("do the big thing", session=session, reply_target="telegram:44"):
            pass
        # A scheduled/headless turn: no channel came in with it.
        headless = await agent.session("headless-turn")
        async for _ in agent.run("do it again", session=headless):
            pass

    assert len(bridges) == 2
    on_channel, off_channel = bridges

    phase = Event(
        type="dynamic.phase", timestamp=0.0, run_id="r1", data={"title": "Read the filings"}
    )
    on_channel(phase)
    off_channel(phase)
    # The bridge schedules each emission as a detached task; let them run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [entry["reply_target"] for entry in progress] == ["telegram:44", None]
    assert progress[0]["event"] == "dynamic.phase"
    assert progress[0]["data"] == {"title": "Read the filings"}

    await agent.shutdown()
