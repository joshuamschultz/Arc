"""D-577 — a live agent's audit chain is sealed at rest, through the real path.

SPEC-062 D-552 chose FULL capture, which makes the WORM chain the highest-value
file on the box. The mechanism lives in ``arctrust.audit_cipher``; this test
exists because a correct mechanism with dead wiring protects nothing. It drives
a real :class:`~arcagent.core.agent.ArcAgent` through startup, produces a genuine
policy record, and reads the bytes that landed on disk.

The at-rest key is derived from the operator seed the deployment already
custodies, so the assertion also pins that no second secret was invented.
"""

from __future__ import annotations

import json
from pathlib import Path

from arctrust import verify_chain
from arctrust.audit_cipher import SEALED_KEY, RecordCipher, derive_record_key

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.tool_policy import PolicyContext, ToolCall


def _config(tmp_path: Path) -> ArcAgentConfig:
    cfg = ArcAgentConfig(
        agent=AgentConfig(name="sealed-agent", workspace=str(tmp_path / "ws")),
        llm=LLMConfig(model="test/model"),
        telemetry=TelemetryConfig(enabled=False),
    )
    cfg.security.operator_key_dir = str(tmp_path / "operator")
    cfg.security.policy_audit_log = "audit/policy-chain.jsonl"
    return cfg


async def test_policy_records_are_sealed_at_rest_and_still_verify(tmp_path: Path) -> None:
    """The captured content is unreadable on disk; the chain still verifies."""
    from arcagent.core.agent import ArcAgent

    agent = ArcAgent(config=_config(tmp_path), config_path=tmp_path / "arcagent.toml")
    await agent.startup()
    pipeline = agent._policy_pipeline
    identity = agent._identity
    assert pipeline is not None and identity is not None

    # An unsigned call is denied, and the denial reason is captured content.
    decision = await pipeline.evaluate(
        ToolCall(
            tool_name="read_file",
            arguments={"path": "/tmp/x"},
            agent_did=identity.did,
            session_id="s1",
            classification="unclassified",
        ),
        PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0),
    )
    assert decision.is_deny()

    path = agent._policy_audit_log_path()
    operator = agent._operator_key
    assert operator is not None
    await agent.shutdown()

    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    policy = [r for r in records if r["event"]["action"] == "policy.evaluate"]
    assert policy, "expected at least one policy.evaluate record"

    # Envelope in the clear (the store ingest indexes on it), content sealed.
    assert policy[-1]["event"]["outcome"] == "deny"
    assert list(policy[-1]["event"]["extra"]) == [SEALED_KEY]

    # The key comes from the operator seed already under custody — no second secret.
    cipher = RecordCipher(derive_record_key(operator.seed))
    assert cipher.unseal(policy[-1]["event"])["extra"]["rule_id"]

    assert verify_chain(path, operator.public_key) is True
