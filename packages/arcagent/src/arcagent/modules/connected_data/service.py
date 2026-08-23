"""Optional source synchronization service composed from typed seams."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from arcagent.connected_data import (
    AuditCallback,
    IngestPort,
    KnowledgeHome,
    ListSourceResources,
    SourceDescription,
    SyncLimits,
    SyncState,
    SyncStatePort,
)
from arcagent.extension.source import InspectSource, SelectSourceResources, SourceResource
from arcagent.extension.source_catalog import SourceCatalog, SourceRegistration
from arcagent.modules.connected_data.coordinator import ConnectedDataCoordinator

_logger = logging.getLogger("arcagent.modules.connected_data.service")

IngestPortFactory = Callable[[SourceDescription], IngestPort | Awaitable[IngestPort]]


class SourceSelectionStore(Protocol):
    async def get(self, connection_id: str) -> tuple[str, ...]: ...

    async def put(self, connection_id: str, resource_ids: tuple[str, ...]) -> None: ...


@dataclass(frozen=True)
class SourceRuntimeStatus:
    """Safe source status; credentials and cursors are never surfaced here."""

    connection_id: str
    status: str
    source_id: str = ""
    detail: str = ""
    description: SourceDescription | None = None
    state: SyncState | None = None


@dataclass(frozen=True)
class SourceOperationResult:
    """Typed, safe outcome for one operator lifecycle request."""

    connection_id: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class MappingProposalStatus:
    """Safe operator-facing mapping proposal, bound to an approval row."""

    connection_id: str
    source_id: str
    allowed_homes: tuple[KnowledgeHome, ...]
    homes: tuple[KnowledgeHome, ...]
    approval_id: str
    approval_status: str = "pending"
    detail: str = ""


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
        resource_selection_store_opener: Callable[[], Awaitable[SourceSelectionStore]]
        | None = None,
        audit: AuditCallback | None = None,
        interval_seconds: float = 60.0,
    ) -> None:
        self._catalog = catalog
        self._agent_did = agent_did
        self._sync_store_opener = sync_store_opener
        self._resource_selection_store_opener = resource_selection_store_opener
        self._ingest_factory = ingest_factory
        self._limits = limits
        self._semaphore = asyncio.Semaphore(global_concurrency)
        self._audit = audit
        self._interval = interval_seconds
        self._store: Any = None
        self._resource_store: SourceSelectionStore | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._statuses: dict[str, SourceRuntimeStatus] = {}
        self._mapping_statuses: dict[str, MappingProposalStatus] = {}
        self._selected_resources: dict[str, tuple[str, ...]] = {}
        self._paused: set[str] = set()
        self._wake = asyncio.Event()
        self._closed = False

    async def start(self) -> None:
        """Start the monitor; an unavailable optional backend becomes degraded."""
        self._store = await self._open_store()
        self._resource_store = await self._open_resource_store()
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
        self._resource_store = None

    async def list_sources(self) -> tuple[SourceRuntimeStatus, ...]:
        """Return safe operational state for UI/operator surfaces."""
        registrations = sorted(
            await self._catalog.snapshot(), key=lambda entry: entry.connection_id
        )
        for registration in registrations:
            if registration.connection_id not in self._statuses:
                await self._inspect_registration(registration)
        return tuple(self._statuses[registration.connection_id] for registration in registrations)

    async def sync_now(self, connection_id: str) -> SourceOperationResult:
        """Schedule one source immediately; unknown or paused sources are refused."""
        if connection_id in self._paused:
            return SourceOperationResult(connection_id, "refused", "source_paused")
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        self._schedule(registration)
        return SourceOperationResult(connection_id, "scheduled")

    async def pause(self, connection_id: str) -> SourceOperationResult:
        """Stop future work while preserving the durable checkpoint."""
        if await self._find(connection_id) is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.add(connection_id)
        return SourceOperationResult(connection_id, "paused")

    async def resume(self, connection_id: str) -> SourceOperationResult:
        """Resume a source from its durable checkpoint."""
        if await self._find(connection_id) is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.discard(connection_id)
        self._wake.set()
        return SourceOperationResult(connection_id, "scheduled")

    async def revoke(self, connection_id: str) -> SourceOperationResult:
        """Remove a source from synchronization and close its adapter."""
        if await self._find(connection_id) is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.add(connection_id)
        task = self._tasks.pop(connection_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._statuses.pop(connection_id, None)
        await self._catalog.unregister(connection_id)
        return SourceOperationResult(connection_id, "revoked")

    async def reindex(self, connection_id: str) -> SourceOperationResult:
        """Reset a durable checkpoint, then backfill the current source snapshot."""
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        task = self._tasks.get(connection_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._store is None or not await self._store.reset(self._agent_did, connection_id):
            return SourceOperationResult(connection_id, "refused", "sync_lease_active")
        self._schedule(registration)
        return SourceOperationResult(connection_id, "scheduled")

    async def stage_mapping(
        self, connection_id: str, *, homes: tuple[KnowledgeHome | str, ...]
    ) -> MappingProposalStatus | None:
        """Stage an operator-selected routing proposal for one granted source."""
        try:
            selected_homes = tuple(KnowledgeHome(home) for home in homes)
        except ValueError:
            return None
        registration = await self._find(connection_id)
        if registration is None or self._ingest_factory is None:
            return None
        description = await registration.adapter.inspect_source(
            InspectSource(connection_id=connection_id)
        )
        candidate = self._ingest_factory(description)
        ingest = await candidate if inspect.isawaitable(candidate) else candidate
        stage = getattr(ingest, "stage_mapping", None)
        allowed = getattr(ingest, "allowed_homes", None)
        source_id = _canonical_source_id(ingest, description)
        if not callable(stage) or not callable(allowed):
            return None
        allowed_homes = tuple(allowed(description))
        if not selected_homes or not set(selected_homes).issubset(allowed_homes):
            return None
        approval_id = await stage(description, selected_homes)
        proposal = MappingProposalStatus(
            connection_id=connection_id,
            source_id=source_id,
            allowed_homes=allowed_homes,
            homes=selected_homes,
            approval_id=str(approval_id),
        )
        self._mapping_statuses[connection_id] = proposal
        return proposal

    async def get_mapping_proposal(self, connection_id: str) -> MappingProposalStatus | None:
        """Return the last staged safe proposal, or source-compatible choices."""
        staged = self._mapping_statuses.get(connection_id)
        if staged is not None:
            registration = await self._find(connection_id)
            if registration is None or self._ingest_factory is None:
                return None
            source = await registration.adapter.inspect_source(
                InspectSource(connection_id=connection_id)
            )
            candidate = self._ingest_factory(source)
            ingest = await candidate if inspect.isawaitable(candidate) else candidate
            status = getattr(ingest, "mapping_approval_status", None)
            if not callable(status) or not staged.approval_id:
                return staged
            return replace(staged, approval_status=await status(staged.approval_id))
        registration = await self._find(connection_id)
        if registration is None or self._ingest_factory is None:
            return None
        description = await registration.adapter.inspect_source(
            InspectSource(connection_id=connection_id)
        )
        candidate = self._ingest_factory(description)
        ingest = await candidate if inspect.isawaitable(candidate) else candidate
        allowed = getattr(ingest, "allowed_homes", None)
        if not callable(allowed):
            return None
        return MappingProposalStatus(
            connection_id=connection_id,
            source_id=_canonical_source_id(ingest, description),
            allowed_homes=tuple(allowed(description)),
            homes=(),
            approval_id="",
            approval_status="not_staged",
        )

    async def list_resources(self, connection_id: str) -> tuple[SourceResource, ...]:
        """List resources the granted source explicitly permits an operator to select."""
        registration = await self._find(connection_id)
        if registration is None:
            return ()
        resources = await registration.adapter.list_source_resources(
            ListSourceResources(connection_id=connection_id)
        )
        selected = self._selected_resources.get(connection_id)
        if selected is None and self._resource_store is not None:
            selected = await self._resource_store.get(connection_id)
        return tuple(
            resource.model_copy(update={"selected": resource.resource_id in set(selected or ())})
            for resource in resources
        )

    async def select_resources(
        self, connection_id: str, *, resource_ids: tuple[str, ...]
    ) -> tuple[SourceResource, ...]:
        """Validate and apply an explicit resource selection before synchronization."""
        registration = await self._find(connection_id)
        if registration is None:
            return ()
        available = await self.list_resources(connection_id)
        allowed = {resource.resource_id for resource in available}
        if not resource_ids or not set(resource_ids).issubset(allowed):
            return tuple(
                resource.model_copy(update={"detail": "invalid_resource_selection"})
                for resource in available
            )
        await registration.adapter.select_source_resources(
            SelectSourceResources(connection_id=connection_id, resource_ids=resource_ids)
        )
        self._selected_resources[connection_id] = resource_ids
        if self._resource_store is not None:
            await self._resource_store.put(connection_id, resource_ids)
        return tuple(
            resource.model_copy(update={"selected": resource.resource_id in set(resource_ids)})
            for resource in available
        )

    async def list_review_items(
        self, *, status: str | None = None, source_id: str | None = None
    ) -> tuple[Any, ...]:
        """Read provenance-bearing profile candidates through the ingest port seam."""
        adapter = await self._first_ingest_adapter()
        list_items = getattr(adapter, "list_review_items", None) if adapter is not None else None
        if not callable(list_items):
            return ()
        return tuple(await list_items(status=status, source_id=source_id))

    async def resolve_review(self, review_id: str, decision: str) -> Any | None:
        """Apply an operator-authenticated review decision through the typed seam."""
        adapter = await self._first_ingest_adapter()
        resolve = getattr(adapter, "resolve_review", None) if adapter is not None else None
        return None if not callable(resolve) else await resolve(review_id, decision)

    async def profile_context(
        self, profile_id: str, *, clearance: str = "unclassified"
    ) -> Any | None:
        """Read approved profile context through the optional ingest seam."""
        adapter = await self._first_ingest_adapter()
        context = getattr(adapter, "profile_context", None) if adapter is not None else None
        return None if not callable(context) else await context(profile_id, clearance=clearance)

    async def profile_recall(
        self, profile_id: str, query: str, *, clearance: str = "unclassified"
    ) -> tuple[Any, ...]:
        """Search approved profile facts through the optional ingest seam."""
        adapter = await self._first_ingest_adapter()
        recall = getattr(adapter, "profile_recall", None) if adapter is not None else None
        return (
            ()
            if not callable(recall)
            else tuple(await recall(profile_id, query, clearance=clearance))
        )

    async def _monitor_loop(self) -> None:
        while not self._closed:
            registrations = await self._catalog.snapshot()
            for registration in registrations:
                if registration.connection_id not in self._paused:
                    self._statuses.setdefault(
                        registration.connection_id,
                        SourceRuntimeStatus(
                            connection_id=registration.connection_id, status="inspecting"
                        ),
                    )
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
            selected = self._selected_resources.get(connection_id)
            if selected is None and self._resource_store is not None:
                selected = await self._resource_store.get(connection_id)
            if selected:
                await registration.adapter.select_source_resources(
                    SelectSourceResources(connection_id=connection_id, resource_ids=selected)
                )
            description = await registration.adapter.inspect_source(
                InspectSource(connection_id=connection_id)
            )
            candidate = self._ingest_factory(description)
            ingest = await candidate if inspect.isawaitable(candidate) else candidate
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id,
                source_id=_canonical_source_id(ingest, description),
                status="syncing",
                description=description,
            )
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
                source_id=_canonical_source_id(ingest, description),
                status=result.status.value,
                description=description,
                state=result,
            )

    async def _inspect_registration(self, registration: SourceRegistration) -> None:
        """Populate the safe descriptor before the operator sees a blank source row."""
        connection_id = registration.connection_id
        try:
            description = await registration.adapter.inspect_source(
                InspectSource(connection_id=connection_id)
            )
            source_id = ""
            if self._ingest_factory is not None:
                candidate = self._ingest_factory(description)
                ingest = await candidate if inspect.isawaitable(candidate) else candidate
                source_id = _canonical_source_id(ingest, description)
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id,
                source_id=source_id,
                status="awaiting_mapping",
                description=description,
            )
        except Exception:
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id, status="failed", detail="source_inspection_failed"
            )

    async def _first_ingest_adapter(self) -> IngestPort | None:
        if self._ingest_factory is None:
            return None
        registrations = await self._catalog.snapshot()
        if not registrations:
            return None
        registration = registrations[0]
        source = await registration.adapter.inspect_source(
            InspectSource(connection_id=registration.connection_id)
        )
        candidate = self._ingest_factory(source)
        return await candidate if inspect.isawaitable(candidate) else candidate

    async def _open_store(self) -> Any:
        if self._sync_store_opener is None:
            return None
        try:
            return await self._sync_store_opener()
        except Exception:
            _logger.warning("connected-data ArcStore backend unavailable", exc_info=True)
            return None

    async def _open_resource_store(self) -> SourceSelectionStore | None:
        if self._resource_selection_store_opener is None:
            return None
        try:
            return await self._resource_selection_store_opener()
        except Exception:
            _logger.warning("connected-data resource selection store unavailable", exc_info=True)
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


def _canonical_source_id(ingest: IngestPort, description: SourceDescription) -> str:
    canonical = getattr(ingest, "canonical_source_id", None)
    return str(canonical(description)) if callable(canonical) else ""


__all__ = [
    "ConnectedDataService",
    "IngestPortFactory",
    "MappingProposalStatus",
    "SourceOperationResult",
    "SourceRuntimeStatus",
]
