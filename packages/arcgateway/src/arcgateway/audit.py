"""arcgateway audit module — canonical audit emission via arctrust.

Provides a thin wrapper around ``arctrust.audit.emit`` so all arcgateway
modules share a single sink configuration rather than each managing their own.

Design:
    A module-level ``_sink`` is initialized to ``NullSink`` (safe default so the
    gateway never crashes if no sink is configured). Operators call
    ``configure_sink(sink)`` at startup to wire a real sink (JsonlSink for
    local compliance, SignedChainSink for tamper-evident federal deployments).

    All arcgateway modules call ``emit_event(actor, action, target, outcome, **extra)``
    rather than constructing AuditEvent directly. This keeps event construction
    central so the schema never drifts across callers.

NIST 800-53 AU-2 / AU-9 compliance:
    Every security-relevant action (pairing lifecycle, runner start/stop, adapter
    auth, delivery, execution) emits an AuditEvent through this module.
    The sink determines where events land (local JSONL, OTel, signed chain).
"""

from __future__ import annotations

import logging
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

_logger = logging.getLogger("arcgateway.audit")

# Module-level sink — NullSink until operator calls configure_sink().
# Using NullSink ensures the gateway never raises if the sink is not wired.
_sink: AuditSink = NullSink()

# Default actor DID for gateway-emitted events.
# Overridable via configure_sink(actor_did=...).
_actor_did: str = "did:arc:gateway:daemon"


def configure_sink(sink: AuditSink, *, actor_did: str | None = None) -> None:
    """Wire an audit sink and optional actor DID for this gateway process.

    Call once at startup before any gateway operations begin. Thread-safe
    for initial configuration; do not reconfigure after startup.

    Args:
        sink: Audit sink implementation (JsonlSink, SignedChainSink, etc.).
        actor_did: DID of this gateway daemon. Defaults to
                   ``did:arc:gateway:daemon``.
    """
    global _sink, _actor_did
    _sink = sink
    if actor_did is not None:
        _actor_did = actor_did
    _logger.info("arcgateway.audit: sink configured as %s", type(sink).__name__)


def emit_event(
    action: str,
    target: str,
    outcome: str,
    *,
    actor_did: str | None = None,
    tier: str | None = None,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a structured audit event via the configured sink.

    Wraps ``arctrust.audit.emit`` so callers do not need to construct
    AuditEvent directly. Swallows all sink errors (AU-5 compliance: the
    audit system must never interrupt the audited operation).

    Args:
        action: Dotted event name, e.g. ``gateway.pairing.minted``.
        target: The resource being acted upon, e.g. ``pairing_code:abc123``.
        outcome: Result: ``allow``, ``deny``, ``error``, or domain value.
        actor_did: Override actor DID for this event. Defaults to gateway DID.
        tier: Deployment tier (``personal``, ``enterprise``, ``federal``).
        request_id: Correlation ID for distributed tracing.
        extra: Additional structured fields included in the event.
    """
    event = AuditEvent(
        actor_did=actor_did or _actor_did,
        action=action,
        target=target,
        outcome=outcome,
        tier=tier,
        request_id=request_id,
        extra=extra or {},
    )
    emit(event, _sink)
