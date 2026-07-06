"""SPEC-033 #5 — WORM audit store lives outside the agent-writable workspace.

AU-9(2): the tamper-evident audit chain must not sit in a directory the agent
can write to (``workspace/.audit`` let a compromised agent truncate/forge its
own record). It belongs in an operator-owned dir beside the workspace. SI-7(7):
a pre-existing chain is integrity-checked at load and a failure raises an alert
event.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from arctrust.keypair import generate_keypair

from arcagent.modules.skill_improver._runtime import _build_worm_sink


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "agent" / "workspace"
    ws.mkdir(parents=True)
    return ws


def test_worm_store_is_outside_workspace(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    key = generate_keypair().private_key
    sink = _build_worm_sink(ws, key, MagicMock())
    assert sink is not None
    try:
        chain = Path(sink._path)
        assert ws not in chain.parents, "audit chain must not live under the workspace"
        assert not (ws / ".audit").exists()
    finally:
        sink.close()


def test_tampered_chain_emits_alert_at_load(tmp_path: Path) -> None:
    from arctrust import WormSink
    from arctrust.audit import AuditEvent

    ws = _workspace(tmp_path)
    key = generate_keypair().private_key
    telemetry = MagicMock()

    # Seed a real, valid chain with two records, then release the flock.
    chain = ws.parent / ".audit" / "skill_improver.worm"
    seed = WormSink(chain, key)
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

    # Tamper the FIRST record's bytes (tip recovery from the tail still works,
    # but the hash chain / signature no longer verifies).
    lines = chain.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("skill-0", "skill-X")
    chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = _build_worm_sink(ws, key, telemetry)
    if reopened is not None:
        reopened.close()
    assert telemetry.audit_event.called
    actions = [c.args[0] for c in telemetry.audit_event.call_args_list if c.args]
    assert any("chain_verify_failed" in a for a in actions)
