"""The fleet capability handed to an agent, and what must be true of it.

An agent asks for these seams and cannot verify what is behind them, so the
guarantees are proven here: eligibility is filtered before a member is offered,
a notice keeps its classification on the wire, and the delivery audit chain is
signed by the real operator authority rather than a key the agent could mint.
"""

from __future__ import annotations

from typing import Any

import pytest
from arctrust import AgentIdentity, OperatorKey

from arcteam.agent_fleet import (
    FleetDirectoryAdapter,
    FleetMessengerAdapter,
    open_fleet_services,
)
from arcteam.types import Entity, EntityStatus, EntityType


class _Notice:
    """The shape an agent sends, without importing the agent package."""

    def __init__(self, kind: str, classification: str = "unclassified") -> None:
        self.sender = "did:arc:test:agent/aaaa"
        self.to = ("agent://bob",)
        self.kind = kind
        self.body = "hello"
        self.classification = classification


class _Registry:
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def list_entities(self, role: str | None = None) -> list[Entity]:
        return self._entities


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Any:
        self.sent.append(message)
        return message


def _entity(handle: str, *, kind: EntityType = EntityType.AGENT) -> Entity:
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    return Entity(
        did=identity.did,
        handle=handle,
        id=f"agent://{handle}",
        name=handle.title(),
        type=kind,
        status=EntityStatus.active,
    )


@pytest.mark.asyncio
async def test_only_agents_are_offered_as_candidates() -> None:
    """A user is addressable but is not something work can be routed to."""
    agent, user = _entity("bob"), _entity("josh", kind=EntityType.USER)
    directory = FleetDirectoryAdapter(_Registry([agent, user]))

    members = await directory.list_agents()

    assert [member.handle for member in members] == ["bob"]


@pytest.mark.asyncio
async def test_a_handle_resolves_to_its_did() -> None:
    agent = _entity("bob")
    directory = FleetDirectoryAdapter(_Registry([agent]))

    assert await directory.resolve("@bob") == agent.did


@pytest.mark.asyncio
async def test_an_unknown_handle_is_refused_not_silently_dropped() -> None:
    directory = FleetDirectoryAdapter(_Registry([]))

    with pytest.raises(ValueError, match="nobody"):
        await directory.resolve("@nobody")


@pytest.mark.asyncio
async def test_a_notice_keeps_its_classification_on_the_wire() -> None:
    """The no-write-down check downstream is only as good as this label."""
    messenger = _Messenger()

    await FleetMessengerAdapter(messenger).send_notice(_Notice("task_assigned", "CUI"))

    assert messenger.sent[0].classification == "CUI"
    assert messenger.sent[0].msg_type == "task_assigned"


@pytest.mark.asyncio
async def test_delivery_audit_is_signed_by_the_operator_not_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent must never be able to sign the chain that records its own sends."""
    import arcteam.agent_fleet as agent_fleet
    import arcteam.audit as audit_mod
    from arcteam.storage import MemoryBackend

    captured: dict[str, Any] = {}
    real_init = audit_mod.AuditLogger.__init__

    def spy_init(self: Any, backend: Any, signer: Any) -> None:
        captured["signer"] = signer
        real_init(self, backend, signer)

    async def fake_backend(url: str) -> Any:
        return MemoryBackend()

    monkeypatch.setattr(audit_mod.AuditLogger, "__init__", spy_init)
    monkeypatch.setattr(agent_fleet, "make_backend", fake_backend)

    identity = AgentIdentity.generate(org="local", agent_type="agent")
    operator_signer = OperatorKey.generate().into_signer()

    directory, messenger = await open_fleet_services(
        nats_url="nats://127.0.0.1:1",
        identity=identity,
        operator_signer=operator_signer,
    )

    assert captured["signer"] is operator_signer
    assert directory is not None
    assert messenger is not None
