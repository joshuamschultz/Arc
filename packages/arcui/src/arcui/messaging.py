"""Embedded arcteam MessagingService construction for the dashboard.

COMP-004 / REQ-090. ``arc ui start`` (arccli) boots the managed NATS broker
and registers the folder's agents, but the Starlette app's ``team_chat``
routes read ``app.state.messaging_service`` — a handle nothing set on a live
deployment, so ``/api/team/channels`` read empty while ``arc team channels``
listed real channels. The lifespan (``server.py``) calls
:func:`build_messaging_service` when the deployment has a ``team_root`` and no
service was injected, mirroring the same construction path arccli's
``_build_service`` uses: ``NatsBackend -> AuditLogger(operator signer) ->
EntityRegistry -> MessagingService``.

Seam rationale: arccli imports arcui, so arcui cannot import arccli without a
cycle. arcui builds from arcteam primitives directly (dependencies point down:
arcui -> arcteam/arctrust). The operator signer is resolved from arctrust's
on-disk operator key — the same audit authority the CLI signs the WORM chain
with, so channel mutations written through this service stay chain-consistent.
The audit authority is never minted here (``generate_if_absent=False``): an
observer must not bootstrap the deployment's signing key. When the key is
absent or the broker is unreachable the builder returns ``(None, None)`` and
the routes surface an explicit ``team_messaging_unavailable`` error instead of
fabricating an empty channel list.

``_connect_backend`` is the monkeypatch seam tests replace with an in-memory
backend, so the construction path is exercised without standing up NATS.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcteam.crypto import MessageSigner

logger = logging.getLogger(__name__)

_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
_PREFLIGHT_TIMEOUT = 0.5
_CONNECT_TIMEOUT = 3.0


def _nats_url() -> str:
    """Resolve the broker URL — same source as the ``arc team`` CLI path."""
    return os.environ.get("ARCTEAM_NATS_URL", _DEFAULT_NATS_URL)


async def _preflight(url: str) -> None:
    """Fail fast if the NATS port is not accepting connections."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4222
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=_PREFLIGHT_TIMEOUT
    )
    writer.close()
    await writer.wait_closed()


async def _connect_backend() -> Any | None:
    """Connect the live NATS JetStream backend, or None if unreachable.

    Fail-open: a refused port or a broker without JetStream returns None so
    the dashboard still serves — the channel routes then report an explicit
    unavailable state. Tests monkeypatch this to inject a MemoryBackend.
    """
    import nats
    from arcteam.backends.nats import NatsBackend

    async def _swallow(_exc: Exception) -> None:
        return None

    url = _nats_url()
    try:
        await _preflight(url)
        nc = await asyncio.wait_for(
            nats.connect(url, connect_timeout=2, allow_reconnect=False, error_cb=_swallow),
            timeout=_CONNECT_TIMEOUT,
        )
    except Exception:  # reason: fail-open — broker down => routes report unavailable
        logger.warning("embedded messaging: NATS broker unreachable at %s", url, exc_info=True)
        return None
    return NatsBackend(nc.jetstream(), nc)


def _operator_signer() -> Any | None:
    """Resolve the on-disk operator key into an audit-chain signer, or None.

    The operator key is the deployment's audit authority (signs the WORM
    chain) and lives at ``${ARC_CONFIG_DIR:-~/.arc}/operator/operator.key`` —
    the same path arccli uses. Never generated here: a missing key means the
    deployment has not been initialised, so the builder degrades rather than
    minting a signing authority from the observer.
    """
    from arcteam.config import default_config_dir
    from arctrust import OperatorKey

    key_path = default_config_dir() / "operator" / "operator.key"
    try:
        return OperatorKey.load(key_path, generate_if_absent=False).into_signer()
    except (OSError, ValueError, RuntimeError):
        logger.warning("embedded messaging: operator key unavailable at %s", key_path)
        return None


@dataclass(frozen=True)
class _OperatorMessaging:
    """The operator's message-signing identity for embedded posting.

    ``did`` and ``public_key_hex`` register the operator as a real EntityRegistry
    entity so agents can verify its posts; ``signer`` binds every posted envelope
    to that DID inside ``MessagingService.send`` (REQ-030). All three derive from
    the one deployment operator key — the operator posts under its own key, never
    a per-viewer-token identity that has no signing material.
    """

    did: str
    public_key_hex: str
    signer: MessageSigner


