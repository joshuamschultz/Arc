"""Item 20 — a connector's credential reads name whoever caused them, not the operator.

``Connections`` recorded ``world.did`` (always the operator DID) as the actor of
every ``secret.read``: a browser tab polling a card and a scheduled probe were
both the operator. The actor now comes from the bound causal context.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import causal
from arctrust.audit import AuditEvent

from arcagent.connection_catalog import AuditChain
from arcagent.connections import Connections

_EXTENSION = "acme_actor"
_INSTANCE = "work"

_MANIFEST = f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme, authorised with a token."

[config.native]
entrypoint = "acme_actor_attachment"

[[secrets]]
name = "api_token"
prompt = "Acme API token."

[[secrets]]
name = "base_url"
prompt = "Acme web address."
sensitive = false

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Ping Acme."
classification = "read_only"

[approval]
default = "outbound"
"""

_ADAPTER = """
from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class Acme:
    def __init__(self, context: dict[str, Any]) -> None:
        self._credential = context["credential"]

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        token = (await self._credential.field("api_token")).reveal()
        return ProbeResult(reachable=bool(token), tools=await self.describe_tools(), detail="ok")

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Ping Acme.")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="pong")


def build_native_attachment(context: dict[str, Any]) -> Acme:
    return Acme(context)
"""


class _Recording:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def world(tmp_path: Path) -> tuple[Connections, _Recording]:
    arc_dir = tmp_path / "arc"
    bundle = arc_dir / "extensions" / _EXTENSION
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    (bundle / "acme_actor_attachment.py").write_text(_ADAPTER, encoding="utf-8")
    backend = FakeBackend()

    async def open_backend() -> FakeBackend:
        return backend

    sink = _Recording()
    connections = Connections.for_deployment(
        arc_dir=arc_dir,
        data_dir=tmp_path / "data",
        state_opener=open_backend,
        audit=AuditChain.held(sink),
    )
    return connections, sink


async def _install(connections: Connections) -> None:
    plan = connections.plan(_EXTENSION, _INSTANCE)
    await connections.install(plan, {"api_token": "tok-1", "base_url": "https://acme.example"})


async def test_a_scheduled_probe_is_attributed_to_the_scheduler(
    world: tuple[Connections, _Recording],
) -> None:
    connections, sink = world
    await _install(connections)
    sink.events.clear()

    scheduler = "did:arc:scheduler:olivia/health-sweep"
    with causal.bind(causal.root("scheduler", scheduler)):
        await connections.authorization(_INSTANCE)

    reads = [e for e in sink.events if e.action == "secret.read"]
    assert reads, "the probe read no credential; this test measures nothing"
    for event in reads:
        assert event.actor_did == scheduler
        assert event.actor_did != connections.world.did
        assert event.causal is not None and event.causal.initiator == "scheduler"


async def test_an_unbound_caller_is_loudly_unattributed_not_the_operator(
    world: tuple[Connections, _Recording],
) -> None:
    connections, sink = world
    await _install(connections)
    sink.events.clear()

    await connections.authorization(_INSTANCE)

    reads = [e for e in sink.events if e.action == "secret.read"]
    assert reads
    assert {e.actor_did for e in reads} == {causal.UNATTRIBUTED}
