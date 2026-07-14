"""SPEC-035 — REAL end-to-end lethal-trifecta proof (no synthetic tags).

The companion ``test_trifecta_dispatch.py`` exercises the ledger→gate *machinery*
with tag-driven legs. This file proves the wiring against REAL leg producers so
the gate is demonstrably LIVE, not merely armed on paper:

  - private_data  ← a real file-read tool (``file_read`` tag)
  - untrusted_input ← a real subprocess/shell tool (``subprocess`` tag — the same
    tag the built-in ``bash`` now carries)
  - external_comms ← a tool that actually routes outbound HTTP through the real
    :class:`EgressProxy` (``_runtime.egress()`` — the previously-dead seam)

The union completes the trifecta on the egress turn, so the arctrust
``GlobalLayer`` FIRES ``global.forbidden_composition`` and arcagent's human gate
engages (pauses; fails closed with no approver, admits exactly one call with an
operator grant).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arctrust.identity import AgentIdentity
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.builtins.capabilities import _runtime
from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    LETHAL_TRIFECTA,
    SessionCapabilityLedger,
    bind_session_id,
    current_session_id,
    reset_session_id,
)
from arcagent.core.tool_policy import PolicyDenied, build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.tools._egress import EgressProxy
from arcagent.tools.human_gate import ApprovalRequest, HumanGate, HumanGateConfig


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()
    yield
    _runtime.reset()


def _build_proxy(ledger: SessionCapabilityLedger) -> tuple[EgressProxy, list[str]]:
    """A real EgressProxy that records the external_comms leg like production."""
    sent: list[str] = []

    async def send_fn(url: str, _method: str, **_: object) -> Any:
        sent.append(url)

        class _Resp:
            status_code = 200

        return _Resp()

    def audit(event: str, _payload: dict[str, Any]) -> None:
        if event == "egress.allowed":
            ledger.record(current_session_id(), frozenset({EXTERNAL_COMMS}))

    proxy = EgressProxy(allowlist={"https://api.example.com"}, send_fn=send_fn, audit_sink=audit)
    return proxy, sent


def _real_tools(workspace: Path) -> list[RegisteredTool]:
    """Three tools that PRODUCE trifecta legs through real mechanisms."""
    (workspace / "secret.txt").write_text("classified\n")

    async def read_exec(**_kw: Any) -> str:
        return (workspace / "secret.txt").read_text()

    async def shell_exec(**_kw: Any) -> str:
        proc = await asyncio.create_subprocess_shell(
            "echo untrusted-output",
            stdout=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
        out, _ = await proc.communicate()
        return out.decode()

    async def egress_exec(**_kw: Any) -> str:
        resp = await _runtime.egress().request("https://api.example.com/exfil", method="POST")
        return f"status={resp.status_code}"

    def _tool(name: str, tags: list[str], fn: Any) -> RegisteredTool:
        return RegisteredTool(
            name=name,
            description=name,
            input_schema={},
            transport=ToolTransport.NATIVE,
            execute=fn,
            source="test",
            classification="state_modifying",
            capability_tags=tags,
        )

    return [
        _tool("read_secret", ["file_read"], read_exec),
        _tool("run_shell", ["subprocess"], shell_exec),
        _tool("do_egress", ["network_egress"], egress_exec),
    ]


def _registry(
    *, workspace: Path, human_gate: HumanGate | None, identity: AgentIdentity
) -> tuple[ToolRegistry, SessionCapabilityLedger, list[str]]:
    ledger = SessionCapabilityLedger()
    proxy, sent = _build_proxy(ledger)
    _runtime.configure(workspace=workspace, egress_proxy=proxy)
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
        capability_ledger=ledger,
        human_gate=human_gate,
    )
    for tool in _real_tools(workspace):
        reg.register(tool)
    return reg, ledger, sent


async def _dispatch(reg: ToolRegistry, name: str, session_id: str) -> Any:
    token = bind_session_id(session_id)
    try:
        wrapped = reg._create_wrapped_execute(reg.tools[name])
        return await wrapped({})
    finally:
        reset_session_id(token)


def _op_signer() -> InProcessSigner:
    return InProcessSigner(bytes(SigningKey.generate()))


@pytest.mark.asyncio
class TestTrifectaRealE2E:
    async def test_egress_tool_really_routes_through_proxy(self, tmp_path: Path) -> None:
        # Proves _runtime.egress() has a LIVE caller: the egress tool, in
        # isolation, actually dispatches through the real EgressProxy.
        identity = AgentIdentity.generate("org", "agent")
        reg, ledger, sent = _registry(workspace=tmp_path, human_gate=None, identity=identity)
        assert await _dispatch(reg, "do_egress", "s") == "status=200"
        assert sent == ["https://api.example.com/exfil"]
        # The real proxy call recorded the external_comms leg for the session.
        assert EXTERNAL_COMMS in ledger.snapshot("s")

    async def test_real_trifecta_fires_the_gate(self, tmp_path: Path) -> None:
        # Read (private_data) + shell (untrusted_input) + egress (external_comms)
        # completes the trifecta with REAL tools → the gate FIRES, no approver → deny.
        identity = AgentIdentity.generate("org", "agent")
        reg, _ledger, sent = _registry(workspace=tmp_path, human_gate=None, identity=identity)
        assert "classified" in await _dispatch(reg, "read_secret", "s")
        assert "untrusted-output" in await _dispatch(reg, "run_shell", "s")
        with pytest.raises(PolicyDenied) as exc:
            await _dispatch(reg, "do_egress", "s")
        assert exc.value.decision.rule_id == "global.forbidden_composition"
        # The completing egress was BLOCKED before it hit the network.
        assert sent == []

    async def test_human_gate_engages_and_admits_one_egress(self, tmp_path: Path) -> None:
        from arctrust.policy import ApprovalGrant, sign_approval_for_hash

        requests: list[ApprovalRequest] = []
        operator = AgentIdentity.generate("operator", "approver")

        async def approve(req: ApprovalRequest) -> ApprovalGrant | None:
            requests.append(req)
            return sign_approval_for_hash(req.call_hash, operator)

        identity = AgentIdentity.generate("org", "agent")
        gate = HumanGate(
            operator_signer=_op_signer(),
            agent_did=identity.did,
            tier="enterprise",
            channel=approve,
        )
        reg, _ledger, sent = _registry(workspace=tmp_path, human_gate=gate, identity=identity)
        await _dispatch(reg, "read_secret", "s")
        await _dispatch(reg, "run_shell", "s")
        # The gate engaged (human asked, agent-labeled) and admitted the one call,
        # which then really egressed through the proxy.
        assert await _dispatch(reg, "do_egress", "s") == "status=200"
        assert requests and requests[0].origin == "agent"
        assert sent == ["https://api.example.com/exfil"]

    async def test_auto_approve_requires_exact_trifecta(self, tmp_path: Path) -> None:
        # A 2-leg auto-approve entry must NOT green-light the full trifecta.
        two_legs = frozenset({"private_data", "external_comms"})
        identity = AgentIdentity.generate("org", "agent")
        gate = HumanGate(
            operator_signer=_op_signer(),
            agent_did=identity.did,
            tier="personal",
            config=HumanGateConfig(auto_approve=[two_legs]),
        )
        reg, _ledger, sent = _registry(workspace=tmp_path, human_gate=gate, identity=identity)
        await _dispatch(reg, "read_secret", "s")
        await _dispatch(reg, "run_shell", "s")
        with pytest.raises(PolicyDenied):
            await _dispatch(reg, "do_egress", "s")
        assert sent == []