def _operator_messaging() -> _OperatorMessaging | None:
    """Load the operator key into an Ed25519 message-signing identity, or None.

    Same key file as :func:`_operator_signer` (the deployment authority), but
    adapted for *message* signing rather than audit-chain signing: arcteam
    verifies message envelopes with Ed25519, so the operator's DID is derived
    from the key's Ed25519 verify key and the raw seed drives the signer. Never
    generated here — a missing key degrades the forwarder to ``None`` exactly as
    the channel routes degrade when no audit authority exists.
    """
    from arcteam.config import default_config_dir
    from arcteam.crypto import MessageSigner
    from arctrust import OperatorKey
    from arctrust.identity import did_from_public_key

    key_path = default_config_dir() / "operator" / "operator.key"
    try:
        op = OperatorKey.load(key_path, generate_if_absent=False)
    except (OSError, ValueError, RuntimeError):
        logger.warning("embedded messaging: operator key unavailable at %s", key_path)
        return None
    did = did_from_public_key(op.public_key, org="local", agent_type="operator")
    return _OperatorMessaging(
        did=did,
        public_key_hex=op.public_key.hex(),
        signer=MessageSigner(did=did, private_key=op.seed),
    )


async def build_messaging_service(
    *, backend: Any | None = None
) -> tuple[Any | None, Any | None, Any | None]:
    """Construct the embedded MessagingService, or ``(None, None, None)``.

    Returns ``(service, registry, backend)`` — the registry lets channel-
    management routes resolve agent refs to DIDs (COMP-005), and the caller
    owns ``backend`` and closes it on shutdown. Returns ``(None, None, None)``
    when the audit authority is absent or the broker is unreachable, so the
    channel routes surface an explicit service-unavailable error instead of a
    fabricated empty list.
    """
    signer = _operator_signer()
    if signer is None:
        return None, None, None

    if backend is None:
        backend = await _connect_backend()
        if backend is None:
            return None, None, None

    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry

    audit = AuditLogger(backend, signer)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    # The operator message signer lets the human operator's channel posts be
    # Ed25519-signed under its own DID, so subscribing agents verify and accept
    # them (REQ-030) instead of quarantining an unsigned envelope to the DLQ.
    op_msg = _operator_messaging()
    service = MessagingService(
        backend, registry, audit, signer=op_msg.signer if op_msg is not None else None
    )
    return service, registry, backend


def build_team_post_forwarder(*, service: Any, registry: Any) -> Any | None:
    """Build the ``/ws/team`` forwarder that posts an operator message to a channel.

    Returns an async callable ``(*, sender, channel, text) -> None`` that a
    trusted operator's group post flows through (REQ-061), or ``None`` when the
    deployment has no operator key (the route then reports ``forward_unavailable``).

    The operator is a first-class signing entity: on first post it self-registers
    in the EntityRegistry under its key-derived DID and auto-joins the target
    channel (both audited), then sends a signed :class:`~arcteam.types.Message`.
    Auto-join is correct for a trusted operator and is what makes a fresh channel
    reachable from the dashboard without a separate ``arc team`` round trip.
    arcui never mints identities or signs here — ``service.send`` signs with the
    operator message signer wired in :func:`build_messaging_service`.
    """
    op = _operator_messaging()
    if op is None:
        return None

    async def forward(*, sender: str, channel: str, text: str) -> None:
        from arcteam.types import Channel, Entity, EntityType, Message

        if await registry.get(op.did) is None:
            await registry.register(
                Entity(
                    did=op.did,
                    handle="operator",
                    id="user://operator",
                    name="Operator",
                    type=EntityType.USER,
                    public_key=op.public_key_hex,
                )
            )
        channels = await service.list_channels()
        existing = next((c for c in channels if c.name == channel), None)
        if existing is None:
            await service.create_channel(Channel(name=channel, members=[op.did]))
        elif op.did not in existing.members:
            await service.join_channel(channel, op.did)
        # The viewer-token DID (``sender``) is opaque, unsigned attribution kept
        # in meta; the signed sender is always the operator's key-bound DID.
        await service.send(
            Message(
                sender=op.did,
                to=[f"channel://{channel}"],
                body=text,
                meta={"operator_token_did": sender},
            )
        )

    return forward


__all__ = ["build_messaging_service", "build_team_post_forwarder"]
