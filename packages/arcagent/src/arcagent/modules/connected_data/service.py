"""Optional source synchronization service composed from typed seams."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from arcagent.connected_data import (
    AuditCallback,
    IngestPort,
    SourceDescription,
    SyncLimits,
    SyncState,
    SyncStatePort,
)
from arcagent.extension.source import InspectSource
from arcagent.extension.source_catalog import SourceCatalog, SourceRegistration
from arcagent.modules.connected_data.coordinator import ConnectedDataCoordinator

_logger = logging.getLogger("arcagent.modules.connected_data.service")

IngestPortFactory = Callable[
    [SourceDescription], IngestPort | Awaitable[IngestPort]
]


@dataclass(frozen=True)
class SourceRuntimeStatus:
    """Safe source status; credentials and cursors are never surfaced here."""

    connection_id: str
    status: str
    detail: str = ""
    description: SourceDescription | None = None
    state: SyncState | None = None


class ConnectedDataService:
    """Run each registered source independently with bounded concurrency."""

    def __init__(
        self,
        catalog: SourceCatalog,
        *,
        agent_did: str,
        sync_store_opener: Callable[[], Awaitable[SyncStatePort]] | None,
        ingest_factory: IngestPortFactory | None,
        limits: SyncLimits,
        global_concurrency: int,
        audit: AuditCallback | None = None,
        interval_seconds: float = 60.0,
    ) -> None:
        self._catalog = catalog
        self._agent_did = agent_did
        self._sync_store_opener = sync_store_opener
        self._ingest_factory = ingest_factory
        self._limits = limits
        self._semaphore = asyncio.Semaphore(global_concurrency)
        self._audit = audit
        self._interval = interval_seconds
        self._store: Any = None
        self._monitor: asyncio.Task[None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._statuses: dict[str, SourceRuntimeStatus] = {}
        self._paused: set[str] = set()
        self._wake = asyncio.Event()
        self._closed = False

    async def start(self) -> None:
        """Start the monitor; an unavailable optional backend becomes degraded."""
        self._store = await self._open_store()
        self._monitor = asyncio.create_task(self._monitor_loop(), name="connected-data-sync")
        self._wake.set()

    async def close(self) -> None:
        """Cancel workers and release only resources owned by this service."""
        self._closed = True
        monitor = self._monitor
        self._monitor = None
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._store = None

    async def list_status(self) -> tuple[SourceRuntimeStatus, ...]:
        """Return safe operational state for UI/operator surfaces."""
        registrations = await self._catalog.snapshot()
        known = {entry.connection_id for entry in registrations}
        return tuple(self._statuses[key] for key in sorted(self._statuses) if key in known)

    async def sync_now(self, connection_id: str) -> bool:
        """Schedule one source immediately; unknown or paused sources are refused."""
        if connection_id in self._paused:
            return False
        registration = await self._find(connection_id)
        if registration is None:
            return False
        self._schedule(registration)
        return True

    async def pause(self, connection_id: str) -> bool:
        """Stop future work while preserving the durable checkpoint."""
        if await self._find(connection_id) is None:
            return False
        self._paused.add(connection_id)
        return True

    async def resume(self, connection_id: str) -> bool:
        """Resume a source from its durable checkpoint."""
        if await self._find(connection_id) is None:
            return False
        self._paused.discard(connection_id)
        self._wake.set()
        return True

    async def revoke(self, connection_id: str) -> bool:
        """Remove a source from synchronization and close its adapter."""
        self._paused.add(connection_id)
        task = self._tasks.pop(connection_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._statuses.pop(connection_id, None)
        await self._catalog.unregister(connection_id)
        return True

    async def _monitor_loop(self) -> None:
        while not self._closed:
            registrations = await self._catalog.snapshot()
            for registration in registrations:
                if registration.connection_id not in self._paused:
                    self._schedule(registration)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            self._wake.clear()

    def _schedule(self, registration: SourceRegistration) -> None:
        current = self._tasks.get(registration.connection_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_one(registration), name=f"sync:{registration.connection_id}"
        )
        self._tasks[registration.connection_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(registration.connection_id, None))

    async def _run_one(self, registration: SourceRegistration) -> None:
        connection_id = registration.connection_id
        try:
            async with self._catalog.lease(connection_id) as leased:
                if leased is None:
                    return
                await self._run_leased(leased)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("connected-data source failed: %s", connection_id, exc_info=True)
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id,
                status="failed",
                detail=type(exc).__name__,
            )

    async def _run_leased(self, registration: SourceRegistration) -> None:
        connection_id = registration.connection_id
        async with self._semaphore:
            if self._store is None:
                self._set_degraded(connection_id, "arcstore_unavailable")
                return
            if self._ingest_factory is None:
                self._set_degraded(connection_id, "ingest_port_unavailable")
                return
            description = await registration.adapter.inspect_source(
                InspectSource(connection_id=connection_id)
            )
            candidate = self._ingest_factory(description)
            ingest = await candidate if inspect.isawaitable(candidate) else candidate
            result = await ConnectedDataCoordinator(
                registration.adapter,
                ingest,
                self._store,
                audit=self._audit,
            ).run(
                description,
                agent_did=self._agent_did,
                owner_id=f"{self._agent_did}:{uuid.uuid4().hex}",
                limits=self._limits,
            )
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id,
                status=result.status.value,
                description=description,
                state=result,
            )

    async def _open_store(self) -> Any:
        if self._sync_store_opener is None:
            return None
        try:
            return await self._sync_store_opener()
        except Exception:
            _logger.warning("connected-data ArcStore backend unavailable", exc_info=True)
            return None

    async def _find(self, connection_id: str) -> SourceRegistration | None:
        for registration in await self._catalog.snapshot():
            if registration.connection_id == connection_id:
                return registration
        return None

    def _set_degraded(self, connection_id: str, detail: str) -> None:
        self._statuses[connection_id] = SourceRuntimeStatus(
            connection_id=connection_id, status="degraded", detail=detail
        )


__all__ = ["ConnectedDataService", "IngestPortFactory", "SourceRuntimeStatus"]
