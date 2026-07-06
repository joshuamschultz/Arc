"""Shared arcteam service bootstrap for the messaging module.

Backend selection, message-signer construction, and self-registration live in
one place so the legacy :class:`MessagingModule` and the decorator capability
path build byte-identical services (DRY). arcagent owns *which* identity signs
and *which* substrate carries the traffic; arcteam owns the messaging itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcteam.crypto import MessageSigner
    from arcteam.storage import StorageBackend
    from arcteam.types import Entity
    from arctrust import AgentIdentity


def message_signer(identity: AgentIdentity | None) -> MessageSigner | None:
    """Build an arcteam ``MessageSigner`` from the agent's arctrust identity.

    Closes REQ-030: the agent signs every message it sends with its own
    Ed25519 key. The signer carries the raw 32-byte seed that
    ``arcteam.crypto.sign_message`` needs. ``AgentIdentity`` exposes no public
    seed accessor, so the seed is read from its own signing key — the agent
    wiring its own signer, not a cross-package reach into a foreign key.
    Returns ``None`` for a verify-only identity (nothing to sign with).
    """
    if identity is None or identity._signing_key is None:
        return None
    from arcteam.crypto import MessageSigner

    seed: bytes = identity._signing_key.encode()
    return MessageSigner(did=identity.did, private_key=seed)


async def make_backend(nats_url: str) -> StorageBackend:
    """Return the messaging storage backend for the configured substrate.

    A configured NATS url selects the JetStream backend — the shared,
    push-capable, federal substrate. With none, the dependency-free in-memory
    backend backs local single-process and test use.
    """
    if nats_url:
        from arcteam.backends.nats import NatsBackend

        return await NatsBackend.connect(nats_url)
    from arcteam.storage import MemoryBackend

    return MemoryBackend()


def derive_handle(entity_id: str, fallback: str) -> str:
    """Return the unique ``@handle`` for a URI or bare entity id.

    ``agent://brad`` -> ``brad``; a bare id is used as-is; an empty id falls
    back to the agent name so an entity always has an addressable handle.
    """
    if "://" in entity_id:
        return entity_id.split("://", 1)[1]
    return entity_id or fallback


def self_entity(
    *,
    entity_id: str,
    entity_name: str,
    handle: str,
    identity: AgentIdentity,
    roles: list[str],
    capabilities: list[str],
) -> Entity:
    """Build this agent's registry ``Entity``, DID-keyed and signature-ready.

    The DID and public key come from the agent's real arctrust identity
    (REQ-001) so peers can verify the messages it signs.
    """
    from arcteam.types import Entity, EntityType

    return Entity(
        did=identity.did,
        handle=handle,
        id=entity_id,
        name=entity_name or entity_id,
        type=EntityType.AGENT,
        public_key=identity.public_key.hex(),
        roles=roles,
        capabilities=capabilities,
    )
