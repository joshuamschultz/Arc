"""The sync worker is supervised: restarted with backoff, killed when hung, never orphaned.

These spawn the real worker (``python -m arcagent.modules.connected_data.sync_worker``)
or a stand-in child that crashes on start; nothing about the process is faked.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from arcagent.modules.connected_data.sync_worker.host import HostService
from arcagent.modules.connected_data.sync_worker.supervisor import SyncWorkerSupervisor


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); ask ps whether it really runs.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607 — reason: ps from PATH in tests
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def _until(check: object, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():  # type: ignore[operator]  # reason: a zero-arg predicate
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


def _supervisor(**kwargs: object) -> SyncWorkerSupervisor:
    return SyncWorkerSupervisor(HostService(arcstore_opener=None, audit_sink=None), **kwargs)  # type: ignore[arg-type]  # reason: test knobs


async def test_the_worker_starts_in_its_own_process_and_reports_up() -> None:
    supervisor = _supervisor()
    assert supervisor.status().state == "down"
    await supervisor.start()
    try:
        assert await supervisor.wait_ready(30)
        status = supervisor.status()
        assert status.state == "up" and status.pid not in (None, os.getpid())
        result, _ = await supervisor.client().call("ping", {}, timeout=5)
        assert result["pid"] == status.pid
    finally:
        await supervisor.stop()
    assert supervisor.status().state == "down"
    assert status.pid is not None and not _alive(status.pid)


async def test_a_killed_worker_is_restarted() -> None:
    supervisor = _supervisor(backoff_initial=0.05)
    await supervisor.start()
    try:
        assert await supervisor.wait_ready(30)
        first = supervisor.status().pid
        await supervisor.kill_worker()
        await _until(
            lambda: supervisor.status().state == "up" and supervisor.status().pid != first
        )
        assert supervisor.status().restarts == 1
    finally:
        await supervisor.stop()


async def test_a_worker_that_keeps_crashing_is_retried_with_growing_backoff() -> None:
    crash = [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(3)"]
    supervisor = _supervisor(command=crash, backoff_initial=0.1, jitter=lambda: 1.0)
    await supervisor.start()
    try:
        await asyncio.sleep(1.6)
        status = supervisor.status()
    finally:
        await supervisor.stop()
    # Waits of 0.1, 0.2, 0.4, 0.8 s: about four attempts in 1.6 s, not dozens.
    assert 2 <= status.restarts <= 5, status
    assert status.state == "restarting" and "code 3" in status.detail


async def test_a_hung_worker_is_killed_and_replaced() -> None:
    supervisor = _supervisor(heartbeat_interval=0.2, heartbeat_misses=2, backoff_initial=0.05)
    await supervisor.start()
    try:
        assert await supervisor.wait_ready(30)
        hung = supervisor.status().pid
        assert hung is not None
        os.kill(hung, signal.SIGSTOP)  # alive, but answers nothing
        await _until(lambda: supervisor.status().state == "up" and supervisor.status().pid != hung)
        assert not _alive(hung)
    finally:
        await supervisor.stop()


async def test_a_write_while_the_worker_restarts_fails_fast_and_typed() -> None:
    from arcagent.modules.connected_data.sync_worker import (
        StoreSpec,
        SyncWorkerUnavailableError,
    )

    supervisor = _supervisor(backoff_initial=5.0, jitter=lambda: 1.0)
    await supervisor.start()
    try:
        assert await supervisor.wait_ready(30)
        await supervisor.kill_worker()
        await _until(lambda: supervisor.status().state == "restarting")
        spec = StoreSpec(kind="own", agent_did="did:x", root="/tmp/none", authority="owner")
        began = time.monotonic()
        with pytest.raises(SyncWorkerUnavailableError) as caught:
            await supervisor.channel().write(spec, "finish_sync", {})
        assert time.monotonic() - began < 1.0
        assert caught.value.code == "sync_worker_unavailable"
        assert caught.value.retry_after is not None and caught.value.retry_after > 0
    finally:
        await supervisor.stop()


_PARENT = textwrap.dedent(
    """
    import asyncio, sys
    from arcagent.modules.connected_data.sync_worker.host import HostService
    from arcagent.modules.connected_data.sync_worker.supervisor import SyncWorkerSupervisor

    async def main():
        supervisor = SyncWorkerSupervisor(HostService(arcstore_opener=None, audit_sink=None))
        await supervisor.start()
        assert await supervisor.wait_ready(60)
        print(supervisor.status().pid, flush=True)
        await asyncio.sleep(3600)

    asyncio.run(main())
    """
)


def test_the_worker_dies_with_a_parent_killed_outright(tmp_path: Path) -> None:
    script = tmp_path / "parent.py"
    script.write_text(_PARENT, encoding="utf-8")
    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout is not None
        worker = int(parent.stdout.readline().strip())
        assert _alive(worker)
        parent.kill()  # SIGKILL: no cleanup code runs in the parent
        parent.wait(timeout=10)
        deadline = time.monotonic() + 10
        while _alive(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(worker), "the sync worker outlived its parent"
    finally:
        if parent.poll() is None:
            parent.kill()
