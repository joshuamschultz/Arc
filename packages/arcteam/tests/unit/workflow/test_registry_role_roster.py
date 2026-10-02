"""The registry is the only source of a gate decider's roles (alpha-2 #67)."""

from __future__ import annotations

import pytest
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityStatus, EntityType
from arcteam.workflow.stores import RegistryOwnerResolver

USER_DID = "did:arc:telegram:4242"


@pytest.fixture
async def roster() -> tuple[EntityRegistry, RegistryOwnerResolver]:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    await registry.register(
        Entity(
            did=USER_DID,
            handle="reviewer-josh",
            id="user://reviewer-josh",
            name="Josh",
            type=EntityType.USER,
            roles=["reviewer"],
        )
    )
    await registry.register(
        Entity(
            did="did:arc:telegram:9",
            handle="gone",
            id="user://gone",
            name="Gone",
            type=EntityType.USER,
            roles=["finance"],
            status=EntityStatus.revoked,
        )
    )
    return registry, RegistryOwnerResolver(registry)


async def test_roles_of_reads_the_registered_roles(
    roster: tuple[EntityRegistry, RegistryOwnerResolver],
) -> None:
    _, resolver = roster
    assert await resolver.roles_of(USER_DID) == frozenset({"reviewer"})


async def test_roles_of_never_resolves_a_handle(
    roster: tuple[EntityRegistry, RegistryOwnerResolver],
) -> None:
    _, resolver = roster
    assert await resolver.roles_of("reviewer-josh") == frozenset()
    assert await resolver.roles_of("@reviewer-josh") == frozenset()


async def test_unknown_and_inactive_members_hold_no_roles(
    roster: tuple[EntityRegistry, RegistryOwnerResolver],
) -> None:
    _, resolver = roster
    assert await resolver.roles_of("did:arc:telegram:1") == frozenset()
    assert await resolver.roles_of("did:arc:telegram:9") == frozenset()


async def test_declared_roles_are_the_active_members_roles(
    roster: tuple[EntityRegistry, RegistryOwnerResolver],
) -> None:
    _, resolver = roster
    assert await resolver.declared_roles() == frozenset({"reviewer"})
