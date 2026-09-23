"""SPEC-082 T-1103 (RED) — an external MCP client end to end through the door.

REQ-410/411/412/416/417 · COMP-001/002/003. An external caller whose DID is
ENROLLED (an operator-signed ``arctrust.policy.EnrollmentGrant``, verified by
``verify_enrollment``) may drive the door: ``tools/list`` + ``tools/call``, with
the policy decision and WORM audit record equal to the equivalent native
``AgentCapabilityProvider.invoke``. A caller with a valid DID and a valid
signature but NO enrollment must be refused, as must unsigned and replayed callers.

The door already verifies DID shape, signature, DID↔key binding, replay, and tier
(see ``identity.verify_inbound``), but it has **no enrollment gate** — so today it
admits any correctly-signed caller regardless of enrollment. That gate is the
Phase-3 GREEN work (T-1104); this test defines its contract.

RED map (verified against the current door):

- ``test_unenrolled_valid_caller_is_refused_at_federal`` — HEADLINE, behavioral:
  a valid, signed, unenrolled caller at federal is *admitted* today (the door
  dispatches the tool), so ``authorize_and_dispatch`` does not raise
  ``InboundRejected``. This is the "enrolled-vs-unenrolled is not enforced" RED.
- ``test_enrolled_caller_is_admitted_and_result_matches_native`` and
  ``test_unenrolled_caller_refused_even_when_another_did_is_enrolled`` drive the
  enrollment seam the door lacks (an ``enrolled`` set of operator-verified DIDs);
  they fail today because ``authorize_and_dispatch`` has no enrollment parameter.
- The unsigned / replayed / tools-list tests are green anchors (already enforced),
  scoped to personal tier so they stay stable once the enrollment gate lands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from arcrun import Tool
from arcteam.crypto import new_nonce
from arctrust import AuditEvent, ReplayCache, generate_keypair, sign
from arctrust import identity as arc_identity
from arctrust.policy import sign_enrollment_grant, verify_enrollment

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.identity import InboundRejected, InboundRequest, sign_inbound
from arcagent.modules.mcp_server.server import McpServer

_ORG = "acme"
_TYPE = "exec"
_TOOL = "read_file"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Operator:
    """A minimal deployment operator authority (ApprovalAuthority) over one keypair.

    The DID is derived with the canonical ``operator``/``approver`` labels so
    ``verify_enrollment`` binds a grant to exactly this key.
    """

    def __init__(self) -> None:
        self._kp = generate_keypair()

    @property
    def did(self) -> str:
        return arc_identity.did_from_public_key(
            self._kp.public_key, org="operator", agent_type="approver"
        )

    @property
    def public_key(self) -> bytes:
        return self._kp.public_key

    @property
    def algorithm(self) -> str:
        return "ed25519"

    def sign(self, message: bytes) -> bytes:
        return sign(message, self._kp.private_key)


def _member_did(public_key: bytes) -> str:
    return arc_identity.did_from_public_key(public_key, org=_ORG, agent_type=_TYPE)


def _enrolled(operator: _Operator, member_public_key: bytes) -> frozenset[str]:
    """Mint + verify an operator enrollment for one member, returning the enrolled DID set.

    This is the real ``arctrust`` enrollment path the door's future gate must consult:
    a DID is enrolled only when an operator-signed grant verifies against the operator
    trust-store key.
    """
    did = _member_did(member_public_key)
    grant = sign_enrollment_grant(
        operator=operator,
        did=did,
        handle="@external",
        harness="arcagent",
        member_public_key=member_public_key,
        capabilities=frozenset(),
        clearance="unclassified",
        audit_mode="full",
        not_before=_now(),
        nonce=new_nonce(),
    )
    assert verify_enrollment(
        grant,
        did=did,
        handle="@external",
        harness="arcagent",
        member_public_key=member_public_key,
        operator_public_key=operator.public_key,
    )
    return frozenset({did})


def _provider(did: str, *, tier: str = "federal") -> AgentCapabilityProvider:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran {_TOOL}({args})"

    return AgentCapabilityProvider(
        tools=[
            Tool(
                name=_TOOL,
                description="read a file",
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier=tier,
        caller_did=did,
    )


def _allowlist(tier: str = "federal") -> ExposureAllowlist:
    return ExposureAllowlist.from_config(McpServerConfig(enabled=True, expose=[_TOOL]), tier=tier)


def _signed_call(did: str, kp: Any, args: dict[str, Any] | None = None) -> InboundRequest:
    content = {"method": "tools/call", "params": {"name": _TOOL, "arguments": args or {}}}
    return sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=kp.private_key,
        public_key=kp.public_key,
    )


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.input_schema: dict[str, Any] = {"type": "object", "properties": {}}


class _FakeRegistry:
    def __init__(self, tools: dict[str, _FakeTool]) -> None:
        self.tools = tools


@pytest.mark.asyncio
async def test_unenrolled_valid_caller_is_refused_at_federal() -> None:
    """HEADLINE RED: a valid, signed, but unenrolled caller must be refused at federal.

    Today the door has no enrollment gate, so it admits this caller and dispatches the
    tool — ``authorize_and_dispatch`` does not raise. That is exactly the
    enrolled-vs-unenrolled distinction going unenforced.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)

    with pytest.raises(InboundRejected):
        await authorize_and_dispatch(
            _signed_call(did, kp),
            provider=_provider(did),
            allowlist=_allowlist(),
            replay_cache=ReplayCache(),
            audit_sink=_RecordingSink(),
            tier="federal",
        )


