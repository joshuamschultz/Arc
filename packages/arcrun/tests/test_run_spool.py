"""arcrun → arcstore.spool auto-instrumentation (SPEC-026 FR-4).

The loop records ``run_event`` spool lines at lifecycle points (strategy
selected / turn start / turn end / loop completed). Recording is gated by an
``actor_did`` and imports only ``arcstore.spool`` (module boundary, AC-4.3).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import LLMResponse, MockModel

import arcrun.events as events_mod
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import Tool


async def _echo(params: dict, ctx: object) -> str:
    return "ok"


def test_eventbus_records_lifecycle_run_events() -> None:
    recorded: list = []
    bus = EventBus(run_id="r1", spool_actor_did="did:arc:acme:analyst/aabbccdd")
    with patch.object(events_mod, "_spool_record", recorded.append):
        bus.emit("turn.start", {"turn": 1})
        bus.emit("tool.start", {"tool": "echo"})  # not a lifecycle event
        bus.emit("turn.end", {"turn": 1})
        bus.emit("loop.completed", {})
    kinds = {r.kind for r in recorded}
    names = [r.name for r in recorded]
    assert kinds == {"run_event"}
    assert names == ["turn.start", "turn.end", "loop.completed"]  # tool.start excluded
    assert all(r.actor_did == "did:arc:acme:analyst/aabbccdd" for r in recorded)
    assert all(r.request_id == "r1" for r in recorded)


def test_no_spool_without_actor_did() -> None:
    recorded: list = []
    bus = EventBus(run_id="r1")  # no actor → no operational recording
    with patch.object(events_mod, "_spool_record", recorded.append):
        bus.emit("turn.start", {"turn": 1})
        bus.emit("loop.completed", {})
    assert recorded == []


@pytest.mark.asyncio
async def test_loop_emits_run_events() -> None:
    recorded: list = []
    bus = EventBus(run_id="run-x", spool_actor_did="did:arc:acme:analyst/aabbccdd")
    tools = [
        Tool(
            name="echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
            execute=_echo,
        )
    ]
    reg = ToolRegistry(tools=tools, event_bus=bus)
    from arcrun._messages import system_message, user_message

    state = RunState(
        messages=[system_message("sys"), user_message("task")],
        registry=reg,
        event_bus=bus,
        run_id="run-x",
    )
    model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
    sandbox = Sandbox(config=None, event_bus=bus)
    with patch.object(events_mod, "_spool_record", recorded.append):
        await react_loop(model, state, sandbox, max_turns=3)
    names = [r.name for r in recorded]
    assert "turn.start" in names
    assert "turn.end" in names
    assert all(r.kind == "run_event" for r in recorded)
