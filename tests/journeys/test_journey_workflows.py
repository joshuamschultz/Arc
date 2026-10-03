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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import TaskStore
from arcteam.workflow.narrator import RunNarrator
from arcteam.workflow.runner import build_workflow_runner, node_task_id
from arctrust import OperatorKey
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


class _Notices:
    """The operator seam ``ArcAgent.notify_operator`` stands behind in production."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def notify(
        self, text: str, idempotency_key: str, link_path: str | None = None
    ) -> str | None:
        self.sent.append((text, idempotency_key))
        return "telegram"


class _World:
    def __init__(self, tmp_path: Path, backend: FakeBackend) -> None:
        self.tmp_path = tmp_path
        self.backend = backend
        self.gateway = _Gateway()
        self.notices = _Notices()
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
            operator_notifier=self.notices.notify,
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

    assert len(world.notices.sent) == 1
    body, key = world.notices.sent[0]
    assert key.startswith(f"workflow-run:{run_id}:failed:")
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
            store,
            SchedulerConfig(enabled=True),
            MagicMock(),
            None,
            bus=None,  # type: ignore[arg-type]
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

    def a_night_has_passed() -> None:
        """Move the row's last firing two days back: durable state, no clock patching."""
        row = store.get("wf:nightly")
        assert row is not None
        meta = row.metadata.model_copy(
            update={"last_run": (datetime.now(UTC) - timedelta(days=2)).isoformat()}
        )
        store.update("wf:nightly", {"metadata": meta.model_dump()})

    a_night_has_passed()
    first = engine_over(world.runner)
    release.set()
    await first._tick()
    await first.drain()
    assert fired == ["wf:nightly"]
    assert store.get("wf:nightly").metadata.last_outcome == "ok"  # type: ignore[union-attr]

    a_night_has_passed()
    await world.runner.aclose()  # the old process releases its runner lease
    second = engine_over(world.new_runner())  # a process restart: all new objects
    release.clear()
    await asyncio.wait_for(second._tick(), timeout=5)  # returns while the start is blocked
    assert second.in_flight == {"wf:nightly"}
    release.set()
    await second.drain()
    await second._tick()  # the slot is spent: no third firing
    await second.drain()

    assert fired == ["wf:nightly", "wf:nightly"]
    row = store.get("wf:nightly")
    assert row is not None and row.metadata.run_count == 2


async def test_restart_between_fire_and_tick_yields_exactly_one_completed_run(
    world: _World,
) -> None:
    """G-B: the trigger fires, the process dies before any tick, and the restarted
    process re-fires the same occurrence. One run exists, and it completes once.
    """
    run_id = "run-nightly-occ1"

    async def fire(runner: Any) -> None:
        await runner.start_run(
            "nightly",
            input={},
            initiator="operator",
            initiator_did="did:arc:ui:operator",
            run_id=run_id,
            detached=True,
        )

    await fire(world.runner)
    # The process dies here: no tick ran, nothing was closed or released.
    restarted = world.new_runner()
    await restarted.resume()
    await fire(restarted)  # the scheduler retries the occurrence it never saw acknowledged

    await restarted.tick()
    for node in ("collect", "archive"):
        row = node_task_id(run_id, node, 0)
        await world.tasks.start_task(row, SALES)
        await world.tasks.finish(row, status="done", resolution="ok", actor_did=SALES, output={})
        await restarted.tick()

    runs = await restarted.runs.list_for_workflow("nightly")
    assert [run.id for run in runs] == [run_id]
    assert runs[0].status == "done"
    assert {state.status for state in runs[0].node_states.values()} == {"done"}
    rows = await world.backend.mutable_query("tasks", where={"metadata.flow_run_id": run_id})
    assert sorted(r["id"] for r in rows) == sorted(
        node_task_id(run_id, node, 0) for node in ("collect", "archive")
    )
    assert world.notices.sent == []


_THREE_NODE_DAG = """
[workflow]
id = "dag3"
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

[[node]]
id = "notify"
kind = "agent"
agent = "@sales"
needs = ["archive"]
"""


async def _start_dag3_with_dead_lettered_archive(world: _World) -> str:
    bundle = world.tmp_path / "workflows" / "dag3"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_THREE_NODE_DAG, encoding="utf-8")
    started = await world.plane.run_workflow("dag3", {}, actor=world.actor)
    run_id = started.value["run_id"]
    await _fail_the_archive_node(world, run_id)
    return str(run_id)


async def test_three_node_dag_dead_letter_cancels_dependents_and_ui_shows_reason(
    world: _World,
) -> None:
    """G-A: a dead-lettered node fails the run; the node behind it says why it never ran."""
    run_id = await _start_dag3_with_dead_lettered_archive(world)

    detail = await world.plane.get_run(run_id, actor=world.actor)

    assert detail["status"] == "failed"
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    assert nodes["collect"]["status"] == "done"
    assert nodes["archive"]["status"] == "failed"
    assert "prompt is too long" in nodes["archive"]["last_error"]
    assert nodes["notify"]["status"] == "cancelled"
    assert nodes["notify"]["reason"].startswith("upstream archive failed")
    assert "prompt is too long" in nodes["notify"]["reason"]


