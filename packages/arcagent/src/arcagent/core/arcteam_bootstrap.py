"""Shared arcteam service bootstrap for the messaging module.

Backend selection, message-signer construction, and self-registration live in
one place so every messaging entry point (the capability runtime and the tool
factory) builds byte-identical services (DRY). arcagent owns *which* identity
signs and *which* substrate carries the traffic; arcteam owns the messaging
itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcteam.crypto import MessageSigner
    from arcteam.storage import StorageBackend
    from arcteam.types import Entity
    from arctrust import AgentIdentity

_logger = logging.getLogger("arcagent.core.arcteam_bootstrap")


def message_signer(identity: AgentIdentity | None) -> MessageSigner | None:
    """Build an arcteam ``MessageSigner`` from the agent's arctrust identity.

    Closes REQ-030: the agent signs every message it sends with its own
    Ed25519 key. ``MessageSigner.from_identity`` is arcteam's single seam for
    reading the identity's seed. A verify-only identity has no seed and raises,
    which maps to ``None`` (optional-signer semantics — nothing to sign with).
    """
    if identity is None:
        return None
    from arcteam.crypto import MessageSigner

    try:
        return MessageSigner.from_identity(identity)
    except ValueError:
        return None


async def make_backend(nats_url: str) -> StorageBackend:
    """Return the messaging storage backend for the configured substrate.

    A configured NATS url selects the JetStream backend — the shared,
    push-capable, federal substrate. With none, the dependency-free in-memory
    backend backs local single-process and test use.

    An unreachable NATS server is the normal solo/local case, so a connection
    failure degrades to the in-memory bus with a single clean warning — never a
    nats-py traceback (F9). Only the connection-error class is caught; an
    unexpected error still surfaces.
    """
    from arcteam.storage import MemoryBackend

    if not nats_url:
        return MemoryBackend()

    from arcteam.backends.nats import NatsBackend

    # OSError covers ConnectionRefusedError; TimeoutError covers the bounded
    # connect timeout. Add nats-py's own connection-error types when installed.
    conn_errors: tuple[type[BaseException], ...] = (OSError, TimeoutError)
    try:
        from nats.errors import NoServersError
        from nats.errors import TimeoutError as NatsTimeoutError

        conn_errors = (*conn_errors, NoServersError, NatsTimeoutError)
    except ImportError:
        pass

    try:
        return await NatsBackend.connect(nats_url)
    except conn_errors as exc:
        _logger.warning(
            "NATS unreachable at %s; using the in-memory bus (this agent will "
            "not see teammates until a server is reachable): %s",
            nats_url,
            exc,
        )
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
