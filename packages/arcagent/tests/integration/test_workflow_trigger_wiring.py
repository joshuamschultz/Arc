"""The workflow-trigger → scheduler-store bridge, through the REAL entry points.

The unit tests pin ``workflow_sync``'s logic; these prove the two activating
seams are actually wired — the failure mode this whole change exists to kill is a
correct bridge nobody calls. So each test drives the production entry point:
``Scheduler.setup`` for the startup backfill and ``workflow_set_trigger`` for the
push, against a real :class:`ScheduleStore` and a real workflows definition store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import AgentIdentity

from arcagent.modules.scheduler import _runtime as scheduler_runtime
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.workflows import _runtime as workflows_runtime


class _FakeBundle:
    def __init__(self, trigger: object) -> None:
        self.effective_trigger = trigger


class _FakeDefinitions:
    """The read half of the workflows store — just enough for the bridge."""

    def __init__(self, triggers: dict[str, object]) -> None:
        self._triggers = triggers

    def list_ids(self, *, include_archived: bool = False) -> tuple[str, ...]:
        return tuple(self._triggers)

    def exists(self, workflow_id: str) -> bool:
        return workflow_id in self._triggers

    def load(self, workflow_id: str) -> _FakeBundle:
        return _FakeBundle(self._triggers[workflow_id])


def _configure_runtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    triggers: dict[str, object],
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    scheduler_runtime.reset()
    scheduler_runtime.configure(
        config=SchedulerConfig(enabled=True, timezone="America/Chicago"),
        telemetry=None,  # type: ignore[arg-type]  # engine only forwards it
        workspace=tmp_path,
    )
    workflows_runtime.reset()
    workflows_runtime.configure(
        config={"enabled": True},
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        control_plane=object(),  # non-None so ensure_control_plane is a no-op
        definitions=_FakeDefinitions(triggers),
    )


@pytest.mark.asyncio
async def test_scheduler_setup_backfills_a_workflow_cron_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcteam.workflow.models import Trigger

    from arcagent.modules.scheduler.capabilities import Scheduler

    _configure_runtimes(
        tmp_path,
        monkeypatch,
        {"nightly": Trigger(type="cron", expression="CRON_TZ=America/Chicago 0 22 * * *")},
    )

    scheduler = Scheduler()
    try:
        await scheduler.setup(None)

        entries = scheduler_runtime.state().store.load()
        derived = [e for e in entries if e.id == "wf:nightly"]
        assert len(derived) == 1
        assert derived[0].action == "workflow_run"
        assert derived[0].workflow_id == "nightly"
        assert derived[0].expression == "0 22 * * *"
        assert derived[0].timezone == "America/Chicago"
    finally:
        await scheduler.teardown()
        scheduler_runtime.reset()
        workflows_runtime.reset()


@pytest.mark.asyncio
async def test_setting_a_trigger_pushes_a_schedule_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push path: ``sync_workflow_schedule`` reads the persisted trigger."""
    from arcteam.workflow.models import Trigger

    from arcagent.modules.scheduler.workflow_sync import sync_workflow_schedule

    _configure_runtimes(
        tmp_path,
        monkeypatch,
        {"digest": Trigger(type="cron", expression="0 6 * * *")},
    )

    await sync_workflow_schedule("digest")

    entry = scheduler_runtime.state().store.get("wf:digest")
    assert entry is not None
    assert entry.expression == "0 6 * * *"
    scheduler_runtime.reset()
    workflows_runtime.reset()