async def test_j3_retry_failed_node_skips_completed_upstream(world: _World) -> None:
    """G3: the operator retries the failed node; the finished node is never executed again."""
    run_id = await _start_dag3_with_dead_lettered_archive(world)
    collect_before = await world.tasks.get(node_task_id(run_id, "collect", 0))
    assert collect_before is not None and collect_before.attempts == 1

    retried = await world.plane.retry_node(run_id, "archive", actor=world.actor)
    assert retried.errors is None, retried.errors
    for node, iteration in (("archive", 1), ("notify", 1)):
        row = node_task_id(run_id, node, iteration)
        await world.tasks.start_task(row, SALES)
        await world.tasks.finish(
            row, status="done", resolution="ok", actor_did=SALES, output={"node": node}
        )
        await world.runner.advance(run_id)

    detail = await world.plane.get_run(run_id, actor=world.actor)
    assert detail["status"] == "done"
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    assert {k: v["status"] for k, v in nodes.items()} == {
        "collect": "done",
        "archive": "done",
        "notify": "done",
    }
    collect_after = await world.tasks.get(node_task_id(run_id, "collect", 0))
    assert collect_after is not None and collect_after.attempts == 1, "collect ran exactly once"
    assert await world.tasks.get(node_task_id(run_id, "collect", 1)) is None


_ENTERPRISE_TOML = '[security]\ntier = "enterprise"\n'


def test_j3_docs_cli_create_sign_run_enterprise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """G4: docs/get-started/workflows.md, verbatim, at enterprise tier.

    ``arc workflow new --from`` -> ``sign`` by id -> ``run --input --detach`` -> the
    run's result is readable. Only the LLM is replaced: a scripted agent answers each
    node's task, the way the agent module does in production.
    """
    from arccli.commands import workflow as wf_cmd
    from arccli.commands.operator import load_operator_key
    from arccli.commands.workflow import workflow_handler
    from arcteam.workflow.stores import WorkflowRunStore

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    backend = FakeBackend()
    monkeypatch.setattr(wf_cmd, "_backend_factory", lambda: backend)

    class _Fleet:
        """The team registry: the doc's example agent exists; nothing else does."""

        async def get(self, handle: str) -> Any:
            return _Entity() if handle == "analyst-1" else None

    async def _bindings(arc_dir: Path) -> tuple[Any, Any]:
        return _Fleet(), None

    monkeypatch.setattr(wf_cmd, "_team_bindings", _bindings)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "arcagent.toml").write_text(_ENTERPRISE_TOML, encoding="utf-8")
    load_operator_key(tmp_path)
    (tmp_path / "in.json").write_text('{"question": "Is the plan sound?"}', encoding="utf-8")

    def arc(*argv: str) -> str:
        capsys.readouterr()
        workflow_handler(list(argv))
        return capsys.readouterr().out

    assert "fanout_synthesize" in arc("templates")
    assert "arc workflow sign weekly-brief" in arc(
        "new", "weekly-brief", "--from", "fanout_synthesize"
    )
    assert "weekly-brief" in arc("list")
    arc("sign", "weekly-brief")
    assert "VALID" in arc("verify", "weekly-brief")
    started = arc("run", "weekly-brief", "--input", "in.json", "--detach")
    assert "Started run" in started
    run_id = started.split("Started run ")[1].split()[0]

    # The gateway's runner host drives the run; a scripted agent answers each node.
    async def drive_to_the_end() -> Any:
        plane, aclose = await wf_cmd._resolve_control_plane(tmp_path, tier="enterprise")
        try:
            tasks = TaskStore(backend)
            outputs = {
                "angle_evidence": {"findings": ["e"], "confidence": "high"},
                "angle_risks": {"findings": ["r"], "confidence": "high"},
                "angle_options": {"findings": ["o"], "confidence": "high"},
                "synthesize": {"answer": "The plan is sound."},
            }
            for node in ("angle_evidence", "angle_risks", "angle_options", "synthesize"):
                await plane.runner.advance(run_id)
                row = node_task_id(run_id, node, 0)
                await tasks.start_task(row, SALES)
                await tasks.finish(
                    row, status="done", resolution="ok", actor_did=SALES, output=outputs[node]
                )
            await plane.runner.advance(run_id)
            return await WorkflowRunStore(backend).get(run_id)
        finally:
            await aclose()

    record = asyncio.run(drive_to_the_end())
    assert record is not None
    assert record.status == "done", getattr(record, "last_error", record)
