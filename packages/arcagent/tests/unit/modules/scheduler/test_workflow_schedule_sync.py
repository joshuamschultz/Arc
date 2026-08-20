"""A workflow's cron/interval trigger must become a real, editable schedule.

The failure this closes: a workflow declaring ``[trigger] type=cron`` sat inert
because the trigger lived only in the workflow document — nothing materialised it
into the scheduler store, and the store is the ONLY thing that fires cron. These
tests pin the bridge: a triggered workflow produces a ``workflow_run`` entry in
``schedules.json`` (so it fires AND is visible/editable there), an operator's
edit to that entry survives reconciliation, and removing the trigger removes the
derived entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from arcteam.workflow.models import ActiveHours as WfActiveHours
from arcteam.workflow.models import Trigger

from arcagent.modules.scheduler import workflow_sync as ws
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry, ScheduleMetadata
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore

_NIGHTLY = "CRON_TZ=America/Chicago 0 22 * * *"


def _cfg() -> SchedulerConfig:
    return SchedulerConfig(enabled=True)


def _store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.json")


# --- parsing --------------------------------------------------------------


def test_parse_cron_tz_extracts_zone_and_bare_expression() -> None:
    assert ws.parse_cron_tz(_NIGHTLY) == ("America/Chicago", "0 22 * * *")


def test_parse_cron_tz_leaves_a_plain_expression_untouched() -> None:
    assert ws.parse_cron_tz("0 22 * * *") == (None, "0 22 * * *")


# --- desired_entry --------------------------------------------------------


def test_desired_entry_maps_a_cron_trigger_to_a_workflow_run_schedule() -> None:
    entry = ws.desired_entry("nightly", Trigger(type="cron", expression=_NIGHTLY), _cfg())
    assert entry is not None
    assert entry.id == "wf:nightly"
    assert entry.action == "workflow_run"
    assert entry.workflow_id == "nightly"
    assert entry.type == "cron"
    assert entry.expression == "0 22 * * *"
    assert entry.timezone == "America/Chicago"
    assert entry.prompt == ""  # a typed action never carries prose
    assert entry.metadata.created_by == "system"


def test_desired_entry_maps_an_interval_trigger() -> None:
    entry = ws.desired_entry("poll", Trigger(type="interval", interval_s=3600), _cfg())
    assert entry is not None
    assert entry.type == "interval"
    assert entry.every_seconds == 3600


def test_desired_entry_returns_none_for_a_manual_trigger() -> None:
    assert ws.desired_entry("wf", Trigger(type="manual"), _cfg()) is None


def test_desired_entry_returns_none_for_a_missing_trigger() -> None:
    assert ws.desired_entry("wf", None, _cfg()) is None


def test_desired_entry_skips_an_interval_below_the_floor() -> None:
    # 5s is below the 60s min_interval floor — an invalid entry, not a clamp.
    assert ws.desired_entry("wf", Trigger(type="interval", interval_s=5), _cfg()) is None


def test_desired_entry_carries_active_hours() -> None:
    trig = Trigger(
        type="cron",
        expression="0 22 * * *",
        active_hours=WfActiveHours(start="20:00", end="23:59", timezone="America/Chicago"),
    )
    entry = ws.desired_entry("wf", trig, _cfg())
    assert entry is not None
    assert entry.active_hours is not None
    assert entry.active_hours.start == "20:00"


# --- full_reconcile (startup backfill + prune) ----------------------------


def test_full_reconcile_creates_a_schedule_for_a_triggered_workflow(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ws.full_reconcile(store, _cfg(), {"nightly": Trigger(type="cron", expression=_NIGHTLY)})

    entries = store.load()
    assert [e.id for e in entries] == ["wf:nightly"]
    assert entries[0].enabled is True
    assert entries[0].workflow_id == "nightly"


def test_anchor_seeds_last_run_so_a_fresh_schedule_does_not_catch_up(tmp_path: Path) -> None:
    """Wiring a 10pm trigger at noon must not fire it at noon."""
    store = _store(tmp_path)
    anchor = "2026-08-20T17:00:00+00:00"
    ws.full_reconcile(
        store, _cfg(), {"nightly": Trigger(type="cron", expression=_NIGHTLY)}, anchor=anchor
    )
    assert store.get("wf:nightly").metadata.last_run == anchor  # type: ignore[union-attr]


def test_full_reconcile_respects_an_operator_edit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Operator disabled the derived schedule in schedules.json.
    store.add(_derived("nightly", enabled=False))

    ws.full_reconcile(store, _cfg(), {"nightly": Trigger(type="cron", expression=_NIGHTLY)})

    # Reconcile does not re-enable it: the store is authoritative once it exists.
    assert store.get("wf:nightly").enabled is False  # type: ignore[union-attr]


def test_full_reconcile_prunes_a_derived_entry_whose_trigger_is_gone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_derived("nightly"))

    # Workflow still exists but its trigger was removed (effective_trigger None).
    ws.full_reconcile(store, _cfg(), {"nightly": None})

    assert store.get("wf:nightly") is None


def test_full_reconcile_never_touches_a_user_prompt_schedule(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(
        ScheduleEntry.model_validate(
            {"id": "sched_user", "type": "interval", "prompt": "check email", "every_seconds": 300}
        )
    )

    ws.full_reconcile(store, _cfg(), {})

    assert store.get("sched_user") is not None


# --- push_one (immediate materialisation when a trigger is set) -----------


def test_push_one_creates_the_schedule(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ws.push_one(store, _cfg(), "nightly", Trigger(type="cron", expression=_NIGHTLY))

    entry = store.get("wf:nightly")
    assert entry is not None
    assert entry.expression == "0 22 * * *"


def test_push_one_updates_timing_but_preserves_operator_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_derived("nightly", enabled=False, deliver_to="telegram:123"))

    # Author changed the workflow trigger to 6am.
    ws.push_one(store, _cfg(), "nightly", Trigger(type="cron", expression="0 6 * * *"))

    entry = store.get("wf:nightly")
    assert entry is not None
    assert entry.expression == "0 6 * * *"  # new timing applied
    assert entry.enabled is False  # operator's disable preserved
    assert entry.deliver_to == "telegram:123"  # operator's delivery preserved


def test_push_one_removes_the_schedule_when_the_trigger_is_cleared(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_derived("nightly"))

    ws.push_one(store, _cfg(), "nightly", None)

    assert store.get("wf:nightly") is None


# --- the entry's own timezone drives firing --------------------------------


def _utc_engine() -> SchedulerEngine:
    """An engine whose config zone is UTC (empty), so only a per-entry zone shifts."""
    config = MagicMock()
    config.timezone = ""
    return SchedulerEngine(
        store=MagicMock(),
        config=config,
        telemetry=MagicMock(),
        agent_run_fn=AsyncMock(),
    )


def test_a_workflows_own_timezone_fires_at_local_ten_pm_not_utc() -> None:
    """A '0 22 * * *' entry in America/Chicago fires at 22:00 Chicago (03:00 UTC),
    while the same expression without a zone waits for 22:00 UTC — so the workflow
    fires at 10pm the author's time regardless of the agent's default zone."""
    engine = _utc_engine()
    last_run = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    now = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)  # 23:00 in Chicago, 04:00 UTC

    chicago = ScheduleEntry(
        id="wf:x",
        type="cron",
        action="workflow_run",
        workflow_id="x",
        expression="0 22 * * *",
        timezone="America/Chicago",
        metadata=ScheduleMetadata(last_run=last_run.isoformat()),
    )
    plain = ScheduleEntry(
        id="sched_x",
        type="cron",
        prompt="digest",
        expression="0 22 * * *",
        metadata=ScheduleMetadata(last_run=last_run.isoformat()),
    )

    assert engine._should_fire_cron(chicago, now) is True
    assert engine._should_fire_cron(plain, now) is False


# --- ownership scoping ------------------------------------------------------


class _Bundle:
    def __init__(self, trigger: object, owner: str) -> None:
        self.effective_trigger = trigger
        self.definition = type("_Def", (), {"owner": owner})()


class _Defs:
    def __init__(self, bundles: dict[str, _Bundle]) -> None:
        self._b = bundles

    def list_ids(self, *, include_archived: bool = False) -> tuple[str, ...]:
        return tuple(self._b)

    def load(self, workflow_id: str) -> _Bundle:
        return self._b[workflow_id]


def test_owned_triggers_returns_only_this_agents_workflows() -> None:
    defs = _Defs(
        {
            "mine": _Bundle(Trigger(type="cron", expression="0 22 * * *"), "@sales_agent"),
            "theirs": _Bundle(Trigger(type="cron", expression="0 6 * * *"), "@josh_agent"),
        }
    )
    owned = ws._owned_triggers(defs, "sales_agent")
    assert set(owned) == {"mine"}


def test_owned_triggers_is_empty_without_an_agent_name() -> None:
    defs = _Defs({"mine": _Bundle(Trigger(type="cron", expression="0 22 * * *"), "@sales_agent")})
    assert ws._owned_triggers(defs, "") == {}


def _derived(workflow_id: str, **overrides: object) -> ScheduleEntry:
    """A schedule as the bridge would have written it, for pre-seeding."""
    data: dict[str, object] = {
        "id": f"wf:{workflow_id}",
        "type": "cron",
        "action": "workflow_run",
        "workflow_id": workflow_id,
        "expression": "0 22 * * *",
        "timezone": "America/Chicago",
        "metadata": ScheduleMetadata(created_by="system").model_dump(),
    }
    data.update(overrides)
    return ScheduleEntry.model_validate(data)
