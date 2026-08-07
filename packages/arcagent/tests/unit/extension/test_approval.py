"""SPEC-062 T-909 — ``ApprovalBinding``: reads free, outbound gated (COMP-013).

Serves REQ-274 (a per-instance approval setting whose default admits inbound
reads and gates every outbound call, relaxable per connected account) and
REQ-275 (a gated call suspends, presents the instance and the outbound target,
and resumes only on a signed operator grant pinned to the operator identity).

Three shapes of assertion, and the second and third are the load-bearing ones:

* **The gate is REAL.** Every test drives an actual
  :class:`~arcagent.tools.human_gate.HumanGate` with real
  :class:`~arctrust.signer.InProcessSigner` keys, so "routes through the
  mechanical approval path" cannot pass by a binding that merely computes a
  correct-looking predicate and asks nobody. A stubbed gate would make
  ``test_a_foreign_key_grant_is_refused`` unwritable, which is exactly the
  assertion that matters.
* **Denial is proven at the ATTACHMENT, not at the return value.** A binding
  that returns "denied" and still forwards the call has done nothing; the
  attachment doubles here count their invocations for that reason
  ([[feedback_producers_unwired_pattern]]).
* **The presented request must be decidable.** ``sales wants to email
  new@stranger.com`` is a decision; ``sales wants to call gmail.send`` is not.
  ``HumanGate`` presents ``arguments`` as it received them (D-572), so the
  recipient reaches the operator in the arguments themselves.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from arctrust.audit import AuditEvent
from arctrust.policy import (
    ApprovalGrant,
    OperatorApprovalAuthority,
    sign_approval_for_hash,
)
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.extension.approval import ApprovalBinding
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.tools.human_gate import ApprovalRequest, HumanGate

_AGENT_DID = "did:arc:example:org:agent:abc"
_INSTANCE = "sales"
_TARGET = "new@stranger.com"

# A read that pulls data IN. Nothing leaves the machine because of it.
_INBOUND = ToolSpec(
    name="list_threads",
    description="List recent mail threads.",
    classification="read_only",
    capability_tags=["extract"],
)

# A call that pushes agent-chosen content to an agent-chosen sink.
_OUTBOUND = ToolSpec(
    name="send_mail",
    description="Send a mail message.",
    classification="state_modifying",
    capability_tags=["network_egress"],
)

# read_only, yet it egresses: classification alone would wave this through.
_READ_ONLY_EGRESS = ToolSpec(
    name="share_report",
    description="Publish a report to a recipient.",
    classification="read_only",
    capability_tags=["network_egress"],
)


class RecordingSink:
    """Audit sink that keeps every event. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


class CountingAttachment:
    """A real ``ExtensionAttachment`` that records what actually reached it.

    Not a mock: a mock answers any attribute, so an assertion that the call was
    blocked can pass while the binding forwarded it under a different name.
    """

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.probes = 0

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        self.probes += 1
        return ProbeResult(reachable=True, tools=[_INBOUND, _OUTBOUND])

    async def describe_tools(self) -> list[ToolSpec]:
        return [_INBOUND, _OUTBOUND, _READ_ONLY_EGRESS]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.invocations.append((tool, dict(args)))
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content="sent")


class RecordingChannel:
    """An approval channel that records the request and answers as configured."""

    def __init__(self, operator: OperatorApprovalAuthority | None) -> None:
        self._operator = operator
        self.requests: list[ApprovalRequest] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalGrant | None:
        self.requests.append(request)
        if self._operator is None:
            return None
        return sign_approval_for_hash(request.call_hash, self._operator)


def _signer() -> InProcessSigner:
    return InProcessSigner(bytes(SigningKey.generate()))


def _gate(channel: RecordingChannel | None, *, signer: InProcessSigner | None = None) -> HumanGate:
    """A real gate wired to the deployment operator key it pins grants to."""
    return HumanGate(
        operator_signer=signer or _signer(),
        agent_did=_AGENT_DID,
        tier="personal",
        channel=channel,
    )


def _binding(
    gate: HumanGate,
    *,
    mode: str = "outbound",
    sink: RecordingSink | None = None,
) -> ApprovalBinding:
    return ApprovalBinding(
        instance=_INSTANCE,
        agent_did=_AGENT_DID,
        gate=gate,
        mode=mode,
        audit_sink=sink,
    )


