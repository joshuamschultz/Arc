"""P18-1 D8 fed by P18-2: zero provider calls while needs_you until the generation changes.

``Connections._credential_generation`` now reads the custody row's ``generation``,
so a dead Arc-held credential is not probed again until the operator reconnects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcstore.backends.memory import FakeBackend
from packages.arcagent.tests.custody_fakes import make_cipher

from arcagent.connections import AuditChain, Connections
from arcagent.extension.attachment import ProbeResult, ToolSpec
from arcagent.extension.connection_health import HealthSignal
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.grants import Connection
from arcagent.extension.state import ConnectionStateStore

REPO_EXTENSIONS = Path(__file__).resolve().parents[4] / "extensions"
CIPHER = make_cipher("generation")


class _Probe:
    calls = 0

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        _Probe.calls += 1
        return ProbeResult(reachable=True, detail="ok", tools=[])

    async def describe_tools(self) -> list[ToolSpec]:
        return []

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        raise NotImplementedError


async def test_dead_credential_is_not_probed_until_its_generation_changes(tmp_path: Path) -> None:
    backend = FakeBackend()

    async def opener() -> Any:
        return backend

    connections = Connections.for_deployment(
        arc_dir=tmp_path / "arc",
        extensions_root=REPO_EXTENSIONS,
        audit=AuditChain(),
        state_opener=opener,
        attachment_factory=lambda *_a, **_k: _Probe(),
        credential_cipher=CIPHER,
    )
    connections.registry.define(
        "work_slack", Connection(extension="slack", approval="auto", agents=())
    )
    rows = CredentialRowStore(backend, CIPHER)
    generation = await rows.put_fields("work_slack", {"user_token": "xoxp-dead"}, actor_did="op")
    await connections.check_health("work_slack", checked_by="did:test")
    _Probe.calls = 0

    from arcagent.extension.connection_health import ConnectionHealthAuthority

    await ConnectionHealthAuthority(ConnectionStateStore(backend)).record(
        "work_slack",
        HealthSignal(
            ok=False,
            source="credential",
            checked_by="did:test",
            reason_code="invalid_grant",
            credential_generation=generation,
        ),
    )
    for _ in range(3):
        record = await connections.check_health("work_slack", checked_by="did:test")
    assert _Probe.calls == 0
    assert record.status == "needs_you"

    await rows.put_fields("work_slack", {"user_token": "xoxp-new"}, actor_did="op")
    record = await connections.check_health("work_slack", checked_by="did:test")
    assert _Probe.calls == 1
    assert record.status == "healthy"
