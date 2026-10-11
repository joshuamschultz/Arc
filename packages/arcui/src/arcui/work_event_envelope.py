"""The pointer envelope a work event leaves arcui as (SPEC-088 D-849, Data Model).

Callbacks and the event stream carry ids, kind, status, correlation id,
classification and links only. Results and payloads are fetched through scoped
reads, and the stored ``callback_url`` never leaves the box. Shared by the
callback dispatcher and the stream routes, which are each optional on their own
(REQ-633), so the envelope lives in neither.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from arcstore.work_events import WorkEvent

__all__ = ["POINTER_FIELDS", "pointer_envelope"]

#: The envelope fields copied from the stored event, in wire order.
POINTER_FIELDS: tuple[str, ...] = (
    "event_id",
    "kind",
    "occurred_at",
    "agent_did",
    "workflow_id",
    "run_id",
    "node_id",
    "task_id",
    "correlation_id",
    "status",
    "classification",
)
_WAITING_KINDS = frozenset({"gate.waiting", "human_input.waiting", "gate.escalated"})


def pointer_envelope(event: WorkEvent, *, include_seq: bool = False) -> dict[str, Any]:
    """The pointer-only envelope for ``event``; ``seq`` only when asked (stream, page)."""
    envelope: dict[str, Any] = {name: getattr(event, name) for name in POINTER_FIELDS}
    envelope["links"] = _links(event)
    if include_seq:
        envelope["seq"] = event.seq
    return envelope


def _links(event: WorkEvent) -> dict[str, str]:
    """Relative links to the scoped reads (and the gate route) the event points at."""
    links: dict[str, str] = {}
    if event.run_id:
        run = quote(event.run_id, safe="")
        if event.workflow_id:
            links["run"] = f"/api/workflow-runs/{run}"
        else:
            links["run"] = f"/api/runs/{run}"
            links["timeline"] = f"/api/runs/{run}/timeline"
    if event.task_id and event.workflow_id and event.kind in _WAITING_KINDS:
        links["gate"] = f"/api/workflow-tasks/{quote(event.task_id, safe='')}/gate"
    return links
