"""Whole-turn serialization and active-run ownership tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcrun
import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.session_coordination import SessionRunCoordinator


def _config(tmp_path: Path) -> ArcAgentConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ArcAgentConfig(
        agent=AgentConfig(
            name="coordinator", org="test", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10_000),
    )


def test_active_run_removal_is_identity_guarded() -> None:
    coordinator = SessionRunCoordinator()
    old = MagicMock(spec=arcrun.RunHandle)
    replacement = MagicMock(spec=arcrun.RunHandle)
    coordinator.register("same", old)
    coordinator.register("same", replacement)

    coordinator.unregister("same", old)

    assert coordinator.active("same") is replacement


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_overlapping_streams_commit_same_session_in_admission_order(
    mock_load_model: MagicMock, tmp_path: Path
) -> None:
    """A later fast response cannot commit ahead of an earlier slow response."""
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(_config(tmp_path))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []
    histories: list[list[tuple[str, Any]]] = []

    async def fake_run_stream(*args: Any, **kwargs: Any) -> AsyncIterator[arcrun.StreamEvent]:
        task = args[3] if len(args) > 3 else kwargs["task"]
        calls.append(task)
        histories.append([(message.role, message.content) for message in kwargs["messages"]])

        async def events() -> AsyncIterator[arcrun.StreamEvent]:
            if task == "first":
                first_started.set()
                await release_first.wait()
            yield arcrun.TurnEndEvent(final_text=f"{task}-answer")

        return events()

    async def consume(text: str, session: Any) -> None:
        async for _ in agent.run(text, session=session):
            pass

    with patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=fake_run_stream):
        await agent.startup()
        try:
            session = await agent.session("shared")
            first = asyncio.create_task(consume("first", session))
            await first_started.wait()
            second = asyncio.create_task(consume("second", session))
            await asyncio.sleep(0)
            assert calls == ["first"]
            release_first.set()
            await asyncio.gather(first, second)
            messages = [(row["role"], row["content"]) for row in session.get_messages()]
        finally:
            await agent.shutdown()

    assert calls == ["first", "second"]
    assert histories[1][-3:] == [
        ("user", "first"),
        ("assistant", "first-answer"),
        ("user", "second"),
    ]
    assert messages[-4:] == [
        ("user", "first"),
        ("assistant", "first-answer"),
        ("user", "second"),
        ("assistant", "second-answer"),
    ]


@pytest.mark.asyncio
async def test_different_sessions_can_finish_in_reverse_order() -> None:
    coordinator = SessionRunCoordinator()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def slow() -> None:
        async with coordinator.turn("first"):
            first_entered.set()
            await release_first.wait()
            order.append("first")

    async def fast() -> None:
        async with coordinator.turn("second"):
            order.append("second")

    slow_task = asyncio.create_task(slow())
    await first_entered.wait()
    await fast()
    release_first.set()
    await slow_task

    assert order == ["second", "first"]


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_tracked_turn_keeps_session_reserved_until_finalizer_commits(
    mock_load_model: MagicMock, tmp_path: Path
) -> None:
    mock_load_model.return_value = MagicMock(close=AsyncMock())
    agent = ArcAgent(_config(tmp_path))
    completions = [asyncio.Event(), asyncio.Event()]
    calls: list[list[tuple[str, Any]]] = []
    handles: list[MagicMock] = []

    async def fake_run_async(*args: Any, **kwargs: Any) -> MagicMock:
        index = len(handles)
        calls.append([(message.role, message.content) for message in kwargs["messages"]])
        handle = MagicMock(spec=arcrun.RunHandle)

        async def result() -> MagicMock:
            await completions[index].wait()
            return MagicMock(content=f"answer-{index + 1}")

        handle.result = result
        handles.append(handle)
        return handle

    with patch("arcagent.core.agent_dispatch.arcrun.run_async", side_effect=fake_run_async):
        await agent.startup()
        try:
            first = await agent.start_tracked_run("first", session_key="shared")
            assert agent.active_run("shared") is first
            second_task = asyncio.create_task(
                agent.start_tracked_run("second", session_key="shared")
            )
            await asyncio.sleep(0)
            assert len(calls) == 1
            completions[0].set()
            second = await second_task
            assert agent.active_run("shared") is second
            completions[1].set()
            await asyncio.gather(*list(agent._run_finalizers))
            session = await agent.session("shared")
            messages = [(row["role"], row["content"]) for row in session.get_messages()]
        finally:
            await agent.shutdown()

    assert calls[1][-3:] == [
        ("user", "first"),
        ("assistant", "answer-1"),
        ("user", "second"),
    ]
    assert messages[-4:] == [
        ("user", "first"),
        ("assistant", "answer-1"),
        ("user", "second"),
        ("assistant", "answer-2"),
    ]
