"""Regression tests for bounded bash capture and process-tree cleanup."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from arcagent.builtins.capabilities import _runtime
from arcagent.builtins.capabilities.bash import bash


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> Path:
    _runtime.reset()
    _runtime.configure(workspace=tmp_path, tier="personal")
    return tmp_path


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_file(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"subprocess did not create {path}")


@pytest.mark.asyncio
async def test_infinite_output_is_drained_but_capture_is_bounded(workspace: Path) -> None:
    result = await bash(command="while :; do printf '0123456789'; done", timeout=0.1)

    assert result == "Error: Command timed out after 0.1s"


@pytest.mark.asyncio
async def test_timeout_terminates_descendant_processes(workspace: Path) -> None:
    pid_file = workspace / "descendant.pid"
    command = f"sleep 30 & echo $! > {pid_file.name}; wait"

    result = await bash(command=command, timeout=0.1)
    await _wait_for_file(pid_file)
    descendant_pid = int(pid_file.read_text())

    assert "timed out" in result
    assert not _process_exists(descendant_pid)


@pytest.mark.asyncio
async def test_cancellation_terminates_descendant_processes(workspace: Path) -> None:
    pid_file = workspace / "cancelled-descendant.pid"
    task = asyncio.create_task(
        bash(command=f"sleep 30 & echo $! > {pid_file.name}; wait", timeout=30)
    )
    await _wait_for_file(pid_file)
    descendant_pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _process_exists(descendant_pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, 3_601, float("inf"), float("nan")])
async def test_invalid_timeout_is_rejected_before_launch(
    workspace: Path,
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="timeout must be greater"):
        await bash(command="echo should-not-run", timeout=timeout)  # type: ignore[arg-type]
