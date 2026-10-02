"""The deployment's proactive connector credential renewer, and the startup migration (P18-2).

**Renewer.** One loop per arcui process. Each tick asks the connector seam to renew
every OAuth connection whose access token has used 75 % of its lifetime
(:meth:`arcagent.Connections.renew_credentials`). It is not the only process that
may renew: a running agent renews on demand under the SAME fenced lease in the
custody row, so the two can never both spend one single-use refresh token. This
loop only makes it rare for an agent to find a stale token at all.

**Startup migration.** Before the renewer and the health monitor start, a legacy
plaintext connector credential file is moved into sealed custody. If it cannot be
moved completely (no cipher, a read-back mismatch, a symlinked file) arcui refuses
to start: plaintext credentials never coexist with a running service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import arcagent
from arctrust import causal
from arctrust.audit import NullSink

logger = logging.getLogger("arcui.credential_renewer")

#: The renewer's identity: every renewal it commits is audited under this DID.
RENEWER_DID = "did:arc:system:credential-renewer"
#: The startup migrator's identity when no operator is at the keyboard.
MIGRATOR_DID = "did:arc:system:migrator"

DEFAULT_TICK_SECONDS = 60.0

ConnectionsFactory = Callable[[], arcagent.Connections]


class CredentialMigrationRefusedError(RuntimeError):
    """The legacy credential file exists and could not be migrated; arcui must not start."""


async def migrate_at_startup(connections: arcagent.Connections) -> arcagent.MigrationReport:
    """Move the legacy credential file into custody, or refuse startup (fail closed)."""
    with causal.bind(causal.root("system", MIGRATOR_DID)):
        try:
            report = await connections.migrate_secrets()
        except arcagent.ExtensionError as exc:
            raise CredentialMigrationRefusedError(
                f"connector credentials could not be moved into sealed custody ({exc.code}: "
                f"{exc.message}). Run `arc connector migrate-secrets` and read its report."
            ) from exc
    if not report.skipped:
        logger.warning(
            "migrated %d connector credential(s) into sealed custody; dropped %d leftover "
            "key(s); deleted %s",
            len(report.migrated),
            len(report.dropped),
            report.path,
        )
    return report


def build_credential_connections(app: Any) -> ConnectionsFactory:
    """Connections bound to this process's audit sink and shared arcstore backend."""
    backend = getattr(app.state, "arcstore_backend", None)
    worm = getattr(app.state, "audit_worm", None)
    sink = worm.sink if worm is not None else NullSink()

    async def open_backend() -> Any:
        return backend

    def factory() -> arcagent.Connections:
        return arcagent.Connections.for_deployment(
            audit=arcagent.AuditChain.held(sink),
            state_opener=open_backend if backend is not None else None,
        )

    return factory


class CredentialRenewer:
    """Renew due OAuth access tokens on a detached timer."""

    def __init__(
        self,
        connections_factory: ConnectionsFactory,
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        concurrency: int = 4,
    ) -> None:
        self._connections_factory = connections_factory
        self._tick_seconds = tick_seconds
        self._concurrency = concurrency
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Run the loop as its own causal root. Idempotent."""
        if self._task is None or self._task.done():
            self._task = causal.spawn_detached(
                self._run(),
                initiator="system",
                initiator_id=RENEWER_DID,
                name="arcui:credential-renewer",
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def tick(self) -> dict[str, str]:
        """One pass. Per-connection failures are already isolated by the seam."""
        return await self._connections_factory().renew_credentials(concurrency=self._concurrency)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: a failed pass is retried next tick; never fatal
                logger.exception("credential renewer: pass failed")
            await asyncio.sleep(self._tick_seconds)


__all__ = [
    "MIGRATOR_DID",
    "RENEWER_DID",
    "CredentialMigrationRefusedError",
    "CredentialRenewer",
    "build_credential_connections",
    "migrate_at_startup",
]
