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
from typing import Any

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


async def build_messaging_service(*, backend: Any | None = None) -> tuple[Any | None, Any | None]:
    """Construct the embedded MessagingService, or ``(None, None)``.

    Returns ``(service, backend)`` — the caller owns ``backend`` and closes it
    on shutdown. Returns ``(None, None)`` when the audit authority is absent or
    the broker is unreachable, so the channel routes surface an explicit
    service-unavailable error instead of a fabricated empty list.
    """
    signer = _operator_signer()
    if signer is None:
        return None, None

    if backend is None:
        backend = await _connect_backend()
        if backend is None:
            return None, None

    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry

    audit = AuditLogger(backend, signer)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    service = MessagingService(backend, registry, audit)
    return service, backend


__all__ = ["build_messaging_service"]
