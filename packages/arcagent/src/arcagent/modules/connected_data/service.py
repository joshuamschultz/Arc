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
from arcagent.extension.source import (
    InspectSource,
    SelectSourceResources,
    SourceError,
    SourceResource,
)
from arcagent.extension.source_catalog import SourceCatalog, SourceRegistration
from arcagent.modules.connected_data.coordinator import ConnectedDataCoordinator

_logger = logging.getLogger("arcagent.modules.connected_data.service")

IngestPortFactory = Callable[[SourceDescription], IngestPort | Awaitable[IngestPort]]


class SourceSelectionStore(Protocol):
    async def get(self, connection_id: str) -> tuple[str, ...]: ...

    async def put(self, connection_id: str, resource_ids: tuple[str, ...]) -> None: ...

    async def delete(self, connection_id: str) -> None: ...


class MappingProposalStore(Protocol):
    async def get(self, connection_id: str) -> dict[str, Any] | None: ...

    async def put(self, connection_id: str, proposal: dict[str, Any]) -> None: ...

    async def delete(self, connection_id: str) -> None: ...


#: Marks a status that came from an inspection that could not run, so a later
#: listing knows to ask again rather than treat it as settled.
_INSPECTION_FAILED = "source_inspection_failed"


class SourceRefusedError(RuntimeError):
    """The source understood the request and rejected it.

    Distinct from unreachable: nothing is wrong with the connection and retrying
    changes nothing. The adapter's own words carry the remedy — a document store
    that can only follow one root says so — so they are kept and shown rather
    than flattened into "the provider did not answer".
    """

    def __init__(self, connection_id: str, code: str, detail: str) -> None:
        super().__init__(detail or f"source {connection_id} refused the request")
        self.connection_id = connection_id
        self.code = code
        self.detail = detail


