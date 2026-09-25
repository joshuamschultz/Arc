"""SPEC-082 abuse battery — the MCP door refuses and audits every hostile inbound.

Registered in the cross-package adversarial battery
(``tests/run_adversarial_tests.py``). These are *abuse cases*, not TDD unit tests:
each one drives the SHIPPED door (`arcagent.modules.mcp_server`) with an attacker's
envelope and asserts the door **fails closed AND writes a ``deny`` audit event**.
None asserts an abuse succeeds.

Threat coverage (README "Threat surface touched"):
- ASI03 identity/privilege abuse — forged inbound DID not bound to the key;
- ASI07 inter-agent replay — replayed nonce, stale timestamp;
- ASI07 forged control action — unsigned / bad-signature envelope;
- ASI02/LLM06 excessive agency — verb-downgrade past the exposure allowlist;
- ASI04 admission-vs-possession — a signed but unenrolled caller at federal.

Every case carries its own positive control (a valid twin request the door *serves*
or a guard the real crypto still catches), so a door that refused everything — or
one whose deny path stopped auditing — fails this suite rather than passing
vacuously. Teeth are demonstrated by mutation in the T-1106 report.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arcrun import Tool
from arcteam.crypto import new_nonce
from arctrust import AuditEvent, ReplayCache, generate_keypair
from arctrust import identity as arc_identity

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import AllowlistRefused, ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.identity import (
    InboundRejected,
    InboundRequest,
    sign_inbound,
    verify_inbound,
)


class _RecordingSink:
    """An ``arctrust`` ``AuditSink`` that keeps every event for assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def denials(self) -> list[AuditEvent]:
        return [event for event in self.events if event.outcome == "deny"]


def _identity(org: str = "acme", agent_type: str = "exec") -> tuple[str, bytes, bytes]:
    """A fresh (did, public_key, private_key) from real arctrust primitives."""
    keypair = generate_keypair()
    did = arc_identity.did_from_public_key(keypair.public_key, org=org, agent_type=agent_type)
    return did, keypair.public_key, keypair.private_key


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _content(name: str = "read_file", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {"path": "/x"}},
    }


