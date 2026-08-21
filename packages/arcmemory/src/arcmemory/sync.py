"""COMP-010 — mutability-driven sync + backfill caps (SPEC-073).

Pure, deterministic decision engine: it decides WHICH external-source changes
reconcile (an immutable source never deletes; a mutable source tombstones)
and WHICH backfill objects a first sync accepts before the zero-trust caps
(count/bytes/age) stop it (LLM10). Never embeds, never ingests, never touches
the model, ``arcrun``, or any scheduler/connector — the trigger is a
connector-side signed workflow run; this module only plans and reconciles.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from arctrust.audit import AuditEvent, AuditSink, emit
from pydantic import BaseModel, Field

from arcmemory.config import MemoryConfig
from arcmemory.types import SourceRecord


class SyncMode(StrEnum):
    """Whether a source may report deletes (see :meth:`SyncEngine.reconcile`)."""

    IMMUTABLE = "immutable"
    MUTABLE = "mutable"


class BackfillObject(BaseModel):
    """One candidate object for a first sync, before extraction/embedding."""

    external_id: str
    size_bytes: int = 0
    age_days: int = 0


class BackfillPlan(BaseModel):
    """The decision: which objects to ingest, and whether the caps stopped it."""

    accepted: list[str] = Field(default_factory=list)
    stopped: bool = False
    reason: str = ""
    resumable_cursor: str = ""


class SourceChange(BaseModel):
    """One reconciled change from a source's incremental sync."""

    op: Literal["upsert", "delete"]
    external_id: str
    record: SourceRecord | None = None


class SyncEngine:
    """Deterministic backfill-cap + mutability-reconciliation decisions."""

    def __init__(
        self, config: MemoryConfig, *, audit_sink: AuditSink | None = None, actor_did: str = ""
    ) -> None:
        self._cfg = config
        self._audit_sink = audit_sink
        self._actor_did = actor_did

    def plan_backfill(self, objects: list[BackfillObject]) -> BackfillPlan:
        """Decide which objects a first sync ingests -- planning only.

        Drops (silently, no stop) objects older than ``backfill_max_age_days``
        or bigger than ``backfill_max_object_bytes``. Accepts the rest in order
        until accepting the next one would exceed ``backfill_max_objects`` or
        ``backfill_max_total_bytes``, then stops: the over-cap object (and
        everything after it) is never accepted, one capped audit event fires,
        and ``resumable_cursor`` names the last accepted id.
        """
        accepted: list[str] = []
        total_bytes = 0
        stopped = False
        reason = ""

        for obj in objects:
            if obj.age_days > self._cfg.backfill_max_age_days:
                continue
            if obj.size_bytes > self._cfg.backfill_max_object_bytes:
                continue

            if len(accepted) + 1 > self._cfg.backfill_max_objects:
                reason = "backfill_max_objects"
                stopped = True
                break
            if total_bytes + obj.size_bytes > self._cfg.backfill_max_total_bytes:
                reason = "backfill_max_total_bytes"
                stopped = True
                break

            accepted.append(obj.external_id)
            total_bytes += obj.size_bytes

        if stopped and self._audit_sink is not None:
            emit(
                AuditEvent(
                    actor_did=self._actor_did,
                    action="ingest.backfill_capped",
                    target=reason,
                    outcome="allow",
                    tier=self._cfg.tier,
                    extra={"accepted": len(accepted), "reason": reason},
                ),
                self._audit_sink,
            )

        return BackfillPlan(
            accepted=accepted,
            stopped=stopped,
            reason=reason,
            resumable_cursor=accepted[-1] if accepted else "",
        )

    def reconcile(self, mode: SyncMode, changes: list[SourceChange]) -> list[SourceChange]:
        """Filter incremental changes per source mutability.

        ``IMMUTABLE`` sources are append-only: 'delete' changes never reconcile.
        ``MUTABLE`` sources reconcile both upserts and deletes (the delete is a
        tombstone marker for the caller -- never a hard delete here).
        """
        if mode is SyncMode.IMMUTABLE:
            return [change for change in changes if change.op == "upsert"]
        return list(changes)


__all__ = ["BackfillObject", "BackfillPlan", "SourceChange", "SyncEngine", "SyncMode"]
