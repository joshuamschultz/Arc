"""Canonical definition and stable due-slot identity for signed schedules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from arcagent.modules.scheduler.models import ScheduleEntry


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class ScheduledOccurrence:
    """One definition revision evaluated at one immutable due slot."""

    run_id: str
    occurrence_id: str
    due_at: datetime
    definition_digest: str
    definition: bytes
    evidence: bytes


def canonical_definition(entry: ScheduleEntry) -> bytes:
    """Exclude mutable execution metadata from the signed control artifact."""
    return _canonical(entry.model_dump(mode="json", exclude={"metadata", "approval"}))


def scheduled_occurrence(
    entry: ScheduleEntry,
    now: datetime,
    *,
    default_timezone: str = "UTC",
) -> ScheduledOccurrence:
    """Select the latest due slot; missed prior slots are skipped, not replayed."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("schedule evaluation time must be timezone-aware")
    zone = ZoneInfo(entry.timezone or default_timezone)
    if entry.type == "interval":
        every = entry.every_seconds
        if every is None or every < 1:
            raise ValueError("interval schedule is incomplete")
        due_at = datetime.fromtimestamp(int(now.timestamp() // every) * every, tz=UTC)
    elif entry.type == "cron":
        if entry.expression is None:
            raise ValueError("cron schedule is incomplete")
        local = now.astimezone(zone)
        due_at = (
            croniter(entry.expression, local + timedelta(microseconds=1))
            .get_prev(datetime)
            .astimezone(UTC)
        )
    else:
        if entry.at is None:
            raise ValueError("one-time schedule is incomplete")
        target = datetime.fromisoformat(entry.at)
        due_at = (target if target.tzinfo is not None else target.replace(tzinfo=zone)).astimezone(
            UTC
        )
        if due_at > now.astimezone(UTC):
            raise ValueError("one-time schedule is not due")
    definition = canonical_definition(entry)
    definition_digest = hashlib.sha256(definition).hexdigest()
    identity = _canonical(["arc.schedule.occurrence.v1", entry.id, due_at.isoformat()])
    occurrence_id = hashlib.sha256(identity).hexdigest()
    evidence = _canonical(
        {
            "artifact_id": entry.id,
            "definition": json.loads(definition),
            "definition_digest": definition_digest,
            "due_at": due_at.isoformat(),
            "occurrence_id": occurrence_id,
        }
    )
    return ScheduledOccurrence(
        run_id="schedule_" + occurrence_id,
        occurrence_id=occurrence_id,
        due_at=due_at,
        definition_digest=definition_digest,
        definition=definition,
        evidence=evidence,
    )
