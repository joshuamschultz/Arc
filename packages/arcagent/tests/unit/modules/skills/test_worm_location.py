"""SPEC-044 P7 / SPEC-033 #5 — WORM audit store lives OUTSIDE the agent workspace.

Re-establishes the deleted ``skill_improver`` security test in the ``modules/skills``
layout. AU-9(2): the tamper-evident chain must not sit where the agent can write
(a compromised agent could truncate/forge its own record); it belongs in the
operator-owned ``.audit`` dir beside the workspace. SI-7(7): a pre-existing chain is
integrity-checked at load and a failure raises an alert event.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from arctrust import WormSink
from arctrust.audit import AuditEvent
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcagent.modules.skills._runtime import _build_worm_sink


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "agent" / "workspace"
    ws.mkdir(parents=True)
    return ws


def test_worm_store_is_outside_workspace(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    key = generate_keypair().private_key
    sink = _build_worm_sink(ws, InProcessSigner(key), MagicMock())
    assert sink is not None
    try:
        chain = Path(sink._path)
        assert ws not in chain.parents, "audit chain must not live under the workspace"
        assert not (ws / ".audit").exists()
    finally:
        sink.close()


def test_tampered_chain_emits_alert_at_load(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    key = generate_keypair().private_key
    telemetry = MagicMock()

    # Seed a real, valid chain with two records under the operator .audit dir.
    chain = ws.parent / ".audit" / "skills.worm"
    seed = WormSink(chain, InProcessSigner(key))
    for i in range(2):
        seed.write(
            AuditEvent(
                actor_did="did:arc:test",
                action="skill.mutate",
                target=f"skill-{i}",
                outcome="allow",
            )
        )
    seed.close()

    # Tamper the FIRST record so the hash chain / signature no longer verifies.
    lines = chain.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("skill-0", "skill-X")
    chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = _build_worm_sink(ws, InProcessSigner(key), telemetry)
    if reopened is not None:
        reopened.close()
    assert telemetry.audit_event.called
    actions = [c.args[0] for c in telemetry.audit_event.call_args_list if c.args]
    assert any("chain_verify_failed" in a for a in actions)
