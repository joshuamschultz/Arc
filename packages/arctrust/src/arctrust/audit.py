"""Structured audit event schema, sinks, and emit function.

Every security-relevant action in Arc emits an AuditEvent. This module
provides the Pydantic schema, pluggable sinks, and a safe emit() function
that swallows sink failures so auditing never breaks the calling path.

NIST 800-53 AU-2 / AU-9 / AU-11 compliance:
- AuditEvent schema captures actor, action, target, outcome, timestamp.
- JsonlSink provides append-only local log file storage.
- SignedChainSink provides Ed25519-signed tamper-evident event chain.
- NullSink is used in tests and air-gapped evaluation where no sink is needed.
- emit() swallows sink exceptions — AU-5 (audit failure response): log locally
  but never propagate to caller.

Extension pattern:
  Implement a class with ``write(event: AuditEvent) -> None`` and pass it
  to emit(). The AuditSink Protocol ensures type checking without coupling
  to a concrete class.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from io import IOBase
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from arctrust.keypair import sign

_logger = logging.getLogger("arctrust.audit")


# ---------------------------------------------------------------------------
# AuditEvent schema
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """Immutable structured audit event.

    Carries all fields required to answer: who did what to what, when,
    and with what outcome. Optional fields support classification-aware
    and request-correlated deployments.

    Pydantic model — frozen so events cannot be mutated after creation.
    """

    model_config = ConfigDict(frozen=True)

    actor_did: str
    """DID of the entity performing the action."""

    action: str
    """Dotted event name (e.g. ``tool.call``, ``policy.evaluate``)."""

    target: str
    """The resource or tool being acted upon."""

    outcome: str
    """Result: ``allow``, ``deny``, ``error``, or domain-specific value."""

    classification: str | None = None
    """Data classification level (e.g. ``UNCLASSIFIED``, ``SECRET``)."""

    tier: str | None = None
    """Deployment tier (``personal``, ``enterprise``, ``federal``)."""

    request_id: str | None = None
    """Correlation ID for distributed tracing."""

    payload_hash: str | None = None
    """SHA-256 hex of the request payload (tamper evidence, no raw data)."""

    ts: str | None = None
    """ISO 8601 UTC timestamp. Auto-populated if omitted."""

    extra: dict[str, Any] = {}

    @model_validator(mode="after")
    def _set_ts(self) -> AuditEvent:
        # Populate timestamp at creation if not provided.
        # Use object.__setattr__ because the model is frozen.
        if self.ts is None:
            ts = datetime.now(UTC).isoformat()
            object.__setattr__(self, "ts", ts)
        return self


# ---------------------------------------------------------------------------
# AuditSink Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditSink(Protocol):
    """Protocol for audit event sinks.

    Any object with a ``write(event: AuditEvent) -> None`` method satisfies
    this protocol. Type checkers will verify conformance without inheritance.
    """

    def write(self, event: AuditEvent) -> None: ...


# ---------------------------------------------------------------------------
# NullSink — for tests and air-gapped evaluation
# ---------------------------------------------------------------------------


class NullSink:
    """No-op audit sink. Events are discarded immediately.

    Use in tests or evaluation contexts where no audit trail is needed.
    ``records`` is always empty — callers should not depend on it.
    """

    @property
    def records(self) -> list[dict[str, Any]]:
        """Always empty — NullSink discards all events."""
        return []

    def write(self, event: AuditEvent) -> None:
        """Discard the event silently."""


# ---------------------------------------------------------------------------
# JsonlSink — append-only JSONL file sink
# ---------------------------------------------------------------------------


class JsonlSink:
    """Append-only JSONL sink for audit events.

    Each call to ``write()`` serialises the event as a single JSON line
    and appends it to the file. Suitable for compliance-grade local logs;
    combine with ``SignedChainSink`` for tamper-evidence.

    Args:
        destination: Path to the .jsonl file, or a file-like object. When a
            Path is provided the file is opened (and created) in append mode
            so multiple processes / restarts accumulate in the same file.
    """

    def __init__(self, destination: Path | IOBase) -> None:
        self._dest = destination

    def write(self, event: AuditEvent) -> None:
        """Append one JSON line to the destination."""
        line = json.dumps(event.model_dump(), default=str) + "\n"
        if isinstance(self._dest, Path):
            with self._dest.open("a", encoding="utf-8") as fh:
                fh.write(line)
        else:
            self._dest.write(line)


# ---------------------------------------------------------------------------
# SignedChainSink — tamper-evident Ed25519 hash chain
# ---------------------------------------------------------------------------


class SignedChainSink:
    """Ed25519-signed audit event chain for tamper evidence.

    Each event is hashed with the SHA-256 of the previous entry's hash
    (hash chaining), and the hash is signed with the operator's private key.
    Any modification to a stored record breaks the chain — ``verify_chain()``
    returns False.

    This meets NIST AU-9 (Protection of Audit Information): log entries are
    signed so unauthorized modification is detectable.

    Args:
        operator_private_key: 32-byte Ed25519 private key. The corresponding
            public key must be stored out-of-band for chain verification.
    """

    def __init__(self, operator_private_key: bytes) -> None:
        self._private_key = operator_private_key
        self._chain_tip = ""
        self._records: list[dict[str, Any]] = []

    @property
    def chain_tip(self) -> str:
        """SHA-256 hex of the most recently written record hash."""
        return self._chain_tip

    @property
    def records(self) -> list[dict[str, Any]]:
        """Ordered list of signed chain records (mutable for tamper tests)."""
        return self._records

    def write(self, event: AuditEvent) -> None:
        """Append a signed record to the chain."""
        event_json = json.dumps(event.model_dump(), sort_keys=True, default=str)
        event_hash = hashlib.sha256(
            (self._chain_tip + event_json).encode("utf-8")
        ).hexdigest()
        sig = sign(event_hash.encode("utf-8"), self._private_key)
        record: dict[str, Any] = {
            "event": event.model_dump(),
            "prev_hash": self._chain_tip,
            "event_hash": event_hash,
            "signature": sig.hex(),
        }
        self._records.append(record)
        self._chain_tip = event_hash

    def verify_chain(self) -> bool:
        """Verify the integrity of the entire chain.

        Returns True if every record's hash matches the computed value from
        its event data and previous hash. Returns False if any record was
        tampered with.
        """
        prev_hash = ""
        for record in self._records:
            event_json = json.dumps(
                record["event"], sort_keys=True, default=str
            )
            expected_hash = hashlib.sha256(
                (prev_hash + event_json).encode("utf-8")
            ).hexdigest()
            if record.get("event_hash") != expected_hash:
                return False
            prev_hash = expected_hash
        return True


# ---------------------------------------------------------------------------
# emit() — safe dispatch to any sink
# ---------------------------------------------------------------------------


def emit(event: AuditEvent, sink: AuditSink) -> None:
    """Emit an audit event to a sink, swallowing all sink errors.

    Per NIST AU-5 (Response to Audit Processing Failures): the audit system
    must never interrupt the operation being audited. Sink failures are
    logged at WARNING but never re-raised.

    Args:
        event: The AuditEvent to emit.
        sink: Any object with a ``write(event) -> None`` method.
    """
    try:
        sink.write(event)
    except Exception:
        _logger.warning(
            "Audit sink %r raised on event %r — swallowing (AU-5)",
            type(sink).__name__,
            event.action,
            exc_info=True,
        )


__all__ = [
    "AuditEvent",
    "AuditSink",
    "JsonlSink",
    "NullSink",
    "SignedChainSink",
    "emit",
]
