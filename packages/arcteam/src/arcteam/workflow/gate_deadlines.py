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

``on_timeout``, ``timeout_s`` and ``escalate_to`` come from the signed
definition node, never the row: the row is only the deadline carrier, so a
forged store write cannot misdirect the alert or block the real escalation.

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
from typing import Any, Literal, cast

from arcstore.tasks import Task
from arcstore.workflow_lease import RunnerFence

from .gate_outcome import CONTINUE_DECISION, outcome_envelope, settled_status
from .narrator import gate_card_text
from .run_events import AuditFn, RunEvents
from .runner_contracts import (
    HUMAN_STEP_KINDS,
    HumanStepSpec,
    RunRecord,
    WorkflowSpec,
    WorkflowTaskStoreLike,
)

TimeoutKind = Literal["fail", "continue", "escalate"]

TellOperator = Callable[[str, str, RunRecord], Awaitable[bool]]
"""The runner's ``_tell_operator(text, idempotency_key, run) -> delivered``."""


@dataclass(frozen=True)
class TimeoutAction:
    """What a passed deadline does to one row, and the deadline it was read at."""

    kind: TimeoutKind
    row: Task
    deadline: str
    step: HumanStepSpec


def _signed_step(definition: WorkflowSpec, row: Task) -> HumanStepSpec | None:
    """The signed human-step node ``row`` was materialized from; None when absent."""
    try:
        node = definition.node_by_id(str(row.metadata.get("node_id", "")))
    except KeyError:
        return None
    return cast(HumanStepSpec, node) if node.kind in HUMAN_STEP_KINDS else None


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
    def due(row: Task, now: datetime, step: HumanStepSpec) -> TimeoutAction | None:
        """The action a passed deadline calls for on ``row``; ``None`` while it holds.

        ``step`` is the signed node the row was materialized from: the only
        source of ``on_timeout``, ``timeout_s`` and ``escalate_to``. Only a
        waiting row of a human step that declares ``timeout_s``, with a readable
        deadline at or before ``now``, is due. An escalation happens once: a row
        already escalated to the signed target fails instead.
        """
        if row.status != "review" or step.kind not in HUMAN_STEP_KINDS or step.timeout_s is None:
            return None
        deadline = _deadline_of(row)
        if deadline is None or now < deadline:
            return None
        kind: TimeoutKind = "fail"
        if step.on_timeout == "continue":
            kind = "continue"
        elif step.on_timeout == "escalate" and step.escalate_to:
            already = row.metadata.get("escalated_to") == step.escalate_to
            kind = "fail" if already else "escalate"
        return TimeoutAction(kind, row, str(row.metadata["wait_deadline"]), step)

    async def sweep(
        self, run: RunRecord, rows: Sequence[Task], now: datetime, definition: WorkflowSpec
    ) -> bool:
        """Apply every due deadline among ``rows``; ``True`` when any row was written.

        ``definition`` is the run's verified definition; a row whose node it
        does not declare as a human step has no signed timeout and is left alone.
        """
        changed = False
        for row in rows:
            step = _signed_step(definition, row)
            action = None if step is None else self.due(row, now, step)
            if action is not None:
                changed |= await self.apply(run, action, now)
        return changed

    async def apply(self, run: RunRecord, action: TimeoutAction, now: datetime) -> bool:
        """Write ``action``; ``False`` when a decision or another runner got there first."""
        if action.kind == "escalate":
            return await self._escalate(run, action, now)
        row = action.row
        decision = CONTINUE_DECISION[action.step.kind] if action.kind == "continue" else "rejected"
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
        # due() only yields "escalate" for a step with timeout_s and escalate_to.
        target = str(action.step.escalate_to)
        timeout_s = float(action.step.timeout_s or 0)
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
            kind=action.step.kind,
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
