"""Structured audit logging for arcgateway.

Single helper used by pairing, stream_bridge, and platform adapters so
audit events flow through one shared schema.  Routes through stdlib
logging at INFO level with uniform ``extra`` keys so operators can
extract events into SIEMs without parsing free-form strings.

NIST 800-53 AU-9 (Protection of Audit Information) compliance note:
    This helper is the single emission point for gateway audit events.
    Consolidating here means a future tamper-evident sink (e.g. a
    write-once log ring or OTel log exporter) only needs to be wired
    in one place.

Schema (stable contract — consumed by log aggregators):
    logger.info("AUDIT event=<event> data=<data>", extra={
        "audit_event": <str>,
        "audit_data": <dict>,
    })
"""

from __future__ import annotations

import logging
from typing import Any


def emit_audit(
    logger: logging.Logger,
    event: str,
    data: dict[str, Any],
) -> None:
    """Emit a structured audit log entry.

    Args:
        logger: stdlib logger to emit on.  Callers pass their own so the
            audit record carries the originating module name.
        event: Event name in dotted form, e.g. ``gateway.pairing.minted``.
        data: Structured event payload.  MUST NOT contain raw secrets
            (codes, tokens, DIDs when classification sensitive).  Callers
            are responsible for hashing/redacting before passing in.
    """
    logger.info(
        "AUDIT event=%s data=%s",
        event,
        data,
        extra={"audit_event": event, "audit_data": data},
    )
