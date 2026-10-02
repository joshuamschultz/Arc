"""Journeys J3 G1 / G2 / G10: a nightly workflow that fails must be heard, seen, and keep firing.

The production workflow failed for a month with ``last_error`` null on every run,
no alert to a human, and a cron that stopped without anyone knowing. Each test
below is one of those, driven through the real runner, the real stores and the
real scheduler engine. Only the network edge (the messenger, the agent's turn)
is replaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import TaskStore
from arcteam.workflow.narrator import RunNarrator
from arcteam.workflow.runner import build_workflow_runner, node_task_id
from arctrust import OperatorKey
from freezegun import freeze_time

from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore
from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import build_dashboard_plane

SALES = "did:arc:test:sales"
OVERFLOW = "ArcLLMAPIError: prompt is too long: 1700000 tokens > 1000000"

_NIGHTLY = """
[workflow]
id = "nightly"
version = 1
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"

[[node]]
id = "archive"
kind = "agent"
agent = "@sales"
needs = ["collect"]
max_attempts = 1
"""


class _Entity:
    did = SALES


class _Registry:
    async def get(self, handle: str) -> Any:
        return _Entity() if handle == "sales" else None


class _Gateway:
    """The fake chat gateway: records every message the runner sends out."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Any:
        self.sent.append(message)
        return message


class _World:
    def __init__(self, tmp_path: Path, backend: FakeBackend) -> None:
        self.tmp_path = tmp_path
        self.backend = backend
        self.gateway = _Gateway()
        self.tasks = TaskStore(backend)
        self.actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
        self.runner = self.new_runner()
        self.plane = build_dashboard_plane(runner=self.runner)

    def new_runner(self) -> Any:
        """A runner built from scratch over the same durable stores: a process restart."""
        return build_workflow_runner(
            tier="personal",
            task_store_backend=self.backend,
            runner_key_path=self.tmp_path / "operator" / "operator.key",
            workspace_root=self.tmp_path,
            registry=_Registry(),
            narrator=RunNarrator(self.gateway, sender_did=SALES),
        )


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[_World]:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "nightly"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_NIGHTLY, encoding="utf-8")
    backend = FakeBackend()
    await backend.start()
    yield _World(tmp_path, backend)
    await backend.stop()


async def _fail_the_archive_node(world: _World, run_id: str) -> None:
    collect = node_task_id(run_id, "collect", 0)
    await world.tasks.start_task(collect, SALES)
    await world.tasks.finish(
        collect, status="done", resolution="ok", actor_did=SALES, output={"files": ["a.txt"]}
    )
    await world.runner.advance(run_id)
    archive = node_task_id(run_id, "archive", 0)
    await world.tasks.start_task(archive, SALES)
    await world.tasks.finish(
        archive, status="failed", resolution="node failed", actor_did=SALES, last_error=OVERFLOW
    )
    await world.runner.advance(run_id)


async def test_j3_failed_cron_run_notifies_operator_with_reason(world: _World) -> None:
    """G1: no channel bound, the operator still hears which node failed and why."""
    started = await world.plane.run_workflow("nightly", {}, actor=world.actor)
    run_id = started.value["run_id"]

    await _fail_the_archive_node(world, run_id)

    notices = [m for m in world.gateway.sent if "user://operator" in m.to]
    assert len(notices) == 1
    body = notices[0].body
    assert "nightly" in body and "archive" in body and "prompt is too long" in body


async def test_j3_run_detail_shows_failed_node_error_and_outputs(world: _World) -> None:
    """G2: the run view carries the failure reason and each node's input and output."""
    started = await world.plane.run_workflow("nightly", {}, actor=world.actor)
    run_id = started.value["run_id"]

    await _fail_the_archive_node(world, run_id)
    detail = await world.plane.get_run(run_id, actor=world.actor)

    assert detail["status"] == "failed"
    assert "prompt is too long" in detail["last_error"]
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    assert "prompt is too long" in nodes["archive"]["last_error"]
    assert nodes["collect"]["output"] == {"files": ["a.txt"]}
    assert nodes["archive"]["input"]["upstream"]["collect"] == {"files": ["a.txt"]}


async def test_j3_cron_trigger_fires_after_runner_restart_and_lease_renewal(
    world: _World,
) -> None:
    """G10: the nightly cron fires once per night across a process restart.

    The schedule row, not the runner, owns "when". A fresh runner and a fresh
    engine over the same durable rows must still fire the next 22:00 exactly
    once, and a firing that is slow to start must never stall the tick.
    """
    store = ScheduleStore(world.tmp_path / "schedules.json")
    store.add(
        ScheduleEntry(
            id="wf:nightly",
            type="cron",
            action="workflow_run",
            workflow_id="nightly",
            expression="0 22 * * *",
            timezone="America/Chicago",
        )
    )
    fired: list[str] = []
    release = asyncio.Event()

    def engine_over(runner: Any) -> SchedulerEngine:
        engine = SchedulerEngine(
            store, SchedulerConfig(enabled=True), MagicMock(), None, bus=None  # type: ignore[arg-type]
        )
        engine.set_agent_run_fn(lambda *a, **k: asyncio.sleep(0))

        async def start_run(_self: SchedulerEngine, entry: ScheduleEntry) -> Any:
            fired.append(entry.id)
            await release.wait()  # a slow start must not hold the tick
            return await runner.start_run(
                entry.workflow_id,
                input={},
                initiator="operator",
                initiator_did="did:arc:ui:operator",
            )

        engine._dispatch = start_run.__get__(engine)  # type: ignore[method-assign]
        return engine

    with freeze_time(datetime(2026, 10, 1, 12, 0, tzinfo=UTC)):
        first = engine_over(world.runner)
        release.set()
        await first._tick()
        await first.drain()
    assert fired == ["wf:nightly"]
    assert store.get("wf:nightly").metadata.last_outcome == "ok"  # type: ignore[union-attr]

    restarted_runner = world.new_runner()
    with freeze_time(datetime(2026, 10, 2, 4, 0, tzinfo=UTC)):  # 23:00 CDT, after 22:00
        second = engine_over(restarted_runner)
        release.clear()
        await asyncio.wait_for(second._tick(), timeout=5)  # returns while the start is blocked
        assert second.in_flight == {"wf:nightly"}
        release.set()
        await second.drain()
        await second._tick()  # the slot is spent: no second firing
        await second.drain()

    assert fired == ["wf:nightly", "wf:nightly"]
    row = store.get("wf:nightly")
    assert row is not None and row.metadata.run_count == 2
