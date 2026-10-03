"""Journey: legacy workflow bundles come back after ``arc workflow migrate --resign``.

The alpha-2 regression: every saved bundle carried ``join = "all"``, so every
workflow failed to parse, the list was empty, and the cron triggers were dead.
This drives the operator's actual recovery: a legacy bundle with a stale
signature on disk, the dashboard list before and after, and the cron firing.
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
from arccli.commands.operator import load_operator_key, resolve_operator_signer
from arccli.commands.workflow import workflow_handler
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner
from arctrust import default_operator_key_path, sign_artifact_with_signer
from arctrust.paths import arc_state, workflows_dir
from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import build_dashboard_plane

_LEGACY = """[workflow]
schema_version = "1.0"
id = "morning-briefing"
version = 1
owner = "@sales"

[trigger]
type = "cron"
expression = "0 7 * * *"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"
join = "all"

[[node]]
id = "send"
kind = "agent"
agent = "@sales"
needs = ["collect"]
join = "all"
"""

_ACTOR = OperatorActor(did="did:arc:ui:operator", session_id="s1")


class _Registry:
    async def get(self, handle: str) -> Any:
        class _Entity:
            did = "did:arc:test:sales"

        return _Entity() if handle == "sales" else None


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
async def node(tmp_path: Path) -> AsyncIterator[tuple[Any, Any, Path]]:
    load_operator_key(tmp_path)
    bundle = workflows_dir(tmp_path) / "morning-briefing"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_LEGACY, encoding="utf-8")
    stale = sign_artifact_with_signer(
        b"signed-by-the-old-runtime",
        signer_did="operator:old",
        signer=resolve_operator_signer(tmp_path),
    )
    (bundle / "workflow.toml.arcsig").write_text(stale.to_json(), encoding="utf-8")

    backend = FakeBackend()
    await backend.start()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=default_operator_key_path(tmp_path),
        workspace_root=arc_state(tmp_path),
        registry=_Registry(),
    )
    yield runner, build_dashboard_plane(runner=runner), bundle
    await runner.aclose()
    await backend.stop()


async def test_migrate_resign_brings_the_workflow_and_its_cron_back(
    node: tuple[Any, Any, Path], tmp_path: Path
) -> None:
    runner, plane, bundle = node

    before = await plane.list_workflows(actor=_ACTOR)
    assert [(w["id"], w["status"]) for w in before] == [("morning-briefing", "unreadable")]

    workflow_handler(["migrate", "--resign", "--dir", str(tmp_path)])

    after = await plane.list_workflows(actor=_ACTOR)
    assert [(w["id"], w["status"], w["health"]) for w in after] == [
        ("morning-briefing", "signed", "ok")
    ]
    assert "join" not in (bundle / "workflow.toml").read_text()

    store = ScheduleStore(tmp_path / "schedules.json")
    store.add(
        ScheduleEntry(
            id="wf:morning-briefing",
            type="cron",
            action="workflow_run",
            workflow_id="morning-briefing",
            expression="0 7 * * *",
            timezone="America/Chicago",
        )
    )
    row = store.get("wf:morning-briefing")
    assert row is not None
    meta = row.metadata.model_copy(
        update={"last_run": (datetime.now(UTC) - timedelta(days=2)).isoformat()}
    )
    store.update("wf:morning-briefing", {"metadata": meta.model_dump()})

    started: list[str] = []
    engine = SchedulerEngine(
        store,
        SchedulerConfig(enabled=True),
        MagicMock(),
        None,
        bus=None,  # type: ignore[arg-type]
    )
    engine.set_agent_run_fn(lambda *a, **k: asyncio.sleep(0))

    async def start_run(_self: SchedulerEngine, entry: ScheduleEntry) -> Any:
        run = await runner.start_run(
            entry.workflow_id,
            input={},
            initiator="operator",
            initiator_did="did:arc:ui:operator",
        )
        started.append(str(run.run_id))
        return run

    engine._dispatch = start_run.__get__(engine)  # type: ignore[method-assign]
    await engine._tick()
    await engine.drain()

    assert len(started) == 1
    fired = store.get("wf:morning-briefing")
    assert fired is not None and fired.metadata.last_outcome == "ok"
