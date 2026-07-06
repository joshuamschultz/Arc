"""Live container execution — real Docker daemon only.

Double-guarded like the Firecracker live test:
- @pytest.mark.slow so it is excluded from the fast unit run.
- @pytest.mark.skipif on the absence of the ``docker`` CLI, so it never runs on a
  host without a container runtime.

Proves the enterprise/personal-default container path actually RUNS agent code and
returns its stdout — the surface the #1 host-path staging bug had left broken.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from arcrun.builtins.execute import make_execute_tool
from arcrun.events import EventBus
from arcrun.types import ToolContext

_HAS_DOCKER = shutil.which("docker") is not None

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _HAS_DOCKER, reason="no docker CLI — container cannot run here"),
]


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="docker-live",
        tool_call_id="tc",
        turn_number=1,
        event_bus=EventBus(run_id="docker-live"),
        cancelled=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_container_execute_python_runs_code() -> None:
    # Explicit container floor (personal + relax=container) → DockerBackend.
    tool = make_execute_tool(tier="personal", relax="container", timeout_seconds=60)
    raw = await tool.execute({"code": "print('arc_docker_live', 6 * 7)"}, _ctx())
    result = json.loads(raw)
    assert "arc_docker_live 42" in result["stdout"]
    assert result["exit_code"] == 0
