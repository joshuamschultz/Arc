"""What a resolved gate or human_input row holds (SPEC-088 D-844, D-845).

A person (or, on a passed deadline, the runner) settles a waiting row by
writing ``status`` and ``output`` in one conditional update. ``output`` is the
one envelope every reader uses, so downstream references read
``$nodes.<id>.output.payload.<field>`` and the runner reads the decision from
the same place. There is no second copy of the decision on the row.

Also here: what the runner stamps on a human-step row at materialization, so
the control plane validates a payload without re-reading bundle files and the
deadline sweep needs nothing but the row.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from arcstore.tasks import Task

from .runner_contracts import HumanInputNodeSpec, HumanStepSpec, NodeSpec
from .schema_check import SchemaRefusedError, load_schema

Decision = Literal["approved", "rejected", "returned_for_revision", "submitted"]

#: What a decider may choose on each human-step kind, mapped to the decision
#: recorded in the envelope. ``submit`` is human_input only; revision is gate only.
DECISIONS: Mapping[str, Mapping[str, Decision]] = {
    "gate": {
        "approve": "approved",
        "fail_run": "rejected",
        "return_for_revision": "returned_for_revision",
    },
    "human_input": {"submit": "submitted", "fail_run": "rejected"},
}

#: Every decision word any human step accepts, for the refusal's admissible list.
DECISION_WORDS: tuple[str, ...] = tuple(sorted({w for words in DECISIONS.values() for w in words}))

#: The decisions that carry the declared payload; the rest record ``payload = {}``.
DATA_DECISIONS: frozenset[str] = frozenset({"approved", "submitted"})

#: The decision a passed ``continue`` deadline records, per kind.
CONTINUE_DECISION: Mapping[str, Decision] = {"gate": "approved", "human_input": "submitted"}

#: The schema a payload meets when the node declares none: any JSON object.
OPEN_PAYLOAD_SCHEMA: Mapping[str, Any] = {"type": "object"}


def outcome_envelope(
    decision: Decision,
    *,
    notes: str,
    payload: Mapping[str, Any],
    decided_by: str,
    on_behalf_of: str | None,
    decided_at: datetime,
    timed_out: bool,
) -> dict[str, Any]:
    """The ``output`` of a settled human step: exactly the seven D-844 keys."""
    return {
        "decision": decision,
        "notes": notes,
        "payload": dict(payload),
        "decided_by": decided_by,
        "on_behalf_of": on_behalf_of,
        "decided_at": decided_at.isoformat(),
        "timed_out": timed_out,
    }


def settled_status(decision: Decision) -> Literal["done", "failed"]:
    """``failed`` only for the outcome that fails the run; the rest settle ``done``."""
    return "failed" if decision == "rejected" else "done"


def recorded_decision(task: Task) -> Decision:
    """What settled the row, read from its envelope.

    A row settled through the plain task-review surface has no envelope; its
    status is the decision then (``done`` approves or submits, ``failed``
    rejects). Revision can only come from the envelope.
    """
    recorded = (task.output or {}).get("decision")
    if recorded in ("approved", "rejected", "returned_for_revision", "submitted"):
        return cast(Decision, recorded)
    if task.status != "done":
        return "rejected"
    return CONTINUE_DECISION.get(str(task.metadata.get("node_kind", "")), "approved")


def recorded_notes(task: Task) -> str:
    """The decider's notes from the envelope; empty when none were written."""
    return str((task.output or {}).get("notes") or "")


def human_step_metadata(node: NodeSpec, bundle_root: str, now: datetime) -> dict[str, Any]:
    """What a waiting human-step row carries beyond the common node metadata.

    ``output_schema_resolved`` is the schema OBJECT read from the verified
    bundle, so a decision is validated without touching files. The deadline is
    absolute, so a runner restarted past it applies it on its first pass.

    Raises:
        SchemaRefusedError: the declared schema is missing, oversized or invalid.
    """
    step = cast(HumanStepSpec, node)
    metadata: dict[str, Any] = {"on_timeout": step.on_timeout, "escalate_to": step.escalate_to}
    if node.kind == "human_input":
        metadata["prompt"] = cast(HumanInputNodeSpec, node).prompt
    if node.output_schema is not None:
        if not bundle_root:
            # Never resolve a schema ref against the process's working directory.
            raise SchemaRefusedError(f"schema {node.output_schema!r}: the bundle root is unknown")
        metadata["output_schema_resolved"] = load_schema(Path(bundle_root), node.output_schema)
    if node.timeout_s is not None:
        metadata["wait_deadline"] = (now + timedelta(seconds=node.timeout_s)).isoformat()
    return metadata


__all__ = [
    "CONTINUE_DECISION",
    "DATA_DECISIONS",
    "DECISIONS",
    "DECISION_WORDS",
    "OPEN_PAYLOAD_SCHEMA",
    "Decision",
    "human_step_metadata",
    "outcome_envelope",
    "recorded_decision",
    "recorded_notes",
    "settled_status",
]
