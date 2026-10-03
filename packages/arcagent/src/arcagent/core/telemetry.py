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
from typing import Any, Protocol

from arctrust.audit import AuditEvent, DurableAuditSink
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, Span

from arcagent.core.config import TelemetryConfig

_NOOP_SPAN = NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)

# Keys matching these patterns are redacted from audit logs
_SENSITIVE_PATTERN = re.compile(
    r"(password|secret|token|key|credential|auth|api_key|private)",
    re.IGNORECASE,
)


class _AuditTelemetry(Protocol):
    def audit_event(self, event_type: str, details: dict[str, Any]) -> None: ...


class TelemetryAuditSink:
    """Adapt typed arctrust audit events to the agent telemetry boundary."""

    def __init__(self, telemetry: _AuditTelemetry) -> None:
        self._telemetry = telemetry

    def write(self, event: AuditEvent) -> None:
        """Forward metadata while letting ``AgentTelemetry`` redact secrets."""
        self._telemetry.audit_event(
            event.action,
            {
                "actor_did": event.actor_did,
                "target": event.target,
                "outcome": event.outcome,
                "classification": event.classification,
                "tier": event.tier,
                "request_id": event.request_id,
                "payload_hash": event.payload_hash,
                **event.extra,
            },
        )

    def emit(self, event: AuditEvent) -> None:
        """Support capability lifecycle objects that call their sink ``emit`` method."""
        self.write(event)


_DURABLE_ACTIONS = frozenset({"secret.write", "secret.delete"})
"""Credential custody changes: always on the signed chain."""


def is_security_event(event: AuditEvent) -> bool:
    """Whether ``event`` belongs on the signed chain, not only in the log.

    A credential that was handed out (``secret.read`` with a value), any change
    to stored credentials, and every refusal. Routine lifecycle and not-found
    reads stay telemetry-only so the ledger is not flooded by polling.
    """
    if event.action in _DURABLE_ACTIONS or event.outcome == "deny":
        return True
    return event.action == "secret.read" and event.outcome == "allow"


class DurableTelemetryAuditSink(TelemetryAuditSink):
    """Telemetry audit plus a durable path into the agent's signed WORM chain.

    ``write`` mirrors every event to telemetry and also appends security events
    (:func:`is_security_event`) to the operator-signed chain, so a credential
    read or a refusal an agent component emits through the single emission
    point is on the ledger. ``write_durable`` is for records an operation must
    not proceed without (an automated promotion decision, the promotion egress
    record): it appends to the chain FIRST and lets a failed append raise, so
    the caller fails closed; only then is the event mirrored to telemetry.
    """

    def __init__(self, telemetry: _AuditTelemetry, chain: DurableAuditSink) -> None:
        super().__init__(telemetry)
        self._chain = chain

    def write(self, event: AuditEvent) -> None:
        """Mirror to telemetry; append security events to the chain (raising on failure)."""
        if is_security_event(event):
            self._chain.write_durable(event)
        super().write(event)

    def write_durable(self, event: AuditEvent) -> None:
        """Append ``event`` to the signed chain (raising on failure), then mirror it."""
        self._chain.write_durable(event)
        super().write(event)


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
            name,
            attributes={"agent.did": self._agent_did, **attributes},
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
