"""H-040 §2.2 — native arcagent is ONE implementation of the HarnessAdapter seam."""

from __future__ import annotations

import pytest
from arcteam.harness.protocol import HarnessAdapter, InboundEnvelope, MemberOutput
from arcteam.types import Message
from arctrust.identity import AgentIdentity

from arcgateway.harness import ArcAgentHarness


class _FakeConfigAgent:
    name = "aria"


class _FakeConfig:
    agent = _FakeConfigAgent()


class _FakeAgent:
    """The minimum ArcAgent surface ArcAgentHarness reads for identity/roster."""

    def __init__(self, identity: AgentIdentity) -> None:
        self._identity = identity
        self._config = _FakeConfig()


def test_arcagent_harness_satisfies_the_protocol() -> None:
    identity = AgentIdentity.generate(org="acme", agent_type="executor")
    member = ArcAgentHarness(_FakeAgent(identity))
    assert isinstance(member, HarnessAdapter)


def test_arcagent_harness_reports_native_identity_and_kind() -> None:
    identity = AgentIdentity.generate(org="acme", agent_type="executor")
    member = ArcAgentHarness(_FakeAgent(identity))
    assert member.harness == "arcagent"
    assert member.did == identity.did
    assert member.public_key == identity.public_key
    assert member.handle == "aria"


async def test_arcagent_harness_declares_the_full_native_capability_surface() -> None:
    member = ArcAgentHarness(_FakeAgent(AgentIdentity.generate(org="acme", agent_type="executor")))
    caps = set(await member.capabilities())
    # A native agent keeps every rich arcagent.toml-driven tab (§4).
    assert {"chat", "tools", "skills", "memory", "workflow"} <= caps


async def test_arcagent_harness_dispatch_maps_run_to_member_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """dispatch drives collect(agent.run(...)) and emits MemberOutput, not Delta."""
    identity = AgentIdentity.generate(org="acme", agent_type="executor")

    class _Result:
        content = "native reply"

    class _RunnableAgent(_FakeAgent):
        async def session(self, key: str) -> str:
            return key

        def run(self, text: str, *, session: str):
            return text  # value collect() is asked to reduce (patched below)

    agent = _RunnableAgent(identity)

    async def _fake_collect(_stream: object) -> _Result:
        return _Result()

    import arcgateway.harness as harness_mod

    monkeypatch.setattr(harness_mod.arcrun, "collect", _fake_collect)

    member = ArcAgentHarness(agent)
    envelope = InboundEnvelope(
        message=Message(sender="agent://x", to=["agent://aria"], body="ping"),
        member_did=identity.did,
        session_key="s1",
    )
    outs = [out async for out in member.dispatch(envelope)]
    assert all(isinstance(o, MemberOutput) for o in outs)
    assert outs[0].kind == "text" and outs[0].text == "native reply"
    assert outs[-1].kind == "done" and outs[-1].is_final
