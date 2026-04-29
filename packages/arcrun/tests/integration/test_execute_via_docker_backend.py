"""Integration test: execute_python tool routes through DockerBackend.

Requires a running Docker daemon.  Skipped automatically if docker is not available.

Run with: pytest -m docker packages/arcrun/tests/integration/test_execute_via_docker_backend.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from arcrun.builtins.execute import make_execute_tool
from arcrun.events import EventBus
from arcrun.types import ToolContext


def _docker_daemon_running() -> bool:
    """Return True only if both the docker CLI is on PATH and the daemon is reachable."""
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 — docker_path resolved via shutil.which (validated)
            [docker_path, "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip entire module if docker CLI is not on PATH or daemon is not running.
docker_available = _docker_daemon_running()

pytestmark = pytest.mark.docker


def _make_ctx() -> ToolContext:
    return ToolContext(
        run_id="docker-test-run",
        tool_call_id="tc-docker",
        turn_number=1,
        event_bus=EventBus(run_id="docker-test-run"),
        cancelled=asyncio.Event(),
    )


@pytest.mark.skipif(not docker_available, reason="docker daemon not available")
@pytest.mark.asyncio
async def test_execute_python_docker_backend_returns_stdout() -> None:
    """Federal tier + docker_image → DockerBackend executes in container."""
    tool = make_execute_tool(
        tier="federal",
        docker_image="python:3.11-slim",
    )
    result = await tool.execute({"code": "print('docker_arc_test')"}, _make_ctx())
    data = json.loads(result)
    assert "docker_arc_test" in data["stdout"]


@pytest.mark.skipif(not docker_available, reason="docker daemon not available")
@pytest.mark.asyncio
async def test_execute_python_docker_backend_tool_name_unchanged() -> None:
    tool = make_execute_tool(tier="federal", docker_image="python:3.11-slim")
    assert tool.name == "execute_python"
