"""SPEC-082 COMP-002 — the door's inbound identity / signature / replay front end.

Before any dispatch, the door verifies a *signed request envelope*: a caller DID,
the caller's Ed25519 public key, and a signature over the ``canonical_json`` of the
request content plus a nonce and timestamp. Verification either returns the caller
DID or fails closed with an audited denial.

No new crypto lives here — it composes arctrust/arcteam seams: ``validate_did``,
``did_matches_pubkey``, ``arctrust.verify`` over ``canonical_json``, and
``arctrust.ReplayCache``. Every anomaly (malformed DID, missing signature,
DID/key mismatch, bad signature, replay, stale timestamp) fails closed and emits a
``deny`` audit event through the single door emission point (:mod:`.audit`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arctrust import AuditSink, ReplayCache, validate_did, verify
from arctrust.canonical import canonical_json
from arctrust.identity import did_matches_pubkey

from arcagent.modules.mcp_server.audit import emit_door_event

#: The verb recorded on an inbound-verification denial.
_INBOUND_VERB = "inbound"


class InboundRejected(Exception):  # noqa: N818 — name is the door's public contract (tests import it)
    """Raised when an inbound request fails verification. Fail-closed."""


@dataclass(frozen=True)
class InboundRequest:
    """A signed inbound request envelope.

    ``signature`` is Ed25519 over ``canonical_json`` of the signed payload
    (``content`` + ``nonce`` + ``ts``); ``public_key`` is the caller's raw key,
    bound to ``caller_did``.
    """

    caller_did: str
    public_key: bytes
    signature: bytes
    content: dict[str, Any]
    nonce: str
    ts: str


def _signing_bytes(content: dict[str, Any], nonce: str, ts: str) -> bytes:
    """The canonical byte string that is signed and verified — order-stable."""
    return canonical_json({"content": content, "nonce": nonce, "ts": ts})


def sign_inbound(
    content: dict[str, Any],
    *,
    nonce: str,
    ts: str,
    caller_did: str,
    private_key: bytes,
    public_key: bytes,
) -> InboundRequest:
    """Build a signed :class:`InboundRequest` — the symmetric signer of the door.

    Mirrors the symmetric ``mcp_attachment`` client: callers sign and verify through
    this module rather than hand-rolling a signature, so the round trip exercises the
    real crypto seam.
    """
    from arctrust import sign

    signature = sign(_signing_bytes(content, nonce, ts), private_key)
    return InboundRequest(
        caller_did=caller_did,
        public_key=public_key,
        signature=signature,
        content=content,
        nonce=nonce,
        ts=ts,
    )


def verify_inbound(
    request: InboundRequest,
    *,
    replay_cache: ReplayCache,
    audit_sink: AuditSink,
    tier: str = "personal",
) -> str:
    """Verify an inbound request, returning the caller DID or failing closed.

    On any anomaly a ``deny`` audit event is emitted and :class:`InboundRejected`
    is raised. The checks run cheapest-first: DID shape, signature presence,
    DID↔key binding, signature validity, then replay/staleness.
    """

    def _deny(reason: str) -> None:
        emit_door_event(
            audit_sink,
            caller_did=request.caller_did,
            verb=_INBOUND_VERB,
            outcome="deny",
            tier=tier,
            arguments=request.content,
        )
        raise InboundRejected(reason)

    try:
        validate_did(request.caller_did)
    except ValueError:
        _deny(f"malformed caller DID: {request.caller_did!r}")

    if not request.signature:
        _deny("unsigned request: empty signature")

    if not did_matches_pubkey(request.caller_did, request.public_key):
        _deny("caller DID is not bound to the presented public key")

    if not verify(
        _signing_bytes(request.content, request.nonce, request.ts),
        request.signature,
        request.public_key,
    ):
        _deny("signature does not verify against the request content")

    if not replay_cache.check_and_record(request.nonce, request.ts):
        _deny("replayed nonce or stale timestamp")

    return request.caller_did


__all__ = [
    "InboundRejected",
    "InboundRequest",
    "sign_inbound",
    "verify_inbound",
]
