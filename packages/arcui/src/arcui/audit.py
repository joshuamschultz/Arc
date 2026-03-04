"""UIAuditLogger — structured audit logging + OTel spans for ArcUI.

Follows the AgentTelemetry.audit_event() pattern from arcagent.
Every security-critical action produces both a structured JSON log
and an OTel span event for compliance (FedRAMP, NIST 800-53 AU family).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from opentelemetry import trace

# Keys matching these patterns are redacted from audit logs
_SENSITIVE_PATTERN = re.compile(
    r"(password|secret|token|key|credential|auth|api_key|private)",
    re.IGNORECASE,
)


def _redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """Redact values whose keys match sensitive patterns.

    Returns a shallow copy with sensitive values replaced by
    ``[REDACTED]``. Nested dicts are recursed.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_PATTERN.search(key):
            result[key] = "[REDACTED]"
        elif isinstance(value, dict):
            result[key] = _redact_sensitive(value)
        else:
            result[key] = value
    return result


class UIAuditLogger:
    """OTel-based audit logger for ArcUI server.

    When OTel is enabled, creates real spans + structured logs.
    When disabled, audit_event still logs for compliance.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._tracer = trace.get_tracer("arcui", "0.1.0") if enabled else None
        self._audit_logger = logging.getLogger("arcui.audit")

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        """Emit structured audit log + OTel span event.

        Always logs (even when OTel spans are disabled) because audit
        trails are a compliance requirement. Sensitive values are
        redacted before logging.
        """
        redacted = _redact_sensitive(details)
        audit_data = {
            "event_type": event_type,
            "details": redacted,
        }

        # Structured log (always)
        self._audit_logger.info(json.dumps(audit_data))

        # Span event (only if enabled and there's an active span)
        if self._enabled:
            current_span = trace.get_current_span()
            if current_span.is_recording():
                current_span.add_event(
                    f"audit:{event_type}",
                    attributes={"audit.details": json.dumps(redacted)},
                )