def _approving_pair() -> tuple[HumanGate, RecordingChannel]:
    """A gate whose channel returns a grant signed by the pinned operator key."""
    signer = _signer()
    channel = RecordingChannel(OperatorApprovalAuthority(signer))
    return _gate(channel, signer=signer), channel


@pytest.mark.asyncio
class TestApprovalDefault:
    """REQ-274 — inbound reads free, every outbound call gated, by default."""

    async def test_inbound_read_is_not_gated(self) -> None:
        gate, channel = _approving_pair()
        decision = await _binding(gate).authorize(_INBOUND, {"limit": 10})
        assert decision.allowed is True
        assert decision.grant is None
        assert channel.requests == []

    async def test_outbound_call_is_gated(self) -> None:
        gate, channel = _approving_pair()
        decision = await _binding(gate).authorize(_OUTBOUND, {"to": _TARGET})
        assert decision.allowed is True
        assert decision.grant is not None
        assert len(channel.requests) == 1

    async def test_read_only_tool_that_egresses_is_still_gated(self) -> None:
        # classification alone would admit this; the external_comms leg is what
        # makes it outbound, and losing that check silently opens the default.
        gate, channel = _approving_pair()
        decision = await _binding(gate).authorize(_READ_ONLY_EGRESS, {"to": _TARGET})
        assert decision.allowed is True
        assert len(channel.requests) == 1

    async def test_an_undeclared_tool_fails_closed(self) -> None:
        # An upstream that serves a tool the manifest never declared must not be
        # able to buy itself the free-read path by omitting a classification.
        gate, channel = _approving_pair()
        unknown = ToolSpec(name="surprise")
        decision = await _binding(gate).authorize(unknown, {})
        assert decision.allowed is True
        assert len(channel.requests) == 1


@pytest.mark.asyncio
class TestPerInstanceRelaxation:
    """REQ-274 — the operator may relax or tighten one connected account."""

    async def test_mode_none_admits_an_outbound_call(self) -> None:
        gate, channel = _approving_pair()
        decision = await _binding(gate, mode="none").authorize(_OUTBOUND, {"to": _TARGET})
        assert decision.allowed is True
        assert decision.grant is None
        assert channel.requests == []

    async def test_mode_all_gates_an_inbound_read(self) -> None:
        gate, channel = _approving_pair()
        decision = await _binding(gate, mode="all").authorize(_INBOUND, {"limit": 10})
        assert decision.allowed is True
        assert len(channel.requests) == 1

    async def test_an_unknown_mode_is_refused_at_construction(self) -> None:
        gate, _channel = _approving_pair()
        with pytest.raises(ValueError, match="approval mode"):
            _binding(gate, mode="off")


@pytest.mark.asyncio
class TestPresentedRequest:
    """REQ-275 — the request names the instance and the outbound target."""

    async def test_request_names_the_instance(self) -> None:
        gate, channel = _approving_pair()
        await _binding(gate).authorize(_OUTBOUND, {"to": _TARGET})
        assert _INSTANCE in channel.requests[0].tool_name
        assert _OUTBOUND.name in channel.requests[0].tool_name

    async def test_request_names_the_outbound_target_unredacted(self) -> None:
        # The recipient reaches the operator in the arguments the gate was
        # handed. If it did not, the prompt would be undecidable and the whole
        # gate is theatre.
        gate, channel = _approving_pair()
        await _binding(gate).authorize(_OUTBOUND, {"to": _TARGET})
        assert channel.requests[0].arguments["to"] == _TARGET

    async def test_grant_binds_to_the_real_target(self) -> None:
        # Two calls differing only in recipient must not share one approval.
        gate, channel = _approving_pair()
        binding = _binding(gate)
        await binding.authorize(_OUTBOUND, {"to": _TARGET})
        await binding.authorize(_OUTBOUND, {"to": "someone.else@example.com"})
        assert channel.requests[0].call_hash != channel.requests[1].call_hash


