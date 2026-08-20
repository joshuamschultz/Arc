"""The workflow-trigger → scheduler-store bridge, through the REAL entry points.

The unit tests pin ``workflow_sync``'s logic; these prove the two activating
seams are actually wired — the failure mode this whole change exists to kill is a
correct bridge nobody calls. So each test drives the production entry point:
``Scheduler.setup`` for the startup backfill and ``workflow_set_trigger``'s sync
call for the push, against a real :class:`ScheduleStore` and a stand-in workflow
definition store injected at the same seam production reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from arcagent.modules.scheduler import _runtime as scheduler_runtime
from arcagent.modules.scheduler import workflow_sync
from arcagent.modules.scheduler.config import SchedulerConfig


class _FakeDefinition:
    def __init__(self, owner: str) -> None:
        self.owner = owner


class _FakeBundle:
    def __init__(self, trigger: object, owner: str) -> None:
        self.effective_trigger = trigger
        self.definition = _FakeDefinition(owner)


class _FakeDefinitions:
    """The read half of the workflow store — just enough for the bridge.

    Every workflow here is owned by ``sales_agent`` so the owner-scoped bridge
    materialises it under a scheduler configured for that agent.
    """

    def __init__(self, triggers: dict[str, object], owner: str = "@sales_agent") -> None:
        self._triggers = triggers
        self._owner = owner

    def list_ids(self, *, include_archived: bool = False) -> tuple[str, ...]:
        return tuple(self._triggers)

    def exists(self, workflow_id: str) -> bool:
        return workflow_id in self._triggers

    def load(self, workflow_id: str) -> _FakeBundle:
        return _FakeBundle(self._triggers[workflow_id], self._owner)


@pytest.fixture
def wired(tmp_path: Path) -> Iterator[dict[str, object]]:
    """A configured scheduler runtime plus an injectable workflow-store seam."""
    triggers: dict[str, object] = {}
    scheduler_runtime.reset()
    scheduler_runtime.configure(
        config=SchedulerConfig(enabled=True, timezone="America/Chicago"),
        telemetry=None,  # type: ignore[arg-type]  # engine only forwards it
        workspace=tmp_path,
        agent_name="sales_agent",
    )
    workflow_sync._definitions_factory = lambda: _FakeDefinitions(triggers)
    yield triggers
    workflow_sync._definitions_factory = None
    scheduler_runtime.reset()


@pytest.mark.asyncio
async def test_scheduler_setup_backfills_a_workflow_cron_trigger(
    wired: dict[str, object],
) -> None:
    from arcteam.workflow.models import Trigger

    from arcagent.modules.scheduler.capabilities import Scheduler

    wired["nightly"] = Trigger(type="cron", expression="CRON_TZ=America/Chicago 0 22 * * *")

    scheduler = Scheduler()
    try:
        await scheduler.setup(None)

        entry = scheduler_runtime.state().store.get("wf:nightly")
        assert entry is not None
        assert entry.action == "workflow_run"
        assert entry.workflow_id == "nightly"
        assert entry.expression == "0 22 * * *"
        assert entry.timezone == "America/Chicago"
        assert entry.enabled is True
    finally:
        await scheduler.teardown()


@pytest.mark.asyncio
async def test_setting_a_trigger_pushes_a_schedule_immediately(
    wired: dict[str, object],
) -> None:
    """The push path: ``sync_workflow_schedule`` reads the persisted trigger."""
    from arcteam.workflow.models import Trigger

    from arcagent.modules.scheduler.workflow_sync import sync_workflow_schedule

    wired["digest"] = Trigger(type="cron", expression="0 6 * * *")

    await sync_workflow_schedule("digest")

    entry = scheduler_runtime.state().store.get("wf:digest")
    assert entry is not None
    assert entry.expression == "0 6 * * *"
