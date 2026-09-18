"""SPEC-082 T-1079 (RED) — the door's inbound identity/signature/replay front end.

REQ-412, REQ-413, REQ-414, REQ-417 / COMP-002. Before any dispatch, the MCP door
must verify an inbound *signed request envelope* — a caller DID, the caller's
Ed25519 public key, a signature over the ``canonical_json`` of the request content
plus a nonce and timestamp — and either return the verified caller DID or fail
closed with an audited denial. It reuses arctrust/arcteam seams (``validate_did``,
``did_matches_pubkey``, ``arctrust.verify``, ``arcteam.crypto.ReplayCache``); it
introduces no new crypto.

This test drives ``arcagent.modules.mcp_server.identity`` (``verify_inbound`` +
``sign_inbound`` + ``InboundRequest`` + ``InboundRejected``), which does not exist
yet. The RED is the import: ``No module named 'arcagent.modules.mcp_server.identity'``.
It goes GREEN when T-1080 adds the module.

The intended contract encoded here:
- ``sign_inbound(content, *, nonce, ts, caller_did, private_key, public_key)`` builds
  a signed :class:`InboundRequest` (the symmetric signer, mirroring the symmetric
  ``mcp_attachment`` client). The test never hand-rolls the signature — it signs and
  verifies through the module so the round trip exercises real crypto.
- ``verify_inbound(request, *, replay_cache, audit_sink, tier="personal") -> str``
  returns the caller DID on success, or raises :class:`InboundRejected` (fail-closed)
  AND emits an :class:`~arctrust.AuditEvent` to ``audit_sink`` on every rejection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from arcteam.crypto import ReplayCache, new_nonce
from arctrust import AuditEvent, generate_keypair
from arctrust import identity as arc_identity

from arcagent.modules.mcp_server.identity import (
    InboundRejected,
    InboundRequest,
    sign_inbound,
    verify_inbound,
)

_CONTENT = {"method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/x"}}}


class _RecordingSink:
    """An ``arctrust`` ``AuditSink`` that keeps every event for assertions.

    ``emit(event, sink)`` fans out to ``sink.write(event)``, so recording ``write``
    captures a denial regardless of whether the door emits directly or via ``emit``.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _identity(org: str = "acme", agent_type: str = "exec") -> tuple[str, bytes, bytes]:
    """A fresh (did, public_key, private_key) built from real arctrust primitives."""
    keypair = generate_keypair()
    did = arc_identity.did_from_public_key(
        keypair.public_key, org=org, agent_type=agent_type
    )
    return did, keypair.public_key, keypair.private_key


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _signed(caller_did: str, public_key: bytes, private_key: bytes, *, ts: str, nonce: str) -> InboundRequest:
    return sign_inbound(
        _CONTENT, nonce=nonce, ts=ts, caller_did=caller_did, private_key=private_key, public_key=public_key
    )


def test_valid_signed_request_returns_the_caller_did() -> None:
    """A well-formed, fresh, signed envelope verifies and yields the caller DID."""
    did, pub, priv = _identity()
    request = _signed(did, pub, priv, ts=_now(), nonce=new_nonce())
    sink = _RecordingSink()

    verified = verify_inbound(request, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")

    assert verified == did
    # A clean pass emits no denial.
    assert all(event.outcome != "deny" for event in sink.events)


def test_malformed_did_is_denied_and_audited() -> None:
    """A caller DID that is not a valid DID string fails closed (``validate_did``)."""
    _, pub, priv = _identity()
    request = InboundRequest(
        caller_did="not-a-did",
        public_key=pub,
        signature=_signed(
            arc_identity.did_from_public_key(pub, org="acme", agent_type="exec"),
            pub,
            priv,
            ts=_now(),
            nonce=new_nonce(),
        ).signature,
        content=_CONTENT,
        nonce=new_nonce(),
        ts=_now(),
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(request, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")
    assert any(event.outcome == "deny" for event in sink.events)


def test_did_not_bound_to_public_key_is_denied() -> None:
    """A valid DID that does not match the request's public key fails closed.

    The signature is produced by keypair A, but the envelope claims keypair B's
    (valid) DID while carrying A's public key. ``did_matches_pubkey(B_did, A_pub)``
    is False, so the binding check must reject it.
    """
    did_b, _, _ = _identity(org="beta")
    _, pub_a, priv_a = _identity(org="alpha")
    request = sign_inbound(
        _CONTENT, nonce=new_nonce(), ts=_now(), caller_did=did_b, private_key=priv_a, public_key=pub_a
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(request, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")
    assert any(event.outcome == "deny" for event in sink.events)


def test_unsigned_request_is_denied_and_audited() -> None:
    """An empty signature is rejected at every tier (REQ-413)."""
    did, pub, priv = _identity()
    base = _signed(did, pub, priv, ts=_now(), nonce=new_nonce())
    unsigned = InboundRequest(
        caller_did=did,
        public_key=pub,
        signature=b"",
        content=base.content,
        nonce=base.nonce,
        ts=base.ts,
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(unsigned, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")
    assert any(event.outcome == "deny" for event in sink.events)


def test_wrong_signature_is_denied() -> None:
    """A signature that does not verify against the content is rejected (``arctrust.verify``)."""
    did, pub, priv = _identity()
    base = _signed(did, pub, priv, ts=_now(), nonce=new_nonce())
    corrupted = InboundRequest(
        caller_did=did,
        public_key=pub,
        signature=bytes(base.signature[:-1]) + bytes([base.signature[-1] ^ 0xFF]),
        content=base.content,
        nonce=base.nonce,
        ts=base.ts,
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(corrupted, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")
    assert any(event.outcome == "deny" for event in sink.events)


def test_replayed_nonce_is_denied() -> None:
    """A second request reusing a nonce is rejected by the shared ReplayCache."""
    did, pub, priv = _identity()
    nonce = new_nonce()
    ts = _now()
    cache = ReplayCache()

    first = _signed(did, pub, priv, ts=ts, nonce=nonce)
    assert verify_inbound(first, replay_cache=cache, audit_sink=_RecordingSink(), tier="personal") == did

    replay = _signed(did, pub, priv, ts=ts, nonce=nonce)
    sink = _RecordingSink()
    with pytest.raises(InboundRejected):
        verify_inbound(replay, replay_cache=cache, audit_sink=sink, tier="personal")
    assert any(event.outcome == "deny" for event in sink.events)


def test_stale_timestamp_is_denied() -> None:
    """A timestamp outside the replay window is rejected as stale."""
    did, pub, priv = _identity()
    stale_ts = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    request = _signed(did, pub, priv, ts=stale_ts, nonce=new_nonce())
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(
            request, replay_cache=ReplayCache(window_seconds=300), audit_sink=sink, tier="personal"
        )
    assert any(event.outcome == "deny" for event in sink.events)


def test_unsigned_request_is_denied_at_federal_too() -> None:
    """REQ-413 is universal: federal rejects an unsigned request just as personal does."""
    did, pub, priv = _identity()
    base = _signed(did, pub, priv, ts=_now(), nonce=new_nonce())
    unsigned = InboundRequest(
        caller_did=did,
        public_key=pub,
        signature=b"",
        content=base.content,
        nonce=base.nonce,
        ts=base.ts,
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(unsigned, replay_cache=ReplayCache(), audit_sink=sink, tier="federal")
    assert any(event.outcome == "deny" for event in sink.events)