@pytest.mark.asyncio
class TestSignedGrantOnly:
    """REQ-275 — resumption requires a signed grant pinned to the operator."""

    async def test_denial_stops_the_call(self) -> None:
        gate = _gate(RecordingChannel(None))
        decision = await _binding(gate).authorize(_OUTBOUND, {"to": _TARGET})
        assert decision.allowed is False
        assert decision.grant is None

    async def test_no_channel_denies(self) -> None:
        gate = _gate(None)
        decision = await _binding(gate).authorize(_OUTBOUND, {"to": _TARGET})
        assert decision.allowed is False

    async def test_a_foreign_key_grant_is_refused(self) -> None:
        # The grant verifies on its own terms — it is a real signature over the
        # real call hash — but it is not the DEPLOYMENT operator's key. Accepting
        # it would let any keypair on the box authorize the agent's egress.
        signer = _signer()
        foreign = OperatorApprovalAuthority(_signer())
        gate = _gate(RecordingChannel(foreign), signer=signer)
        decision = await _binding(gate).authorize(_OUTBOUND, {"to": _TARGET})
        assert decision.allowed is False


@pytest.mark.asyncio
class TestGatedAttachment:
    """The binding is only worth anything if the denied call never lands."""

    async def test_denied_call_never_reaches_the_attachment(self) -> None:
        attachment = CountingAttachment()
        gate = _gate(RecordingChannel(None))
        gated = _binding(gate).bind(attachment, [_INBOUND, _OUTBOUND])

        result = await gated.invoke(_OUTBOUND.name, {"to": _TARGET})

        assert attachment.invocations == []
        assert result.outcome is ToolOutcome.ERROR
        assert _INSTANCE in result.content

    async def test_granted_call_reaches_the_attachment(self) -> None:
        attachment = CountingAttachment()
        gate, _channel = _approving_pair()
        gated = _binding(gate).bind(attachment, [_INBOUND, _OUTBOUND])

        result = await gated.invoke(_OUTBOUND.name, {"to": _TARGET})

        assert attachment.invocations == [(_OUTBOUND.name, {"to": _TARGET})]
        assert result.outcome is ToolOutcome.OK

    async def test_inbound_read_reaches_the_attachment_unasked(self) -> None:
        attachment = CountingAttachment()
        gate, channel = _approving_pair()
        gated = _binding(gate).bind(attachment, [_INBOUND, _OUTBOUND])

        result = await gated.invoke(_INBOUND.name, {"limit": 10})

        assert attachment.invocations == [(_INBOUND.name, {"limit": 10})]
        assert result.outcome is ToolOutcome.OK
        assert channel.requests == []

    async def test_the_other_three_hook_methods_pass_through(self) -> None:
        # The wrapper must remain a whole ExtensionAttachment; a bind that gates
        # invoke and drops probe would break the loader that holds it.
        attachment = CountingAttachment()
        gate, _channel = _approving_pair()
        gated = _binding(gate).bind(attachment, [_INBOUND, _OUTBOUND])

        assert gated.requirements() == []
        assert (await gated.probe()).reachable is True
        assert attachment.probes == 1
        assert len(await gated.describe_tools()) == 3

    async def test_concurrent_calls_are_each_gated(self) -> None:
        # One approval admits one call. A binding that cached a grant would let
        # the second recipient ride the first operator decision.
        attachment = CountingAttachment()
        gate, channel = _approving_pair()
        gated = _binding(gate).bind(attachment, [_OUTBOUND])

        await asyncio.gather(
            gated.invoke(_OUTBOUND.name, {"to": _TARGET}),
            gated.invoke(_OUTBOUND.name, {"to": "other@example.com"}),
        )

        assert len(channel.requests) == 2
        assert len(attachment.invocations) == 2


@pytest.mark.asyncio
class TestAudit:
    """A control that decides correctly and records nothing is not a control."""

    async def test_a_grant_is_recorded_with_instance_and_target(self) -> None:
        sink = RecordingSink()
        gate, _channel = _approving_pair()
        await _binding(gate, sink=sink).authorize(_OUTBOUND, {"to": _TARGET})

        assert "connector.approval_granted" in sink.actions()
        event = sink.events[-1]
        assert event.extra["instance"] == _INSTANCE
        assert event.extra["outbound_target"] == _TARGET
        assert event.outcome == "allow"

    async def test_a_denial_is_recorded(self) -> None:
        sink = RecordingSink()
        gate = _gate(RecordingChannel(None))
        await _binding(gate, sink=sink).authorize(_OUTBOUND, {"to": _TARGET})

        assert "connector.approval_denied" in sink.actions()
        assert sink.events[-1].outcome == "deny"

    async def test_a_free_read_is_not_audited_as_an_approval(self) -> None:
        sink = RecordingSink()
        gate, _channel = _approving_pair()
        await _binding(gate, sink=sink).authorize(_INBOUND, {"limit": 10})
        assert sink.events == []
