"""UIAuditLogger — structured audit logging + OTel spans for ArcUI.

Follows the AgentTelemetry.audit_event() pattern from arcagent.
Every security-critical action produces both a structured JSON log
and an OTel span event for compliance (FedRAMP, NIST 800-53 AU family).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, WormSink
from opentelemetry import trace
from pydantic import BaseModel

_logger = logging.getLogger("arcui.audit")


class UIAuditEvent(StrEnum):
    """Canonical UI audit event names.

    Every emitter (`AuthMiddleware`, future module) references these by
    name. A new event MUST be added here first —
    string-literal emissions elsewhere are blocked by the
    `tests/unit/test_audit_event_taxonomy.py` regression test.
    """

    SESSION_START = "ui.session_start"
    AGENT_AUTOCONNECT = "ui.agent_autoconnect"
    AUTH_FAILURE = "auth.failure"
    AUTH_SUCCESS = "auth.success"
    AUTH_REJECTED = "auth.rejected"
    # COMP-010: one event name for every UI-originated mutation (memory
    # edit/delete, channel create/membership, workspace-file save). The
    # specific verb rides in the ``operation`` field, so the taxonomy stays
    # stable as mutation routes are added.
    UI_MUTATION = "ui.mutation"


class SessionStartFields(BaseModel):
    """Required fields for `ui.session_start` (SR-3, NIST AU-3).

    Pydantic enforces presence + type at construction; an emitter that
    drops a field gets a validation error instead of a silent audit gap
    that an auditor finds later.

    SPEC-025 §FR-7 — ``username`` resolves the FedRAMP Low audit gate by
    binding every session-start event to a real OS user (NIST AU-3).
    The emitter fills it via ``pwd.getpwuid(uid).pw_name`` on POSIX and
    falls back to ``"<unknown:uid=N>"`` (uid-suffixed) when the lookup
    fails so different uids can never collapse into one audit identity.

    Architecture-review §M-2 — ``username`` is REQUIRED at the model
    boundary. A caller that forgets to populate it gets a ValidationError
    rather than silently emitting a fallback string. The resolver
    (``arcui.auth._resolve_username``) is the only path allowed to
    produce the ``"<unknown:...>"`` fallback.
    """

    session_id: str
    uid: int
    username: str  # REQUIRED — see arcui.auth._resolve_username for fallback
    remote_addr: str
    auth_method: str  # "browser_bootstrap" | "manual_token"


class AgentAutoconnectFields(BaseModel):
    """Required fields for `ui.agent_autoconnect` (SR-3, T5.2)."""

    agent_id: str
    uid: int
    url: str
    reason: str


class MutationAuditFields(BaseModel):
    """Required fields for a UI-originated mutation (COMP-010, NIST AU-3).

    Pydantic enforces presence at construction so a mutation route can never
    silently emit a partial audit record. ``actor_role`` + ``session_id`` bind
    the change to the authenticated operator session; ``target`` names the
    thing changed (agent DID, channel name, memory id); ``operation`` is the
    verb (``channel.create``); ``outcome`` records whether it took effect.
    """

    actor_role: str
    session_id: str
    target: str
    operation: str
    outcome: str  # "applied" | "denied" | "error"
    detail: str = ""


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


_WORM_FILENAME = "audit-chain-arcui.jsonl"


@dataclass(frozen=True)
class MutationWormWriter:
    """Durable, operator-signed WORM record for UI mutations (COMP-010, NIST AU-9).

    The log line + OTel span :class:`UIAuditLogger` emits are ephemeral — they never
    reach the arcui Security screen, which reads the ``audit_chain`` the Observe
    ingest tails out of the shared worm dir. This writer closes that gap: every
    operator control action (task mutation, approval, cancellation, file/skill/
    channel write) is also appended to an Ed25519-signed hash chain the ingest
    picks up, so the mutation is visible AND tamper-evident.

    Holds a long-lived :class:`~arctrust.audit.WormSink` (which keeps an exclusive
    ``flock`` for its lifetime — hence the per-writer ``audit-chain-arcui.jsonl``
    filename, distinct from each agent's own chain) plus the deployment operator
    DID the records are attributed to.
    """

    sink: WormSink
    operator_did: str

    def write(self, fields: MutationAuditFields) -> None:
        """Append one signed, chained record for a mutation. Fail-open (AU-5)."""
        self.sink.write(
            AuditEvent(
                actor_did=self.operator_did,
                action=fields.operation,
                target=fields.target,
                outcome=fields.outcome,
                request_id=fields.session_id,
                extra={"actor_role": fields.actor_role, "detail": fields.detail},
            )
        )


def build_mutation_worm_writer(data_dir: Path) -> MutationWormWriter | None:
    """Build the operator-signed WORM writer for UI mutations, or ``None``.

    Resolves the on-box operator key (the deployment's audit-signing authority, the
    same key the approval/cancellation routes and the ``arc`` CLI sign with) into a
    :class:`~arctrust.audit.WormSink` over ``<data_dir>/worm/audit-chain-arcui.jsonl``
    — the same worm dir the Observe ingest tails, so mutations reach the Security
    screen. Returns ``None`` when the operator key is absent (an uninitialised
    deployment) or the chain file can't be opened, so the server degrades to
    log+OTel rather than minting a signing authority out of nothing.
    """
    from arctrust import OperatorKey, default_operator_key_path
    from arctrust.policy import OperatorApprovalAuthority

    try:
        signer = OperatorKey.load(
            default_operator_key_path(), generate_if_absent=False
        ).into_signer()
    except (OSError, ValueError, RuntimeError):
        _logger.warning("arcui mutation WORM: operator key unavailable; mutations log+OTel only")
        return None
    worm_path = Path(data_dir) / "worm" / _WORM_FILENAME
    try:
        sink = WormSink(worm_path, signer)
    except (OSError, RuntimeError):
        _logger.warning("arcui mutation WORM: could not open %s", worm_path, exc_info=True)
        return None
    return MutationWormWriter(sink=sink, operator_did=OperatorApprovalAuthority(signer).did)


def emit_mutation_audit(
    request: Any,
    *,
    target: str,
    operation: str,
    outcome: str,
    detail: str = "",
) -> None:
    """Single emission point for every UI-originated mutation (COMP-010).

    Resolves the actor (role + session id) from the auth layer's per-request state
    and records one mutation to two independent surfaces: the shared
    ``app.state.audit`` sink (ephemeral log + OTel span) AND, when present, the
    signed ``app.state.audit_worm`` chain the Security screen ingests. Mutation
    routes call this — never ``audit_event`` directly — so actor/target/operation/
    outcome are recorded uniformly. Both surfaces are optional: a bare test app has
    neither, matching the session-start emitter's tolerance.
    """
    role = getattr(request.state, "role", None) or "unknown"
    session_id = getattr(request.state, "session_id", None) or "unknown"
    fields = MutationAuditFields(
        actor_role=role,
        session_id=session_id,
        target=target,
        operation=operation,
        outcome=outcome,
        detail=detail,
    )
    audit = getattr(request.app.state, "audit", None)
    if audit is not None:
        audit.audit_event(UIAuditEvent.UI_MUTATION, fields.model_dump())
    worm = getattr(request.app.state, "audit_worm", None)
    if worm is not None:
        worm.write(fields)


__all__ = [
    "AgentAutoconnectFields",
    "MutationAuditFields",
    "MutationWormWriter",
    "SessionStartFields",
    "UIAuditEvent",
    "UIAuditLogger",
    "build_mutation_worm_writer",
    "emit_mutation_audit",
]
