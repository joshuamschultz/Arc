"""SPEC-082 — gaps the door's other tests leave open (test-author flagged).

The Phase-2 tests prove each door piece and prove the federal door *refuses* a
certless request. These cover the payload-hash stability across argument key order
and prove the composed pipeline (verify → allowlist → dispatch → audit) runs end to
end at the ``authorize_and_dispatch`` seam.

The HTTP-wire happy paths this file once drove with a hand-rolled JSON-RPC body
(federal-with-cert *serves*, and a signed ``tools/call`` travels the wire) now go
through the official ``mcp`` SDK transport, so they are proven by a REAL SDK client
in ``test_door_sdk_client.py`` (``test_real_sdk_client_...`` and
``test_call_tool_decision_and_audit_equal_the_native_path``) rather than by posting a
bare body the SDK handshake would reject.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from arcrun import Tool
from arcteam.crypto import new_nonce
from arctrust import AuditEvent, ReplayCache, generate_keypair
from arctrust import identity as arc_identity

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.audit import emit_door_event
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.identity import sign_inbound


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_canonical_payload_hash_is_stable_across_argument_key_order() -> None:
    """Two arg dicts with the same content in different key order hash identically."""
    ordered = _RecordingSink()
    shuffled = _RecordingSink()

    emit_door_event(
        ordered,
        caller_did="did:arc:acme:exec/deadbeef",
        verb="tools/call",
        outcome="allow",
        tier="personal",
        arguments={"a": 1, "b": 2, "c": 3},
    )
    emit_door_event(
        shuffled,
        caller_did="did:arc:acme:exec/deadbeef",
        verb="tools/call",
        outcome="allow",
        tier="personal",
        arguments={"c": 3, "b": 2, "a": 1},
    )

    assert ordered.events[0].payload_hash == shuffled.events[0].payload_hash


@pytest.mark.asyncio
async def test_composed_pipeline_verifies_allowlists_dispatches_and_audits() -> None:
    """A signed, allowlisted call runs the whole pipeline and emits one allow event."""
    keypair = generate_keypair()
    did = arc_identity.did_from_public_key(keypair.public_key, org="acme", agent_type="exec")

    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran search({args})"

    provider = AgentCapabilityProvider(
        tools=[
            Tool(
                name="search",
                description="search",
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier="personal",
        caller_did=did,
    )
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["search"]), tier="personal"
    )
    sink = _RecordingSink()
    content = {"method": "tools/call", "params": {"name": "search", "arguments": {"x": "hi"}}}
    request = sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=keypair.private_key,
        public_key=keypair.public_key,
    )

    result = await authorize_and_dispatch(
        request,
        provider=provider,
        allowlist=allowlist,
        replay_cache=ReplayCache(),
        audit_sink=sink,
        tier="personal",
    )

    assert "ran search" in result.content
    assert result.is_error is False
    allows = [e for e in sink.events if e.outcome == "allow"]
    assert len(allows) == 1
    assert allows[0].action == "mcp.tools/call"
    assert allows[0].actor_did == did
