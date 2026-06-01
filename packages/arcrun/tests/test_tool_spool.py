"""arcrun → arcstore.spool tool-event recording (SPEC-028 FR-1/FR-2).

The executor computes args/result digests + sizes at source (where the data
lives — C1), and the EventBus maps ``tool.*`` events to ``tool_event`` spool
records. Bodies (args, result/code) ride ``extra`` only under
``store_raw_bodies=true`` (NFR-2). Recording is gated by ``spool_actor_did`` and
is fail-open. Sampling thins ``tool_event`` only — never lifecycle or errors.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest
from conftest import ToolCall

import arcrun.events as events_mod
from arcrun.events import EventBus
from arcrun.executor import execute_tool_call
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import Tool

_ACTOR = "did:arc:acme:analyst/aabbccdd"


async def _echo(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


async def _explode(params: dict, ctx: object) -> str:
    raise RuntimeError("boom")


def _bus(**kwargs: object) -> EventBus:
    return EventBus(run_id="run-x", spool_actor_did=_ACTOR, **kwargs)  # type: ignore[arg-type]


def _state(bus: EventBus, tool: Tool) -> RunState:
    from arcrun._messages import user_message

    return RunState(
        messages=[user_message("go")],
        registry=ToolRegistry(tools=[tool], event_bus=bus),
        event_bus=bus,
        run_id="run-x",
    )


def _tool(name: str, fn: object) -> Tool:
    return Tool(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=fn,  # type: ignore[arg-type]
    )


async def _run_tool(bus: EventBus, tool: Tool, args: dict) -> list:
    state = _state(bus, tool)
    sandbox = Sandbox(config=None, event_bus=bus)
    tc = ToolCall(id="tc1", name=tool.name, arguments=args)
    recorded: list = []
    with patch.object(events_mod, "_spool_record", recorded.append):
        await execute_tool_call(tc, state, sandbox)
    return recorded


@pytest.mark.asyncio
async def test_tool_events_spooled() -> None:
    """Task 2.1 — a tool call spools tool_event start + end (name/outcome/latency)."""
    recorded = await _run_tool(_bus(), _tool("echo", _echo), {"input": "hi"})
    tool_events = [r for r in recorded if r.kind == "tool_event"]
    phases = [r.phase for r in tool_events]
    assert phases == ["start", "end"]
    assert all(r.tool_name == "echo" for r in tool_events)
    assert all(r.actor_did == _ACTOR for r in tool_events)
    assert all(r.request_id == "run-x" for r in tool_events)  # joins on run_id (§11.4)
    end = tool_events[1]
    assert end.outcome == "ok"
    assert end.latency_ms is not None and end.latency_ms >= 0


@pytest.mark.asyncio
async def test_tool_event_metadata_only_default() -> None:
    """Task 2.2 — store_raw_bodies=false → digests/sizes only, no args/result body."""
    recorded = await _run_tool(_bus(), _tool("echo", _echo), {"input": "secret"})
    tool_events = [r for r in recorded if r.kind == "tool_event"]
    start, end = tool_events
    # Digests + sizes present...
    assert start.args_digest is not None and start.args_size is not None
    assert end.result_digest is not None and end.result_size is not None
    # ...but NO bodies leak into extra (default secure posture).
    assert start.extra == {}
    assert end.extra == {}


@pytest.mark.asyncio
async def test_tool_event_bodies_under_flag() -> None:
    """store_raw_bodies=true → args + result bodies ride extra (explicit opt-in)."""
    recorded = await _run_tool(
        _bus(store_raw_bodies=True), _tool("echo", _echo), {"input": "hi"}
    )
    tool_events = [r for r in recorded if r.kind == "tool_event"]
    start, end = tool_events
    assert start.extra["args"] == {"input": "hi"}
    assert end.extra["result"] == "echo: hi"


@pytest.mark.asyncio
async def test_tool_error_spooled() -> None:
    """Task 2.3 — a raising tool spools phase=error, outcome=error."""
    recorded = await _run_tool(_bus(), _tool("bomb", _explode), {"input": "x"})
    tool_events = [r for r in recorded if r.kind == "tool_event"]
    assert any(r.phase == "error" and r.outcome == "error" for r in tool_events)
    err = next(r for r in tool_events if r.phase == "error")
    assert err.tool_name == "bomb"
    # Security (NFR-2): the error record still carries the args digest but never a
    # body — the error string and arguments never reach the spool by default.
    assert err.args_digest is not None and err.args_size is not None
    assert err.extra == {}


@pytest.mark.asyncio
async def test_result_digest_is_content_not_length() -> None:
    """Task 2.4a (C1) — result_digest is sha256 of the result CONTENT, not its length."""
    recorded = await _run_tool(_bus(), _tool("echo", _echo), {"input": "hi"})
    end = next(r for r in recorded if r.kind == "tool_event" and r.phase == "end")
    expected = hashlib.sha256(b"echo: hi").hexdigest()
    assert end.result_digest == expected
    assert end.result_size == len(b"echo: hi")


@pytest.mark.asyncio
async def test_code_exec_event() -> None:
    """Task 2.5 — code-exec produces a tool_event identifiable by name, code digest always."""

    async def _fake_exec(params: dict, ctx: object) -> str:
        return json.dumps({"stdout": "42\n", "exit_code": 0})

    code = "print(6 * 7)"
    bus = _bus()
    recorded = await _run_tool(bus, _tool("execute_python", _fake_exec), {"code": code})
    tool_events = [r for r in recorded if r.kind == "tool_event"]
    start = tool_events[0]
    assert start.tool_name == "execute_python"  # UI recognizes code-exec by name
    # code digest/size always present (metadata-only default)...
    assert start.args_digest is not None and start.args_size is not None
    assert start.extra == {}  # ...code body absent without the flag

    recorded_raw = await _run_tool(
        _bus(store_raw_bodies=True), _tool("execute_python", _fake_exec), {"code": code}
    )
    start_raw = next(r for r in recorded_raw if r.kind == "tool_event" and r.phase == "start")
    assert start_raw.extra["args"] == {"code": code}  # code body under the flag


@pytest.mark.asyncio
async def test_tool_events_sampled_lifecycle_and_errors_kept() -> None:
    """Task 2.6 — sample_rate<1 thins tool_event, but never run_event/errors."""
    bus = _bus(sample_rate=0.0)  # drop all routine tool events
    recorded: list = []
    with patch.object(events_mod, "_spool_record", recorded.append):
        bus.emit("tool.start", {"name": "t", "args_digest": "d", "args_size": 1})
        bus.emit("tool.error", {"name": "t", "error": "boom", "args_digest": "d", "args_size": 1})
        bus.emit("turn.start", {"turn": 1})
        bus.emit("loop.completed", {})
    kinds = [(r.kind, r.phase if r.kind == "tool_event" else r.name) for r in recorded]
    # routine tool.start sampled out; error + lifecycle always kept.
    assert ("tool_event", "start") not in kinds
    assert ("tool_event", "error") in kinds
    assert ("run_event", "turn.start") in kinds
    assert ("run_event", "loop.completed") in kinds
