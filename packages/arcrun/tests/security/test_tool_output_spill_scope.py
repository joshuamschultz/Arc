"""Abuse cases for the tool-output spill: a handle reads this run's output and no other."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from packages.arcrun.tests.conftest import ToolCall

import arcrun
from arcrun.executor import execute_tool_call
from arcrun.loop import _build_state
from arcrun.types import Tool

REFUSED = "Error: no saved tool output for that handle in this run."
SECRET = "SECRET-FROM-RUN-A " * 8000


def _tool(text: str) -> Tool:
    async def execute(params: dict[str, Any], ctx: object) -> str:
        return text

    return Tool(
        name="big",
        description="big",
        input_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def _run(work_dir: Path, run_id: str, text: str = SECRET) -> Any:
    return _build_state(
        arcrun.StaticProvider([_tool(text)]), "sys", "go", work_dir=work_dir, run_id=run_id
    )


async def _call(built: Any, name: str, args: dict[str, Any], call_id: str = "tc") -> str:
    state, sandbox = built
    message, _ = await execute_tool_call(
        ToolCall(id=call_id, name=name, arguments=args), state, sandbox
    )
    return arcrun.content_text(message.content[0].content)


async def _spill(built: Any) -> str:
    shown = await _call(built, "big", {})
    return shown.split("saved as ")[1].split(".")[0]


def _refusals(built: Any) -> list[Any]:
    return [e for e in built[0].event_bus.events if e.type == "tool_output.refused"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,extra", [("read_tool_output", {}), ("search_tool_output", {"query": "SECRET"})]
)
async def test_handle_from_another_run_is_refused(tmp_path, tool, extra):
    run_a, run_b = _run(tmp_path, "run-a"), _run(tmp_path, "run-b")
    handle = await _spill(run_a)

    reply = await _call(run_b, tool, {"handle": handle, **extra})

    assert reply == REFUSED
    assert "SECRET" not in reply
    assert [e.data["reason"] for e in _refusals(run_b)] == ["unknown_handle"]


@pytest.mark.asyncio
async def test_handle_from_another_agent_workspace_is_refused(tmp_path):
    agent_a, agent_b = _run(tmp_path / "a", "run-1"), _run(tmp_path / "b", "run-1")
    handle = await _spill(agent_a)

    assert await _call(agent_b, "read_tool_output", {"handle": handle}) == REFUSED


@pytest.mark.asyncio
async def test_another_runs_tool_cannot_be_driven_with_this_runs_context(tmp_path):
    run_a, run_b = _run(tmp_path, "run-a"), _run(tmp_path, "run-b")
    handle = await _spill(run_a)
    tool_a = run_a[0].registry.get("read_tool_output")
    assert tool_a is not None
    run_b[0].registry._tools["read_tool_output"] = tool_a  # a borrowed tool, run B's context

    assert await _call(run_b, "read_tool_output", {"handle": handle}) == REFUSED
    assert [e.data["reason"] for e in _refusals(run_b)] == ["run_mismatch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handle",
    [
        "../run-a/anything",
        "/etc/passwd",
        "spill_" + "0" * 32,  # well-formed, never issued
        "spill_" + "0" * 31 + "/",
        "",
    ],
)
async def test_forged_or_traversing_handles_are_refused(tmp_path, handle):
    run_a = _run(tmp_path, "run-a")
    await _spill(run_a)

    assert await _call(run_a, "read_tool_output", {"handle": handle}) == REFUSED


@pytest.mark.asyncio
async def test_symlink_swapped_in_after_the_spill_is_refused(tmp_path):
    run_a = _run(tmp_path, "run-a")
    handle = await _spill(run_a)
    saved = next((tmp_path / "spill").rglob(handle))
    outside = tmp_path / "outside.txt"
    outside.write_text(saved.read_text())  # same bytes, so only the link itself gives it away
    saved.unlink()
    saved.symlink_to(outside)

    assert await _call(run_a, "read_tool_output", {"handle": handle}) == REFUSED


@pytest.mark.asyncio
async def test_spill_file_rewritten_in_the_workspace_is_refused(tmp_path):
    run_a = _run(tmp_path, "run-a")
    handle = await _spill(run_a)
    next((tmp_path / "spill").rglob(handle)).write_text("attacker-chosen replacement")

    assert await _call(run_a, "read_tool_output", {"handle": handle}) == REFUSED


@pytest.mark.asyncio
async def test_unsafe_run_id_cannot_steer_the_spill_path(tmp_path):
    built = _run(tmp_path, "../../escape")
    await _spill(built)

    assert not (tmp_path.parent / "escape").exists()
    assert list((tmp_path / "spill").iterdir())  # landed under the spill root, hashed
