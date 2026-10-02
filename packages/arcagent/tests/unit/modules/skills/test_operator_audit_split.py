"""SPEC-053 + W0-skill — the operator is the only signing authority in the skills module.

The AUDIT chain (who-did-what, tamper-evident) is signed by the OPERATOR key and
verifies only under it. Skill content is never signed by the agent DID: the module
holds no agent signer at all; an applied improver change is an operator-signed
anchored revision (see ``tests/integration/test_improver_operator_revisions_w0.py``).
"""

from __future__ import annotations

from pathlib import Path

from arctrust import OperatorKey, verify_chain
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity

from arcagent.modules.skills import _runtime
from arcagent.modules.skills._runtime import _build_worm_sink, _build_writer


def test_audit_chain_uses_the_operator_key(tmp_path: Path) -> None:
    ws = tmp_path / "agent" / "workspace"
    ws.mkdir(parents=True)
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    operator = OperatorKey.generate()
    assert operator.public_key != agent.public_key

    sink = _build_worm_sink(ws, operator.into_signer(), None)
    assert sink is not None
    chain = Path(sink._path)
    sink.write(
        AuditEvent(actor_did=agent.did, action="skill.mutate", target="demo", outcome="allow")
    )
    sink.close()

    assert verify_chain(chain, operator.public_key) is True
    assert verify_chain(chain, agent.public_key) is False


def test_the_module_holds_no_agent_signer() -> None:
    assert not hasattr(_runtime, "_build_signer")
    assert not hasattr(_runtime, "_SidecarSigner")


def test_no_writer_without_an_anchored_authority_or_operator_signer() -> None:
    operator = OperatorKey.generate().into_signer()
    assert _build_writer(None, operator, lambda _name: None) is None
    assert _build_writer(object(), operator, lambda _name: None) is None
