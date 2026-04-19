"""Audit helper with exception swallowing — never break the calling path.

Many modules need to emit audit events without letting a telemetry
failure cascade into the business operation (e.g. a memory-read veto
must still be enforced even if the audit sink is unavailable).

This helper consolidates the three-line try/except pattern that
previously lived inline in:

    arcagent.modules.voice.voice_module._emit_audit
    arcagent.modules.memory_acl.memory_acl_module._emit_veto_audit
    arcagent.modules.memory_acl.memory_acl_module._emit_cross_session_read_audit

NIST 800-53 AU-5 (Response to Audit Processing Failures):
    Audit failures are logged locally at WARNING level but never
    allowed to propagate to the caller.  This matches the compliance
    intent: the system continues to operate while the failure is
    recorded for operator review.

Typing note:
    ``telemetry`` is typed as ``Any`` because ``AgentTelemetry`` lives
    in ``arcagent.core.telemetry`` and we want this helper importable
    from modules that cannot hard-depend on core at type-check time.
"""

from __future__ import annotations

import logging
from typing import Any

_default_logger = logging.getLogger("arcagent.utils.audit")


async def safe_audit(
    telemetry: Any | None,
    event: str,
    payload: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Emit an audit event without letting failures propagate.

    Args:
        telemetry: ``AgentTelemetry``-compatible instance exposing
            ``audit_event(event_type, details)``.  When None the call
            is a no-op (optional debug log on the provided logger).
        event: Audit event name (dotted), e.g. ``voice.transcribed``.
        payload: Structured event payload.  Callers are responsible
            for redacting secrets before passing in.
        logger: Optional logger for the warn-on-failure path.  When
            None, falls back to ``arcagent.utils.audit``.

    Notes:
        - No return value — callers must not depend on success/failure
          of the audit emission.
        - Async signature preserved for forward compatibility with an
          async audit sink (OTel log exporter / NATS sink).  The body
          is currently sync-over-async.
    """
    log = logger if logger is not None else _default_logger

    if telemetry is None:
        log.debug("safe_audit: telemetry unavailable, skipping %s", event)
        return

    try:
        telemetry.audit_event(event, payload)
    except Exception:
        # Audit errors must never propagate and silently break the caller.
        log.warning("safe_audit: failed to emit audit event %s", event, exc_info=True)