@pytest.mark.asyncio
async def test_enrolled_caller_is_admitted_and_result_matches_native() -> None:
    """RED: an operator-enrolled DID is admitted, and its result equals the native invoke.

    Drives the enrollment seam the door lacks (an ``enrolled`` set of operator-verified
    DIDs). Fails today because ``authorize_and_dispatch`` has no enrollment parameter.
    """
    operator = _Operator()
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    enrolled = _enrolled(operator, kp.public_key)

    native = await _provider(did).invoke(_TOOL, {"path": "/x"}, caller_did=did)

    sink = _RecordingSink()
    result = await authorize_and_dispatch(
        _signed_call(did, kp, {"path": "/x"}),
        provider=_provider(did),
        allowlist=_allowlist(),
        replay_cache=ReplayCache(),
        audit_sink=sink,
        tier="federal",
        enrolled=enrolled,
    )

    assert result.content == native.content
    assert result.is_error == native.is_error
    allows = [event for event in sink.events if event.outcome == "allow"]
    assert len(allows) == 1
    assert allows[0].actor_did == did


@pytest.mark.asyncio
async def test_unenrolled_caller_refused_even_when_another_did_is_enrolled() -> None:
    """RED: enrollment is per-DID — a different valid, signed, unenrolled DID is refused.

    Fails today because the enrollment seam does not exist (no ``enrolled`` parameter).
    """
    operator = _Operator()
    enrolled_kp = generate_keypair()
    enrolled = _enrolled(operator, enrolled_kp.public_key)

    other_kp = generate_keypair()
    other_did = _member_did(other_kp.public_key)

    with pytest.raises(InboundRejected):
        await authorize_and_dispatch(
            _signed_call(other_did, other_kp),
            provider=_provider(other_did),
            allowlist=_allowlist(),
            replay_cache=ReplayCache(),
            audit_sink=_RecordingSink(),
            tier="federal",
            enrolled=enrolled,
        )


@pytest.mark.asyncio
async def test_unsigned_caller_is_refused() -> None:
    """Anchor (already enforced): an empty-signature envelope is refused fail-closed."""
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    content = {"method": "tools/call", "params": {"name": _TOOL, "arguments": {}}}
    unsigned = InboundRequest(
        caller_did=did,
        public_key=kp.public_key,
        signature=b"",
        content=content,
        nonce=new_nonce(),
        ts=_now(),
    )

    with pytest.raises(InboundRejected):
        await authorize_and_dispatch(
            unsigned,
            provider=_provider(did, tier="personal"),
            allowlist=_allowlist("personal"),
            replay_cache=ReplayCache(),
            audit_sink=_RecordingSink(),
            tier="personal",
        )


@pytest.mark.asyncio
async def test_replayed_caller_is_refused() -> None:
    """Anchor (already enforced): a reused nonce is refused on the second call.

    Scoped to personal tier (enrollment not required) so it stays green once T-1104
    adds the federal enrollment gate.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    cache = ReplayCache()
    nonce, ts = new_nonce(), _now()
    content = {"method": "tools/call", "params": {"name": _TOOL, "arguments": {}}}
    request = sign_inbound(
        content,
        nonce=nonce,
        ts=ts,
        caller_did=did,
        private_key=kp.private_key,
        public_key=kp.public_key,
    )

    await authorize_and_dispatch(
        request,
        provider=_provider(did, tier="personal"),
        allowlist=_allowlist("personal"),
        replay_cache=cache,
        audit_sink=_RecordingSink(),
        tier="personal",
    )

    with pytest.raises(InboundRejected):
        await authorize_and_dispatch(
            request,
            provider=_provider(did, tier="personal"),
            allowlist=_allowlist("personal"),
            replay_cache=cache,
            audit_sink=_RecordingSink(),
            tier="personal",
        )


def test_enrolled_caller_can_list_the_allowlisted_catalog() -> None:
    """Anchor: the read-only tools/list surface returns the agent's tool catalog."""
    server = McpServer(_FakeRegistry({_TOOL: _FakeTool(_TOOL)}))  # type: ignore[arg-type]

    page = server.list_tools()

    assert {tool["name"] for tool in page.tools} == {_TOOL}
