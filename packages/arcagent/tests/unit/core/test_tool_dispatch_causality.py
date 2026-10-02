"""Item 20 — a tool call's signed policy record names the run, the call and the agent.

Before, ``policy.evaluate`` rows carried the agent DID and a session id, and no
run or tool-call id, so the Security screen could not say which run made a
call. Driven through the real registry, the real arctrust pipeline and a real
operator-signed WORM chain; only the tool body is a stub.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import arcrun
from arctrust import AgentIdentity, causal
from arctrust.audit import WormSink, verify_chain, worm_policy_sink
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_policy import build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.identity = AgentIdentity.generate(org="test", agent_type="exec")
        self.operator = generate_keypair()
        self.chain = tmp_path / "audit-chain-olivia.jsonl"
        self.worm = WormSink(self.chain, InProcessSigner(self.operator.private_key))
        pipeline = build_pipeline(
            tier="enterprise",
            agent_registry={self.identity.did: self.identity.public_key},
            audit_sink=worm_policy_sink(self.worm),
        )
        telemetry = MagicMock()
        span = MagicMock()
        span.__aenter__ = AsyncMock(return_value=MagicMock())
        span.__aexit__ = AsyncMock(return_value=False)
        telemetry.tool_span = MagicMock(return_value=span)
        config = ArcAgentConfig(agent=AgentConfig(name="t"), llm=LLMConfig(model="test/model"))
        self.registry = ToolRegistry(
            config=config.tools,
            bus=ModuleBus(),
            telemetry=telemetry,
            policy_pipeline=pipeline,
            identity=self.identity,
            tier="enterprise",
        )
        self.seen: list[causal.CausalContext | None] = []

        async def probe(**_: Any) -> str:
            self.seen.append(causal.current())
            return "ok"

        self.registry.register(
            RegisteredTool(
                name="probe",
                description="probe",
                input_schema={"type": "object"},
                transport=ToolTransport.NATIVE,
                execute=probe,
                classification="read_only",
            )
        )

    async def call(self, run_id: str, call_id: str, args: dict[str, Any] | None = None) -> None:
        (tool,) = self.registry.to_arcrun_tools()
        ctx = arcrun.ToolContext(
            run_id=run_id,
            tool_call_id=call_id,
            turn_number=0,
            event_bus=None,
            cancelled=asyncio.Event(),
        )
        await tool.execute(args or {}, ctx)

    def policy_events(self) -> list[dict[str, Any]]:
        self.worm.close()
        assert verify_chain(self.chain, self.operator.public_key)
        records = [json.loads(line) for line in self.chain.read_text().splitlines()]
        return [r["event"] for r in records if r["event"]["action"] == "policy.evaluate"]


async def test_a_tool_call_in_a_run_records_run_call_and_agent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    await harness.call("run-1", "call-1")

    (event,) = harness.policy_events()
    assert event["actor_did"] == harness.identity.did
    assert event["causal"]["initiator"] == "agent"
    assert event["causal"]["initiator_id"] == harness.identity.did
    assert event["causal"]["run_id"] == "run-1"
    assert event["causal"]["tool_call_id"] == "call-1"


async def test_the_tool_body_runs_inside_the_same_causal_context(tmp_path: Path) -> None:
    """Anything the tool audits (a secret read, a connector probe) inherits the ids."""
    harness = _Harness(tmp_path)
    await harness.call("run-1", "call-1")
    (seen,) = harness.seen
    assert seen is not None
    assert (seen.run_id, seen.tool_call_id) == ("run-1", "call-1")
    assert causal.current() is None


async def test_an_agent_acting_for_a_ui_session_records_on_behalf_of(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    with causal.bind(causal.root("ui_session", "did:arc:user:josh")):
        await harness.call("run-2", "call-9")
    (event,) = harness.policy_events()
    assert event["actor_did"] == harness.identity.did
    assert event["causal"]["initiator"] == "agent"
    assert event["causal"]["on_behalf_of"] == "did:arc:user:josh"


async def test_causal_claims_in_tool_arguments_are_ignored(tmp_path: Path) -> None:
    """LLM01/ASI03: a model cannot attribute its call to someone else via arguments."""
    harness = _Harness(tmp_path)
    forged = {"causal": {"initiator": "operator", "initiator_id": "did:arc:operator:root"}}
    await harness.call("run-1", "call-1", {**forged, "initiator": "operator"})
    (event,) = harness.policy_events()
    assert event["causal"]["initiator"] == "agent"
    assert event["causal"]["initiator_id"] == harness.identity.did
