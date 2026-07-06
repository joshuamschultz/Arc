"""SPEC-053 T-07/T-08 — arcagent loads the operator key read-only and signs the
policy WORM chain with it, never with the agent DID seed.

The audited subject (the agent) must not hold the audit authority. After this
rewire, the policy chain verifies ONLY under the operator public key; the agent
DID public key — the same key that authenticates its dispatches — must fail.
"""

from __future__ import annotations

from pathlib import Path

from arctrust import OperatorKey, verify_chain

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.tool_policy import PolicyContext, ToolCall


def _config(tmp_path: Path) -> ArcAgentConfig:
    cfg = ArcAgentConfig(
        agent=AgentConfig(name="op-agent", workspace=str(tmp_path / "ws")),
        llm=LLMConfig(model="test/model"),
        telemetry=TelemetryConfig(enabled=False),
    )
    # Operator key lives OUTSIDE the workspace (REQ-004).
    cfg.security.operator_key_dir = str(tmp_path / "operator")
    return cfg


def _unsigned_call(agent_did: str) -> ToolCall:
    return ToolCall(
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        agent_did=agent_did,
        session_id="s1",
        classification="unclassified",
    )


class TestOperatorKeyStartup:
    async def test_operator_key_loaded_outside_workspace(self, tmp_path: Path) -> None:
        from arcagent.core.agent import ArcAgent

        agent = ArcAgent(config=_config(tmp_path), config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        try:
            op = agent._operator_key
            assert isinstance(op, OperatorKey)
            key_path = agent._operator_key_path()
            # personal tier auto-bootstraps a key with zero config (REQ-006).
            assert key_path.exists()
            # The key is not reachable by the workspace-confined file tools (REQ-004).
            assert agent._workspace not in key_path.parents
        finally:
            await agent.shutdown()

    async def test_policy_chain_signed_by_operator_not_agent(self, tmp_path: Path) -> None:
        from arcagent.core.agent import ArcAgent

        agent = ArcAgent(config=_config(tmp_path), config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        pipeline = agent._policy_pipeline
        identity = agent._identity
        operator = agent._operator_key
        assert pipeline is not None and identity is not None and operator is not None

        ctx = PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0)
        decision = await pipeline.evaluate(_unsigned_call(identity.did), ctx)
        assert decision.is_deny()

        path = agent._policy_audit_log_path()
        agent_pub = identity.public_key
        operator_pub = operator.public_key
        await agent.shutdown()

        assert path.exists()
        # Signed by the OPERATOR, not the agent — this is the whole point.
        assert verify_chain(path, operator_pub) is True
        assert verify_chain(path, agent_pub) is False
        assert operator_pub != agent_pub


def test_no_worm_sink_construction_uses_signing_seed() -> None:
    """Grep proof (REQ-002/008): no WormSink(...) in arcagent is fed the agent
    DID seed. The audit authority is always the operator key."""
    src = Path(__file__).resolve().parents[4] / "src" / "arcagent"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if "WormSink(" in line and "signing_seed" in line:
                offenders.append(f"{py.relative_to(src)}:{lineno}: {line.strip()!r}")
    assert not offenders, "WormSink must never be signed with an agent seed:\n" + "\n".join(
        offenders
    )
