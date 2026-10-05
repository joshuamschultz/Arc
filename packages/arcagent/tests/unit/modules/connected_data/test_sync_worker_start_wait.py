"""A write issued while the sync worker is starting waits for it, instead of failing.

DGX evidence (2026-10-05): the first RPC after a deploy, ``claim_profile``, failed with
``RpcUnavailableError: cannot connect: TimeoutError`` and became
``SyncWorkerUnavailableError`` although the worker came up moments later. These spawn
the real worker. A write the worker has no authority for is answered with a typed
refusal, which proves the write reached the live worker; only a connect that
failed is injected, because starving a loop on demand is not repeatable.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.connected_data.sync_worker import (
    StoreSpec,
    SyncWorkerRefusedError,
    SyncWorkerUnavailableError,
)
from arcagent.modules.connected_data.sync_worker.host import HostService
from arcagent.modules.connected_data.sync_worker.rpc import RpcClient, RpcConnectError
from arcagent.modules.connected_data.sync_worker.supervisor import SyncWorkerSupervisor


def _supervisor(**kwargs: object) -> SyncWorkerSupervisor:
    return SyncWorkerSupervisor(HostService(arcstore_opener=None, audit_sink=None), **kwargs)  # type: ignore[arg-type]  # reason: test knobs


def _spec(tmp_path: Path) -> StoreSpec:
    return StoreSpec(kind="own", agent_did="did:x", root=str(tmp_path), authority="owner")


async def test_a_write_issued_while_the_worker_is_starting_reaches_it_once_it_is_up(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor()
    await supervisor.start()
    try:
        assert supervisor.status().state == "restarting", "the worker has not reported up yet"
        write = supervisor.channel().write(_spec(tmp_path), "no_such_method", {})
        with pytest.raises(SyncWorkerRefusedError, match="unknown_method"):
            await asyncio.wait_for(write, 60)
        assert supervisor.status().state == "up"
    finally:
        await supervisor.stop()


async def test_a_connect_that_failed_before_anything_was_sent_waits_for_the_worker_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor()
    await supervisor.start()
    try:
        assert await supervisor.wait_ready(30)
        real_call = RpcClient.call
        writes: list[str] = []

        async def call(self: RpcClient, op: str, *args: Any, **kwargs: Any) -> Any:
            if op == "write":
                writes.append(op)
                if len(writes) == 1:
                    raise RpcConnectError("cannot connect: TimeoutError")
            return await real_call(self, op, *args, **kwargs)

        monkeypatch.setattr(RpcClient, "call", call)

        with pytest.raises(SyncWorkerRefusedError, match="unknown_method"):
            await supervisor.channel().write(_spec(tmp_path), "no_such_method", {})

        assert len(writes) == 2, "the write was sent again once the worker answered"
    finally:
        await supervisor.stop()


async def test_a_write_that_outlasts_the_start_wait_stays_a_typed_retryable_defer(
    tmp_path: Path,
) -> None:
    never_ready = ["python3", "-c", "import time; time.sleep(30)"]
    supervisor = _supervisor(command=never_ready, ready_timeout=0.3)
    await supervisor.start()
    try:
        began = time.monotonic()
        with pytest.raises(SyncWorkerUnavailableError) as caught:
            await supervisor.channel().write(_spec(tmp_path), "no_such_method", {})
        assert time.monotonic() - began < 5.0, "the wait is bounded"
        assert caught.value.code == "sync_worker_unavailable"
        assert caught.value.retry_after is not None
    finally:
        await supervisor.stop()
