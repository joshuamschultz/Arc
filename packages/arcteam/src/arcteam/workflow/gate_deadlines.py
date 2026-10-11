"""Gate and human_input wait deadlines (SPEC-088 COMP-009, D-846).

``timeout_s`` on a human step is an absolute deadline stamped on its row at
materialization (``metadata.wait_deadline``). Each runner pass applies the
step's ``on_timeout`` once that deadline has passed:

* ``fail``     -> ``failed``, decision ``rejected``; the run follows ``on_failure``.
* ``continue`` -> ``done``, decision ``approved`` (gate) / ``submitted``
  (human_input), ``payload = {}``.
* ``escalate`` -> the first expiry adds the SIGNED ``escalate_to`` as
  ``metadata.escalated_to`` and sets one new deadline, then pushes a card and a
  ``gate.escalated`` work event; the second expiry fails.

Every write is ``update_if(where status == review AND metadata.wait_deadline ==
<the deadline read>)``. A human decision writes on ``status == review``, so a
decision racing the timeout either lands first (the timeout's write matches
nothing) or loses (its write matches nothing): one decision, never two.

Kept out of ``runner.py`` to hold that file's size; the decision itself
(:meth:`GateDeadlines.due`) is pure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from arcstore.tasks import Task
from arcstore.workflow_lease import RunnerFence

from .gate_outcome import CONTINUE_DECISION, outcome_envelope, settled_status
from .narrator import gate_card_text
from .run_events import AuditFn, RunEvents
from .runner_contracts import HUMAN_STEP_KINDS, RunRecord, WorkflowTaskStoreLike

TimeoutKind = Literal["fail", "continue", "escalate"]

TellOperator = Callable[[str, str, RunRecord], Awaitable[bool]]
"""The runner's ``_tell_operator(text, idempotency_key, run) -> delivered``."""


@dataclass(frozen=True)
class TimeoutAction:
    """What a passed deadline does to one row, and the deadline it was read at."""

    kind: TimeoutKind
    row: Task
    deadline: str


def _deadline_of(row: Task) -> datetime | None:
    raw = row.metadata.get("wait_deadline")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class GateDeadlines:
    """Applies passed human-step deadlines through the same conditional write a decision uses."""

    def __init__(
        self,
        *,
        tasks: WorkflowTaskStoreLike,
        runner_did: str,
        events: RunEvents,
        tell_operator: TellOperator,
        audit: AuditFn,
        fence: Callable[[], RunnerFence | None],
    ) -> None:
        self._tasks = tasks
        self._runner_did = runner_did
        self._events = events
        self._tell_operator = tell_operator
        self._audit = audit
        self._fence = fence

    @staticmethod
    def due(row: Task, now: datetime) -> TimeoutAction | None:
        """The action a passed deadline calls for on ``row``; ``None`` while it holds.

        Only a waiting human-step row with a readable deadline at or before
        ``now`` is due. An escalation happens once: a row already escalated, or
        one with no escalation target, fails instead.
        """
        waiting = row.status == "review"
        if not waiting or str(row.metadata.get("node_kind", "")) not in HUMAN_STEP_KINDS:
            return None
        deadline = _deadline_of(row)
        if deadline is None or now < deadline:
            return None
        on_timeout = row.metadata.get("on_timeout")
        kind: TimeoutKind = "fail"
        if on_timeout == "continue":
            kind = "continue"
        elif on_timeout == "escalate" and row.metadata.get("escalate_to"):
            kind = "fail" if row.metadata.get("escalated_to") else "escalate"
        return TimeoutAction(kind, row, str(row.metadata["wait_deadline"]))

    async def sweep(self, run: RunRecord, rows: Sequence[Task], now: datetime) -> bool:
        """Apply every due deadline among ``rows``; ``True`` when any row was written."""
        changed = False
        for row in rows:
            action = self.due(row, now)
            if action is not None:
                changed |= await self.apply(run, action, now)
        return changed

    async def apply(self, run: RunRecord, action: TimeoutAction, now: datetime) -> bool:
        """Write ``action``; ``False`` when a decision or another runner got there first."""
        if action.kind == "escalate":
            return await self._escalate(run, action, now)
        row = action.row
        node_kind = str(row.metadata.get("node_kind", ""))
        decision = CONTINUE_DECISION[node_kind] if action.kind == "continue" else "rejected"
        envelope = outcome_envelope(
            decision,
            notes="",
            payload={},
            decided_by=self._runner_did,
            on_behalf_of=None,
            decided_at=now,
            timed_out=True,
        )
        patch = {"status": settled_status(decision), "output": envelope, "resolution": "timed out"}
        if await self._write(action, patch) is None:
            return False
        self._audit_row("workflow.gate.timed_out", run, row, action.kind)
        return True

    async def _escalate(self, run: RunRecord, action: TimeoutAction, now: datetime) -> bool:
        row = action.row
        target = str(row.metadata["escalate_to"])
        timeout_s = float(row.metadata.get("timeout_s") or 0)
        metadata = {
            **row.metadata,
            "escalated_to": target,
            "wait_deadline": (now + timedelta(seconds=timeout_s)).isoformat(),
        }
        updated = await self._write(action, {"metadata": metadata})
        if updated is None:
            return False
        self._audit_row("workflow.gate.escalated", run, row, "escalated", escalated_to=target)
        node_id = str(row.metadata.get("node_id", ""))
        text = gate_card_text(
            run.workflow_id,
            run.run_id,
            node_id,
            row.id,
            kind=str(row.metadata.get("node_kind", "")),
            escalated_to=target,
        )
        await self._tell_operator(text, f"workflow-gate:{row.id}:escalated", run)
        await self._events.escalated(run, updated)
        return True

    async def _write(self, action: TimeoutAction, patch: dict[str, Any]) -> Task | None:
        return await self._tasks.update_if(
            action.row.id,
            patch,
            where={"status": "review", "metadata.wait_deadline": action.deadline},
            actor_did=self._runner_did,
            fence=self._fence(),
        )

    def _audit_row(
        self, action: str, run: RunRecord, row: Task, outcome: str, **extra: Any
    ) -> None:
        self._audit(
            action,
            target=f"{run.workflow_id}/{row.metadata.get('node_id', '')}",
            outcome=outcome,
            extra={"run_id": run.run_id, "task_id": row.id, **extra},
        )


__all__ = ["GateDeadlines", "TimeoutAction", "TimeoutKind"]
