"""Workflow work events: what the runner tells the work-event log (SPEC-088 COMP-008, D-847).

The runner appends four kinds through an injected :class:`WorkEventSink`
(``arcstore.WorkEventLog`` in production): ``gate.waiting`` /
``human_input.waiting`` when a human step starts waiting, ``gate.escalated``
when its deadline escalates, and ``run.finished`` / ``run.failed`` when a run
ends. Event ids are derived from what happened, never minted, so a restarted
runner that re-emits stores nothing twice.

Emission is one-way, like narration (D-538): an unavailable log is logged and
audited, and the run moves on. ``agent_did`` is the workflow's owner agent,
the agent an app's ``events:subscribe`` and ``gates:resolve`` are scoped to.

The callback URL rides on the run input as ``{"intake": {"callback_url": ...}}``;
any other shape means no callback. Admission is not decided here: the
dispatcher re-checks the allowlist and the resolved address before every
delivery (COMP-013).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from arcstore.tasks import Task
from arcstore.work_events import WorkEvent, WorkEventKind

from .runner_contracts import RunRecord

logger = logging.getLogger(__name__)

#: A workflow run has no classification of its own; its rows default to the lowest tier.
RUN_CLASSIFICATION = "UNCLASSIFIED"

_WAITING: Mapping[str, WorkEventKind] = {
    "gate": "gate.waiting",
    "human_input": "human_input.waiting",
}
_ENDED: Mapping[str, WorkEventKind] = {
    "done": "run.finished",
    "done_with_failures": "run.finished",
    "failed": "run.failed",
    "cancelled": "run.failed",
}

AuditFn = Callable[..., None]
"""The runner's ``_audit(action, *, target, outcome, extra=...)``."""


class WorkEventSink(Protocol):
    """Where work events go: insert-if-absent by ``event_id``."""

    async def append(self, event: WorkEvent) -> bool: ...


def callback_url_of(run_input: Mapping[str, Any]) -> str | None:
    """``intake.callback_url`` from the run input when it is a non-empty string, else ``None``."""
    intake = run_input.get("intake")
    if not isinstance(intake, Mapping):
        return None
    url = intake.get("callback_url")
    return url if isinstance(url, str) and url else None


def work_event_id(*parts: str) -> str:
    """sha256 over ``parts`` as a JSON array, so no part can bleed into the next."""
    material = json.dumps(["workflow", *parts], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class RunEvents:
    """Builds and appends one run's work events; a no-op when no sink is wired."""

    def __init__(
        self,
        sink: WorkEventSink | None,
        *,
        clock: Callable[[], datetime],
        owner_of: Callable[[RunRecord], Awaitable[str | None]],
        audit: AuditFn,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._owner_of = owner_of
        self._audit = audit

    async def node_waiting(self, run: RunRecord, task: Task) -> None:
        """``gate.waiting`` / ``human_input.waiting`` for a human-step row."""
        kind = _WAITING.get(str(task.metadata.get("node_kind", "")))
        if kind is not None:
            await self._emit(run, kind, task.id, task=task, status=task.status)

    async def escalated(self, run: RunRecord, task: Task) -> None:
        """``gate.escalated``: one per row, whatever its kind (one escalation step)."""
        await self._emit(run, "gate.escalated", task.id, task=task, status=task.status)

    async def run_ended(self, run: RunRecord, status: str) -> None:
        """``run.finished`` / ``run.failed`` for a run that reached ``status``."""
        kind = _ENDED.get(status)
        if kind is not None:
            await self._emit(run, kind, run.run_id, task=None, status=status)

    async def _emit(
        self, run: RunRecord, kind: WorkEventKind, subject: str, *, task: Task | None, status: str
    ) -> None:
        if self._sink is None:
            return
        try:
            owner = await self._owner_of(run)
            if owner is None:
                raise LookupError(f"workflow {run.workflow_id!r} has no resolvable owner agent")
            event = WorkEvent(
                event_id=work_event_id(kind, subject),
                kind=kind,
                occurred_at=self._clock().isoformat(),
                agent_did=owner,
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                node_id=None if task is None else str(task.metadata.get("node_id", "")),
                task_id=None if task is None else task.id,
                status=status,
                classification=RUN_CLASSIFICATION if task is None else task.classification,
                callback_url=callback_url_of(run.input),
            )
            await self._sink.append(event)
        except Exception as exc:  # reason: notification is one-way; the run must move on
            logger.warning("work event %s for run %s not appended", kind, run.run_id, exc_info=exc)
            self._audit(
                "workflow.work_event.append_failed",
                target=f"{run.workflow_id}/{run.run_id}",
                outcome="degraded",
                extra={"kind": kind, "error": type(exc).__name__},
            )


__all__ = ["RUN_CLASSIFICATION", "RunEvents", "WorkEventSink", "callback_url_of", "work_event_id"]
