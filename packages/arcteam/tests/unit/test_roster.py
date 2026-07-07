"""Tests for C2 Roster.snapshot — join registry + presence (REQ-011)."""

from __future__ import annotations

import pytest
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.roster import Roster, RosterEntry
from arcteam.storage import MemoryBackend
from arcteam.team import Team
from arcteam.types import Entity, EntityStatus, EntityType


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    return EntityRegistry(backend, audit)


def _agent(name: str, caps: list[str] | None = None) -> Entity:
    return Entity(
        did=f"did:arc:test:agent/{name}",
        handle=name,
        id=f"agent://{name}",
        name=name.title(),
        type=EntityType.AGENT,
        capabilities=caps or [],
    )


def _team(members: list[str]) -> Team:
    return Team(id="ops", name="Ops", members=members, default_channel="channel://ops")


class TestSnapshot:
    async def test_joins_registry_and_presence(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("alice", caps=["search"]))
        await registry.register(_agent("bob", caps=["write"]))
        await registry.set_status("agent://bob", EntityStatus.idle)

        team = _team(["did:arc:test:agent/alice", "did:arc:test:agent/bob"])
        roster = Roster(registry)
        entries = await roster.snapshot(team)

        by_did = {e.did: e for e in entries}
        alice = by_did["did:arc:test:agent/alice"]
        assert isinstance(alice, RosterEntry)
        assert alice.handle == "alice"
        assert alice.status is EntityStatus.active
        assert alice.capabilities == ["search"]

        bob = by_did["did:arc:test:agent/bob"]
        assert bob.status is EntityStatus.idle
        assert bob.capabilities == ["write"]

    async def test_preserves_member_order(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("alice"))
        await registry.register(_agent("bob"))
        team = _team(["did:arc:test:agent/bob", "did:arc:test:agent/alice"])
        entries = await Roster(registry).snapshot(team)
        assert [e.did for e in entries] == [
            "did:arc:test:agent/bob",
            "did:arc:test:agent/alice",
        ]

    async def test_unregistered_member_is_offline(self, registry: EntityRegistry) -> None:
        team = _team(["did:arc:test:agent/ghost"])
        entries = await Roster(registry).snapshot(team)
        assert len(entries) == 1
        ghost = entries[0]
        assert ghost.did == "did:arc:test:agent/ghost"
        assert ghost.status is EntityStatus.offline
        assert ghost.handle == "did:arc:test:agent/ghost"
        assert ghost.capabilities == []

    async def test_empty_team(self, registry: EntityRegistry) -> None:
        assert await Roster(registry).snapshot(_team([])) == []
