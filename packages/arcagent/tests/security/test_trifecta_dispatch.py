"""SPEC-035 REQ-010/012/014 — lethal-trifecta gate MACHINERY through dispatch.

Drives the real ToolRegistry dispatch path + arctrust GlobalLayer with
tag-declared legs (the ledger→policy wiring): read-private (turn 1) then
untrusted-fetch (turn 2) then egress (turn 3) trips the forbidden-composition
gate on turn 3; the human gate either pauses+fails closed (no approver) or
admits exactly one approved call; sessions accumulate independently.

This is the checker/machinery test. The proof that the gate is LIVE against REAL
leg producers — a real file read, a real subprocess, and a real EgressProxy
network call (``_runtime.egress()``) — lives in ``test_trifecta_e2e.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from arctrust.identity import AgentIdentity
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    LETHAL_TRIFECTA,
    SessionCapabilityLedger,
)
from arcagent.core.tool_policy import PolicyDenied, build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.tools.human_gate import ApprovalRequest, HumanGate, HumanGateConfig


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def audit_event(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _tool(name: str, tags: list[str]) -> RegisteredTool:
    async def execute(**_kwargs: Any) -> str:
        return f"{name}-ok"

    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="test",
        classification="read_only",
        capability_tags=tags,
    )


def _registry(*, human_gate: HumanGate | None, identity: AgentIdentity) -> ToolRegistry:
    pipeline = build_pipeline(
        tier="personal",
        agent_registry={identity.did: identity.public_key},
        forbidden_compositions=[LETHAL_TRIFECTA],
    )
    reg = ToolRegistry(
        config=ToolsConfig(),
        bus=ModuleBus(),
        telemetry=_Telemetry(),
        policy_pipeline=pipeline,
        identity=identity,
        tier="personal",
        capability_ledger=SessionCapabilityLedger(),
        human_gate=human_gate,
    )
    reg.register(_tool("reader", ["file_read"]))
    reg.register(_tool("fetch", ["extract"]))
    reg.register(_tool("egress", ["network_egress"]))
    return reg


def _op_signer() -> InProcessSigner:
    return InProcessSigner(bytes(SigningKey.generate()))


async def _dispatch(reg: ToolRegistry, name: str) -> Any:
    wrapped = reg._create_wrapped_execute(reg.tools[name])
    return await wrapped({})


async def _dispatch_in_session(reg: ToolRegistry, name: str, session_id: str) -> Any:
    from arcagent.core.session_internal.capability_ledger import (
        bind_session_id,
        reset_session_id,
    )

    token = bind_session_id(session_id)
    try:
        return await _dispatch(reg, name)
    finally:
        reset_session_id(token)


@pytest.mark.asyncio
class TestTrifectaDispatch:
    async def test_accumulated_trifecta_denies_on_third_call(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        reg = _registry(human_gate=None, identity=identity)
        # Turn 1 + 2 accumulate two legs — allowed.
        assert await _dispatch(reg, "reader") == "reader-ok"
        assert await _dispatch(reg, "fetch") == "fetch-ok"
        # Turn 3 completes the trifecta — denied (no gate → fail closed).
        with pytest.raises(PolicyDenied) as exc:
            await _dispatch(reg, "egress")
        assert exc.value.decision.rule_id == "global.forbidden_composition"

    async def test_isolated_egress_call_is_allowed(self) -> None:
        # The same egress call in isolation (no accumulated legs) is fine.
        identity = AgentIdentity.generate("org", "agent")
        reg = _registry(human_gate=None, identity=identity)
        assert await _dispatch(reg, "egress") == "egress-ok"

    async def test_human_gate_fail_closed_without_channel(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        gate = HumanGate(operator_signer=_op_signer(), agent_did=identity.did, tier="personal")
        reg = _registry(human_gate=gate, identity=identity)
        await _dispatch(reg, "reader")
        await _dispatch(reg, "fetch")
        with pytest.raises(PolicyDenied):
            await _dispatch(reg, "egress")

    async def test_human_gate_auto_approve_admits_the_call(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        gate = HumanGate(
            operator_signer=_op_signer(),
            agent_did=identity.did,
            tier="personal",
            config=HumanGateConfig(auto_approve=[LETHAL_TRIFECTA]),
        )
        reg = _registry(human_gate=gate, identity=identity)
        await _dispatch(reg, "reader")
        await _dispatch(reg, "fetch")
        # Approved one-shot → the completing call proceeds.
        assert await _dispatch(reg, "egress") == "egress-ok"

    async def test_sessions_accumulate_legs_independently(self) -> None:
        # The ledger must be keyed by the real session id, not a process-global
        # "" bucket — two sessions never bleed trifecta legs into each other.
        identity = AgentIdentity.generate("org", "agent")
        reg = _registry(human_gate=None, identity=identity)
        # Session s1 accumulates two legs (private_data + untrusted_input).
        await _dispatch_in_session(reg, "reader", "s1")
        await _dispatch_in_session(reg, "fetch", "s1")
        # Session s2 accumulates only one leg (private_data).
        await _dispatch_in_session(reg, "reader", "s2")
        # s1 completing egress trips the trifecta (it holds the other two legs)…
        with pytest.raises(PolicyDenied):
            await _dispatch_in_session(reg, "egress", "s1")
        # …but s2's egress is fine — it only reaches two legs, not the trifecta.
        assert await _dispatch_in_session(reg, "egress", "s2") == "egress-ok"

    async def test_channel_approval_admits_the_call(self) -> None:
        approvals: list[ApprovalRequest] = []

        async def approve(req: ApprovalRequest) -> bool:
            approvals.append(req)
            return True

        identity = AgentIdentity.generate("org", "agent")
        gate = HumanGate(
            operator_signer=_op_signer(),
            agent_did=identity.did,
            tier="enterprise",
            channel=approve,
        )
        reg = _registry(human_gate=gate, identity=identity)
        await _dispatch(reg, "reader")
        await _dispatch(reg, "fetch")
        assert await _dispatch(reg, "egress") == "egress-ok"
        # The request was labeled agent-originated (ASI09).
        assert approvals and approvals[0].origin == "agent"
