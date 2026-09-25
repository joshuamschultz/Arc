"""A due schedule slot has one stable effect identity across restart."""

from datetime import UTC, datetime, timedelta

from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import scheduled_occurrence


def test_interval_slot_identity_survives_restart_and_metadata_changes() -> None:
    entry = ScheduleEntry(
        id="schedule-1", type="interval", prompt="Check status", every_seconds=60
    )
    first = scheduled_occurrence(entry, datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC))
    restarted = scheduled_occurrence(
        entry.model_copy(update={"metadata": entry.metadata.model_copy(update={"run_count": 7})}),
        datetime(2026, 9, 25, 12, 0, 50, tzinfo=UTC),
    )
    later = scheduled_occurrence(entry, datetime(2026, 9, 25, 12, 1, 1, tzinfo=UTC))
    assert first.run_id == restarted.run_id
    assert first.definition_digest == restarted.definition_digest
    assert later.run_id != first.run_id
    assert later.due_at - first.due_at == timedelta(minutes=1)


def test_revision_change_does_not_reuse_slot_for_new_effect() -> None:
    entry = ScheduleEntry(
        id="schedule-1", type="interval", prompt="Check status", every_seconds=60
    )
    revised = entry.model_copy(update={"prompt": "Check alarms"})
    now = datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC)
    original = scheduled_occurrence(entry, now)
    changed = scheduled_occurrence(revised, now)
    assert original.run_id == changed.run_id
    assert original.definition_digest != changed.definition_digest
    assert original.evidence != changed.evidence


def test_once_occurrence_uses_configured_local_zone() -> None:
    entry = ScheduleEntry(
        id="once-1",
        type="once",
        prompt="Reminder",
        at="2026-09-25T08:00:00",
        timezone="America/Chicago",
    )
    occurrence = scheduled_occurrence(entry, datetime(2026, 9, 25, 14, tzinfo=UTC))
    assert occurrence.due_at == datetime(2026, 9, 25, 13, tzinfo=UTC)


def test_cron_occurrence_refuses_future_slot() -> None:
    entry = ScheduleEntry(id="cron-1", type="cron", prompt="Daily", expression="0 8 * * *")
    before = scheduled_occurrence(entry, datetime(2026, 9, 25, 7, 59, tzinfo=UTC))
    after = scheduled_occurrence(entry, datetime(2026, 9, 25, 8, 1, tzinfo=UTC))
    assert before.due_at == datetime(2026, 9, 24, 8, tzinfo=UTC)
    assert after.due_at == datetime(2026, 9, 25, 8, tzinfo=UTC)
