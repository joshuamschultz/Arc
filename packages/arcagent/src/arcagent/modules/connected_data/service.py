"""Optional source synchronization service composed from typed seams."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol

from arcagent.connected_data import (
    AuditCallback,
    IngestPort,
    KnowledgeHome,
    ListSourceResources,
    SourceDescription,
    SyncError,
    SyncLimits,
    SyncState,
    SyncStatePort,
    SyncStatus,
)
from arcagent.extension.connection_health import (
    HealthReporter,
    HealthSignal,
    classify,
)
from arcagent.extension.credentials import CredentialRenewalError
from arcagent.extension.knowledge_subscriptions import (
    KnowledgeSubscription,
    knowledge_principal,
)
from arcagent.extension.source import (
    InspectSource,
    SelectSourceResources,
    SourceError,
    SourceResource,
)
from arcagent.extension.source_catalog import SourceCatalog, SourceRegistration
from arcagent.extension.state import ConnectionStatus
from arcagent.modules.connected_data.coordinator import ConnectedDataCoordinator
from arcagent.modules.connected_data.health import (
    REPEATED_FAILURES,
    ConnectionHealthTracker,
    is_terminal_sync_failure,
)
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter
from arcagent.modules.connected_data.shared import SHARED_HOMES, SharedKnowledge
from arcagent.modules.connected_data.supervision import SyncSchedule

_logger = logging.getLogger("arcagent.modules.connected_data.service")
_CATALOG_RETRY_MAX_SECONDS = 30.0
#: How soon an automatic move is tried again when another agent is moving into the
#: same shared store right now (it holds the store's lease while it adopts).
_MIGRATION_RETRY_SECONDS = 30.0

IngestPortFactory = Callable[[SourceDescription], IngestPort | Awaitable[IngestPort]]

#: How long the short-lived lease that stamps a terminal status may be held.
_TERMINAL_LEASE_SECONDS = 30.0


class _NullHealthReporter:
    """The health path when no arcstore is wired: reports vanish, statuses are unknown.

    A standalone agent keeps its local backoff; it just has no shared record to
    write to, which a caller reads as "unknown", not "healthy".
    """

    async def report(self, connection: str, signal: HealthSignal) -> None:
        return None

    async def statuses(self) -> dict[str, ConnectionStatus]:
        return {}


class CredentialRenewal(Protocol):
    """Proactive renewal of one connection's credential before a sync reads it (P18-2)."""

    async def ensure_fresh(self, connection: str) -> bool: ...


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
    #: How many documents this source has in its indexed pool. Derived from the
    #: ingest port, not the sync counters, so the card can show what is actually
    #: searchable rather than only how many transfer pages were read.
    documents_indexed: int = 0


@dataclass(frozen=True)
class CatalogEntry:
    """One connected source as the agent's prompt and tools describe it."""

    name: str
    kind: str
    status: str
    #: Where the operator mapped this source's knowledge; empty until staged.
    homes: tuple[KnowledgeHome, ...]

    @property
    def homes_text(self) -> str:
        """The homes as a prompt/tool line shows them."""
        return ", ".join(home.value for home in self.homes) or "not mapped"


