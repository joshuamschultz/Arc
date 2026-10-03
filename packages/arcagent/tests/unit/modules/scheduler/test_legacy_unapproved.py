"""Schedules created before the control authority have no signed revision.

Startup must mark them (never auto-approve), log one warning line per schedule
with no traceback, and leave every other schedule reconciling normally.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcteam.workflow.models import Trigger

from arcagent.modules.scheduler import _runtime
from arcagent.modules.scheduler import workflow_sync as ws
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry, ScheduleMetadata
from arcagent.modules.scheduler.store import ScheduleStore

_NIGHTLY = "CRON_TZ=America/Chicago 0 22 * * *"


class _Bundle:
    def __init__(self, trigger: object, owner: str) -> None:
        self.effective_trigger = trigger
        self.definition = MagicMock(owner=owner)


class _Defs:
    def __init__(self, bundles: dict[str, _Bundle]) -> None:
        self._b = bundles

    def list_ids(self, *, include_archived: bool = False) -> tuple[str, ...]:
        return tuple(self._b)

    def load(self, workflow_id: str) -> _Bundle:
        return self._b[workflow_id]


def _legacy(workflow_id: str = "nightly") -> ScheduleEntry:
    return ScheduleEntry.model_validate(
        {
            "id": f"wf:{workflow_id}",
            "type": "cron",
            "action": "workflow_run",
            "workflow_id": workflow_id,
            "expression": "0 22 * * *",
            "timezone": "America/Chicago",
            "metadata": ScheduleMetadata(created_by="system").model_dump(),
        }
    )


@pytest.fixture
def wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ScheduleStore, AsyncMock]]:
    store = ScheduleStore(tmp_path / "schedules.json")
    authority = AsyncMock()
    state = _runtime._State(
        config=SchedulerConfig(enabled=True),
        workspace=tmp_path,
        telemetry=MagicMock(),
        store=store,
        agent_name="sales_agent",
        control_artifact_authority=authority,
        control_tenant_id="tenant",
        control_actor_proof_source=AsyncMock(return_value=b"proof"),
        agent_did="did:arc:local:agent/one",
    )
    _runtime.bind(state)
    bundle = _Bundle(Trigger(type="cron", expression=_NIGHTLY), "@sales_agent")
    monkeypatch.setattr(ws, "_definitions_factory", lambda: _Defs({"nightly": bundle}))
    yield store, authority
    _runtime.reset()


@pytest.mark.asyncio
async def test_legacy_schedule_is_marked_unapproved_without_traceback(
    wired: tuple[ScheduleStore, AsyncMock], caplog: pytest.LogCaptureFixture
) -> None:
    store, authority = wired
    store.add(_legacy())
    caplog.set_level(logging.DEBUG)

    await ws.reconcile_workflow_schedules()

    row = store.get("wf:nightly")
    assert row is not None
    assert row.enabled is False
    assert row.metadata.disabled_reason == "unapproved"
    assert row.approval is None  # never auto-approved
    authority.register_revision.assert_not_called()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "wf:nightly" in warnings[0].getMessage()
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.asyncio
async def test_unrelated_legacy_user_schedule_is_marked_too(
    wired: tuple[ScheduleStore, AsyncMock],
) -> None:
    store, _ = wired
    store.add(ScheduleEntry(id="s1", type="interval", prompt="Check", every_seconds=300))

    await ws.reconcile_workflow_schedules()

    row = store.get("s1")
    assert row is not None and row.metadata.disabled_reason == "unapproved"
    assert row.enabled is False


@pytest.mark.asyncio
async def test_second_startup_does_not_warn_again(
    wired: tuple[ScheduleStore, AsyncMock], caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = wired
    store.add(_legacy())
    await ws.reconcile_workflow_schedules()
    caplog.clear()
    caplog.set_level(logging.WARNING)

    await ws.reconcile_workflow_schedules()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
