"""SPEC-034 T-601/T-602 — arcagent wires a WORM-backed policy audit sink.

Proves that agent construction routes policy decisions into a durable,
verifiable WORM chain, and that the personal-tier ALLOW / fail-closed rules
hold at the dispatch context (fields default None until SPEC-038/036/033 fill
them).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arctrust import verify_chain

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.tool_policy import PolicyContext, ToolCall, sign_call


def _config(tmp_path: Path) -> ArcAgentConfig:
    cfg = ArcAgentConfig(
        agent=AgentConfig(name="worm-agent", workspace=str(tmp_path / "ws")),
        llm=LLMConfig(model="test/model"),
        telemetry=TelemetryConfig(enabled=False),
    )
    # SPEC-053 — operator key (audit authority) lives outside the workspace.
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


class TestPolicyWormWiring:
    async def test_denied_evaluation_lands_in_verifiable_worm_chain(self, tmp_path: Path) -> None:
        from arcagent.core.agent import ArcAgent

        agent = ArcAgent(config=_config(tmp_path), config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        pipeline = agent._policy_pipeline
        identity = agent._identity
        assert pipeline is not None and identity is not None

        ctx = PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0)
        # Unsigned call -> IdentityLayer denies -> the wired WORM sink records it.
        decision = await pipeline.evaluate(_unsigned_call(identity.did), ctx)
        assert decision.is_deny()

        path = agent._policy_audit_log_path()
        operator = agent._operator_key
        assert operator is not None
        # SPEC-053 — chain is signed by the OPERATOR key, not the agent DID.
        pub = operator.public_key
        agent_pub = identity.public_key
        await agent.shutdown()  # release the exclusive chain lock

        assert path.exists()
        assert verify_chain(path, pub) is True
        assert verify_chain(path, agent_pub) is False
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
        policy_records = [r for r in records if r["event"]["action"] == "policy.evaluate"]
        assert policy_records, "expected at least one policy.evaluate record"
        assert policy_records[-1]["event"]["outcome"] == "deny"

    async def test_personal_signed_call_allows_with_default_none_state(
        self, tmp_path: Path
    ) -> None:
        """T-602: personal tier relaxes to ALLOW when provider/runtime state is
        absent (the dispatch site leaves the new context fields None)."""
        from arcagent.core.agent import ArcAgent

        agent = ArcAgent(config=_config(tmp_path), config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        pipeline = agent._policy_pipeline
        identity = agent._identity
        assert pipeline is not None and identity is not None

        ctx = PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0)
        assert ctx.provider_usage is None and ctx.tool_runtime is None
        signed = sign_call(_unsigned_call(identity.did), identity)
        decision = await pipeline.evaluate(signed, ctx)
        await agent.shutdown()
        assert decision.outcome == "allow"


@pytest.mark.parametrize("configured", ["custom/audit.jsonl", None])
def test_policy_audit_log_path_resolution(tmp_path: Path, configured: str | None) -> None:
    from arcagent.core.agent import ArcAgent

    config = _config(tmp_path)
    config.security.policy_audit_log = configured
    agent = ArcAgent(config=config, config_path=tmp_path / "arcagent.toml")
    path = agent._policy_audit_log_path()
    if configured is None:
        assert path == agent._workspace / "audit" / "policy-chain.jsonl"
    else:
        assert path == agent._workspace / "custom" / "audit.jsonl"
