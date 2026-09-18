"""SPEC-082 COMP-001/COMP-003 — the door's single audit-emission point.

Every inbound door operation, allow AND deny, emits exactly one
:class:`~arctrust.AuditEvent`. The event carries the caller DID, the verb
(``action="mcp.<verb>"``), the outcome, the tier, and a *hash* of the payload —
never the raw argument values (LLM02/LLM07). The hash is taken over
``arctrust.canonical.canonical_json`` of the arguments so it is deterministic
regardless of key order.

Sink selection is tiered: a tamper-evident :class:`~arctrust.WormSink` at
enterprise/federal, a :class:`~arctrust.NullSink` at personal.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from arctrust import AuditEvent, AuditSink, NullSink, Signer, WormSink, emit
from arctrust.canonical import canonical_json

#: Tiers whose audit trail must be tamper-evident.
_TAMPER_EVIDENT_TIERS = frozenset({"enterprise", "federal"})

#: Fixed audit target for every door operation, so the trail is queryable.
_DOOR_TARGET = "mcp.door"


def _payload_hash(arguments: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of ``arguments`` — order-stable, raw-value free."""
    return hashlib.sha256(canonical_json(arguments)).hexdigest()


def emit_door_event(
    sink: AuditSink,
    *,
    caller_did: str,
    verb: str,
    outcome: str,
    tier: str,
    arguments: dict[str, Any],
) -> None:
    """Emit one audit event for a door operation. Raw args never enter the event."""
    event = AuditEvent(
        actor_did=caller_did,
        action=f"mcp.{verb}",
        target=_DOOR_TARGET,
        outcome=outcome,
        tier=tier,
        payload_hash=_payload_hash(arguments),
    )
    emit(event, sink)


def select_sink(
    tier: str,
    *,
    worm_path: Path | None = None,
    signer: Signer | None = None,
) -> AuditSink:
    """Return the tier-appropriate audit sink.

    Enterprise/federal require a tamper-evident :class:`WormSink`, so a missing
    ``worm_path`` or ``signer`` fails closed rather than silently degrading to a
    sink that drops the record.
    """
    if tier in _TAMPER_EVIDENT_TIERS:
        if worm_path is None or signer is None:
            raise ValueError(
                f"tier {tier!r} requires a tamper-evident WormSink; "
                "worm_path and signer are both required"
            )
        return WormSink(worm_path, signer)
    return NullSink()


__all__ = ["emit_door_event", "select_sink"]