def _provider(
    did: str, *, verb: str = "read_file", tier: str = "personal"
) -> AgentCapabilityProvider:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran {verb}({args})"

    return AgentCapabilityProvider(
        tools=[
            Tool(
                name=verb,
                description=verb,
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier=tier,
        caller_did=did,
    )


# --------------------------------------------------------------------------- #
# Case 1 — Forged inbound DID (ASI03): a caller DID not bound to the key.
# --------------------------------------------------------------------------- #
def test_forged_inbound_did_not_bound_to_pubkey_is_denied_and_audited() -> None:
    """A signature made with key A, presented under a *different* valid DID (bound
    to key B) but carrying key A, must be refused: ``did_matches_pubkey`` fails.

    Positive control: verify_inbound signs the same content correctly and yields
    the caller DID in ``test_signed_call_is_served`` below, so this deny is not a
    door that simply rejects everything.
    """
    did_b, _, _ = _identity(org="beta")
    _, pub_a, priv_a = _identity(org="alpha")
    forged = sign_inbound(
        _content(),
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did_b,
        private_key=priv_a,
        public_key=pub_a,
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(forged, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")
    assert sink.denials(), "a forged DID/key binding must emit a deny audit event"


def test_signed_call_is_served() -> None:
    """Positive control: a well-formed, key-bound, signed envelope verifies.

    Without this, every deny-only assertion above would pass against a door that
    refuses all traffic. This proves the pipeline discriminates good from hostile.
    """
    did, pub, priv = _identity()
    good = sign_inbound(
        _content(),
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=priv,
        public_key=pub,
    )
    sink = _RecordingSink()

    verified = verify_inbound(good, replay_cache=ReplayCache(), audit_sink=sink, tier="personal")

    assert verified == did
    assert not sink.denials(), "a valid signed request must not be denied"


# --------------------------------------------------------------------------- #
# Case 2 — Replayed request (ASI07): the same nonce presented twice.
# --------------------------------------------------------------------------- #
def test_replayed_nonce_second_presentation_is_denied_and_audited() -> None:
    """The first presentation is served; the replay of the same nonce is refused."""
    did, pub, priv = _identity()
    nonce, ts = new_nonce(), _now()
    cache = ReplayCache()

    first = sign_inbound(
        _content(), nonce=nonce, ts=ts, caller_did=did, private_key=priv, public_key=pub
    )
    assert (
        verify_inbound(first, replay_cache=cache, audit_sink=_RecordingSink(), tier="personal")
        == did
    )

    replay = sign_inbound(
        _content(), nonce=nonce, ts=ts, caller_did=did, private_key=priv, public_key=pub
    )
    sink = _RecordingSink()
    with pytest.raises(InboundRejected):
        verify_inbound(replay, replay_cache=cache, audit_sink=sink, tier="personal")
    assert sink.denials(), "a replayed nonce must emit a deny audit event"


# --------------------------------------------------------------------------- #
# Case 3 — Stale timestamp (ASI07): ts outside the replay window.
# --------------------------------------------------------------------------- #
def test_stale_timestamp_outside_replay_window_is_denied_and_audited() -> None:
    """A timestamp far outside the acceptance window is rejected as stale."""
    did, pub, priv = _identity()
    stale_ts = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    stale = sign_inbound(
        _content(),
        nonce=new_nonce(),
        ts=stale_ts,
        caller_did=did,
        private_key=priv,
        public_key=pub,
    )
    sink = _RecordingSink()

    with pytest.raises(InboundRejected):
        verify_inbound(
            stale, replay_cache=ReplayCache(window_seconds=300), audit_sink=sink, tier="personal"
        )
    assert sink.denials(), "a stale timestamp must emit a deny audit event"


# --------------------------------------------------------------------------- #
# Case 4 — Unsigned / bad-signature call (ASI07).
# --------------------------------------------------------------------------- #
def test_unsigned_call_is_denied_and_audited() -> None:
    """An empty signature is refused — at personal and (universally) at federal."""
    did, pub, priv = _identity()
    base = sign_inbound(
        _content(), nonce=new_nonce(), ts=_now(), caller_did=did, private_key=priv, public_key=pub
    )
    unsigned = InboundRequest(
        caller_did=did,
        public_key=pub,
        signature=b"",
        content=base.content,
        nonce=base.nonce,
        ts=base.ts,
    )
    for tier in ("personal", "federal"):
        sink = _RecordingSink()
        with pytest.raises(InboundRejected):
            verify_inbound(unsigned, replay_cache=ReplayCache(), audit_sink=sink, tier=tier)
        assert sink.denials(), f"an unsigned request must be denied and audited at {tier}"


def test_tampered_signature_is_denied_and_audited() -> None:
    """A signature that does not verify against the content is refused (arctrust.verify)."""
    did, pub, priv = _identity()
    base = sign_inbound(
        _content(), nonce=new_nonce(), ts=_now(), caller_did=did, private_key=priv, public_key=pub
    )
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
    assert sink.denials(), "a bad signature must emit a deny audit event"


# --------------------------------------------------------------------------- #
# Case 5 — Verb downgrade / allowlist bypass (ASI02/LLM06): a tools/call for a
# verb outside the exposure allowlist is refused BEFORE dispatch.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unallowlisted_verb_is_refused_before_dispatch_and_audited() -> None:
    """The door exposes only ``read_file``; a signed call for ``delete_everything``
    (a verb the provider could technically run) is refused at the allowlist layer,
    before any dispatch, and a ``deny`` event is written.
    """
    did, pub, priv = _identity()
    provider = _provider(did, verb="delete_everything")
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file"]), tier="personal"
    )
    sink = _RecordingSink()
    request = sign_inbound(
        _content(name="delete_everything", arguments={"target": "/"}),
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=priv,
        public_key=pub,
    )

    with pytest.raises(AllowlistRefused):
        await authorize_and_dispatch(
            request,
            provider=provider,
            allowlist=allowlist,
            replay_cache=ReplayCache(),
            audit_sink=sink,
            tier="personal",
        )

    denials = sink.denials()
    assert denials, "an unallowlisted verb must emit a deny audit event"
    assert all(event.outcome != "allow" for event in sink.events), (
        "the call must be refused BEFORE dispatch — no allow event may be emitted"
    )


# --------------------------------------------------------------------------- #
# Case 6 — Unenrolled caller at federal (ASI04): valid signature, valid DID,
# absent from the enrolled roster → denied (the gap Phase 3 closed).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("enrolled", [None, frozenset(), frozenset({"did:arc:acme:exec/other"})])
async def test_unenrolled_caller_at_federal_is_denied_and_audited(enrolled: Any) -> None:
    """A perfectly-signed, key-bound, allowlisted call from a DID that is not on the
    fleet roster is refused at enterprise/federal — key possession is not admission.
    An empty or None roster enrolls nobody (fail-closed).
    """
    did, pub, priv = _identity()
    provider = _provider(did, tier="federal")
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file"]), tier="federal"
    )
    sink = _RecordingSink()
    request = sign_inbound(
        _content(),
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=priv,
        public_key=pub,
    )

    with pytest.raises(InboundRejected):
        await authorize_and_dispatch(
            request,
            provider=provider,
            allowlist=allowlist,
            replay_cache=ReplayCache(),
            audit_sink=sink,
            tier="federal",
            enrolled=enrolled,
        )

    denials = sink.denials()
    assert denials, "an unenrolled federal caller must emit a deny audit event"
    assert any(event.action == "mcp.enrollment" for event in denials), (
        "the denial must come from the enrollment gate, not a downstream layer"
    )
    assert all(event.outcome != "allow" for event in sink.events), (
        "an unenrolled caller must never reach dispatch"
    )


@pytest.mark.asyncio
async def test_enrolled_caller_at_federal_is_served() -> None:
    """Positive control: the SAME federal envelope, once its DID is on the roster,
    is served end to end — proving Case 6's deny is enrollment, not blanket refusal.
    """
    did, pub, priv = _identity()
    provider = _provider(did, tier="federal")
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file"]), tier="federal"
    )
    sink = _RecordingSink()
    request = sign_inbound(
        _content(),
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=priv,
        public_key=pub,
    )

    result = await authorize_and_dispatch(
        request,
        provider=provider,
        allowlist=allowlist,
        replay_cache=ReplayCache(),
        audit_sink=sink,
        tier="federal",
        enrolled=frozenset({did}),
    )

    assert result.is_error is False
    assert "ran read_file" in result.content
    assert not sink.denials(), "an enrolled, signed, allowlisted call must not be denied"