@dataclass(frozen=True)
class SourceOperationResult:
    """Typed, safe outcome for one operator lifecycle request."""

    connection_id: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class MigrationResult:
    """What moving one connection into its shared store did (P18-4)."""

    connection_id: str
    #: migrated | would_migrate | already_shared | nothing_to_migrate | not_eligible | refused
    status: str
    detail: str = ""
    documents: int = 0
    adopted: int = 0
    deduplicated: int = 0
    skipped: int = 0


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
        renewals: CredentialRenewal | None = None,
        health: HealthReporter | None = None,
        restart_backoff_seconds: float = 30.0,
        restart_backoff_max_seconds: float = 1800.0,
        stall_grace_seconds: float = 120.0,
        terminal_recheck_seconds: float = 3600.0,
        failure_ceiling: int = 5,
        shared: SharedKnowledge | None = None,
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
        self._stop_listening: Callable[[], None] | None = None
        self._closed = False
        # Proactive credential renewal (COMP-007, P18-2) and terminal-failure
        # health (COMP-008). ``renewals`` is the agent's credential broker
        # registry; None (a standalone agent with no custody) renews nothing here,
        # and an attachment still renews on demand through its handle.
        self._reporter: HealthReporter = health or _NullHealthReporter()
        self._renewals = renewals
        # A credential that died is rechecked slowly rather than never: it can come
        # back without any act inside Arc (a host binary re-signed in its own
        # keyring), and a latch nothing clears is a source that never syncs again.
        self._health = ConnectionHealthTracker(recheck_after=terminal_recheck_seconds)
        self._timing = SyncSchedule(
            interval_seconds=interval_seconds,
            backoff_seconds=restart_backoff_seconds,
            backoff_max_seconds=restart_backoff_max_seconds,
        )
        self._stall_grace = stall_grace_seconds
        self._failure_ceiling = failure_ceiling
        # One sync and one store per connection (P18-4). ``_lanes`` holds the
        # connections this agent reads from a shared store, by connection id; a
        # connection absent from it is synced into the agent's own store as before.
        self._shared = shared
        self._lanes: dict[str, KnowledgeSubscription] = {}
        # Runs an operator asked for: they skip the "another subscriber synced it
        # recently" shortcut, which otherwise makes a shared connection's sync free.
        self._forced: set[str] = set()
        self._migration_retries: dict[str, asyncio.TimerHandle] = {}

    async def start(self) -> None:
        """Start the monitor; an unavailable optional backend becomes degraded."""
        self._store = await self._open_store()
        self._resource_store = await self._open_resource_store()
        self._mapping_store = await self._open_mapping_store()
        # A source attached, replaced or removed after start is seen at once, not
        # an interval later: the monitor otherwise slept on an empty catalog.
        self._stop_listening = self._catalog.on_change(self._wake.set)
        self._monitor = asyncio.create_task(self._monitor_loop(), name="connected-data-sync")
        self._wake.set()

    async def close(self) -> None:
        """Cancel workers and release only resources owned by this service."""
        self._closed = True
        if self._stop_listening is not None:
            self._stop_listening()
            self._stop_listening = None
        for task in tuple(self._inspections.values()):
            task.cancel()
        self._inspections.clear()
        for retry in self._migration_retries.values():
            retry.cancel()
        self._migration_retries.clear()
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
            elif registration.connection_id in self._lanes:
                # A shared connection may have been synced by another subscriber.
                await self._refresh_shared_status(registration.connection_id)
        return tuple(self._statuses[registration.connection_id] for registration in registrations)

    async def catalog_entries(self, *, refresh: bool = False) -> tuple[CatalogEntry, ...]:
        """Describe every connected source from what is already known.

        The agent's prompt reads this on EVERY turn, so it answers from the cache
        and the durable mapping row only and never calls an adapter: a vendor CLI
        that hangs must not stall every turn of every granted
        agent. ``refresh`` is for a tool the agent chose to call, which may start the
        usual background inspection of anything not yet described.
        """
        if refresh:
            statuses: tuple[SourceRuntimeStatus, ...] = await self.list_sources()
        else:
            registrations = sorted(
                await self._catalog.snapshot(), key=lambda entry: entry.connection_id
            )
            known = (self._statuses.get(entry.connection_id) for entry in registrations)
            statuses = tuple(status for status in known if status is not None)
        entries: list[CatalogEntry] = []
        for status in statuses:
            source = status.description
            if source is None:
                continue
            staged = await self._staged_proposal(status.connection_id)
            entries.append(
                CatalogEntry(
                    name=source.display_name or source.source_kind,
                    kind=source.source_kind,
                    status=status.status,
                    homes=staged.homes if staged is not None else (),
                )
            )
        return tuple(entries)

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

        def _inspected(_: asyncio.Task[None]) -> None:
            self._inspections.pop(connection_id, None)
            # The monitor waits for a source's first description before its first run.
            self._wake.set()

        task.add_done_callback(_inspected)

    async def sync_now(self, connection_id: str) -> SourceOperationResult:
        """Schedule one source immediately; unknown or paused sources are refused."""
        if connection_id in self._paused:
            return SourceOperationResult(connection_id, "refused", "source_paused")
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        # An explicit operator sync clears any needs-attention backoff.
        self._health.clear(connection_id)
        self._timing.run_now(connection_id)
        self._forced.add(connection_id)
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
        self._health.clear(connection_id)
        self._timing.run_now(connection_id)
        self._forced.add(connection_id)
        self._wake.set()
        return SourceOperationResult(connection_id, "scheduled")

    async def revoke(self, connection_id: str) -> SourceOperationResult:
        """Purge a source before removing its registration and durable state.

        On a shared connection (P18-4) the agent's subscription goes first, so its
        reads stop before anything else happens; the shared store itself is purged
        only when no other agent still reads it.
        """
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.add(connection_id)
        await self._cancel(connection_id)
        try:
            shared = await self._unsubscribe(connection_id, reason="revoked")
        except Exception:
            # Fail closed: while the subscription cannot be removed the agent can
            # still read, so the revoke is not done and reconcile tries again.
            _logger.exception("connected-data unsubscribe failed: %s", connection_id)
            return SourceOperationResult(connection_id, "refused", "unsubscribe_failed")
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
        finally:
            await _release(ingest)
        if shared:
            await self._purge_unread_store(connection_id, description)
        self._statuses.pop(connection_id, None)
        self._mapping_statuses.pop(connection_id, None)
        self._selected_resources.pop(connection_id, None)
        self._descriptions.pop(connection_id, None)
        self._health.clear(connection_id)
        self._timing.forget(connection_id)
        await self._catalog.unregister(connection_id)
        # The pause only fenced the purge. A connection granted again under the same
        # name is a new source and must be free to sync.
        self._paused.discard(connection_id)
        return SourceOperationResult(connection_id, "revoked")

    async def reindex(self, connection_id: str) -> SourceOperationResult:
        """Reset a durable checkpoint, then backfill the current source snapshot.

        On a shared connection this resets the one shared store: every agent that
        reads it sees the backfill.
        """
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        self._paused.add(connection_id)
        await self._cancel(connection_id)
        ingest, description = await self._port_for(registration)
        if ingest is None or description is None:
            return SourceOperationResult(connection_id, "refused", "ingest_port_unavailable")
        try:
            await ingest.reset_source(description)
        except Exception:
            _logger.exception("connected-data source reset failed: %s", connection_id)
            return SourceOperationResult(connection_id, "refused", "source_reset_failed")
        finally:
            await _release(ingest)
        key = self._sync_key(connection_id)
        if self._store is None or not await self._store.reset(key, connection_id):
            return SourceOperationResult(connection_id, "refused", "sync_lease_active")
        self._paused.discard(connection_id)
        self._health.clear(connection_id)
        self._timing.run_now(connection_id)
        self._forced.add(connection_id)
        self._schedule(registration)
        return SourceOperationResult(connection_id, "scheduled")

    async def relayout(self, connection_id: str) -> SourceOperationResult:
        """Move a source's stored documents to their mirrored folders, no re-embedding.

        Sync is held off while files move and put back exactly as it was found.
        """
        registration = await self._find(connection_id)
        if registration is None:
            return SourceOperationResult(connection_id, "not_found")
        was_paused = connection_id in self._paused
        self._paused.add(connection_id)
        await self._cancel(connection_id)
        ingest, description = await self._port_for(registration)
        relayout = getattr(ingest, "relayout_source", None)
        if ingest is None or description is None or not callable(relayout):
            if not was_paused:
                self._paused.discard(connection_id)
            return SourceOperationResult(connection_id, "refused", "relayout_unavailable")
        try:
            counts = await relayout(description)
        except Exception:
            _logger.exception("connected-data relayout failed: %s", connection_id)
            return SourceOperationResult(connection_id, "refused", "relayout_failed")
        finally:
            await _release(ingest)
            if not was_paused:
                self._paused.discard(connection_id)
        detail = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        return SourceOperationResult(connection_id, "relayout_done", detail)

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
        """Start every source that is due; outlive any one bad tick.

        Sleeps until the next source is due or something wakes it (an operator
        action, a run ending). Every source runs in its own supervised task.
        """
        while not self._closed:
            try:
                registrations = await self._catalog.snapshot()
            except Exception as exc:
                _logger.warning(
                    "connected-data catalog unavailable; retrying (%s)", type(exc).__name__
                )
                await asyncio.sleep(min(max(self._interval, 0.1), _CATALOG_RETRY_MAX_SECONDS))
                continue
            try:
                self._schedule_due(registrations, await self._reporter.statuses())
            except Exception as exc:  # reason: the monitor must outlive one bad tick
                _logger.exception("connected-data monitor tick failed")
                await self._emit("connected_data.sync.monitor_failed", {"error": _name(exc)})
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._timing.seconds_until_next()
                )
            except TimeoutError:
                pass
            self._wake.clear()

    def _schedule_due(
        self,
        registrations: tuple[SourceRegistration, ...],
        statuses: dict[str, ConnectionStatus],
    ) -> None:
        for registration in registrations:
            connection_id = registration.connection_id
            if connection_id in self._paused or not self._timing.is_due(connection_id):
                continue
            # Describe a source before its first run. The description reads the
            # durable record, and a connection that already needs a human must be
            # backed off BEFORE it is run again, not noticed after a restart has
            # hammered its dead credential one more time. The finished inspection
            # wakes this loop.
            if connection_id in self._inspections:
                continue
            if connection_id not in self._statuses:
                self._statuses[connection_id] = SourceRuntimeStatus(
                    connection_id=connection_id, status="idle", detail="inspecting"
                )
                self._start_inspection(registration)
                continue
            # A source that needs a human is backed off: re-running it every
            # tick just hammers a dead credential and floods the audit log. The
            # backoff ends the moment the shared record stops saying "needs you":
            # the operator reconnected, and waiting out the recheck window would
            # leave a working account unsynced for up to an hour.
            if self._health.is_backed_off(connection_id):
                if self._reconnected(connection_id, statuses):
                    self._health.clear(connection_id)
                else:
                    continue
            self._schedule(registration)

    @staticmethod
    def _reconnected(connection_id: str, statuses: dict[str, ConnectionStatus]) -> bool:
        """True when the shared record says this connection no longer waits on a person."""
        status = statuses.get(_instance_of(connection_id))
        return status is not None and status != "needs_you"

    def _schedule(self, registration: SourceRegistration) -> None:
        connection_id = registration.connection_id
        current = self._tasks.get(connection_id)
        if current is not None and not current.done():
            return
        self._timing.started(connection_id)
        task = asyncio.create_task(self._run_one(registration), name=f"sync:{connection_id}")
        self._tasks[connection_id] = task
        task.add_done_callback(lambda done: self._run_ended(connection_id, done))

    def _run_ended(self, connection_id: str, task: asyncio.Task[None]) -> None:
        """Forget a finished run and let the monitor look at what is due next."""
        if self._tasks.get(connection_id) is task:
            self._tasks.pop(connection_id, None)
        self._wake.set()

    async def _run_one(self, registration: SourceRegistration) -> None:
        """One supervised run: its crash or stall is this source's alone."""
        connection_id = registration.connection_id
        try:
            # Cancelled if the operator replaces or removes this source: the
            # coordinator marks the run cancelled and keeps its cursor.
            async with self._catalog.lease(connection_id, cancel_on_retire=True) as leased:
                more_work = False if leased is None else await self._run_leased(leased)
        except asyncio.CancelledError:
            raise
        except _SyncStalledError:
            await self._run_failed(connection_id, "stalled", "sync_stalled")
        except SyncError as exc:
            # A terminal failure (a revoked credential) needs a human, not a
            # retry: surface it as needs_attention, notify once, and back the
            # source off the timer. Anything else is retried after a backoff.
            if is_terminal_sync_failure(exc.code):
                await self._mark_needs_attention(connection_id, exc.code or "auth_required")
                self._timing.completed(connection_id, more_work=False)
            else:
                await self._run_failed(connection_id, "crashed", exc.code or "", exc)
        except Exception as exc:  # reason: one source's crash must not reach the service
            await self._run_failed(connection_id, "crashed", _name(exc), exc)
        else:
            self._timing.completed(connection_id, more_work=more_work)

    async def _run_failed(
        self, connection_id: str, event: str, detail: str, exc: Exception | None = None
    ) -> None:
        """Mark, audit and back off one failed run; the next try comes on the timer.

        Past the consecutive-failure ceiling the source is handed to a human: an
        unknown error otherwise means "retry", and retrying one for a month is a
        silent outage. The failure is also made durable: a stall or a lost lease
        never reached the store's own end-of-run write, which left a tracker
        ``running`` for nine days.
        """
        _logger.warning("connected-data source %s: %s", event, connection_id, exc_info=exc)
        state = await self._record_failure(connection_id, detail)
        self._statuses[connection_id] = self._still_described(
            connection_id, status="failed", detail=detail, state=state
        )
        delay = self._timing.failed(connection_id)
        failures = self._timing.failures(connection_id)
        await self._emit(
            f"connected_data.sync.{event}",
            {
                "source": _safe_id(connection_id),
                "error": detail if exc is None else _name(exc),
                "failures": failures,
                "retry_in_seconds": round(delay, 3),
            },
        )
        await self._report(
            connection_id, ok=False, code=detail, text="" if exc is None else str(exc)
        )
        if failures >= self._failure_ceiling:
            await self._mark_needs_attention(connection_id, REPEATED_FAILURES)

    async def _record_failure(
        self, connection_id: str, code: str, *, force: bool = False
    ) -> SyncState | None:
        """Leave a terminal status behind when the run could not.

        A coordinator that ended its own run has already written ``failed`` with a
        reason. One that was cut off (a stall) or lost its lease could not, and its
        row still says ``running`` or ``cancelled``: this stamps ``failed`` with the
        reason, leaving ``last_synced_at`` (the last GOOD sync) untouched. It runs
        under a short lease of its own so it can never overwrite a live run. ``force``
        replaces the coordinator's own reason, for the one the service decides itself
        (a ceiling of repeated failures), which a restart must be able to read.
        """
        state = await self._persisted_state(connection_id)
        failed = state is not None and state.status is SyncStatus.FAILED
        if self._store is None or (
            failed and (not force or (state is not None and state.error_code == code))
        ):
            return state
        owner = f"{self._agent_did}:terminal:{uuid.uuid4().hex}"
        key = self._sync_key(connection_id)
        try:
            lease = await self._store.acquire_lease(
                key, connection_id, owner, ttl_seconds=_TERMINAL_LEASE_SECONDS
            )
            if lease is None:
                return state
            try:
                await self._store.set_status(
                    key,
                    connection_id,
                    SyncStatus.FAILED,
                    owner_id=owner,
                    fencing_token=lease.fencing_token,
                    error_code=code,
                )
            finally:
                await self._store.release_lease(
                    key,
                    connection_id,
                    owner_id=owner,
                    fencing_token=lease.fencing_token,
                )
        except Exception:  # reason: a failed status write must not mask the run's own failure
            _logger.warning("connected-data terminal status unwritable: %s", connection_id)
            return state
        return await self._persisted_state(connection_id)

    def _stall_seconds(self) -> float:
        """How long one run may take before it is treated as hung.

        The coordinator bounds its own working time by ``max_seconds`` and rests
        for the remainder of its duty cycle, so a healthy run takes at most
        ``max_seconds / max_duty_fraction`` of wall time. A run that outlives
        that plus a grace is stuck on something that never returns.
        """
        return self._limits.max_seconds / self._limits.max_duty_fraction + self._stall_grace

    async def _run_leased(self, registration: SourceRegistration) -> bool:
        """Run one source with a slot and a stall bound; True if it has more to do."""
        async with self._semaphore:
            try:
                async with asyncio.timeout(self._stall_seconds()) as guard:
                    return await self._sync_leased(registration)
            except TimeoutError as exc:
                if guard.expired():
                    raise _SyncStalledError(registration.connection_id) from exc
                raise

    async def _sync_leased(self, registration: SourceRegistration) -> bool:
        connection_id = registration.connection_id
        if self._store is None:
            self._set_degraded(connection_id, "arcstore_unavailable")
            return False
        if self._ingest_factory is None:
            self._set_degraded(connection_id, "ingest_port_unavailable")
            return False
        # Renew the credential proactively, before any read hits the provider,
        # so a token in its renewal window is refreshed on the clock rather
        # than only after a call returns 401. A terminal renewal failure has
        # already marked the connection and escalated to the operator path;
        # aborting here keeps the sync from hammering a rejected credential.
        if self._renewals is not None:
            try:
                await self._renewals.ensure_fresh(connection_id)
            except CredentialRenewalError as exc:
                if not exc.terminal:
                    raise  # counted: the run-failed path backs off and reports it
                await self._mark_needs_attention(connection_id, exc.error_code)
                return False
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
        # An operator-requested run is consumed here, whichever store it syncs.
        forced = connection_id in self._forced
        self._forced.discard(connection_id)
        # An own copy of a shareable connection moves into the shared store before
        # this run picks a store, so a run never syncs a copy it is about to retire.
        await self._migrate_automatically(registration)
        candidate = self._ingest_factory(raw_description)
        ingest = await candidate if inspect.isawaitable(candidate) else candidate
        try:
            lane = await self._lane_for(connection_id, raw_description, ingest)
            if lane is None:
                return await self._run_with_port(
                    registration, raw_description, ingest, key=self._agent_did
                )
        finally:
            # The port holds this run's memory-database connection; a run that
            # ends, fails or is cancelled (stall, revoke, shutdown) gives it back.
            await _release(ingest)
        return await self._run_shared(registration, raw_description, lane, forced=forced)

    async def _run_shared(
        self,
        registration: SourceRegistration,
        raw_description: SourceDescription,
        lane: KnowledgeSubscription,
        *,
        forced: bool,
    ) -> bool:
        """Sync a shared connection once for every agent that reads it (P18-4).

        Whichever subscriber holds the connection's lease runs; the others find
        the lease taken and adopt its state. A run another subscriber completed
        within this agent's interval is adopted without asking the provider at all.
        """
        connection_id = registration.connection_id
        if self._shared is None:
            return False
        writer = await self._shared.writer(connection_id, lane.approval_id)
        try:
            if not forced and await self._shared_is_fresh(connection_id, lane):
                await self._adopt_shared_status(connection_id, raw_description, writer, lane)
                return False
            return await self._run_with_port(
                registration, raw_description, writer, key=lane.principal
            )
        finally:
            await _release(writer)

    async def _shared_is_fresh(self, connection_id: str, lane: KnowledgeSubscription) -> bool:
        """True when another subscriber completed a full pass within this interval."""
        if self._store is None:
            return False
        state: SyncState = await self._store.get_state(lane.principal, connection_id)
        if state.status is not SyncStatus.COMPLETE or state.budget_reached:
            return False
        synced = state.last_synced_at
        if synced is None:
            return False
        return (datetime.now(UTC) - synced).total_seconds() < self._interval

    async def _adopt_shared_status(
        self,
        connection_id: str,
        raw_description: SourceDescription,
        port: IngestPort,
        lane: KnowledgeSubscription,
    ) -> None:
        """Show this agent the shared run's outcome as its own card row."""
        description = await self._with_generation(raw_description, port)
        state = await self._persisted_state(connection_id)
        self._statuses[connection_id] = SourceRuntimeStatus(
            connection_id=connection_id,
            source_id=lane.source_id,
            status=state.status.value if state is not None else "idle",
            detail=(state.error_code or "") if state is not None else "",
            description=self._descriptions.get(connection_id) or description,
            state=state,
            documents_indexed=await self._documents_indexed(port, description),
        )

    async def _run_with_port(
        self,
        registration: SourceRegistration,
        raw_description: SourceDescription,
        ingest: IngestPort,
        *,
        key: str,
    ) -> bool:
        """Run one source through ``ingest``; ``key`` is whose sync row it advances."""
        connection_id = registration.connection_id
        description = await self._with_generation(raw_description, ingest)
        if key == self._agent_did:
            self._descriptions[connection_id] = description
        self._statuses[connection_id] = SourceRuntimeStatus(
            connection_id=connection_id,
            source_id=_canonical_source_id(ingest, description),
            status="syncing",
            description=description,
        )
        coordinator = ConnectedDataCoordinator(
            registration.adapter,
            ingest,
            self._store,
            audit=self._audit,
        )
        result = await coordinator.run(
            description,
            agent_did=key,
            owner_id=f"{self._agent_did}:{uuid.uuid4().hex}",
            limits=self._limits,
        )
        self._statuses[connection_id] = SourceRuntimeStatus(
            connection_id=connection_id,
            source_id=_canonical_source_id(ingest, description),
            status=result.status.value,
            description=description,
            state=result,
            documents_indexed=await self._documents_indexed(ingest, description),
        )
        # A run that completed clears any prior needs-attention backoff.
        self._health.clear(connection_id)
        if result.status is SyncStatus.COMPLETE:
            await self._report(connection_id, ok=True)
        elif result.status is SyncStatus.FAILED:
            await self._report(connection_id, ok=False, code=result.error_code)
        return result.status is SyncStatus.COMPLETE and coordinator.stopped_at_ceiling

    async def _inspect_registration(self, registration: SourceRegistration) -> None:
        """Populate the safe descriptor before the operator sees a blank source row."""
        connection_id = registration.connection_id
        try:
            raw_description = await registration.adapter.inspect_source(
                InspectSource(connection_id=connection_id)
            )
            description = raw_description
            source_id = ""
            documents_indexed = 0
            lane: KnowledgeSubscription | None = None
            if self._ingest_factory is not None:
                candidate = self._ingest_factory(description)
                ingest = await candidate if inspect.isawaitable(candidate) else candidate
                try:
                    description = await self._with_generation(description, ingest)
                    lane = await self._lane_for(connection_id, raw_description, ingest)
                    if lane is None:
                        source_id = _canonical_source_id(ingest, description)
                        documents_indexed = await self._documents_indexed(ingest, description)
                finally:
                    await _release(ingest)
            if lane is not None:
                source_id = lane.source_id
                documents_indexed = await self._shared_documents_indexed(lane, raw_description)
            self._descriptions[connection_id] = description
            # Read back what this source actually did, rather than declaring it
            # unmapped. The sync state is durable and the runtime status was
            # not, so every restart wiped a completed source back to
            # "awaiting_mapping" with no pages, no bytes and no source id —
            # which also left its documents unaddressable and its search empty.
            # The coordinator keys that durable row by ``connection_id`` (the
            # only stable id it has, being ingest-agnostic); reading it back by
            # the doc pool's canonical id found nothing, so the counters read 0.
            state = await self._persisted_state(connection_id)
            # A durable terminal error_code (a revoked credential) survives a
            # restart: re-establish the backoff and surface needs_attention so a
            # fresh process does not resume hammering a dead credential.
            if state is not None and is_terminal_sync_failure(state.error_code):
                self._health.note_terminal_failure(connection_id)
                self._statuses[connection_id] = SourceRuntimeStatus(
                    connection_id=connection_id,
                    source_id=source_id,
                    status="needs_attention",
                    detail=state.error_code or "",
                    description=description,
                    state=state,
                    documents_indexed=documents_indexed,
                )
                return
            self._statuses[connection_id] = SourceRuntimeStatus(
                connection_id=connection_id,
                source_id=source_id,
                status=str(state.status.value) if state is not None else "awaiting_mapping",
                detail=state.error_code or "" if state is not None else "",
                description=description,
                state=state,
                documents_indexed=documents_indexed,
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
            state: SyncState = await self._store.get_state(self._sync_key(source_id), source_id)
        except Exception:
            _logger.warning("connected-data sync state unreadable: %s", source_id)
            return None
        return state

    async def _documents_indexed(self, ingest: IngestPort, description: SourceDescription) -> int:
        """Count this source's indexed documents through the optional ingest seam.

        The transfer counters answer "how many pages did we pull"; an operator
        wants "how many documents can the agent actually search". A backend that
        does not expose the count, or a read that fails, degrades to 0 rather
        than failing the whole inspection — a missing count must never blank a
        source's status row.
        """
        count = getattr(ingest, "documents_indexed", None)
        if not callable(count):
            return 0
        try:
            return int(await count(description))
        except Exception:
            _logger.warning(
                "connected-data document count unavailable: %s", description.connection_id
            )
            return 0

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

    # -- one sync and one store per connection (P18-4) -----------------------

    def _sync_key(self, connection_id: str) -> str:
        """Whose sync row a connection advances: its shared principal, or this agent."""
        lane = self._lanes.get(connection_id)
        return lane.principal if lane is not None else self._agent_did

    async def _lane_for(
        self, connection_id: str, raw_description: SourceDescription, private: IngestPort
    ) -> KnowledgeSubscription | None:
        """Decide whether this agent reads ``connection_id`` from its shared store.

        It does when its own approved mapping is exactly the shareable homes, its own
        store holds nothing left to migrate, and it embeds the way the shared store
        was embedded. Joining writes the durable subscription its reads are checked
        against. A failure to decide leaves the current answer unchanged.
        """
        shared = self._shared
        approved = getattr(private, "approved_mapping", None)
        if shared is None or not callable(approved):
            return None
        try:
            description = await self._with_generation(raw_description, private)
            plan = await approved(description)
            if plan is None or set(plan.homes) != SHARED_HOMES:
                await self._leave(connection_id, raw_description, "mapping_not_shared")
                return None
            if await self._documents_indexed(private, description) > 0:
                # The agent's own store still holds this connection: migrate it
                # first, or every document would be read twice.
                await self._leave(connection_id, raw_description, "migration_pending")
                return None
            if not shared.claim_profile(connection_id):
                await self._leave(connection_id, raw_description, "embedding_profile_differs")
                return None
            return await self._subscribe(connection_id, raw_description, plan.mapping_id)
        except Exception:
            _logger.warning(
                "connected-data shared store undecided: %s", connection_id, exc_info=True
            )
            return self._lanes.get(connection_id)

    async def _subscribe(
        self, connection_id: str, raw_description: SourceDescription, approval_id: str
    ) -> KnowledgeSubscription:
        """Record that this agent reads the connection's shared store (durably)."""
        shared = self._shared
        if shared is None:
            raise RuntimeError("shared knowledge is not configured")
        reader = await shared.reader(connection_id)
        try:
            source_id = _canonical_source_id(
                reader, await self._with_generation(raw_description, reader)
            )
        finally:
            await _release(reader)
        subscription = KnowledgeSubscription(
            agent_did=self._agent_did,
            connection_id=connection_id,
            source_id=source_id,
            approval_id=approval_id,
            profile=shared.profile(),
        )
        registry = await shared.registry()
        if await registry.get(self._agent_did, connection_id) != subscription:
            await registry.put(subscription)
            await self._emit(
                "connected_data.knowledge.subscribed",
                {"source": _safe_id(connection_id), "store": subscription.principal},
            )
        self._lanes[connection_id] = subscription
        return subscription

    async def _unsubscribe(self, connection_id: str, *, reason: str) -> bool:
        """Stop reading a shared store; True when a subscription was removed.

        The durable row goes first: reads are authorized against it, so a revoke
        is in force at the retrieval boundary before anything else is cleaned up.
        A failure to remove it propagates: a revoke must never report success
        while the agent can still read.
        """
        shared = self._shared
        if shared is None:
            self._lanes.pop(connection_id, None)
            return False
        registry = await shared.registry()
        current = await registry.get(self._agent_did, connection_id)
        if current is not None:
            await registry.delete(self._agent_did, connection_id)
        self._lanes.pop(connection_id, None)
        if current is None:
            return False
        await self._emit(
            "connected_data.knowledge.unsubscribed",
            {"source": _safe_id(connection_id), "store": current.principal, "reason": reason},
        )
        return True

    async def _leave(
        self, connection_id: str, raw_description: SourceDescription, reason: str
    ) -> None:
        """Stop reading a shared store, and purge it if no agent reads it any more."""
        if await self._unsubscribe(connection_id, reason=reason):
            await self._purge_unread_store(connection_id, raw_description)

    async def _purge_unread_store(
        self, connection_id: str, description: SourceDescription
    ) -> None:
        """Purge a shared store once the last agent reading it has gone."""
        shared = self._shared
        if shared is None or self._store is None:
            return
        principal = knowledge_principal(connection_id)
        audit = {"source": _safe_id(connection_id), "store": principal}
        try:
            if await (await shared.registry()).for_connection(connection_id):
                return
            reader = await shared.reader(connection_id)
            try:
                described = await self._with_generation(description, reader)
                if not await self._store.purge(principal, connection_id):
                    await self._emit(
                        "connected_data.knowledge.purge_deferred",
                        {**audit, "reason": "sync_lease_active"},
                    )
                    return
                await reader.purge_source(described)
            finally:
                await _release(reader)
            await asyncio.to_thread(shutil.rmtree, shared.root(connection_id), True)
        except Exception as exc:  # reason: the agent's own revoke already took effect
            _logger.exception("connected-data shared store purge failed: %s", connection_id)
            await self._emit(
                "connected_data.knowledge.purge_failed", {**audit, "error": _name(exc)}
            )
            return
        await self._emit("connected_data.knowledge.purged", audit)

    async def _port_for(
        self, registration: SourceRegistration
    ) -> tuple[IngestPort | None, SourceDescription | None]:
        """The port an operator action acts on: the shared store's, or the agent's own."""
        connection_id = registration.connection_id
        lane = self._lanes.get(connection_id)
        if lane is None or self._shared is None:
            return await self._ingest_for(registration)
        try:
            raw = self._descriptions.get(connection_id) or await self._inspect(registration)
            writer = await self._shared.writer(connection_id, lane.approval_id)
            return writer, await self._with_generation(raw, writer)
        except Exception:
            _logger.exception("connected-data shared store unavailable: %s", connection_id)
            return None, None

    async def _shared_documents_indexed(
        self, lane: KnowledgeSubscription, description: SourceDescription
    ) -> int:
        if self._shared is None:
            return 0
        reader = await self._shared.reader(lane.connection_id)
        try:
            return await self._documents_indexed(
                reader, await self._with_generation(description, reader)
            )
        finally:
            await _release(reader)

    async def _refresh_shared_status(self, connection_id: str) -> None:
        """Bring a shared connection's card row up to the shared run's durable state.

        The run may have been another subscriber's, possibly in another process.
        """
        lane = self._lanes.get(connection_id)
        known = self._statuses.get(connection_id)
        if lane is None or known is None or known.description is None:
            return
        state = await self._persisted_state(connection_id)
        if state is None or state == known.state:
            return
        try:
            indexed = await self._shared_documents_indexed(lane, known.description)
        except Exception:  # reason: a status read must never fail the listing
            _logger.warning("connected-data shared store count unavailable: %s", connection_id)
            indexed = known.documents_indexed
        self._statuses[connection_id] = replace(
            known,
            status=state.status.value,
            detail=state.error_code or "",
            source_id=lane.source_id,
            state=state,
            documents_indexed=indexed,
        )

    async def shared_document_search(
        self,
        query: str,
        *,
        caller_did: str,
        source_ids: Sequence[str] | None = None,
        clearance: str = "unclassified",
        top_k: int | None = None,
    ) -> list[Any]:
        """Search the shared stores this agent is subscribed to, best hit first.

        Only for this agent: a caller naming any other DID is refused and audited.
        Only connections the agent still reads: the subscription is checked in the
        durable store on every call, so a revoke hides the data at once. Never
        above ``clearance``.
        """
        hits: list[Any] = []
        for subscription in await self._readable(caller_did, source_ids):
            search = partial(
                _search_pool, query, subscription.source_id, clearance=clearance, top_k=top_k
            )
            hits.extend(await self._read_pool(subscription, search))
        return sorted(hits, key=lambda hit: float(getattr(hit, "score", 0.0)), reverse=True)

    async def shared_documents(
        self, source_id: str, *, caller_did: str, query: str | None = None, limit: int = 50
    ) -> list[Any] | None:
        """One shared pool's documents (or search hits); ``None`` if not a pool it reads."""
        readable = await self._readable(caller_did, (source_id,))
        if not readable:
            return None
        subscription = readable[0]
        if query:
            return await self._read_pool(
                subscription,
                lambda reader: reader.search_pool(
                    query, subscription.source_id, clearance="unclassified", top_k=limit
                ),
            )
        return await self._read_pool(
            subscription, lambda reader: reader.list_pool(subscription.source_id, limit=limit)
        )

    async def _readable(
        self, caller_did: str, source_ids: Sequence[str] | None
    ) -> list[KnowledgeSubscription]:
        """The subscriptions a read may use; fails closed on any doubt (ASI03)."""
        shared = self._shared
        if shared is None:
            return []
        if caller_did != self._agent_did:
            await self._emit(
                "connected_data.knowledge.read_refused",
                {"reason": "caller_is_not_this_agent", "caller": _safe_id(caller_did)},
            )
            return []
        try:
            rows = await (await shared.registry()).for_agent(self._agent_did)
        except Exception:
            _logger.warning("connected-data subscriptions unreadable", exc_info=True)
            return []
        profile = shared.profile()
        wanted = None if source_ids is None else set(source_ids)
        return [
            row
            for row in rows
            if row.agent_did == self._agent_did
            and row.profile == profile
            and (wanted is None or row.source_id in wanted)
        ]

    async def _read_pool(
        self,
        subscription: KnowledgeSubscription,
        read: Callable[[Any], Awaitable[list[Any]]],
    ) -> list[Any]:
        """Run one read against a shared store; a sick store degrades to no hits."""
        if self._shared is None:
            return []
        reader = await self._shared.reader(subscription.connection_id)
        try:
            return list(await read(reader))
        except Exception:
            _logger.warning(
                "connected-data shared store unreadable: %s",
                subscription.connection_id,
                exc_info=True,
            )
            return []
        finally:
            await _release(reader)

    # -- migrating an agent's own store into the shared one (P18-4) ----------

    async def preview_migration(self) -> tuple[MigrationResult, ...]:
        """Report, changing nothing, what moving own stores into shared ones would do.

        The move itself is automatic (``_migrate_automatically``); this is the
        read-only preview behind ``arc knowledge migrate``. Every connection's
        outcome is audited.
        """
        results: list[MigrationResult] = []
        for registration in sorted(
            await self._catalog.snapshot(), key=lambda entry: entry.connection_id
        ):
            result = await self._migrate(registration, dry_run=True)
            results.append(result)
            await self._emit_migration(result, dry_run=True, trigger="operator")
        return tuple(results)

    async def _migrate_automatically(self, registration: SourceRegistration) -> None:
        """Move this agent's own copy of one connection into its shared store, if it can.

        Runs inside the connection's own supervised sync task, so it never delays
        startup or chat. A connection with nothing to move, or one this agent may
        not share, is a silent no-op; a failure keeps the own copy, is logged and
        audited, and is retried on the next run.
        """
        if self._shared is None:
            return
        connection_id = registration.connection_id
        result = await self._migrate(registration, dry_run=False)
        if result.status in _QUIET_MIGRATION_STATUSES:
            return
        if result.detail == "shared_sync_active":
            # Another agent is moving into this store right now; it is not a failure.
            self._retry_migration_soon(connection_id)
            return
        if result.status == "refused":
            _logger.warning(
                "connected-data migration kept the agent's own copy: %s (%s)",
                connection_id,
                result.detail,
            )
        await self._emit_migration(result, dry_run=False, trigger="automatic")

    def _retry_migration_soon(self, connection_id: str) -> None:
        """Run this connection again shortly, so the move is not left for the next interval."""
        if self._closed or connection_id in self._migration_retries:
            return

        def again() -> None:
            self._migration_retries.pop(connection_id, None)
            self._timing.run_now(connection_id)
            self._wake.set()

        self._migration_retries[connection_id] = asyncio.get_running_loop().call_later(
            _MIGRATION_RETRY_SECONDS, again
        )

    async def _emit_migration(
        self, result: MigrationResult, *, dry_run: bool, trigger: str
    ) -> None:
        await self._emit(
            "connected_data.knowledge.migration",
            {
                "source": _safe_id(result.connection_id),
                "status": result.status,
                "outcome": "failed" if result.status == "refused" else "ok",
                "detail": result.detail,
                "dry_run": dry_run,
                "trigger": trigger,
                "documents": result.documents,
                "adopted": result.adopted,
                "deduplicated": result.deduplicated,
                "skipped": result.skipped,
            },
        )

    async def _migrate(
        self, registration: SourceRegistration, *, dry_run: bool
    ) -> MigrationResult:
        connection_id = registration.connection_id
        if self._shared is None or self._store is None or self._ingest_factory is None:
            return MigrationResult(connection_id, "refused", "shared_store_unavailable")
        # A real move runs inside this connection's own sync task, so nothing else
        # is syncing the agent's copy while it moves.
        try:
            return await self._migrate_held(registration, dry_run=dry_run)
        except _ReadBackError as exc:
            _logger.warning("connected-data migration read-back failed: %s", connection_id)
            return MigrationResult(connection_id, "refused", f"read_back_mismatch:{exc}")
        except Exception as exc:  # reason: one connection's failure must not stop the rest
            _logger.exception("connected-data migration failed: %s", connection_id)
            return MigrationResult(connection_id, "refused", f"migration_failed:{_name(exc)}")

    async def _migrate_held(
        self, registration: SourceRegistration, *, dry_run: bool
    ) -> MigrationResult:
        connection_id = registration.connection_id
        shared, store, factory = self._shared, self._store, self._ingest_factory
        if shared is None or store is None or factory is None:
            return MigrationResult(connection_id, "refused", "shared_store_unavailable")
        raw = self._descriptions.get(connection_id) or await self._inspect(registration)
        candidate = factory(raw)
        private = await candidate if inspect.isawaitable(candidate) else candidate
        try:
            if not isinstance(private, ArcMemoryIngestAdapter):
                return MigrationResult(connection_id, "refused", "migration_unsupported")
            description = await self._with_generation(raw, private)
            plan = await private.approved_mapping(description)
            if plan is None or set(plan.homes) != SHARED_HOMES:
                return MigrationResult(connection_id, "not_eligible", "mapping_not_shared")
            if await self._documents_indexed(private, description) == 0:
                if not dry_run:
                    await self._lane_for(connection_id, raw, private)
                shared_now = connection_id in self._lanes
                return MigrationResult(
                    connection_id, "already_shared" if shared_now else "nothing_to_migrate"
                )
            if not shared.profile_compatible(connection_id):
                return MigrationResult(connection_id, "refused", "embedding_profile_differs")
            writer = await shared.migration_writer(connection_id, plan.mapping_id)
            try:
                target = await self._with_generation(raw, writer)
                if dry_run:
                    counts = await writer.adopt_documents(
                        target, private, description, dry_run=True
                    )
                    return MigrationResult(connection_id, "would_migrate", "", **counts)
                if not shared.claim_profile(connection_id):
                    return MigrationResult(connection_id, "refused", "embedding_profile_differs")
                adopted = await self._adopt_under_lease(
                    connection_id, writer, target, private, description
                )
            finally:
                await _release(writer)
            if adopted is None:
                return MigrationResult(connection_id, "refused", "shared_sync_active")
            await private.reset_source(description)
            await store.reset(self._agent_did, connection_id)
            await self._lane_for(connection_id, raw, private)
        finally:
            await _release(private)
        return MigrationResult(connection_id, "migrated", "", **adopted)

    async def _adopt_under_lease(
        self,
        connection_id: str,
        writer: Any,
        target: SourceDescription,
        private: IngestPort,
        description: SourceDescription,
    ) -> dict[str, int] | None:
        """Adopt the agent's documents while holding the shared store's sync lease.

        The lease keeps a subscriber's sync from writing the store mid-adoption; it
        is renewed while the documents are re-indexed. ``None``: a sync holds it.
        """
        store = self._store
        if store is None:
            return None
        principal = knowledge_principal(connection_id)
        owner = f"{self._agent_did}:migration:{uuid.uuid4().hex}"
        ttl = self._limits.max_seconds
        prior: SyncState = await store.get_state(principal, connection_id)
        lease = await store.acquire_lease(principal, connection_id, owner, ttl_seconds=ttl)
        if lease is None:
            return None
        token = lease.fencing_token
        heartbeat = asyncio.create_task(
            _keep_lease(store, principal, connection_id, owner, token, ttl),
            name=f"connected-data-migration-lease:{connection_id}",
        )
        try:
            counts: dict[str, int] = await writer.adopt_documents(
                target, private, description, dry_run=False
            )
            await self._verify_landed(writer, target, counts)
            await self._seed_shared_state(connection_id, owner, token, prior)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await store.release_lease(
                principal, connection_id, owner_id=owner, fencing_token=token
            )
        return counts

    async def _verify_landed(
        self, writer: IngestPort, target: SourceDescription, counts: dict[str, int]
    ) -> None:
        """Read the shared store back: it must hold every document the agent's copy did.

        Checked before the agent's copy is emptied and before the store's cursor is
        seeded, so a document that did not land is neither lost nor skipped by the
        next incremental sync.
        """
        expected = counts.get("documents", 0)
        held = await self._documents_indexed(writer, target)
        if counts.get("skipped", 0) or held < expected:
            raise _ReadBackError(f"{held}/{expected}")

    async def _seed_shared_state(
        self, connection_id: str, owner: str, token: int, prior: SyncState
    ) -> None:
        """Start a never-synced shared store from the agent's own cursor; else leave it be.

        A store some subscriber already synced keeps its own cursor and status (the
        lease set it running; it is put back). One that never synced continues from
        where the agent's own crawl stopped, so the next sync is incremental.
        """
        store = self._store
        if store is None:
            return
        principal = knowledge_principal(connection_id)
        mine: SyncState = await store.get_state(self._agent_did, connection_id)
        never_synced = prior.cursor is None and prior.last_synced_at is None and not prior.pages
        if never_synced and mine.cursor is not None:
            await store.commit_page(
                principal,
                connection_id,
                expected_cursor=None,
                next_cursor=mine.cursor,
                page_id=f"migration:{_safe_id(self._agent_did)}",
                page_count=mine.pages,
                page_bytes=mine.bytes_processed,
                owner_id=owner,
                fencing_token=token,
            )
            status = SyncStatus.COMPLETE if mine.status is SyncStatus.COMPLETE else SyncStatus.IDLE
            await store.set_status(
                principal,
                connection_id,
                status,
                owner_id=owner,
                fencing_token=token,
                budget_reached=mine.budget_reached,
            )
            return
        await store.set_status(
            principal,
            connection_id,
            prior.status,
            owner_id=owner,
            fencing_token=token,
            error_code=prior.error_code,
            budget_reached=prior.budget_reached,
        )

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
            # The durable row holds plain strings; every reader calls ``.value`` on a
            # home, so they are restored as the enum they were staged as. Left as
            # strings they crashed ``connected_sources`` and the prompt catalog after
            # every restart (J1 F2).
            restored = MappingProposalStatus(
                connection_id=connection_id,
                source_id=str(row.get("source_id", "")),
                allowed_homes=tuple(KnowledgeHome(home) for home in row.get("allowed_homes", ())),
                homes=tuple(KnowledgeHome(home) for home in row.get("homes", ())),
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
        self._statuses[connection_id] = self._still_described(
            connection_id, status="degraded", detail=detail
        )

    def _still_described(
        self, connection_id: str, *, status: str, detail: str, state: SyncState | None = None
    ) -> SourceRuntimeStatus:
        """A status row for a source that is unwell but still connected.

        Keeps what the source is (name, kind, pool id, indexed count). A row built
        without them dropped a failed source out of the agent's catalog and its
        name resolution while every page it had already indexed was still searchable.
        """
        previous = self._statuses.get(connection_id)
        known = previous.description if previous is not None else None
        return SourceRuntimeStatus(
            connection_id=connection_id,
            status=status,
            source_id=previous.source_id if previous is not None else "",
            detail=detail,
            description=known or self._descriptions.get(connection_id),
            state=state,
            documents_indexed=previous.documents_indexed if previous is not None else 0,
        )

    async def _mark_needs_attention(self, connection_id: str, reason: str) -> None:
        """Back a source off and tell the health authority; it owns the one notice."""
        self._health.note_terminal_failure(connection_id)
        await self._report(connection_id, ok=False, code=reason)
        if is_terminal_sync_failure(reason):
            # Durable, so the next process knows without being told again.
            await self._record_failure(connection_id, reason, force=True)
        self._statuses[connection_id] = self._still_described(
            connection_id,
            status="needs_attention",
            detail=reason or "",
            state=await self._persisted_state(connection_id),
        )

    async def _report(
        self, connection_id: str, *, ok: bool, code: str | None = None, text: str = ""
    ) -> None:
        """Tell the shared health record what this run found, keyed by the instance.

        A multi-source bundle registers ``<instance>:<suffix>``; the account is the
        instance, so that is what the record is keyed by. The authority decides what
        the report means (one blip is not an outage) and who is told.
        """
        description = self._descriptions.get(connection_id)
        provider = description.source_kind.title() if description is not None else ""
        signal = HealthSignal(
            ok=ok,
            source="sync",
            checked_by=self._agent_did,
            reason_code=None if ok else classify(code, text),
            detail=text or (code or ""),
            provider=provider,
        )
        await self._reporter.report(_instance_of(connection_id), signal)

    async def _emit(self, action: str, payload: dict[str, Any]) -> None:
        """Audit through the module's callback; an audit failure never stops a sync."""
        if self._audit is None:
            return
        try:
            result = self._audit(action, payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # reason: AU-5 — an audit sink failure is logged, not raised
            _logger.warning("connected-data audit emit failed: %s", action, exc_info=True)

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


async def _keep_lease(
    store: Any, principal: str, connection_id: str, owner: str, token: int, ttl: float
) -> None:
    """Renew a held lease every third of its life until cancelled or lost."""
    while await store.renew_lease(
        principal, connection_id, owner_id=owner, fencing_token=token, ttl_seconds=ttl
    ):
        await asyncio.sleep(ttl / 3)


async def _search_pool(
    query: str, source_id: str, reader: Any, *, clearance: str, top_k: int | None
) -> list[Any]:
    return list(await reader.search_pool(query, source_id, clearance=clearance, top_k=top_k))


def _instance_of(connection_id: str) -> str:
    """The connected account behind a source id (``<instance>`` or ``<instance>:<suffix>``)."""
    return connection_id.split(":", 1)[0]


def _canonical_source_id(ingest: IngestPort, description: SourceDescription) -> str:
    canonical = getattr(ingest, "canonical_source_id", None)
    return str(canonical(description)) if callable(canonical) else ""


async def _release(ingest: IngestPort) -> None:
    """Close a port's own resources (its memory-database connection) if it has any.

    Optional like the other port hooks; a close failure is logged, never
    raised over the outcome of the operation that used the port.
    """
    close = getattr(ingest, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # reason: releasing must not mask the operation's own result
        _logger.warning("connected-data ingest port close failed", exc_info=True)


class _ReadBackError(RuntimeError):
    """The shared store did not hold every document the agent's own copy did."""


#: Outcomes of an automatic move that need no log line or audit event: nothing to do.
_QUIET_MIGRATION_STATUSES = frozenset({"already_shared", "nothing_to_migrate", "not_eligible"})


class _SyncStalledError(RuntimeError):
    """A run outlived its time bound plus grace: it is stuck, not slow."""


def _name(exc: BaseException) -> str:
    return type(exc).__name__


def _safe_id(value: str) -> str:
    """A non-reversible audit id; connection names can carry account detail."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


__all__ = [
    "CatalogEntry",
    "ConnectedDataService",
    "IngestPortFactory",
    "MappingProposalStatus",
    "MigrationResult",
    "SourceOperationResult",
    "SourceRuntimeStatus",
]
