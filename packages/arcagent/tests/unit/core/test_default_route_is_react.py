"""A basic turn goes straight to a full-context react turn — no strategy gate.

The open strategy default made arcrun fire an extra ``select_strategy`` model
call on *every* un-pinned run: a stripped, single-block prompt (strategy
descriptions + tool list, no identity/context) whose forced tool call the model
answers before the real turn ever runs, and which can route an ordinary message
onto the ``code`` / ``dynamic`` strategies that let a model author its own
control flow. An ordinary inbound message must not pay that call.

The fix defaults the **un-pinned** route (``requested is None``) to ``react``
alone — with one strategy allowed arcrun skips ``select_strategy`` entirely —
while leaving the operator ceiling open so an **author-declared** workflow node
that pins ``code`` is still honoured (never narrowed to empty). Federal is
covered by ``test_allowed_strategies_tier``.

These tests drive the real ``narrowed_loop_controls`` against a real started
agent and the real ``arcrun.select_strategy``, not a mock of either, so a
regression that reopens the un-pinned default is caught where it costs a call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from arcrun._messages import system_message, user_message
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.strategies import select_strategy

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.session_internal.manager import SessionManager
from arcagent.tools.approval_policy import narrowed_loop_controls


class _FailIfInvoked:
    """A model that fails loudly if a strategy-selection call is ever made."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("a strategy-selection model call fired on the default route")


def _state() -> RunState:
    bus = EventBus(run_id="default-route")
    return RunState(
        messages=[system_message("s"), user_message("please summarise the Q3 report")],
        registry=ToolRegistry(tools=[], event_bus=bus),
        event_bus=bus,
    )


@pytest.fixture()
async def agent(tmp_path: Path) -> AsyncIterator[ArcAgent]:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("Agent: sales_agent")
    (ws / "context.md").write_text("Open loops: none.")
    cfg = ArcAgentConfig(
        agent=AgentConfig(name="sales_agent", org="testorg", type="executor", workspace=str(ws)),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=100000),
    )
    with patch("arcagent.core.model_manager.load_eval_model", return_value=MagicMock()):
        a = ArcAgent(config=cfg)
        await a.startup()
        yield a


async def _session(agent: ArcAgent) -> SessionManager:
    return await agent.session("route-test")


async def test_unpinned_turn_pins_react_only(agent: ArcAgent) -> None:
    controls = narrowed_loop_controls(agent, await _session(agent), None)
    assert controls["allowed_strategies"] == ["react"]


@pytest.mark.asyncio
async def test_unpinned_turn_fires_no_strategy_selection_call(agent: ArcAgent) -> None:
    controls = narrowed_loop_controls(agent, await _session(agent), None)
    model = _FailIfInvoked()

    chosen = await select_strategy(controls["allowed_strategies"], model, _state())

    assert chosen == "react"
    assert model.calls == 0


async def test_author_declared_node_strategy_is_not_narrowed_away(agent: ArcAgent) -> None:
    # A workflow node pins ``code`` explicitly (SPEC-061 REQ-243). The open
    # operator ceiling must honour it verbatim — the react default is only for
    # the un-pinned route, never a silent clamp on a declared strategy.
    controls = narrowed_loop_controls(agent, await _session(agent), ["code"])
    assert controls["allowed_strategies"] == ["code"]
