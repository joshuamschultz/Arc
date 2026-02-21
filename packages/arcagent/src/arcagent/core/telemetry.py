"""Telemetry — OTel spans, structured logging, audit events.

Creates parent spans that ArcLLM's spans auto-nest under via OTel
context propagation. Every action produces an audit event for
compliance (FedRAMP, NIST 800-53 AU family).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, Span

from arcagent.core.config import TelemetryConfig

_NOOP_SPAN = NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)

# Keys matching these patterns are redacted from audit logs
_SENSITIVE_PATTERN = re.compile(
    r"(password|secret|token|key|credential|auth|api_key|private)",
    re.IGNORECASE,
)


class AgentTelemetry:
    """OTel-based telemetry with structured audit logging.

    When enabled, creates real OTel spans. When disabled, all span
    context managers are no-ops but audit_event still logs.
    """

    def __init__(self, config: TelemetryConfig, agent_did: str) -> None:
        self._config = config
        self._agent_did = agent_did
        self._enabled = config.enabled
        self._tracer = trace.get_tracer("arcagent", "0.1.0") if self._enabled else None
        self._audit_logger = logging.getLogger("arcagent.audit")
        self._audit_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    def set_agent_did(self, agent_did: str) -> None:
        """Update agent DID after identity is resolved.

        Avoids reconstructing the entire telemetry instance just
        to update the DID from 'pending' to the real value.
        """
        self._agent_did = agent_did

    @contextlib.asynccontextmanager
    async def _span(self, name: str, attributes: dict[str, Any]) -> AsyncIterator[Span]:
        """Create an OTel span or yield a no-op if disabled."""
        if not self._enabled or self._tracer is None:
            yield _NOOP_SPAN
            return
        with self._tracer.start_as_current_span(
            name, attributes={"agent.did": self._agent_did, **attributes},
        ) as span:
            yield span

    def session_span(self, task: str) -> AbstractAsyncContextManager[Span]:
        """Top-level span: arcagent.session. All turns nest under this."""
        return self._span("arcagent.session", {"agent.task": task})

    def turn_span(self, turn_number: int) -> AbstractAsyncContextManager[Span]:
        """Per-turn span: arcagent.turn. LLM calls nest under this."""
        return self._span("arcagent.turn", {"agent.turn_number": turn_number})

    def tool_span(self, tool_name: str, args: dict[str, Any]) -> AbstractAsyncContextManager[Span]:
        """Per-tool-call span: arcagent.tool."""
        return self._span("arcagent.tool", {"tool.name": tool_name})

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        """Emit structured audit log + span event.

        Always logs (even when OTel spans are disabled) because audit
        trails are a compliance requirement. Sensitive values are
        redacted before logging.
        """
        redacted = _redact_sensitive(details)
        audit_data = {
            "event_type": event_type,
            "agent_did": self._agent_did,
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
