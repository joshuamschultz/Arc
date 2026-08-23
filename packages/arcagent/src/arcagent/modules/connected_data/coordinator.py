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
    SourceAdapter,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
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
            cursor, pages, processed = current.cursor, 0, 0
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
                    raise SyncError("page limit exceeded")
                page = await self._fetch_with_retry(source, cursor, chosen, started, cancel_event)
                page_bytes = await self._ingest_page(
                    source,
                    page,
                    mapping,
                    chosen,
                    started,
                    cancel_event,
                    chosen.max_bytes - processed,
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
            await self._set_status(
                agent_did,
                source_id,
                SyncStatus.COMPLETE,
                owner_id=owner_id,
                fencing_token=lease.fencing_token,
            )
            await self._emit("completed", source, {"pages": pages, "bytes": processed})
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
                        page_size=limits.page_size,
                    )
                )
            except (TransientSyncError, SourceError) as exc:
                if isinstance(exc, SourceError) and exc.code is not SourceFailureCode.TRANSIENT:
                    raise SyncError(str(exc)) from exc
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
        remaining_bytes: int,
    ) -> int:
        mappings: list[tuple[SourceObject, SourceContent | None, MappingPlan]] = []
        page_bytes = 0
        for source_object in page.objects:
            self._check_cancel(cancel_event)
            self._check_deadline(started, limits)
            content = None
            if source_object.kind.value != "deleted" and source_object.version is not None:
                available = remaining_bytes - page_bytes
                if available <= 0:
                    raise SyncError("byte limit exceeded")
                request = FetchSourceObject(
                    connection_id=source.connection_id,
                    object_id=source_object.object_id,
                    version=source_object.version or "",
                    max_bytes=available,
                )
                content = await self._retry_call(
                    lambda request=request: self._source.fetch_source(request),
                    limits,
                    started,
                    cancel_event,
                )
                page_bytes += _content_bytes(content)
            page_bytes += _metadata_bytes(source_object)
            if page_bytes > remaining_bytes:
                raise SyncError("byte limit exceeded")
            mappings.append((source_object, content, mapping))
        semaphore = asyncio.Semaphore(limits.max_concurrency)

        async def apply(
            source_object: SourceObject, content: SourceContent | None, mapping: MappingPlan
        ) -> None:
            async with semaphore:
                await self._retry_call(
                    lambda: self._ingest.ingest(source, source_object, content, mapping),
                    limits,
                    started,
                    cancel_event,
                )

        try:
            async with asyncio.TaskGroup() as group:
                for entry in mappings:
                    group.create_task(apply(*entry))
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None
        return page_bytes

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
                    raise SyncError(str(exc)) from exc
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

    @staticmethod
    def _check_cancel(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _CancellationError()

    def _check_deadline(self, started: float, limits: SyncLimits) -> None:
        if self._clock() - started >= limits.max_seconds:
            raise SyncError("time limit exceeded")


class _CancellationError(Exception):
    pass


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
