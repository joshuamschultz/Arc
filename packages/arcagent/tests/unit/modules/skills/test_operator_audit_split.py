"""SPEC-044 P7 / SPEC-053 — audit chain → OPERATOR key, skill signing → AGENT DID.

Re-establishes the security test deleted with ``modules/skill_improver`` in the new
``modules/skills`` layout (arcskill relocation). The crux of SPEC-053: two different
attestations by two different authorities. The AUDIT chain (who-did-what, tamper-evident)
is signed by the OPERATOR key; the mutated-SKILL sidecar (SPEC-033 D3, provenance) stays
on the AGENT DID. Moving only the audit-chain signer is the whole fix.
"""

from __future__ import annotations

from pathlib import Path

from arctrust import OperatorKey, verify_chain
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity

from arcagent.capabilities.artifact_signing import load_signature
from arcagent.modules.skills._runtime import _build_signer, _build_worm_sink


def test_audit_chain_uses_operator_key_skill_signing_uses_agent_did(tmp_path: Path) -> None:
    ws = tmp_path / "agent" / "workspace"
    ws.mkdir(parents=True)
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    operator = OperatorKey.generate()
    assert operator.public_key != agent.public_key

    # Skill signing (SPEC-033 D3) is pinned to the AGENT DID.
    signer = _build_signer(agent)
    assert signer is not None
    artifact = ws / "SKILL.md"
    artifact.write_text("# skill\n", encoding="utf-8")
    signer.sign(artifact, b"# skill\n")
    manifest = load_signature(artifact)
    assert manifest is not None
    assert manifest.signer_did == agent.did

    # The audit chain is signed by the OPERATOR key, verifiable ONLY under it.
    sink = _build_worm_sink(ws, operator.into_signer(), None)
    assert sink is not None
    chain = Path(sink._path)
    sink.write(
        AuditEvent(actor_did=agent.did, action="skill.mutate", target="demo", outcome="allow")
    )
    sink.close()

    assert verify_chain(chain, operator.public_key) is True
    assert verify_chain(chain, agent.public_key) is False
