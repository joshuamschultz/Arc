"""Concurrency and failure contracts for the ArcAgent lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import arcagent.core.agent as agent_module
from arcagent.core.agent import ArcAgent
from arcagent.core.config import AgentConfig, ArcAgentConfig, IdentityConfig, LLMConfig


def _agent(config: Any) -> ArcAgent:
    return ArcAgent(config=config)


@pytest.fixture()
def agent_config(tmp_path: Any) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="lifecycle", org="test", workspace=str(tmp_path / "work")),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
    )


@pytest.mark.asyncio
async def test_concurrent_startup_initializes_once(agent_config: Any) -> None:
    agent = _agent(agent_config)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def initialize() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    agent._startup_impl = initialize  # type: ignore[method-assign]
    first = asyncio.create_task(agent.startup())
    await entered.wait()
    second = asyncio.create_task(agent.startup())
    release.set()
    await asyncio.gather(first, second)

    assert agent._started
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_startup_is_restartable(agent_config: Any) -> None:
    agent = _agent(agent_config)
    attempts = 0

    async def initialize() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boot failed")

    agent._startup_impl = initialize  # type: ignore[method-assign]
    agent._release_partial_startup = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boot failed"):
        await agent.startup()
    await agent.startup()

    assert attempts == 2
    assert agent._started


@pytest.mark.asyncio
async def test_shutdown_rejects_new_work_before_teardown_finishes(agent_config: Any) -> None:
    agent = _agent(agent_config)
    agent._started = True
    # Use the enum value established by startup without importing an internal type.
    agent._startup_impl = AsyncMock()  # type: ignore[method-assign]
    agent._started = False
    await agent.startup()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def emit(*_args: Any, **_kwargs: Any) -> None:
        entered.set()
        await release.wait()

    agent._bus = MagicMock(emit=emit)
    agent._tool_registry = MagicMock(shutdown=AsyncMock())
    stopping = asyncio.create_task(agent.shutdown())
    await entered.wait()

    with pytest.raises(RuntimeError, match="not started"):
        agent._ensure_started()
    release.set()
    await stopping


@pytest.mark.asyncio
async def test_shutdown_cancels_runs_and_continues_after_cleanup_error(agent_config: Any) -> None:
    agent = _agent(agent_config)
    agent._startup_impl = AsyncMock()  # type: ignore[method-assign]
    await agent.startup()
    bus = MagicMock(emit=AsyncMock())
    loader = MagicMock(shutdown=AsyncMock(side_effect=RuntimeError("broken")))
    registry = MagicMock(shutdown=AsyncMock())
    handle = MagicMock(cancel=AsyncMock())
    finalized = asyncio.Event()

    async def finish() -> None:
        finalized.set()

    finalizer = asyncio.create_task(finish())
    agent._bus = bus
    agent._capability_loader = loader
    agent._tool_registry = registry
    agent._active_runs["session"] = handle
    agent._run_finalizers.add(finalizer)

    await agent.shutdown()
    await agent.shutdown()

    handle.cancel.assert_awaited_once()
    assert finalized.is_set()
    registry.shutdown.assert_awaited_once()
    assert not agent._started


@pytest.mark.asyncio
async def test_shutdown_continues_after_cleanup_timeout(
    agent_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(agent_config)
    agent._startup_impl = AsyncMock()  # type: ignore[method-assign]
    await agent.startup()
    never = asyncio.Event()

    async def hang() -> None:
        await never.wait()

    agent._capability_loader = MagicMock(shutdown=hang)
    agent._tool_registry = MagicMock(shutdown=AsyncMock())
    monkeypatch.setattr(agent_module, "_SHUTDOWN_STEP_TIMEOUT_SECONDS", 0.01)

    await agent.shutdown()

    agent._tool_registry.shutdown.assert_awaited_once()
