"""Structured audit logging and log-hashing helpers for arcgateway.

Consolidates two cross-cutting patterns used by pairing, stream_bridge,
session routing, and platform adapters:

    * ``emit_audit``      — single structured audit-log emission point.
    * ``hash_user_did``   — truncated SHA-256 of a user DID for safe
      inclusion in log lines (raw DIDs must never appear in log output
      per SDD §4.2 + PRD privacy mandate).

NIST 800-53 AU-3 (Content of Audit Records) / AU-9 (Protection of Audit
Information): centralising both helpers here means a future tamper-
evident sink or privacy-enhanced hash function only needs to be wired
in one place.

Schema for ``emit_audit`` (stable contract — consumed by log
aggregators):

    logger.info("AUDIT event=<event> data=<data>", extra={
        "audit_event": <str>,
        "audit_data": <dict>,
    })
"""

from __future__ import annotations

import hashlib
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


def hash_user_did(user_did: str) -> str:
    """Return SHA-256 first-16 hex chars of a user DID for safe log use.

    Matches the 16-char truncation used by ``pairing._hash_user_id`` so
    both paths hash with the same resolution.  Raw DIDs MUST NOT appear
    in gateway logs (SDD §4.2; NIST AU-3): a raw DID is PII-adjacent and
    can correlate a user across adapters if log aggregators ingest it.

    Args:
        user_did: The raw user DID (may be empty).

    Returns:
        16-char lowercase hex digest of the UTF-8 bytes, or the literal
        string ``"empty"`` when the input is empty.
    """
    if not user_did:
        return "empty"
    return hashlib.sha256(user_did.encode("utf-8")).hexdigest()[:16]
