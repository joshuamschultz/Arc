"""Item 20 P20-4 — agent security events reach the signed chain, not only the log.

``TelemetryAuditSink`` used to send every agent event to the log/OTel span only,
so a credential read by a connector or a refused call never reached the WORM
ledger. The agent's sink now tees security events into its operator-signed
chain through the one durable path; routine lifecycle events stay telemetry-only.
Driven through the real ``SecretStore`` and a real ``WormSink``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from arctrust import causal
from arctrust.audit import AuditEvent, WormSink, emit, verify_chain
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcagent.core.telemetry import DurableTelemetryAuditSink
from arcagent.extension.secrets import EnvFile, SecretRef, SecretStore

_AGENT = "did:arc:example:org:agent:abc"


def _chain(tmp_path: Path) -> tuple[WormSink, Path, bytes]:
    operator = generate_keypair()
    path = tmp_path / "audit-chain.jsonl"
    return WormSink(path, InProcessSigner(operator.private_key)), path, operator.public_key


def _actions(path: Path) -> list[dict[str, object]]:
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


async def test_a_secret_read_with_a_value_is_on_the_signed_chain(tmp_path: Path) -> None:
    worm, path, public_key = _chain(tmp_path)
    sink = DurableTelemetryAuditSink(MagicMock(), worm)
    store = SecretStore(EnvFile(tmp_path / "secrets.env"), sink=sink)
    ref = SecretRef(connection="work_gmail", field="token")

    with (
        causal.bind(causal.root("agent", _AGENT, on_behalf_of="did:arc:user:josh")),
        causal.refine(connection_id="work_gmail"),
    ):
        await store.put(ref, "s3cret")
        found = await store.get(ref)
        missing = await store.get(SecretRef(connection="work_gmail", field="absent"))

    assert found is not None and missing is None
    worm.close()
    assert verify_chain(path, public_key)
    events = _actions(path)
    assert [(e["action"], e["outcome"]) for e in events] == [
        ("secret.write", "allow"),
        ("secret.read", "allow"),
    ]
    for event in events:
        assert event["actor_did"] == _AGENT
        assert event["causal"]["connection_id"] == "work_gmail"
        assert "s3cret" not in json.dumps(event)


def test_a_denied_act_is_on_the_signed_chain_and_routine_events_are_not(
    tmp_path: Path,
) -> None:
    worm, path, public_key = _chain(tmp_path)
    sink = DurableTelemetryAuditSink(MagicMock(), worm)
    with causal.bind(causal.root("agent", _AGENT)):
        emit(AuditEvent(actor_did=_AGENT, action="capability.added", target="x", outcome="ok"), sink)
        emit(AuditEvent(actor_did=_AGENT, action="connector.refused", target="y", outcome="deny"), sink)

    worm.close()
    assert verify_chain(path, public_key)
    assert [e["action"] for e in _actions(path)] == ["connector.refused"]


def test_the_connector_module_audits_through_the_agent_durable_sink(tmp_path: Path) -> None:
    from arctrust import AgentIdentity

    from arcagent.modules.connectors import _runtime
    from arcagent.modules.connectors.capabilities import _audit_sink

    worm, _path, _key = _chain(tmp_path)
    durable = DurableTelemetryAuditSink(MagicMock(), worm)
    _runtime.reset()
    try:
        _runtime.configure(
            identity=AgentIdentity.generate(org="t", agent_type="a"),
            telemetry=MagicMock(),
            workspace=tmp_path,
            audit_sink=durable,
        )
        assert _audit_sink(_runtime.state()) is durable
    finally:
        _runtime.reset()
        worm.close()