class SourceUnreachableError(RuntimeError):
    """A granted source could not be read — the provider refused or was unreachable.

    The background sync loop already treats this as a degraded source. The
    operator-facing reads could not: they let the adapter's own exception escape
    to the HTTP boundary, where a read timeout or an expired credential became
    an opaque 500 on the mapping screen. Typed here so the boundary can say
    which source failed and that retrying is the remedy.
    """

    def __init__(self, connection_id: str) -> None:
        super().__init__(f"source {connection_id} could not be inspected")
        self.connection_id = connection_id


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
        mapping_proposal_store_opener: Callable[[], Awaitable[MappingProposalStore]] | None = None,
        audit: AuditCallback | None = None,
        interval_seconds: float = 3600.0,
    ) -> None:
        self._catalog = catalog
        self._agent_did = agent_did
        self._sync_store_opener = sync_store_opener
        self._resource_selection_store_opener = resource_selection_store_opener
        self._mapping_proposal_store_opener = mapping_proposal_store_opener
        self._ingest_factory = ingest_factory
        self._limits = limits
        self._semaphore = asyncio.Semaphore(global_concurrency)
        self._audit = audit
        self._interval = interval_seconds
        self._store: Any = None
        self._resource_store: SourceSelectionStore | None = None
        self._mapping_store: MappingProposalStore | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._statuses: dict[str, SourceRuntimeStatus] = {}
        self._mapping_statuses: dict[str, MappingProposalStatus] = {}
        self._descriptions: dict[str, SourceDescription] = {}
        self._selected_resources: dict[str, tuple[str, ...]] = {}
        self._inspections: dict[str, asyncio.Task[None]] = {}
        self._paused: set[str] = set()
        self._wake = asyncio.Event()
        self._closed = False

    async def start(self) -> None:
        """Start the monitor; an unavailable optional backend becomes degraded."""
        self._store = await self._open_store()
        self._resource_store = await self._open_resource_store()
        self._mapping_store = await self._open_mapping_store()
        self._monitor = asyncio.create_task(self._monitor_loop(), name="connected-data-sync")
        self._wake.set()

    async def close(self) -> None:
        """Cancel workers and release only resources owned by this service."""
        self._closed = True
        for task in tuple(self._inspections.values()):
            task.cancel()
        self._inspections.clear()
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
        """Return safe operational state for UI/operator surfaces.

        Never waits on a provider. Inspecting one inline meant a single sick
        connector — a vendor CLI that hangs rather than answering — held the
        whole connections page for minutes and every other healthy source with
        it. A source not yet described is listed as inspecting and filled in by
        a background pass.
        """
        registrations = sorted(
            await self._catalog.snapshot(), key=lambda entry: entry.connection_id
        )
        for registration in registrations:
            known = self._statuses.get(registration.connection_id)
            if known is None:
                self._statuses[registration.connection_id] = SourceRuntimeStatus(
                    connection_id=registration.connection_id,
                    status="idle",
                    detail="inspecting",
                )
                self._start_inspection(registration)
            elif known.detail == _INSPECTION_FAILED:
                # A provider that was briefly unreachable — mid-startup, mid
                # token refresh — was written off for the life of the process,
                # because a cached failure meant it was never asked again. Try
                # once more in the background; the in-flight guard keeps it to
                # one attempt at a time.
                self._start_inspection(registration)
        return tuple(self._statuses[registration.connection_id] for registration in registrations)

    def _start_inspection(self, registration: SourceRegistration) -> None:
        """Describe a source out of band, at most one attempt in flight."""
        connection_id = registration.connection_id
        existing = self._inspections.get(connection_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._inspect_registration(registration),
            name=f"connected-data-inspect:{connection_id}",
        )
        self._inspections[connection_id] = task
        task.add_done_callback(lambda _: self._inspections.pop(connection_id, None))

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
        """Purge a source before removing its registration and durable state."""
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.add(connection_id)
        await self._cancel(connection_id)
        ingest, description = await self._ingest_for(registration, use_cached=True)
        if ingest is None or description is None:
            return SourceOperationResult(connection_id, "refused", "ingest_port_unavailable")
        try:
            if self._store is None or not await self._store.purge(self._agent_did, connection_id):
                return SourceOperationResult(connection_id, "refused", "sync_lease_active")
            await ingest.purge_source(description)
            if self._resource_store is not None:
                await self._resource_store.delete(connection_id)
            if self._mapping_store is not None:
                await self._mapping_store.delete(connection_id)
        except Exception:
            _logger.exception("connected-data source purge failed: %s", connection_id)
            return SourceOperationResult(connection_id, "refused", "source_purge_failed")
        self._statuses.pop(connection_id, None)
        self._mapping_statuses.pop(connection_id, None)
        self._selected_resources.pop(connection_id, None)
        self._descriptions.pop(connection_id, None)
        await self._catalog.unregister(connection_id)
        return SourceOperationResult(connection_id, "revoked")

    async def reindex(self, connection_id: str) -> SourceOperationResult:
        """Reset a durable checkpoint, then backfill the current source snapshot."""
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.add(connection_id)
        await self._cancel(connection_id)
        ingest, description = await self._ingest_for(registration)
        if ingest is None or description is None:
            return SourceOperationResult(connection_id, "refused", "ingest_port_unavailable")
        try:
            await ingest.reset_source(description)
        except Exception:
            _logger.exception("connected-data source reset failed: %s", connection_id)
            return SourceOperationResult(connection_id, "refused", "source_reset_failed")
        if self._store is None or not await self._store.reset(self._agent_did, connection_id):
            return SourceOperationResult(connection_id, "refused", "sync_lease_active")
        self._paused.discard(connection_id)
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
        raw_description = await self._inspect(registration)
        candidate = self._ingest_factory(raw_description)
        ingest = await candidate if inspect.isawaitable(candidate) else candidate
        description = await self._describe(registration, ingest)
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
        if self._mapping_store is not None:
            await self._mapping_store.put(
                connection_id,
                {
                    "source_id": proposal.source_id,
                    "allowed_homes": [str(home) for home in proposal.allowed_homes],
                    "homes": [str(home) for home in proposal.homes],
                    "approval_id": proposal.approval_id,
                },
            )
        return proposal

    async def get_mapping_proposal(self, connection_id: str) -> MappingProposalStatus | None:
        """Return the last staged safe proposal, or source-compatible choices."""
        staged = await self._staged_proposal(connection_id)
        if staged is not None:
            registration = await self._find(connection_id)
            if registration is None or self._ingest_factory is None:
                return None
            raw_source = await self._inspect(registration)
            candidate = self._ingest_factory(raw_source)
            ingest = await candidate if inspect.isawaitable(candidate) else candidate
            await self._describe(registration, ingest)
            status = getattr(ingest, "mapping_approval_status", None)
            if not callable(status) or not staged.approval_id:
                return staged
            return replace(staged, approval_status=await status(staged.approval_id))
        registration = await self._find(connection_id)
        if registration is None or self._ingest_factory is None:
            return None
        raw_description = await self._inspect(registration)
        candidate = self._ingest_factory(raw_description)
        ingest = await candidate if inspect.isawaitable(candidate) else candidate
        description = await self._describe(registration, ingest)
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
        try:
            resources = await registration.adapter.list_source_resources(
                ListSourceResources(connection_id=connection_id)
            )
        except SourceError as exc:
            raise SourceRefusedError(connection_id, str(exc.code), exc.detail) from exc
        except Exception as exc:
            _logger.warning("connected-data resource listing failed: %s", connection_id)
            raise SourceUnreachableError(connection_id) from exc
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
        try:
            await registration.adapter.select_source_resources(
                SelectSourceResources(connection_id=connection_id, resource_ids=resource_ids)
            )
        except SourceError as exc:
            raise SourceRefusedError(connection_id, str(exc.code), exc.detail) from exc
        except Exception as exc:
            _logger.warning("connected-data resource selection failed: %s", connection_id)
            raise SourceUnreachableError(connection_id) from exc
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
            raw_description = await registration.adapter.inspect_source(
                InspectSource(connection_id=connection_id)
            )
            candidate = self._ingest_factory(raw_description)
            ingest = await candidate if inspect.isawaitable(candidate) else candidate
            description = await self._with_generation(raw_description, ingest)
            self._descriptions[connection_id] = description
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
                description = await self._with_generation(description, ingest)
                source_id = _canonical_source_id(ingest, description)
            self._descriptions[connection_id] = description
            # Read back what this source actually did, rather than declaring it
            # unmapped. The sync state is durable and the runtime status was
            # not, so every restart wiped a completed source back to
            # "awaiting_mapping" with no pages, no bytes and no source id —
            # which also left its documents unaddressable and its search empty.
            state = await self._persisted_state(source_id)
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id,
                source_id=source_id,
                status=str(state.status.value) if state is not None else "awaiting_mapping",
                detail=state.error_code or "" if state is not None else "",
                description=description,
                state=state,
            )
        except Exception:
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id, status="failed", detail=_INSPECTION_FAILED
            )

    async def _persisted_state(self, source_id: str) -> SyncState | None:
        """The durable record of this source's last run, if there is a store."""
        if self._store is None or not source_id:
            return None
        try:
            state: SyncState = await self._store.get_state(self._agent_did, source_id)
        except Exception:
            _logger.warning("connected-data sync state unreadable: %s", source_id)
            return None
        return state

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

    async def _ingest_for(
        self, registration: SourceRegistration, *, use_cached: bool = False
    ) -> tuple[IngestPort | None, SourceDescription | None]:
        if self._ingest_factory is None:
            return None, None
        try:
            description = (
                self._descriptions.get(registration.connection_id) if use_cached else None
            )
            if description is None:
                description = await registration.adapter.inspect_source(
                    InspectSource(connection_id=registration.connection_id)
                )
            candidate = self._ingest_factory(description)
            ingest = await candidate if inspect.isawaitable(candidate) else candidate
            if not use_cached:
                description = await self._with_generation(description, ingest)
                self._descriptions[registration.connection_id] = description
            return ingest, description
        except Exception:
            _logger.exception(
                "connected-data source inspection failed: %s", registration.connection_id
            )
            return None, None

    async def _cancel(self, connection_id: str) -> None:
        task = self._tasks.pop(connection_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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

    async def _open_mapping_store(self) -> MappingProposalStore | None:
        if self._mapping_proposal_store_opener is None:
            return None
        try:
            return await self._mapping_proposal_store_opener()
        except Exception:
            _logger.warning("connected-data mapping proposal store unavailable", exc_info=True)
            return None

    async def _staged_proposal(self, connection_id: str) -> MappingProposalStatus | None:
        """The operator's staged choice, from memory or the durable store."""
        staged = self._mapping_statuses.get(connection_id)
        if staged is not None or self._mapping_store is None:
            return staged
        row = await self._mapping_store.get(connection_id)
        if row is None:
            return None
        try:
            restored = MappingProposalStatus(
                connection_id=connection_id,
                source_id=str(row.get("source_id", "")),
                allowed_homes=tuple(row.get("allowed_homes", ())),
                homes=tuple(row.get("homes", ())),
                approval_id=str(row.get("approval_id", "")),
            )
        except (TypeError, ValueError):
            _logger.warning("connected-data staged mapping unreadable: %s", connection_id)
            return None
        self._mapping_statuses[connection_id] = restored
        return restored

    async def _find(self, connection_id: str) -> SourceRegistration | None:
        for registration in await self._catalog.snapshot():
            if registration.connection_id == connection_id:
                return registration
        return None

    def _set_degraded(self, connection_id: str, detail: str) -> None:
        self._statuses[connection_id] = SourceRuntimeStatus(
            connection_id=connection_id, status="degraded", detail=detail
        )

    async def _inspect(self, registration: SourceRegistration) -> Any:
        """Inspect a source, turning a provider failure into a typed refusal.

        The adapter runs third-party code against someone else's service, so it
        can raise anything at all. Every operator-facing read goes through here
        so none of them can hand a raw provider exception to a caller.
        """
        try:
            return await registration.adapter.inspect_source(
                InspectSource(connection_id=registration.connection_id)
            )
        except SourceError as exc:
            raise SourceRefusedError(
                registration.connection_id, str(exc.code), exc.detail
            ) from exc
        except Exception as exc:
            _logger.warning(
                "connected-data source inspection failed: %s", registration.connection_id
            )
            raise SourceUnreachableError(registration.connection_id) from exc

    async def _describe(
        self, registration: SourceRegistration, ingest: IngestPort
    ) -> SourceDescription:
        description = await self._inspect(registration)
        description = await self._with_generation(description, ingest)
        self._descriptions[registration.connection_id] = description
        return description

    @staticmethod
    async def _with_generation(
        description: SourceDescription, ingest: IngestPort
    ) -> SourceDescription:
        generation = getattr(ingest, "source_generation", None)
        if not callable(generation):
            return description
        value = await generation(description)
        return description.model_copy(update={"generation": int(value)})


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
