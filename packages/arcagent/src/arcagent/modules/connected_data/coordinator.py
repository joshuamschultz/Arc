"""Bounded, restartable orchestration for connected-data source adapters."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from collections.abc import Mapping
from typing import Any

from arcagent.connected_data import (
    AuditCallback,
    FetchSourceObject,
    IngestPort,
    LeaseLostError,
    MappingPendingError,
    MappingPlan,
    ObjectNotIngestibleError,
    SourceAdapter,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SyncError,
    SyncLimits,
    SyncSource,
    SyncSourcePage,
    SyncState,
    SyncStatePort,
    SyncStatus,
    TransientSyncError,
)


class ConnectedDataCoordinator:
    """Run pages through mapping and ingest before advancing a checkpoint."""

    def __init__(
        self,
        source: SourceAdapter,
        ingest: IngestPort,
        state: SyncStatePort,
        *,
        audit: AuditCallback | None = None,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._source, self._ingest, self._state = source, ingest, state
        self._audit, self._clock, self._sleep = audit, clock, sleep

    async def run(
        self,
        source: SourceDescription,
        *,
        agent_did: str,
        owner_id: str,
        limits: SyncLimits | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SyncState:
        chosen = limits or SyncLimits()
        source_id = source.connection_id
        lease = await self._state.acquire_lease(
            agent_did, source_id, owner_id, ttl_seconds=chosen.max_seconds
        )
        if lease is None:
            return await self._state.get_state(agent_did, source_id)
        started = self._clock()
        current = await self._state.get_state(agent_did, source_id)
        await self._emit("started", source, {"owner": _safe_id(owner_id)})
        lease_lost = False
        try:
            await self._set_status(
                agent_did,
                source_id,
                SyncStatus.RUNNING,
                owner_id=owner_id,
                fencing_token=lease.fencing_token,
            )
            mapping = await self._retry_call(
                lambda: self._ingest.require_approved_mapping(source),
                chosen,
                started,
                cancel_event,
            )
            await self._register_live_datastore(source, mapping, chosen, started, cancel_event)
            cursor, pages, processed = current.cursor, 0, 0
            snapshot_ids: set[str] = set()
            # True when this run stopped at a ceiling rather than at the end of
            # the account. Everything downstream that assumes it saw the WHOLE
            # account must be skipped, or a partial listing reads as deletions.
            budget_reached = False
            while True:
                self._check_cancel(cancel_event)
                self._check_deadline(started, chosen)
                if not await self._state.renew_lease(
                    agent_did,
                    source_id,
                    owner_id=owner_id,
                    fencing_token=lease.fencing_token,
                    ttl_seconds=chosen.max_seconds,
                ):
                    raise LeaseLostError()
                if pages >= chosen.max_pages:
                    budget_reached = True
                    break
                page = await self._fetch_with_retry(source, cursor, chosen, started, cancel_event)
                snapshot_ids.update(
                    item.object_id
                    for item in page.objects
                    if not item.deleted and not _is_container(item)
                )
                page_bytes = await self._ingest_page(
                    source, page, mapping, chosen, started, cancel_event
                )
                page_id = _page_id(source_id, cursor, page)
                if not await self._state.commit_page(
                    agent_did,
                    source_id,
                    expected_cursor=cursor,
                    next_cursor=page.next_checkpoint,
                    page_id=page_id,
                    page_count=1,
                    page_bytes=page_bytes,
                    owner_id=owner_id,
                    fencing_token=lease.fencing_token,
                ):
                    raise LeaseLostError()
                pages, processed, cursor = pages + 1, processed + page_bytes, page.next_checkpoint
                if not page.has_more:
                    break
                # Checked here, after the cursor is committed: the crawl always
                # advances by at least one page per run and picks up exactly
                # where it stopped, however large the account.
                if processed >= chosen.max_bytes:
                    budget_reached = True
                    break
            # Reconciling a PARTIAL listing would mark every document this run
            # never reached as deleted. A run that stopped at a ceiling has not
            # seen the account, so it reconciles nothing.
            if not source.supports_incremental and not budget_reached:
                await self._retry_call(
                    lambda: self._ingest.complete_snapshot(
                        source, frozenset(snapshot_ids), mapping
                    ),
                    chosen,
                    started,
                    cancel_event,
                )
            await self._set_status(
                agent_did,
                source_id,
                SyncStatus.COMPLETE,
                owner_id=owner_id,
                fencing_token=lease.fencing_token,
            )
            await self._emit(
                "completed",
                source,
                {"pages": pages, "bytes": processed, "budget_reached": budget_reached},
            )
        except asyncio.CancelledError:
            await self._mark_cancelled(source, agent_did, owner_id, lease.fencing_token)
            raise
        except _CancellationError:
            await self._mark_cancelled(source, agent_did, owner_id, lease.fencing_token)
        except LeaseLostError:
            lease_lost = True
            await self._emit("lease_lost", source, {})
            raise
        except MappingPendingError as exc:
            try:
                await self._set_status(
                    agent_did,
                    source_id,
                    SyncStatus.AWAITING_MAPPING,
                    owner_id=owner_id,
                    fencing_token=lease.fencing_token,
                    error_code=exc.code,
                )
            except LeaseLostError:
                lease_lost = True
                await self._emit("lease_lost", source, {})
                raise
            await self._emit("awaiting_mapping", source, {})
        except SyncError as exc:
            try:
                await self._set_status(
                    agent_did,
                    source_id,
                    SyncStatus.FAILED,
                    owner_id=owner_id,
                    fencing_token=lease.fencing_token,
                    error_code=exc.code,
                )
            except LeaseLostError:
                lease_lost = True
                await self._emit("lease_lost", source, {})
                raise
            await self._emit("failed", source, {"error_code": exc.code})
            raise
        except Exception:
            try:
                await self._set_status(
                    agent_did,
                    source_id,
                    SyncStatus.FAILED,
                    owner_id=owner_id,
                    fencing_token=lease.fencing_token,
                    error_code="sync_error",
                )
            except LeaseLostError:
                lease_lost = True
                await self._emit("lease_lost", source, {})
                raise
            await self._emit("failed", source, {"error_code": "sync_error"})
            raise
        finally:
            if not lease_lost:
                await self._state.release_lease(
                    agent_did, source_id, owner_id=owner_id, fencing_token=lease.fencing_token
                )
        return await self._state.get_state(agent_did, source_id)

    async def _fetch_with_retry(
        self,
        source: SourceDescription,
        cursor: str | None,
        limits: SyncLimits,
        started: float,
        cancel_event: asyncio.Event | None,
    ) -> SyncSourcePage:
        for attempt in range(limits.retries + 1):
            try:
                return await self._source.sync_source(
                    SyncSource(
                        connection_id=source.connection_id,
                        checkpoint=cursor,
                        root_locator=source.root_locator,
                        page_size=limits.page_size,
                    )
                )
            except (TransientSyncError, SourceError) as exc:
                if isinstance(exc, SourceError) and exc.code is not SourceFailureCode.TRANSIENT:
                    raise SyncError(str(exc), code=str(exc.code)) from exc
                if attempt >= limits.retries:
                    raise TransientSyncError(str(exc), retry_after=exc.retry_after or 0.0) from exc
                self._check_cancel(cancel_event)
                self._check_deadline(started, limits)
                await self._sleep_capped(
                    max(exc.retry_after or 0.0, limits.retry_backoff_seconds * (attempt + 1)),
                    started,
                    limits,
                )
        raise SyncError("source retry loop exhausted")

    async def _ingest_page(
        self,
        source: SourceDescription,
        page: SyncSourcePage,
        mapping: MappingPlan,
        limits: SyncLimits,
        started: float,
        cancel_event: asyncio.Event | None,
    ) -> int:
        mappings: list[tuple[SourceObject, SourceContent | None, MappingPlan]] = []
        page_bytes = 0
        for source_object in page.objects:
            self._check_cancel(cancel_event)
            self._check_deadline(started, limits)
            # A folder is the shape of the tree, not a document. Passed on with no
            # content the ingest port refused it, and one folder in a listing
            # failed the whole sync — which is every real document account.
            if _is_container(source_object):
                continue
            content = None
            if source_object.kind.value != "deleted" and source_object.version is not None:
                # One object bigger than the entire per-sync budget can never be
                # taken, on this run or any later one. Skipping it is the only
                # terminating choice; failing the sync would retry it forever
                # and no other document in the account would ever be indexed.
                if _declared_bytes(source_object) > limits.max_bytes:
                    await self._emit_skip(source, source_object, "object_too_large")
                    continue
                # One object may not exceed the whole per-sync ceiling — the same
                # rule the declared-size check above applies, so a source that
                # enforces it agrees with us. Deliberately NOT what is left of
                # the budget: a shrinking cap meant every file after the first
                # few in a page was refused as too large, and a whole account
                # read as nothing but oversized files.
                request = FetchSourceObject(
                    connection_id=source.connection_id,
                    object_id=source_object.object_id,
                    version=source_object.version or "",
                    max_bytes=limits.max_bytes,
                )
                try:
                    content = await self._retry_call(
                        lambda request=request: self._source.fetch_source(request),
                        limits,
                        started,
                        cancel_event,
                    )
                except SyncError as exc:
                    # The source knows sizes this side only estimated. An object
                    # IT refuses as too large is skipped like any other; every
                    # other refusal still ends the run, because a source that is
                    # actually broken must never look like a pile of big files.
                    # Read from the original verdict, not from the message text.
                    cause = exc.__cause__
                    too_large = (
                        isinstance(cause, SourceError)
                        and cause.code is SourceFailureCode.TOO_LARGE
                    )
                    if not too_large:
                        raise
                    await self._emit_skip(source, source_object, "object_too_large")
                    continue
                page_bytes += _content_bytes(content)
            page_bytes += _metadata_bytes(source_object)
            mappings.append((source_object, content, mapping))
        semaphore = asyncio.Semaphore(limits.max_concurrency)

        async def apply(
            source_object: SourceObject, content: SourceContent | None, mapping: MappingPlan
        ) -> None:
            async with semaphore:
                try:
                    await self._retry_call(
                        lambda: self._ingest.ingest(source, source_object, content, mapping),
                        limits,
                        started,
                        cancel_event,
                    )
                except ObjectNotIngestibleError as refusal:
                    # About this object, not about the account. One file nothing
                    # can read must not cost an operator every other document
                    # beside it.
                    await self._emit_skip(source, source_object, refusal.reason)

        try:
            async with asyncio.TaskGroup() as group:
                for entry in mappings:
                    group.create_task(apply(*entry))
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None
        return page_bytes

    async def _register_live_datastore(
        self,
        source: SourceDescription,
        mapping: MappingPlan,
        limits: SyncLimits,
        started: float,
        cancel_event: asyncio.Event | None,
    ) -> None:
        """Register a selected live datastore only after its exact mapping is approved.

        The source and ingest seams keep this structural: document/blob sources do
        not implement the hook, while a datastore implementation owns both its
        credentials and its read-only port.  Database rows are never copied through
        the document synchronizer merely to make them searchable.
        """
        register = getattr(self._ingest, "register_datastore", None)
        if register is None:
            return
        await self._retry_call(
            lambda: register(source, self._source, mapping), limits, started, cancel_event
        )

    async def _retry_call(
        self,
        operation: Any,
        limits: SyncLimits,
        started: float,
        cancel_event: asyncio.Event | None,
    ) -> Any:
        for attempt in range(limits.retries + 1):
            try:
                return await operation()
            except (TransientSyncError, SourceError) as exc:
                if isinstance(exc, SourceError) and exc.code is not SourceFailureCode.TRANSIENT:
                    raise SyncError(str(exc), code=str(exc.code)) from exc
                if attempt >= limits.retries:
                    raise TransientSyncError(str(exc), retry_after=exc.retry_after or 0.0) from exc
                self._check_cancel(cancel_event)
                self._check_deadline(started, limits)
                await self._sleep_capped(
                    max(exc.retry_after or 0.0, limits.retry_backoff_seconds * (attempt + 1)),
                    started,
                    limits,
                )
        raise SyncError("retry loop exhausted")

    async def _mark_cancelled(
        self, source: SourceDescription, agent_did: str, owner_id: str, token: int
    ) -> None:
        persisted = await self._state.set_status(
            agent_did,
            source.connection_id,
            SyncStatus.CANCELLED,
            owner_id=owner_id,
            fencing_token=token,
            error_code="cancelled",
        )
        await self._emit("cancelled", source, {"persisted": persisted})

    async def _set_status(
        self,
        agent_did: str,
        source_id: str,
        status: SyncStatus,
        *,
        owner_id: str,
        fencing_token: int,
        error_code: str | None = None,
    ) -> None:
        if not await self._state.set_status(
            agent_did,
            source_id,
            status,
            owner_id=owner_id,
            fencing_token=fencing_token,
            error_code=error_code,
        ):
            raise LeaseLostError()

    async def _sleep_capped(self, delay: float, started: float, limits: SyncLimits) -> None:
        remaining = limits.max_seconds - (self._clock() - started)
        if remaining <= 0:
            raise SyncError("time limit exceeded")
        await self._sleep(min(delay, remaining))

    async def _emit(
        self, event: str, source: SourceDescription, payload: Mapping[str, Any]
    ) -> None:
        if self._audit is None:
            return
        result = self._audit(
            f"connected_data.sync.{event}",
            {"source": _safe_id(source.connection_id), **dict(payload)},
        )
        if inspect.isawaitable(result):
            await result

    async def _emit_skip(
        self, source: SourceDescription, source_object: SourceObject, reason: str
    ) -> None:
        """One object left out, named so an operator can see what was not taken."""
        await self._emit(
            "object_skipped",
            source,
            {"object": _safe_id(source_object.object_id), "reason": reason},
        )

    @staticmethod
    def _check_cancel(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _CancellationError()

    def _check_deadline(self, started: float, limits: SyncLimits) -> None:
        if self._clock() - started >= limits.max_seconds:
            raise SyncError("time limit exceeded")


class _CancellationError(Exception):
    pass


def _declared_bytes(source_object: SourceObject) -> int:
    """The object's own size, or zero when the source does not declare one."""
    size = getattr(source_object, "size", 0)
    return int(size) if isinstance(size, int) and size > 0 else 0


def _is_container(source_object: SourceObject) -> bool:
    """True for an entry that holds other entries rather than content of its own."""
    return source_object.kind is SourceObjectKind.FOLDER


def _page_bytes(page: SyncSourcePage) -> int:
    return sum(_metadata_bytes(source_object) for source_object in page.objects)


def _page_id(source_id: str, cursor: str | None, page: SyncSourcePage) -> str:
    value = f"{source_id}\0{cursor or ''}\0{page.next_checkpoint}\0" + ",".join(
        source_object.object_id + ":" + (source_object.version or "")
        for source_object in page.objects
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _content_bytes(content: SourceContent) -> int:
    return len(content.content) + _bounded_metadata_bytes(content.metadata)


def _metadata_bytes(source_object: SourceObject) -> int:
    return _bounded_metadata_bytes(source_object.metadata)


def _bounded_metadata_bytes(metadata: Mapping[str, Any]) -> int:
    return min(64 * 1024, sum(len(str(key)) + len(str(value)) for key, value in metadata.items()))


__all__ = ["ConnectedDataCoordinator"]
