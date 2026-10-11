"""The workflows module's typed run entry (SPEC-061 COMP-017 counterpart).

A trigger must dispatch a run *directly*: the scheduler calls this, the control
plane starts the run, and no model is anywhere in the decision to start it. That
is the whole point of a typed schedule action — today's alternative is firing
English at the loop and hoping it calls the right tool, which is not a trigger,
it is a suggestion.

Kept in its own module, deliberately: the scheduler imports exactly this one
function lazily, so the two modules share one narrow, typed seam rather than the
scheduler reaching into the builder-tool surface.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, NamedTuple

from arcagent.modules.workflows import _runtime

_logger = logging.getLogger("arcagent.modules.workflows.run_entry")

ScheduleTriggerKind = Literal["cron", "interval", "manual"]
"""The trigger kinds a typed run entry may name. ``event`` is never one: an event
run starts only through the control plane's ``fire_event`` (SPEC-088 COMP-007)."""

_TRIGGER_KINDS: dict[str, ScheduleTriggerKind] = {
    "cron": "cron",
    "interval": "interval",
    "manual": "manual",
}


class RunTrigger(NamedTuple):
    """What started a run, in the ``(kind, name)`` shape the control plane reads.

    The agent's own value: arcagent never imports the orchestration layer, so it
    names the trigger structurally and the control plane reads ``kind``/``name``.
    """

    kind: ScheduleTriggerKind
    name: str | None = None


async def start_workflow_run(
    workflow_id: str,
    workflow_input: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    trigger_digest: str | None = None,
    trigger_kind: str,
) -> dict[str, Any]:
    """Start a run of ``workflow_id``. Raises on refusal so a caller's breaker sees it.

    ``trigger_kind`` is the schedule entry type (``cron`` / ``interval``) or
    ``manual``; the run records it as ``arc_trigger`` and validates its input
    (D-843). Any other kind (``once``, ``event``, a typo) is refused fail-closed.

    Raising rather than returning an error is deliberate: the scheduler's
    circuit breaker counts consecutive failures, and a swallowed refusal would
    let a permanently broken trigger fire forever without ever tripping it.
    """
    kind = _TRIGGER_KINDS.get(trigger_kind)
    if kind is None:
        raise ValueError(
            f"workflow '{workflow_id}' cannot be started by a {trigger_kind!r} trigger; "
            f"allowed: {', '.join(_TRIGGER_KINDS)}"
        )
    await _runtime.ensure_control_plane()
    st = _runtime.state()
    if st.control_plane is None:
        raise RuntimeError(
            f"workflow control plane unavailable — cannot start a run for '{workflow_id}'"
        )
    result = await st.control_plane.run(
        workflow_id,
        input=workflow_input or {},
        initiator="scheduler",
        actor_did=st.identity.did,
        trigger=RunTrigger(kind),
        run_id=run_id,
        trigger_digest=trigger_digest,
    )
    if not result.ok:
        # The control plane RETURNS refusals; the scheduler's breaker counts
        # raised failures. Translating here is what keeps a permanently broken
        # trigger from firing forever without ever tripping it.
        detail = "; ".join(str(getattr(e, "error", e)) for e in result.errors)
        raise RuntimeError(f"workflow '{workflow_id}' refused to start: {detail}")
    _logger.info("Started workflow run for %s", workflow_id)
    record = result.run
    return {
        "run_id": getattr(record, "run_id", ""),
        "workflow_id": workflow_id,
        "status": str(getattr(record, "status", "")),
    }


__all__ = ["RunTrigger", "ScheduleTriggerKind", "start_workflow_run"]
