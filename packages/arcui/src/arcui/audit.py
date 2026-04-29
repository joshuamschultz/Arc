"""UIAuditLogger — structured audit logging + OTel spans for ArcUI.

Follows the AgentTelemetry.audit_event() pattern from arcagent.
Every security-critical action produces both a structured JSON log
and an OTel span event for compliance (FedRAMP, NIST 800-53 AU family).
"""

from __future__ import annotations

import json
import logging
import re
from enum import StrEnum
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel


class UIAuditEvent(StrEnum):
    """Canonical UI audit event names.

    Every emitter (`AuthMiddleware`, `UIReporterModule`, future module)
    references these by name. A new event MUST be added here first —
    string-literal emissions elsewhere are blocked by the
    `tests/unit/test_audit_event_taxonomy.py` regression test.
    """

    SESSION_START = "ui.session_start"
    AGENT_AUTOCONNECT = "ui.agent_autoconnect"
    AUTH_FAILURE = "auth.failure"
    AUTH_SUCCESS = "auth.success"
    AUTH_REJECTED = "auth.rejected"


class SessionStartFields(BaseModel):
    """Required fields for `ui.session_start` (SR-3, NIST AU-3).

    Pydantic enforces presence + type at construction; an emitter that
    drops a field gets a validation error instead of a silent audit gap
    that an auditor finds later.
    """

    session_id: str
    uid: int
    remote_addr: str
    auth_method: str  # "browser_bootstrap" | "manual_token" | "agent_token"


class AgentAutoconnectFields(BaseModel):
    """Required fields for `ui.agent_autoconnect` (SR-3, T5.2)."""

    agent_id: str
    uid: int
    url: str
    reason: str


# Key names that mark a value as sensitive.
#
# `_SENSITIVE_KEY_EXACT`: matched case-insensitively against the WHOLE
# key. Generic names that ONLY make sense as credentials when used alone
# (`token` is sensitive; `auth_method` is a label and must NOT be).
#
# `_SENSITIVE_KEY_PREFIXES`: matched case-insensitively against the
# START of the key. Names with semantic suffixes (`auth_token_v2`,
# `access_token_id`) — anything starting with these is a credential.
#
# Wave 2 simplification: the original consolidated regex had overlapping
# clauses (e.g., `auth_token` appeared in both anchored and prefix
# branches with subtly different meanings). Two collections, two clear
# rules — easier to extend without regression.
_SENSITIVE_KEY_EXACT: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "tokens",
        "key",
        "credential",
        "authorization",
        "auth_token",
        "api_key",
        "private",
    }
)
_SENSITIVE_KEY_PREFIX_PATTERN = re.compile(
    r"^(password|secret|api_key|private_key|auth_token"
    r"|access_token|refresh_token|bearer)",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    """A key is sensitive if it matches an exact name or starts with one of the prefixes."""
    return (
        key.lower() in _SENSITIVE_KEY_EXACT or _SENSITIVE_KEY_PREFIX_PATTERN.match(key) is not None
    )


# Value-side patterns: even when the key name doesn't flag the field as
# sensitive, certain content shapes always leak credentials.
# - `auth=<hex>` from URL hashes (32+ hex chars per SPEC-019 viewer token).
# - `Bearer <token>` from header echoes.
# Catches Wave 1 finding M-3 — error messages or registration payloads
# that incidentally embed a token.
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"auth=[A-Fa-f0-9]{16,}"),
    re.compile(r"[Bb]earer\s+\S+"),
)


def _redact_value(value: Any) -> Any:
    """Replace any embedded credential pattern in a string value with [REDACTED]."""
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """Redact values whose keys OR contents match sensitive patterns.

    Key-name redaction protects fields whose role is to carry a credential.
    Value-content redaction (review M-3) catches stray credentials that
    leak through innocuously-named fields — error messages, URLs, etc.
    Nested dicts are recursed.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            result[key] = "[REDACTED]"
        elif isinstance(value, dict):
            result[key] = _redact_sensitive(value)
        else:
            result[key] = _redact_value(value)
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

    def audit_event(self, event_type: UIAuditEvent | str, details: dict[str, Any]) -> None:
        """Emit structured audit log + OTel span event.

        Always logs (even when OTel spans are disabled) because audit
        trails are a compliance requirement. Sensitive values are
        redacted before logging — both by key-name and by value-content
        pattern (the latter catches stray tokens in error messages).

        `event_type` accepts either a `UIAuditEvent` enum or a raw string;
        the enum is the preferred path. Raw-string emissions are still
        accepted for legacy callers but flagged by the taxonomy test.
        """
        event_name = event_type.value if isinstance(event_type, UIAuditEvent) else event_type
        redacted = _redact_sensitive(details)
        audit_data = {
            "event_type": event_name,
            "details": redacted,
        }

        # Structured log (always)
        self._audit_logger.info(json.dumps(audit_data))

        # Span event (only if enabled and there's an active span)
        if self._enabled:
            current_span = trace.get_current_span()
            if current_span.is_recording():
                current_span.add_event(
                    f"audit:{event_name}",
                    attributes={"audit.details": json.dumps(redacted)},
                )


__all__ = [
    "AgentAutoconnectFields",
    "SessionStartFields",
    "UIAuditEvent",
    "UIAuditLogger",
]
