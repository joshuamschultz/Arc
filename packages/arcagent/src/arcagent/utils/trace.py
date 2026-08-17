"""Record an implicit agent operation as a tool_event so a run's trace shows it.

Not everything an agent does is a model-issued tool call. A memory recall runs
while the prompt is assembled; a notification is a delivery the agent attempts on
the model's behalf. Neither passes through the loop's tool dispatch, so neither
appears in a run's timeline — which is how a memory lookup that clearly happened
was nowhere to be seen, and a send that failed looked like it never occurred.

Recording them as ``tool_event`` records — the very shape a real tool writes —
makes them render inline with the reads and the model call. Correlation is free:
the ambient run ``request_id`` (bound by the dispatcher around the whole turn) is
inherited by any spool record that does not carry its own, so an implicit op
lands in the right run's timeline on every surface that opens one — a chat turn,
a workflow stage, a dynamic script, a spawned child.
"""

from __future__ import annotations

import hashlib
from typing import Any

from arcstore.records import SpoolRecord
from arcstore.spool import current_request_id
from arcstore.spool import record as _spool


def _digest(text: str) -> tuple[str, int]:
    """The content digest + byte size a tool_event carries (never the body)."""
    data = text.encode("utf-8")
    return hashlib.sha256(data).hexdigest(), len(data)


def spool_auto_tool(
    name: str,
    *,
    actor_did: str,
    outcome: str = "ok",
    latency_ms: float | None = None,
    args: str = "",
    result: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record one implicit operation as a start+end ``tool_event`` pair.

    ``request_id`` is left unset so it is inherited from the ambient run context
    (arcstore), which is what threads the pair into the right run's timeline. The
    pair is marked ``implicit`` so a reader can tell an auto-run step from a
    model-issued call. Fail-open: the underlying spool never raises.

    Outside a run (no ambient id) this is a no-op: an implicit op with no run to
    attach to is an orphan no timeline would show, so it is not written at all.
    """
    if current_request_id() is None:
        return
    marker: dict[str, Any] = {"implicit": True, **(extra or {})}
    args_digest, args_size = _digest(args)
    result_digest, result_size = _digest(result)
    _spool(
        SpoolRecord(
            kind="tool_event",
            actor_did=actor_did,
            tool_name=name,
            phase="start",
            args_digest=args_digest,
            args_size=args_size,
            extra=marker,
        )
    )
    _spool(
        SpoolRecord(
            kind="tool_event",
            actor_did=actor_did,
            tool_name=name,
            phase="end",
            outcome=outcome,
            latency_ms=latency_ms,
            result_digest=result_digest,
            result_size=result_size,
            extra=marker,
        )
    )


__all__ = ["spool_auto_tool"]
