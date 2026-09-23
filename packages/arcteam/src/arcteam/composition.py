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


def is_fleet_transport_error(exc: Exception) -> bool:
    """Classify a transient shared-bus failure without exposing provider errors upward."""
    from arcteam.storage import FleetBackendUnavailableError

    if isinstance(exc, (FleetBackendUnavailableError, ConnectionError, TimeoutError)):
        return True
    try:
        from nats.errors import (
            ConnectionClosedError,
            ConnectionReconnectingError,
            NoServersError,
            StaleConnectionError,
        )
        from nats.errors import TimeoutError as NatsTimeoutError
    except ImportError:
        return False
    return isinstance(
        exc,
        (
            ConnectionClosedError,
            ConnectionReconnectingError,
            NoServersError,
            StaleConnectionError,
            NatsTimeoutError,
        ),
    )


async def make_backend(nats_url: str) -> StorageBackend:
    """Select shared NATS or intentional standalone memory, never a silent substitute."""
    from arcteam.storage import FleetBackendUnavailableError, MemoryBackend

    if not nats_url:
        return MemoryBackend()
    try:
        from nats.errors import NoServersError
        from nats.errors import TimeoutError as NatsTimeoutError

        from arcteam.backends.nats import NatsBackend
    except ModuleNotFoundError as exc:
        if exc.name is None or not exc.name.startswith("nats"):
            raise
        raise FleetBackendUnavailableError("configured NATS client is unavailable") from exc

    try:
        return await NatsBackend.connect(nats_url)
    except (OSError, TimeoutError, NoServersError, NatsTimeoutError) as exc:
        _logger.warning("configured NATS unavailable: %s", type(exc).__name__)
        raise FleetBackendUnavailableError("configured fleet backend is unavailable") from exc


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


__all__ = [
    "derive_handle",
    "is_fleet_transport_error",
    "make_backend",
    "message_signer",
    "self_entity",
]
