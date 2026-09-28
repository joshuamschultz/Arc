"""SPEC-083 T-1230 (REQ-514, COMP-031) — an un-pinned turn lets the model choose its strategy.

Operator decision (SPEC-083) reverses the 2026-08-17 react-only default. An
ordinary (un-pinned) turn on a personal or enterprise agent now offers the model
every **auto-selectable** strategy — ``react``, ``code``, ``dynamic`` — within
the operator ceiling (``ArcRunConfig.allowed_strategies``), and arcrun's
``select_strategy`` makes one selection call. ``plan_execute`` and ``oneshot``
stay manual-only: a caller must pin them by name. Federal stays ``react`` only,
with zero selection calls. A pinned workflow request is still intersected with
the ceiling — a caller can never widen what the operator allowed.

The file keeps its historical name because PLAN T-1230 lists this path; its
subject is now "the default route selects a strategy".

These tests drive the real ``narrowed_loop_controls`` against a real started
agent and hand its result to the real ``arcrun.select_strategy``. Only the LLM
is faked, at the ``model.invoke`` boundary, and it records which strategies the
selection call offered (the ``select_strategy`` tool's ``enum``).

Enterprise and federal agents cannot start in a unit environment (operator-key
custody and FIPS crypto both fail closed, correctly). Tier only enters loop
controls through ``agent._config.security.tier``, so those cases start a
personal agent and swap in a config copy at the target tier before building the
controls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
    ArcRunConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.tools.approval_policy import narrowed_loop_controls

_AUTO_SELECTABLE = {"react", "code", "dynamic"}


class _RecordingSelector:
    """Fake LLM at ``model.invoke``: answers selection with react, records the offer."""

    def __init__(self) -> None:
        self.offers: list[list[str]] = []

    async def invoke(self, messages: list[Any], tools: list[Any] | None = None, **_: Any) -> Any:
        tool = next(t for t in tools or [] if getattr(t, "name", "") == "select_strategy")
        self.offers.append(list(tool.parameters["properties"]["strategy"]["enum"]))
        return SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(id="sel", name="select_strategy", arguments={"strategy": "react"})
            ],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0, total_tokens=0),
            cost_usd=0.0,
            stop_reason="tool_use",
        )


def _state() -> RunState:
    bus = EventBus(run_id="default-route")
    return RunState(
        messages=[system_message("s"), user_message("please summarise the Q3 report")],
        registry=ToolRegistry(tools=[], event_bus=bus),
        event_bus=bus,
    )


AgentFactory = Callable[..., Awaitable[ArcAgent]]


@pytest.fixture()
async def make_agent(tmp_path: Path) -> AsyncIterator[AgentFactory]:
    async def _make(*, tier: str = "personal", ceiling: list[str] | None = None) -> ArcAgent:
        ws = tmp_path / "workspace"
        ws.mkdir(exist_ok=True)
        (ws / "identity.md").write_text("Agent: sales_agent")
        (ws / "context.md").write_text("Open loops: none.")
        cfg = ArcAgentConfig(
            agent=AgentConfig(
                name="sales_agent", org="testorg", type="executor", workspace=str(ws)
            ),
            llm=LLMConfig(model="test/model"),
            identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
            telemetry=TelemetryConfig(enabled=True),
            context=ContextConfig(max_tokens=100000),
            arcrun=ArcRunConfig(allowed_strategies=ceiling),
        )
        with patch("arcagent.core.model_manager.load_eval_model", return_value=MagicMock()):
            agent = ArcAgent(config=cfg)
            await agent.startup()
        if tier != "personal":
            agent._config = agent._config.model_copy(
                update={"security": SecurityConfig(tier=tier)}
            )
        return agent

    yield _make


async def _offered(agent: ArcAgent, requested: list[str] | None) -> tuple[str, list[list[str]]]:
    """Run the real route: narrowed controls → real select_strategy → recorded offers."""
    session = await agent.session("route-test")
    controls = narrowed_loop_controls(agent, session, requested)
    model = _RecordingSelector()
    chosen = await select_strategy(controls["allowed_strategies"], model, _state())
    return chosen, model.offers


# ------------------------------------------------------------ un-pinned, open ceiling


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["personal", "enterprise"])
async def test_unpinned_turn_open_ceiling_offers_every_auto_selectable_strategy(
    make_agent: AgentFactory, tier: str
) -> None:
    agent = await make_agent(tier=tier)

    _, offers = await _offered(agent, None)

    assert len(offers) == 1, "an un-pinned turn makes exactly one selection call"
    assert set(offers[0]) == _AUTO_SELECTABLE


@pytest.mark.asyncio
async def test_unpinned_turn_never_offers_manual_only_strategies(
    make_agent: AgentFactory,
) -> None:
    agent = await make_agent()

    _, offers = await _offered(agent, None)

    assert offers, "selection call expected on an un-pinned personal turn"
    assert "plan_execute" not in offers[0]
    assert "oneshot" not in offers[0]


# ---------------------------------------------------------- un-pinned, with a ceiling


@pytest.mark.asyncio
async def test_unpinned_turn_offers_ceiling_intersection(make_agent: AgentFactory) -> None:
    agent = await make_agent(ceiling=["react", "code"])

    _, offers = await _offered(agent, None)

    assert len(offers) == 1
    assert set(offers[0]) == {"react", "code"}


@pytest.mark.asyncio
async def test_unpinned_turn_ceiling_listing_manual_only_still_offers_auto_selectable_only(
    make_agent: AgentFactory,
) -> None:
    # The operator ceiling bounds what a run may use; it does not turn a
    # manual-only strategy into an auto-offered one.
    agent = await make_agent(ceiling=["react", "code", "plan_execute", "oneshot"])

    _, offers = await _offered(agent, None)

    assert len(offers) == 1
    assert set(offers[0]) == {"react", "code"}


@pytest.mark.asyncio
async def test_unpinned_turn_ceiling_of_react_alone_makes_no_selection_call(
    make_agent: AgentFactory,
) -> None:
    agent = await make_agent(ceiling=["react"])

    chosen, offers = await _offered(agent, None)

    assert chosen == "react"
    assert offers == []


# ------------------------------------------------------------------------- federal


@pytest.mark.asyncio
async def test_unpinned_federal_turn_is_react_only_with_zero_selection_calls(
    make_agent: AgentFactory,
) -> None:
    agent = await make_agent(tier="federal")

    chosen, offers = await _offered(agent, None)

    assert chosen == "react"
    assert offers == []


@pytest.mark.asyncio
async def test_unpinned_federal_turn_ignores_a_wide_ceiling(make_agent: AgentFactory) -> None:
    agent = await make_agent(tier="federal", ceiling=["react", "code", "dynamic"])

    chosen, offers = await _offered(agent, None)

    assert chosen == "react"
    assert offers == []


# -------------------------------------------------------------------------- pinned


@pytest.mark.asyncio
async def test_pinned_request_is_honoured_verbatim_under_open_ceiling(
    make_agent: AgentFactory,
) -> None:
    # A workflow node pins ``code`` (SPEC-061 REQ-243): the open ceiling keeps it.
    agent = await make_agent()
    session = await agent.session("route-test")

    controls = narrowed_loop_controls(agent, session, ["code"])

    assert controls["allowed_strategies"] == ["code"]


@pytest.mark.asyncio
async def test_pinned_manual_only_strategy_is_honoured_when_named(
    make_agent: AgentFactory,
) -> None:
    agent = await make_agent()
    session = await agent.session("route-test")

    controls = narrowed_loop_controls(agent, session, ["plan_execute"])

    assert controls["allowed_strategies"] == ["plan_execute"]


@pytest.mark.asyncio
async def test_pinned_request_is_intersected_with_the_ceiling(make_agent: AgentFactory) -> None:
    agent = await make_agent(ceiling=["react", "code"])

    _, offers = await _offered(agent, ["code", "dynamic"])

    # Intersection is {"code"} — one strategy, so no selection call at all.
    assert offers == []
    session = await agent.session("route-test")
    assert narrowed_loop_controls(agent, session, ["code", "dynamic"])["allowed_strategies"] == [
        "code"
    ]
