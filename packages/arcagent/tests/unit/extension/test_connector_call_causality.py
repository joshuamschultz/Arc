"""Item 20 P20-4 — a connector call names the connection it reached.

The connection id is refined onto the bound causal context at the attachment
boundary, so the call, its approval verdict and anything the connector audits
carry ``connection_id`` beside the run and tool-call ids. Arguments that claim a
different connection or identity change nothing (battery).
"""

from __future__ import annotations

from typing import Any

from arctrust import causal
from arctrust.audit import AuditEvent
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
from arcagent.tools.human_gate import HumanGate

_AGENT = "did:arc:example:org:agent:abc"
_SEND = ToolSpec(
    name="send_mail",
    description="Send a mail message.",
    classification="state_modifying",
    capability_tags=["network_egress"],
)


class _Recording:
    def __init__(self) -> None:
        self.seen: list[causal.CausalContext | None] = []

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        self.seen.append(causal.current())
        return ProbeResult(reachable=True, tools=[_SEND])

    async def describe_tools(self) -> list[ToolSpec]:
        return [_SEND]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.seen.append(causal.current())
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content="sent")


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _bound(mode: str = "none", sink: _Sink | None = None) -> tuple[Any, _Recording]:
    attachment = _Recording()
    gate = HumanGate(
        operator_signer=InProcessSigner(bytes(SigningKey.generate())),
        agent_did=_AGENT,
        tier="personal",
        channel=None,
    )
    binding = ApprovalBinding(
        instance="work_gmail", agent_did=_AGENT, gate=gate, mode=mode, audit_sink=sink
    )
    return binding.bind(attachment, [_SEND]), attachment


async def test_a_connector_call_inside_a_tool_call_carries_the_connection() -> None:
    gated, attachment = _bound()
    with (
        causal.bind(causal.root("agent", _AGENT, on_behalf_of="did:arc:user:josh")),
        causal.refine(run_id="R", tool_call_id="T"),
    ):
        await gated.invoke("send_mail", {"to": "a@b.c"})

    (ctx,) = attachment.seen
    assert ctx is not None
    assert ctx.connection_id == "work_gmail"
    assert (ctx.run_id, ctx.tool_call_id) == ("R", "T")
    assert (ctx.initiator_id, ctx.on_behalf_of) == (_AGENT, "did:arc:user:josh")


async def test_an_unbound_connector_call_is_the_agent_on_that_connection() -> None:
    gated, attachment = _bound()
    await gated.invoke("send_mail", {"to": "a@b.c"})
    await gated.probe()

    for ctx in attachment.seen:
        assert ctx is not None
        assert (ctx.initiator, ctx.initiator_id) == ("agent", _AGENT)
        assert ctx.connection_id == "work_gmail"


async def test_the_approval_verdict_record_names_the_connection() -> None:
    sink = _Sink()
    gated, _attachment = _bound(mode="all", sink=sink)
    with causal.bind(causal.root("agent", _AGENT)):
        await gated.invoke("send_mail", {"to": "a@b.c"})

    (denied,) = sink.events
    assert denied.action == "connector.approval_denied"
    assert denied.causal is not None
    assert denied.causal.connection_id == "work_gmail"


async def test_forged_connection_and_initiator_in_arguments_are_ignored() -> None:
    """Battery: the model cannot claim another account or principal through args."""
    gated, attachment = _bound()
    forged = {
        "to": "a@b.c",
        "connection_id": "ceo_gmail",
        "causal": {"initiator": "operator", "initiator_id": "did:arc:operator:root"},
    }
    with causal.bind(causal.root("agent", _AGENT)):
        await gated.invoke("send_mail", forged)

    (ctx,) = attachment.seen
    assert ctx is not None
    assert ctx.connection_id == "work_gmail"
    assert (ctx.initiator, ctx.initiator_id) == ("agent", _AGENT)
