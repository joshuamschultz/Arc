"""Deterministic crash/replay checks for the optional tool ledger seam."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

import arcllm
import pytest

from arcrun.events import EventBus
from arcrun.executor import execute_tool_call
from arcrun.ledger import (
    ToolExecutionIntent,
    ToolExecutionOutcome,
    ToolLedgerEntry,
    tool_invocation_key,
)
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import Tool


@dataclass
class _Record:
    status: Literal["new", "completed", "unresolved"]
    outcome: ToolExecutionOutcome | None = None


class _CrashableLedger:
    def __init__(self) -> None:
        self.records: dict[str, _Record] = {}
        self.intents: list[ToolExecutionIntent] = []

    async def begin(self, intent: ToolExecutionIntent) -> _Record:
        self.intents.append(intent)
        return self.records.setdefault(intent.invocation_key, _Record("new"))

    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        self.records[outcome.invocation_key] = _Record("completed", outcome)


class _BlockingLedger(_CrashableLedger):
    def __init__(self, *, block: Literal["begin", "complete"]) -> None:
        super().__init__()
        self.block = block
        self.started = asyncio.Event()

    async def begin(self, intent: ToolExecutionIntent) -> ToolLedgerEntry:
        if self.block == "begin":
            self.started.set()
            await asyncio.Event().wait()
        return await super().begin(intent)

    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        if self.block == "complete":
            self.started.set()
            await asyncio.Event().wait()
        await super().complete(outcome)


class _FailingLedger(_CrashableLedger):
    def __init__(self, *, fail: Literal["begin", "complete"]) -> None:
        super().__init__()
        self.fail = fail

    async def begin(self, intent: ToolExecutionIntent) -> _Record:
        if self.fail == "begin":
            raise RuntimeError("ledger unavailable")
        return await super().begin(intent)

    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        if self.fail == "complete":
            raise RuntimeError("ledger unavailable")
        await super().complete(outcome)


def _state(ledger: _CrashableLedger, tool: Tool) -> RunState:
    bus = EventBus("run-1")
    return RunState(
        messages=[],
        registry=ToolRegistry([tool], bus),
        event_bus=bus,
        run_id="run-1",
        tool_ledger=ledger,
    )


@pytest.mark.asyncio
async def test_completed_outcome_replays_without_duplicate_side_effect() -> None:
    side_effects = 0

    async def _write(_args: dict[str, Any], _ctx: Any) -> str:
        nonlocal side_effects
        side_effects += 1
        return "written"

    tool = Tool(name="write", description="write", input_schema={"type": "object"}, execute=_write)
    ledger = _CrashableLedger()
    first = await execute_tool_call(
        arcllm.ToolCall(id="call-1", name="write", arguments={}),
        _state(ledger, tool),
        Sandbox(None, EventBus("s")),
    )
    replay_state = _state(ledger, tool)
    replay = await execute_tool_call(
        arcllm.ToolCall(id="call-1", name="write", arguments={}),
        replay_state,
        Sandbox(None, EventBus("s2")),
    )

    assert first[1] is replay[1] is True
    assert side_effects == 1
    assert replay_state.tool_calls_made == 1
    assert [event.type for event in replay_state.event_bus.events][-1] == "tool.end"


@pytest.mark.asyncio
async def test_crash_after_intent_fails_closed_on_resume() -> None:
    side_effects = 0

    async def _write(_args: dict[str, Any], _ctx: Any) -> str:
        nonlocal side_effects
        side_effects += 1
        return "written"

    tool = Tool(name="write", description="write", input_schema={"type": "object"}, execute=_write)
    ledger = _CrashableLedger()
    state = _state(ledger, tool)
    key = tool_invocation_key("run-1", "call-1", "write", {})
    ledger.records[key] = _Record("unresolved")

    _result, ok = await execute_tool_call(
        arcllm.ToolCall(id="call-1", name="write", arguments={}),
        state,
        Sandbox(None, EventBus("s")),
    )
    assert ok is False
    assert side_effects == 0


def test_non_json_arguments_are_refused_without_identity_collision() -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        tool_invocation_key("run", "call", "tool", {"value": object()})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "complete"])
async def test_ledger_cancellation_emits_terminal_error_and_cleans_child_work(
    phase: Literal["begin", "complete"],
) -> None:
    side_effects = 0

    async def _write(_args: dict[str, Any], _ctx: Any) -> str:
        nonlocal side_effects
        side_effects += 1
        return "written"

    tool = Tool(name="write", description="write", input_schema={"type": "object"}, execute=_write)
    ledger = _BlockingLedger(block=phase)
    state = _state(ledger, tool)
    call = asyncio.create_task(
        execute_tool_call(
            arcllm.ToolCall(id="call-1", name="write", arguments={}),
            state,
            Sandbox(None, EventBus("s")),
        )
    )
    await asyncio.wait_for(ledger.started.wait(), timeout=1)
    state.cancel_event.set()
    await state.cancel_active_work()
    _result, success = await asyncio.wait_for(call, timeout=1)

    assert success is False
    assert not state.active_work
    assert [event.type for event in state.event_bus.events][-1] == "tool.error"
    assert side_effects == (1 if phase == "complete" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "complete"])
async def test_ledger_failure_emits_sanitized_terminal_error(
    phase: Literal["begin", "complete"],
) -> None:
    side_effects = 0

    async def _write(_args: dict[str, Any], _ctx: Any) -> str:
        nonlocal side_effects
        side_effects += 1
        return "written"

    tool = Tool(name="write", description="write", input_schema={"type": "object"}, execute=_write)
    state = _state(_FailingLedger(fail=phase), tool)
    _result, success = await execute_tool_call(
        arcllm.ToolCall(id="call-1", name="write", arguments={}),
        state,
        Sandbox(None, EventBus("s")),
    )

    assert success is False
    assert [event.type for event in state.event_bus.events] == ["tool.start", "tool.error"]
    assert state.event_bus.events[-1].data == {"name": "write", "error": "RuntimeError"}
    assert side_effects == (1 if phase == "complete" else 0)
