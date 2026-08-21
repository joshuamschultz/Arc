"""COMP-010 (T-1040 RED / T-1041 GREEN) — mutability-driven sync + backfill caps.

``arcmemory.sync`` is a pure, deterministic decision engine: it decides WHICH
external-source changes reconcile (immutable sources never delete; mutable
sources tombstone) and WHICH backfill objects a first sync accepts before the
zero-trust caps (count/bytes/age) stop it — never embeds, never ingests, never
imports the model or arcrun.

RED because ``arcmemory.sync`` does not exist yet (ModuleNotFoundError) — the
whole engine is unimplemented, not merely mis-named.
"""

from __future__ import annotations

from arcmemory.sync import (
    BackfillObject,
    SourceChange,
    SyncEngine,
    SyncMode,
)
from arctrust.audit import AuditEvent

from arcmemory.config import MemoryConfig
from arcmemory.types import SourceRecord

_DID = "did:arc:sync-test"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _changes() -> list[SourceChange]:
    return [
        SourceChange(op="upsert", external_id="1", record=SourceRecord(external_id="1", text="a")),
        SourceChange(op="delete", external_id="2"),
        SourceChange(op="upsert", external_id="3", record=SourceRecord(external_id="3", text="c")),
    ]


# -- reconcile: mutability drives delete handling ----------------------------


def test_reconcile_immutable_drops_deletes_keeps_upserts() -> None:
    """An immutable source is append-only: a 'delete' change is never returned."""
    engine = SyncEngine(MemoryConfig())

    result = engine.reconcile(SyncMode.IMMUTABLE, _changes())

    assert [c.op for c in result] == ["upsert", "upsert"]
    assert {c.external_id for c in result} == {"1", "3"}


def test_reconcile_mutable_keeps_upserts_and_deletes() -> None:
    """A mutable source reconciles both upserts and deletes (tombstone markers)."""
    engine = SyncEngine(MemoryConfig())

    result = engine.reconcile(SyncMode.MUTABLE, _changes())

    assert len(result) == 3
    assert {c.op for c in result} == {"upsert", "delete"}
    deleted = next(c for c in result if c.op == "delete")
    assert deleted.external_id == "2"


def test_reconcile_immutable_with_no_deletes_is_unchanged() -> None:
    """Immutable reconcile is a no-op filter when there is nothing to drop."""
    engine = SyncEngine(MemoryConfig())
    changes = [
        SourceChange(op="upsert", external_id="1", record=SourceRecord(external_id="1", text="a")),
        SourceChange(op="upsert", external_id="2", record=SourceRecord(external_id="2", text="b")),
    ]

    result = engine.reconcile(SyncMode.IMMUTABLE, changes)

    assert len(result) == 2


# -- plan_backfill: age/size drop (silent, no stop) --------------------------


def test_plan_backfill_drops_stale_and_oversized_objects_without_stopping() -> None:
    """Objects older than the age cap or bigger than the per-object cap never
    reach ``accepted`` -- but the plan does not STOP for them, since there was
    no exhaustion of the total-count/total-bytes budget."""
    config = MemoryConfig(
        backfill_max_age_days=30,
        backfill_max_object_bytes=100,
        backfill_max_objects=1000,
        backfill_max_total_bytes=10_000_000,
    )
    engine = SyncEngine(config)
    objects = [
        BackfillObject(external_id="fresh", size_bytes=10, age_days=1),
        BackfillObject(external_id="too-old", size_bytes=10, age_days=999),
        BackfillObject(external_id="too-big", size_bytes=10_000, age_days=1),
    ]

    plan = engine.plan_backfill(objects)

    assert plan.accepted == ["fresh"]
    assert plan.stopped is False
    assert "too-old" not in plan.accepted
    assert "too-big" not in plan.accepted


# -- plan_backfill: object-count cap (STOP + audit) --------------------------


def test_plan_backfill_stops_at_object_count_cap_and_emits_capped_audit() -> None:
    config = MemoryConfig(
        backfill_max_objects=2,
        backfill_max_total_bytes=10_000_000,
        backfill_max_age_days=90,
        backfill_max_object_bytes=10_000_000,
    )
    sink = RecordingSink()
    engine = SyncEngine(config, audit_sink=sink, actor_did=_DID)
    objects = [BackfillObject(external_id=f"obj-{i}", size_bytes=10, age_days=1) for i in range(5)]

    plan = engine.plan_backfill(objects)

    assert plan.accepted == ["obj-0", "obj-1"]
    assert plan.stopped is True
    assert plan.reason  # which cap tripped
    assert plan.resumable_cursor == "obj-1"  # last accepted id
    # Every over-cap id is NOT in accepted -- the audit fires BEFORE acceptance.
    assert {"obj-2", "obj-3", "obj-4"}.isdisjoint(plan.accepted)
    capped = [e for e in sink.events if e.action == "ingest.backfill_capped"]
    assert len(capped) == 1
    assert capped[0].actor_did == _DID


# -- plan_backfill: total-bytes cap (STOP + audit) ---------------------------


def test_plan_backfill_stops_at_total_bytes_cap_before_over_cap_object() -> None:
    config = MemoryConfig(
        backfill_max_total_bytes=25,
        backfill_max_objects=1000,
        backfill_max_age_days=90,
        backfill_max_object_bytes=10_000_000,
    )
    sink = RecordingSink()
    engine = SyncEngine(config, audit_sink=sink)
    objects = [
        BackfillObject(external_id="a", size_bytes=10, age_days=1),
        BackfillObject(external_id="b", size_bytes=10, age_days=1),
        BackfillObject(external_id="c", size_bytes=10, age_days=1),  # would push to 30 > 25
    ]

    plan = engine.plan_backfill(objects)

    assert plan.accepted == ["a", "b"]
    assert plan.stopped is True
    assert "c" not in plan.accepted
    assert any(e.action == "ingest.backfill_capped" for e in sink.events)


def test_plan_backfill_never_embeds_or_ingests_planning_only() -> None:
    """``plan_backfill`` is planning only -- it returns ids, not embedded text."""
    engine = SyncEngine(MemoryConfig())
    objects = [BackfillObject(external_id="x", size_bytes=10, age_days=1)]

    plan = engine.plan_backfill(objects)

    assert plan.accepted == ["x"]
    assert plan.stopped is False
    assert plan.reason == ""
