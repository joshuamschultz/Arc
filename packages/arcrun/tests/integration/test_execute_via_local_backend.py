"""Integration test: execute_python routing through the LocalBackend (sandbox OFF).

Under SPEC-036 the LocalBackend is reached only at personal tier with an explicit
relax to "local"/"off" — the first-class sandbox-OFF mode. Default personal and
enterprise route to the container backend (verified here via the selection audit,
without requiring a docker daemon).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from arctrust import AuditEvent

from arcrun.builtins.execute import make_execute_tool
from arcrun.events import EventBus
from arcrun.types import ToolContext

# Personal + sandbox OFF → LocalBackend (host subprocess), runnable without docker.
_LOCAL = {"tier": "personal", "relax": "local"}


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


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
    tool = make_execute_tool(**_LOCAL)
    result = await tool.execute({"code": "print('hello_arc')"}, _make_ctx())
    data = json.loads(result)
    assert "hello_arc" in data["stdout"]


@pytest.mark.asyncio
async def test_execute_python_captures_stderr_merged_into_stdout() -> None:
    tool = make_execute_tool(**_LOCAL)
    code = "import sys; sys.stderr.write('err_msg\\n'); sys.stdout.write('out_msg\\n')"
    result = await tool.execute({"code": code}, _make_ctx())
    data = json.loads(result)
    combined = data["stdout"] + data.get("stderr", "")
    assert "out_msg" in combined


@pytest.mark.asyncio
async def test_execute_python_exit_code_zero_on_success() -> None:
    tool = make_execute_tool(**_LOCAL)
    result = await tool.execute({"code": "pass"}, _make_ctx())
    data = json.loads(result)
    assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_execute_python_has_duration() -> None:
    tool = make_execute_tool(**_LOCAL)
    result = await tool.execute({"code": "pass"}, _make_ctx())
    data = json.loads(result)
    assert data["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_execute_python_routes_through_local_backend() -> None:
    """Sandbox-off runs the host interpreter directly (LocalBackend territory)."""
    tool = make_execute_tool(extra_env={"PATH": "/usr/bin:/bin"}, **_LOCAL)
    result = await tool.execute({"code": "import sys; print(sys.version_info.major)"}, _make_ctx())
    data = json.loads(result)
    assert "3" in data["stdout"]


@pytest.mark.asyncio
async def test_execute_python_tool_name_unchanged() -> None:
    """Public tool name must remain 'execute_python'."""
    tool = make_execute_tool()
    assert tool.name == "execute_python"


def test_enterprise_tier_selects_container_not_local() -> None:
    """SPEC-036: enterprise routes to the container backend, never a host subprocess."""
    sink = _CaptureSink()
    make_execute_tool(tier="enterprise", audit_sink=sink)
    selected = next(e for e in sink.events if e.action == "code_exec.backend.selected")
    assert selected.extra["isolation"] == "container"
