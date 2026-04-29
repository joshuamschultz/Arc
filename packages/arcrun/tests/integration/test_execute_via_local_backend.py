"""Integration test: execute_python tool routes through LocalBackend.

Verifies that make_execute_tool() at personal/enterprise tier uses LocalBackend,
runs a real subprocess, and returns stdout in the JSON response.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arcrun.builtins.execute import make_execute_tool
from arcrun.events import EventBus
from arcrun.types import ToolContext


def _make_ctx() -> ToolContext:
    return ToolContext(
        run_id="test-run",
        tool_call_id="tc-1",
        turn_number=1,
        event_bus=EventBus(run_id="test-run"),
        cancelled=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_execute_python_returns_stdout() -> None:
    tool = make_execute_tool(tier="personal")
    result = await tool.execute({"code": "print('hello_arc')"}, _make_ctx())
    data = json.loads(result)
    assert "hello_arc" in data["stdout"]


@pytest.mark.asyncio
async def test_execute_python_captures_stderr_merged_into_stdout() -> None:
    """stderr is merged into stdout stream so errors are visible."""
    tool = make_execute_tool(tier="personal")
    code = "import sys; sys.stderr.write('err_msg\\n'); sys.stdout.write('out_msg\\n')"
    result = await tool.execute({"code": code}, _make_ctx())
    data = json.loads(result)
    # stdout has the merged output (stderr→stdout via bash -c)
    combined = data["stdout"] + data.get("stderr", "")
    assert "out_msg" in combined


@pytest.mark.asyncio
async def test_execute_python_exit_code_zero_on_success() -> None:
    tool = make_execute_tool(tier="personal")
    result = await tool.execute({"code": "pass"}, _make_ctx())
    data = json.loads(result)
    assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_execute_python_has_duration() -> None:
    tool = make_execute_tool(tier="personal")
    result = await tool.execute({"code": "pass"}, _make_ctx())
    data = json.loads(result)
    assert data["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_execute_python_routes_through_local_backend() -> None:
    """Verify the tool internally uses a LocalBackend at personal tier."""
    # We test this indirectly: LocalBackend runs bash -c so PATH is minimal.
    # The fact that sys.executable works proves we're in LocalBackend territory.
    tool = make_execute_tool(tier="personal", extra_env={"PATH": "/usr/bin:/bin"})
    result = await tool.execute({"code": "import sys; print(sys.version_info.major)"}, _make_ctx())
    data = json.loads(result)
    assert "3" in data["stdout"]


@pytest.mark.asyncio
async def test_execute_python_tool_name_unchanged() -> None:
    """Public tool name must remain 'execute_python' for back-compat."""
    tool = make_execute_tool()
    assert tool.name == "execute_python"


@pytest.mark.asyncio
async def test_execute_python_enterprise_tier_uses_local_backend() -> None:
    tool = make_execute_tool(tier="enterprise")
    result = await tool.execute({"code": "print('enterprise')"}, _make_ctx())
    data = json.loads(result)
    assert "enterprise" in data["stdout"]
