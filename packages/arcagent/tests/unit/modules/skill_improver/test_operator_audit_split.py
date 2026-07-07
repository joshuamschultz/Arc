"""SPEC-053 T-09 — skill-improver: audit chain → operator key, skill signing → agent DID.

The crux of SPEC-053: these are two different attestations by two different
authorities. The AUDIT chain (who-did-what, tamper-evident) is signed by the
OPERATOR key. The mutated-SKILL signature (SPEC-033 D3, artifact provenance)
stays on the AGENT DID. Moving only the audit-chain signer is the whole fix;
moving the skill signer too would be wrong.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from arctrust import OperatorKey, verify_chain
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity

from arcagent.core.config import EvalConfig
from arcagent.modules.skill_improver import _runtime


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "agent" / "workspace"
    ws.mkdir(parents=True)
    return ws


def test_audit_chain_uses_operator_key_skill_signing_uses_agent_did(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    operator = OperatorKey.generate()
    assert operator.public_key != agent.public_key

    try:
        _runtime.configure(
            config={},
            eval_config=EvalConfig(),
            telemetry=MagicMock(),
            workspace=ws,
            skill_registry=None,
            agent_name="a",
            identity=agent,
            operator_signer=operator.into_signer(),
        )
        st = _runtime.state()

        # Skill signing (SPEC-033 D3) stays pinned to the AGENT DID.
        assert st.signer_did == agent.did
        assert st.signing_key == agent.signing_seed

        # The audit chain is signed by the OPERATOR key.
        sink = st.candidate_store._audit_sink
        assert sink is not None
        chain = Path(sink._path)
        sink.write(
            AuditEvent(
                actor_did=agent.did,
                action="skill.mutate",
                target="demo-skill",
                outcome="allow",
            )
        )
        sink.close()

        assert verify_chain(chain, operator.public_key) is True
        assert verify_chain(chain, agent.public_key) is False
    finally:
        _runtime.reset()
