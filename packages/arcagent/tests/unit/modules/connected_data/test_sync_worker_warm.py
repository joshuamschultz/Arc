"""The sync worker loads an agent's embedding model before that agent's first write.

The worker is a fresh process: its first embed would otherwise load the model
(torch import plus weights, seconds) inside a sync run's first page and count
against the run's stall budget. The main process asks the worker to warm each
agent's embedder when the agent starts, and again after every worker restart,
before the restarted worker reports up.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from arcagent.modules.connected_data.sync_worker import worker as worker_module
from arcagent.modules.connected_data.sync_worker.host import HostService
from arcagent.modules.connected_data.sync_worker.supervisor import SyncWorkerSupervisor
from arcagent.modules.connected_data.sync_worker.worker import StoreWriters, WorkerHandler

_DID = "did:arc:test:agent"


class _Host:
    async def call(self, op: str, args: dict[str, Any]) -> Any:
        raise AssertionError(f"a warm never asks the host anything ({op})")


class _RecordingEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.0] for _ in texts]


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingEmbedder]:
    made: list[_RecordingEmbedder] = []

    def build(agent_did: str, settings: tuple[str, str, str]) -> _RecordingEmbedder:
        del agent_did, settings
        made.append(_RecordingEmbedder())
        return made[-1]

    monkeypatch.setattr(worker_module, "embedder_from", build)
    return made


async def test_a_warm_loads_the_agents_local_model_once(
    built: list[_RecordingEmbedder],
) -> None:
    handler = WorkerHandler(StoreWriters(_Host()))  # type: ignore[arg-type]  # reason: host stand-in
    args = {"agent_did": _DID, "embed": ["local", "all-MiniLM-L6-v2", ""]}

    first, _ = await handler("warm", args, b"")
    again, _ = await handler("warm", args, b"")

    assert first == {"warmed": True} and again == {"warmed": True}
    assert len(built) == 1, "the embedder a later write uses is the one already warmed"
    assert len(built[0].calls) == 1, "a warm model is not loaded twice"


async def test_a_remote_embedder_is_never_called_to_warm(
    built: list[_RecordingEmbedder],
) -> None:
    """Warming must not spend a provider call (or egress) at every agent start."""
    handler = WorkerHandler(StoreWriters(_Host()))  # type: ignore[arg-type]  # reason: host stand-in
    result, _ = await handler(
        "warm", {"agent_did": _DID, "embed": ["provider", "m", "https://embed.example"]}, b""
    )
    assert result == {"warmed": False}
    assert all(not embedder.calls for embedder in built)


async def test_a_malformed_warm_is_refused(built: list[_RecordingEmbedder]) -> None:
    handler = WorkerHandler(StoreWriters(_Host()))  # type: ignore[arg-type]  # reason: host stand-in
    with pytest.raises(Exception, match="malformed_warm"):
        await handler("warm", {"agent_did": _DID, "embed": ["local"]}, b"")
    assert not built


async def test_a_restarted_worker_is_warmed_again_before_it_reports_up() -> None:
    supervisor = SyncWorkerSupervisor(
        HostService(arcstore_opener=None, audit_sink=None),  # type: ignore[arg-type]  # reason: test knobs
        backoff_initial=0.05,
    )
    await supervisor.start()
    try:
        assert await supervisor.wait_ready(30)
        # An uncached model name: the warm finds nothing to load and says so fast.
        await supervisor.warm(_DID, ("local", "no-such-model-in-the-cache", ""), timeout=30)
        first = supervisor.status().pid
        assert (await supervisor.client().call("ping", {}, timeout=5))[0]["embedders"] == 1

        await supervisor.kill_worker()
        deadline = time.monotonic() + 30
        while not (supervisor.status().state == "up" and supervisor.status().pid != first):
            assert time.monotonic() < deadline, "the worker never came back"
            await asyncio.sleep(0.05)

        pong, _ = await supervisor.client().call("ping", {}, timeout=5)
        assert pong["embedders"] == 1, "the restarted worker reported up before it was warmed"
    finally:
        await supervisor.stop()
