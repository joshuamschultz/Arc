"""The user's message is on disk the moment the turn is accepted.

A turn can run for minutes. If the user turn were written only at turn end,
a person who leaves and returns mid-run would see history without their own
message. These tests hold the turn on an Event and read the session file.
"""

from __future__ import annotations

import asyncio
import json
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
    TelemetryConfig,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="persist-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


def _user_records(workspace: Path, key: str) -> list[dict[str, Any]]:
    path = workspace / "sessions" / f"{key}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("type") == "message" and r.get("role") == "user"]


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_user_message_in_session_file_while_turn_is_held(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig, workspace: Path
) -> None:
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(config=agent_config)
    reached_model = asyncio.Event()
    release = asyncio.Event()

    async def held_stream() -> AsyncIterator[StreamEvent]:
        reached_model.set()
        await release.wait()
        yield TokenEvent(text="done")
        yield TurnEndEvent(final_text="done")

    async def factory(*_a: Any, **_k: Any) -> AsyncIterator[StreamEvent]:
        return held_stream()

    with patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=factory):
        await agent.startup()
        try:
            session = await agent.session("unit:held")

            async def drain() -> None:
                async for _ in agent.run("hello while away", session=session):
                    pass

            task = asyncio.create_task(drain())
            await asyncio.wait_for(reached_model.wait(), timeout=5)
            held = _user_records(workspace, "unit:held")
            release.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            await agent.shutdown()

    assert [r["content"] for r in held] == ["hello while away"]
    # Exactly once: completing the turn adds no second user record.
    assert [r["content"] for r in _user_records(workspace, "unit:held")] == ["hello while away"]


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_user_message_persisted_before_context_is_built(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig, workspace: Path
) -> None:
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(config=agent_config)
    seen_at_build: list[int] = []

    import arcagent.core.agent_dispatch as dispatch

    real_build = dispatch.build_run_context

    async def spying_build(a: Any, task: str) -> Any:
        seen_at_build.append(len(_user_records(workspace, "unit:early")))
        return await real_build(a, task)

    async def factory(*_a: Any, **_k: Any) -> AsyncIterator[StreamEvent]:
        async def gen() -> AsyncIterator[StreamEvent]:
            yield TurnEndEvent(final_text="ok")

        return gen()

    with (
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=factory),
        patch.object(dispatch, "build_run_context", spying_build),
    ):
        await agent.startup()
        try:
            session = await agent.session("unit:early")
            async for _ in agent.run("first words", session=session):
                pass
        finally:
            await agent.shutdown()

    assert seen_at_build == [1]


@pytest.mark.asyncio
async def test_turn_context_attaches_to_the_user_record_and_survives_resume(
    tmp_path: Path,
) -> None:
    from arcagent.core.config import SessionConfig
    from arcagent.core.session_internal.manager import SessionManager

    def make() -> SessionManager:
        return SessionManager(SessionConfig(), ContextConfig(max_tokens=10000), None, tmp_path)

    manager = make()
    await manager.open_or_resume("unit:ctx")
    await manager.append_message({"role": "user", "content": "hi"})
    await manager.attach_turn_context("recalled: x")

    resumed = make()
    messages = await resumed.resume_session("unit:ctx")
    assert [m["content"] for m in messages] == ["hi"]
    assert messages[0]["turn_context"] == "recalled: x"
    assert len(_user_records(tmp_path, "unit:ctx")) == 1
