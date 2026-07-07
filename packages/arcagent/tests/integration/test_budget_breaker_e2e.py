"""SPEC-038 F1 — budget circuit-breaker reachable through the REAL agent path.

These tests drive ``agent.run`` (the single dispatch/stream entry) with the
REAL arcrun loop — no ``arcrun_run_stream`` patch, no hand-built RunState. They
prove the LLM10 breaker actually halts a runaway agent when a token ceiling is
configured, closing the "budget is dead end-to-end" gap the security review
found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcrun import TurnEndEvent, collect

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    BudgetConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.core.tool_policy import PolicyDenied
from arcagent.core.tool_registry import RegisteredTool, ToolTransport

pytestmark = pytest.mark.asyncio


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _Resp:
    content: str = ""
    stop_reason: str = "tool_use"
    tool_calls: list[_ToolCall] = field(default_factory=list)
    cost_usd: float = 0.0
    usage: _Usage | None = None


class _RunawayModel:
    """A model that would loop forever — every turn calls a tool, burning tokens.

    Each ``invoke`` reports 100 total tokens and requests a tool call, so without
    a ceiling the loop only stops at ``max_turns``. With a token ceiling the
    breaker must halt it far sooner.
    """

    def __init__(self, *, tokens_per_turn: int) -> None:
        self._tokens = tokens_per_turn
        self.calls = 0

    async def invoke(self, _messages: Any, tools: Any = None, **_: Any) -> Any:
        self.calls += 1
        return _Resp(
            content="working",
            stop_reason="tool_use",
            tool_calls=[_ToolCall(id=f"tc{self.calls}", name="nonexistent_tool", arguments={})],
            usage=_Usage(input_tokens=60, output_tokens=40, total_tokens=self._tokens),
        )

    async def close(self) -> None:
        return None


def _config(workspace: Path, tmp_path: Path, *, budget: BudgetConfig) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="breaker-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        budget=budget,
    )


async def test_token_ceiling_halts_real_run_through_dispatch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(workspace, tmp_path, budget=BudgetConfig(max_tokens=80))
    agent = ArcAgent(config=config)
    await agent.startup()
    # Inject the runaway model directly (bypass the real provider load); the
    # real arcrun loop + real dispatch path are exercised unchanged.
    agent._model = _RunawayModel(tokens_per_turn=100)

    session = await agent.session("breaker")
    result = await collect(agent.run("go forever", session=session))

    # The breaker halted after the first turn crossed the 80-token ceiling;
    # without wiring the loop would have run to max_turns (25).
    assert result.turns == 1
    assert agent._model.calls == 1
    await agent.shutdown()


class _RunState:
    def __init__(self, *, tokens: int, cost: float) -> None:
        self.tokens_used = {"input": 0, "output": 0, "total": tokens}
        self.cost_usd = cost
        self.tool_calls_made = 0


async def test_wired_provider_layer_denies_over_budget_dispatch(tmp_path: Path) -> None:
    """The config-resolved ProviderLayer (built in agent startup) denies an
    over-budget dispatch — proving the SPEC-034 seam is lit by SPEC-038 wiring,
    not by a hand-assembled pipeline."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = ArcAgentConfig(
        agent=AgentConfig(
            name="prov-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        security=SecurityConfig(tier="enterprise", custody="in_process"),
        budget=BudgetConfig(max_tokens=100),
    )
    agent = ArcAgent(config=config)
    await agent.startup()

    async def _exec(**_kwargs: Any) -> str:
        return "ok"

    agent._tool_registry.register(
        RegisteredTool(
            name="do_thing",
            description="d",
            input_schema={},
            transport=ToolTransport.NATIVE,
            execute=_exec,
            source="test",
            classification="read_only",
        )
    )
    wrapped = agent._tool_registry._create_wrapped_execute(agent._tool_registry.tools["do_thing"])
    with pytest.raises(PolicyDenied) as exc:
        await wrapped({}, parent_state=_RunState(tokens=500, cost=0.0))
    assert exc.value.decision.rule_id == "provider.budget_exceeded"
    await agent.shutdown()


async def test_no_ceiling_lets_run_proceed(tmp_path: Path) -> None:
    """Sanity: with no configured budget the breaker never fires (unbounded)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(workspace, tmp_path, budget=BudgetConfig())
    agent = ArcAgent(config=config)
    await agent.startup()
    agent._model = _RunawayModel(tokens_per_turn=100)

    session = await agent.session("nobudget")
    events = [ev async for ev in agent.run("go", session=session)]

    # Ran to max_turns (25) — many more model calls than the ceiling test.
    assert isinstance(events[-1], TurnEndEvent)
    assert agent._model.calls > 1
    await agent.shutdown()
