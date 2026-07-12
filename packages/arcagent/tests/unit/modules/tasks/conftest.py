"""Shared fixtures for the ``tasks`` module tests — SPEC-056 Phase B.

Mirrors ``tests/unit/modules/messaging/conftest.py``: a peer-entity builder for
arcteam's ``EntityRegistry`` (assign_task/create_task resolve ``@handle`` refs
against it, per SDD §3) and a throwaway operator signer for the registry's
audit chain. Kept independent of the messaging test package (each module's
tests are self-contained, per structure.md's "each module independent" rule).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcteam.registry import EntityRegistry
    from arcteam.types import Entity


def make_operator_signer() -> Any:
    """Build a deployment operator ``Signer`` for the registry's audit chain.

    Distinct from any agent identity — the audited subject must not be its
    own audit authority (SPEC-037 F4).
    """
    from arctrust import OperatorKey

    return OperatorKey.generate().into_signer()


def make_registry() -> EntityRegistry:
    """Build a fresh in-memory arcteam ``EntityRegistry`` for handle resolution."""
    from arcteam.audit import AuditLogger
    from arcteam.registry import EntityRegistry
    from arcteam.storage import MemoryBackend

    backend = MemoryBackend()
    audit = AuditLogger(backend, make_operator_signer())
    return EntityRegistry(backend, audit)


def make_peer_entity(
    handle: str, name: str | None = None, roles: list[str] | None = None
) -> Entity:
    """Build a DID-keyed peer ``Entity`` so ``@handle`` refs resolve in tests."""
    from arcteam.types import Entity, EntityType
    from arctrust import AgentIdentity

    identity = AgentIdentity.generate(org="local", agent_type="agent")
    return Entity(
        did=identity.did,
        handle=handle,
        id=f"agent://{handle}",
        name=name or handle.title(),
        type=EntityType.AGENT,
        public_key=identity.public_key.hex(),
        roles=roles or [],
    )
