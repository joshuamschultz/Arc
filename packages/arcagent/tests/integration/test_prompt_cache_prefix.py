"""End-to-end: two turns of a real dispatch must present a cacheable prefix.

The unit tests pin tiering inside ``ContextManager``. This one drives the actual
``agent.run`` path — session append, ``build_run_context``, the arcrun call — and
asserts what the provider actually sees on turn two:

* the system segments are byte-identical to turn one, and
* turn one's messages are an exact prefix of turn two's.

Together those two facts are the whole cache story. If either breaks, every
turn re-bills the entire conversation, which is exactly the regression this
guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from arcrun import TurnEndEvent

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.module_bus import EventContext
from arcagent.core.session_internal.context import TURN_CONTEXT_KEY


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("Agent: cache-agent")
    (ws / "context.md").write_text("Open loops: none.")
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="cache-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=100000),
    )


@patch("arcagent.core.model_manager.load_eval_model")
async def test_second_turn_reuses_the_first_turns_prefix(
    mock_load_model: MagicMock, agent_config: ArcAgentConfig
) -> None:
    mock_load_model.return_value = MagicMock()
    calls: list[dict[str, Any]] = []

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)

        async def _gen() -> Any:
            yield TurnEndEvent(final_text="done", tool_calls_made=0)

        return _gen()

    agent = ArcAgent(config=agent_config)
    await agent.startup()

    # A module that injects fresh per-turn material, as memory recall does.
    async def inject_recall(ctx: EventContext) -> None:
        ctx.data["sections"]["recall"] = f"recalled for: {ctx.data['query']}"

    assert agent._bus is not None
    agent._bus.subscribe("agent:assemble_prompt", inject_recall, priority=50)

    # The channel arcmemory captures from. It must carry the conversation only:
    # distilling the agent's own retrieved text back into memory would feed the
    # store its own output.
    captured_for_memory: list[Any] = []

    async def capture(ctx: EventContext) -> None:
        captured_for_memory.extend(ctx.data["messages"])

    agent._bus.subscribe("agent:post_respond", capture)

    with patch("arcagent.core.agent_dispatch.arcrun_run_stream", side_effect=_fake_run_stream):
        session = await agent.session("cache-test")
        async for _ in agent.run("first question", session=session):
            pass
        async for _ in agent.run("second, unrelated question", session=session):
            pass

    assert len(calls) == 2
    first, second = calls

    # 1. The system prompt did not move a byte between turns.
    assert first["system_prompt"] == second["system_prompt"]
    assert len(first["system_prompt"]) <= 2

    # 2. Per-turn material still reached the model — it rode with the user turn.
    turn_one_user = first["messages"][-1].content
    assert "recalled for: first question" in turn_one_user
    assert "recalled for" not in "".join(first["system_prompt"])

    # 3. Turn one's messages are an exact prefix of turn two's (append-only).
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert len(second["messages"]) > len(first["messages"])

    # 4. The session itself is the conversation. What a chat view, a session
    #    tool, or memory capture reads is what the person actually typed — the
    #    retrieved material is a sibling field, never inside the text.
    stored = [m for m in session.get_messages() if m["role"] == "user"]
    assert [m["content"] for m in stored] == ["first question", "second, unrelated question"]
    assert all("<agent-context>" not in m["content"] for m in stored)
    assert "recalled for: first question" in stored[0]["turn_context"]

    # 5. Memory capture sees the conversation, never the retrieved material.
    assert captured_for_memory
    assert all("recalled for" not in m["content"] for m in captured_for_memory)
    assert all(TURN_CONTEXT_KEY not in m for m in captured_for_memory)
