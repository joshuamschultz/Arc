"""Deterministic crash/replay checks for the optional tool ledger seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import arcllm
import pytest

from arcrun.events import EventBus
from arcrun.executor import execute_tool_call
from arcrun.ledger import ToolExecutionIntent, ToolExecutionOutcome, tool_invocation_key
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


def _state(ledger: _CrashableLedger, tool: Tool) -> RunState:
    bus = EventBus("run-1")
    return RunState(
        messages=[], registry=ToolRegistry([tool], bus), event_bus=bus, run_id="run-1", tool_ledger=ledger
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
        arcllm.ToolCall(id="call-1", name="write", arguments={}), _state(ledger, tool), Sandbox(None, EventBus("s"))
    )
    replay = await execute_tool_call(
        arcllm.ToolCall(id="call-1", name="write", arguments={}), _state(ledger, tool), Sandbox(None, EventBus("s2"))
    )

    assert first[1] is replay[1] is True
    assert side_effects == 1


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
        arcllm.ToolCall(id="call-1", name="write", arguments={}), state, Sandbox(None, EventBus("s"))
    )
    assert ok is False
    assert side_effects == 0
