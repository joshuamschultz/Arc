"""Optional fleet composition helpers owned by ArcTeam."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arctrust import AgentIdentity
    from arcteam.crypto import MessageSigner
    from arcteam.storage import StorageBackend
    from arcteam.types import Entity

_logger = logging.getLogger("arcteam.composition")


def message_signer(identity: AgentIdentity | None) -> MessageSigner | None:
    """Build a signer from an agent identity, if it can sign."""
    if identity is None:
        return None
    from arcteam.crypto import MessageSigner

    try:
        return MessageSigner.from_identity(identity)
    except ValueError:
        return None


async def make_backend(nats_url: str) -> StorageBackend:
    """Select NATS JetStream, degrading to a local memory bus when absent."""
    from arcteam.storage import MemoryBackend

    if not nats_url:
        return MemoryBackend()
    from arcteam.backends.nats import NatsBackend

    errors: tuple[type[BaseException], ...] = (OSError, TimeoutError)
    try:
        from nats.errors import NoServersError
        from nats.errors import TimeoutError as NatsTimeoutError

        errors = (*errors, NoServersError, NatsTimeoutError)
    except ImportError:
        pass
    try:
        return await NatsBackend.connect(nats_url)
    except errors as exc:
        _logger.warning("NATS unavailable at configured endpoint: %s", exc)
        return MemoryBackend()


def derive_handle(entity_id: str, fallback: str) -> str:
    """Return an addressable handle from an entity URI or fallback name."""
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
    """Build the signed roster entity for one composed agent."""
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


__all__ = ["derive_handle", "make_backend", "message_signer", "self_entity"]
