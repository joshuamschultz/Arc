"""Bounded, restartable orchestration for connected-data source adapters."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arcagent.connected_data import (
    AuditCallback,
    FetchSourceObject,
    IngestPort,
    LeaseLostError,
    MappingDeniedError,
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
from arcagent.modules.connected_data.pacing import DutyCycleLimiter
from arcagent.modules.connected_data.sync_worker.specs import SyncWorkerUnavailableError

# Failures that are about ONE object, not the account. The source is healthy — it
# knows a size, a deletion, a content type or a version this side only guessed at —
# so the object is left out and the crawl keeps going. Everything NOT here means the
# source itself is unusable this run and the sync aborts.
_OBJECT_SKIP_CODES = frozenset(
    {
        SourceFailureCode.TOO_LARGE,
        SourceFailureCode.NOT_FOUND,
        SourceFailureCode.VERSION_CHANGED,
        SourceFailureCode.UNSUPPORTED_CONTENT,
    }
)

# Reasons that condemn the whole run, not one object: a revoked credential, a rate
# limit, a checkpoint the source rejects, a transient outage that survived retries.
# On any of these an ingest task re-raises and ends the page; every other ingest
# error is charged to its one object so the rest of the page still lands. Values,
# because a wrapped SyncError carries the code as a string.
_FATAL_SYNC_CODES = frozenset(
    {
        SourceFailureCode.AUTH_REQUIRED.value,
        SourceFailureCode.RATE_LIMITED.value,
        SourceFailureCode.CHECKPOINT_INVALID.value,
        SourceFailureCode.TRANSIENT.value,
        SyncWorkerUnavailableError.code,
    }
)


_CHECKPOINT_INVALID = SourceFailureCode.CHECKPOINT_INVALID.value
_RATE_LIMITED = SourceFailureCode.RATE_LIMITED.value
#: Runs that end at their last committed page and come back later, counted as
#: nothing: the provider paced us, or the sync worker that writes the store is
#: restarting. The cursor is kept either way.
_DEFERRED_CODES = frozenset({_RATE_LIMITED, SyncWorkerUnavailableError.code})

_logger = logging.getLogger("arcagent.modules.connected_data.coordinator")


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
        self._stopped_at_ceiling = False

    @property
    def stopped_at_ceiling(self) -> bool:
        """True when the last run ended at a page/byte/time ceiling with more to fetch.

        Distinct from the durable ``budget_reached`` flag, which also stays set
        for the run that finishes a resumed crawl (its snapshot is partial). A
        scheduler reads this to continue a backfill promptly instead of waiting
        a whole interval.
        """
        return self._stopped_at_ceiling

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
        self._stopped_at_ceiling = False
        lease = await self._state.acquire_lease(
            agent_did, source_id, owner_id, ttl_seconds=chosen.max_seconds
        )
        if lease is None:
            return await self._state.get_state(agent_did, source_id)
        run = _Run(
            agent_did=agent_did,
            source_id=source_id,
            owner_id=owner_id,
            fencing_token=lease.fencing_token,
            ttl=chosen.max_seconds,
            started=self._clock(),
            pacer=DutyCycleLimiter(
                chosen.max_duty_fraction,
                clock=self._clock,
                sleep=self._sleep,
                # One rest never outlasts a third of the lease it must keep.
                max_rest=chosen.max_seconds / 3,
            ),
        )
        current = await self._state.get_state(agent_did, source_id)
        await self._emit("started", source, {"owner": _safe_id(owner_id)})
        lease_lost = False
        heartbeat = asyncio.create_task(
            self._heartbeat(run, asyncio.current_task()), name=f"sync-lease:{source_id}"
        )
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
                run,
                cancel_event,
            )
            await self._register_live_datastore(source, mapping, chosen, run, cancel_event)
            cursor, pages, processed = current.cursor, 0, 0
            snapshot_ids: set[str] = set()
            # True when this run stopped at a ceiling rather than at the end of
            # the account. Everything downstream that assumes it saw the WHOLE
            # account must be skipped, or a partial listing reads as deletions.
            #
            # Seeded from the DURABLE flag on resume: a run that continues a prior
            # partial crawl (a non-null cursor) is itself partial — its snapshot
            # covers only the resumed tail — so it must not tombstone either. A
            # fresh crawl (cursor None) starts False so a full pass can reconcile;
            # this also resets a stale durable True once a full crawl completes.
            budget_reached = current.budget_reached if current.cursor is not None else False
            reset_done = False
            while True:
                self._check_cancel(cancel_event)
                # The time budget, like the page and byte budgets, ends a run at
                # a page boundary with its cursor committed: it bounds one run's
                # work and is not a verdict on the account. It is checked ONLY
                # here. A deadline inside a page threw the page away, so a page
                # that overran once overran on every retry (sweep D8); the page
                # in hand now always lands. Before the first page it is still a
                # failure, so a run always advances or says why it could not.
                if self._elapsed(run) >= chosen.max_seconds:
                    if not pages:
                        raise SyncError("time limit exceeded")
                    budget_reached = self._stopped_at_ceiling = True
                    break
                if pages >= chosen.max_pages:
                    budget_reached = self._stopped_at_ceiling = True
                    break
                try:
                    page = await self._fetch_with_retry(source, cursor, chosen, run, cancel_event)
                except SyncError as exc:
                    # A cursor the provider no longer honours is not an outage: start
                    # over from a snapshot, once. A second rejection, or a reject of
                    # a snapshot that has no cursor to drop, is a real failure.
                    if exc.code != _CHECKPOINT_INVALID or cursor is None or reset_done:
                        raise
                    await self._reset_checkpoint(source, run, cursor)
                    cursor, reset_done = None, True
                    continue
                snapshot_ids.update(
                    item.object_id
                    for item in page.objects
                    if not item.deleted and not _is_container(item)
                )
                landed = await self._ingest_page(source, page, mapping, chosen, run, cancel_event)
                page_bytes = landed.bytes
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
                await self._emit_page(source, landed)
                pages, processed, cursor = pages + 1, processed + page_bytes, page.next_checkpoint
                if not page.has_more:
                    break
                # Checked here, after the cursor is committed: the crawl always
                # advances by at least one page per run and picks up exactly
                # where it stopped, however large the account.
                if processed >= chosen.max_bytes:
                    budget_reached = self._stopped_at_ceiling = True
                    break
            # Reconciling a PARTIAL listing would mark every document this run
            # never reached as deleted. A run that stopped at a ceiling has not
            # seen the account, so it reconciles nothing.
            if not source.supports_incremental and not budget_reached:
                await self._retry_call(
                    lambda: self._paced(
                        run,
                        lambda: self._ingest.complete_snapshot(
                            source, frozenset(snapshot_ids), mapping
                        ),
                    ),
                    chosen,
                    run,
                    cancel_event,
                )
            await self._finish_run(source, chosen, run, cancel_event)
            await self._set_status(
                agent_did,
                source_id,
                SyncStatus.COMPLETE,
                owner_id=owner_id,
                fencing_token=lease.fencing_token,
                budget_reached=budget_reached,
            )
            await self._emit(
                "completed",
                source,
                {"pages": pages, "bytes": processed, "budget_reached": budget_reached},
            )
        except asyncio.CancelledError:
            if run.lease_lost and _absorb_own_cancel():
                # The heartbeat lost the lease and stopped this run: nothing more
                # may be written under a lease another writer may now hold.
                lease_lost = True
                await self._emit("lease_lost", source, {})
                raise LeaseLostError() from None
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
            if exc.code in _DEFERRED_CODES:
                if exc.code == _RATE_LIMITED:
                    await self._finish_after_failure(source, run)
                lease_lost = await self._defer(source, run, exc)
                raise
            await self._finish_after_failure(source, run)
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
            await self._finish_after_failure(source, run)
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
            heartbeat.cancel()
            if not lease_lost:
                # Shielded: shutdown cancels a run and the catalog's retire cancels
                # it again. A second cancel landing here skipped the release, and
                # the lease then blocked the next process for max_seconds.
                await asyncio.shield(
                    self._state.release_lease(
                        agent_did, source_id, owner_id=owner_id, fencing_token=lease.fencing_token
                    )
                )
        return await self._state.get_state(agent_did, source_id)

    async def _reset_checkpoint(self, source: SourceDescription, run: _Run, cursor: str) -> None:
        """Drop a dead cursor durably, so the snapshot that follows is what is resumed.

        Committed through the same fenced path as a page, with a page id unique to
        this reset: a crash between the reset and the first snapshot page resumes
        from the snapshot, not from the dead cursor again. The index is kept, not
        wiped: a snapshot re-offers every object and the monotonic revision rule
        makes the unchanged ones no-ops, so search keeps working through the rebuild.
        """
        reset_id = _safe_id(f"reset\0{run.source_id}\0{cursor}\0{run.fencing_token}")
        if not await self._state.commit_page(
            run.agent_did,
            run.source_id,
            expected_cursor=cursor,
            next_cursor=None,
            page_id=reset_id,
            page_count=0,
            page_bytes=0,
            owner_id=run.owner_id,
            fencing_token=run.fencing_token,
        ):
            raise LeaseLostError()
        await self._emit("checkpoint_reset", source, {})

    async def _fetch_with_retry(
        self,
        source: SourceDescription,
        cursor: str | None,
        limits: SyncLimits,
        run: _Run,
        cancel_event: asyncio.Event | None,
    ) -> SyncSourcePage:
        request = SyncSource(
            connection_id=source.connection_id,
            checkpoint=cursor,
            root_locator=source.root_locator,
            page_size=limits.page_size,
        )
        page: SyncSourcePage = await self._retry_call(
            lambda: self._paced(run, lambda: self._source.sync_source(request)),
            limits,
            run,
            cancel_event,
        )
        return page

    async def _ingest_page(
        self,
        source: SourceDescription,
        page: SyncSourcePage,
        mapping: MappingPlan,
        limits: SyncLimits,
        run: _Run,
        cancel_event: asyncio.Event | None,
    ) -> _PageLanded:
        mappings: list[tuple[SourceObject, SourceContent | None, MappingPlan]] = []
        page_bytes = 0
        fetched = 0
        fetch_failure: TransientSyncError | None = None
        for source_object in page.objects:
            self._check_cancel(cancel_event)
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
                        lambda request=request: self._paced(
                            run, lambda: self._source.fetch_source(request)
                        ),
                        limits,
                        run,
                        cancel_event,
                    )
                except SyncError as exc:
                    # A failure about THIS object — too large, gone, an unreadable
                    # content type, a version that moved under us — is one skipped
                    # file, not a dead account. An account-wide refusal (auth, a
                    # rate limit, a rejected checkpoint) still ends the run, because
                    # a source that is actually broken must never look like a pile
                    # of skippable files. Read the typed verdict, not the message.
                    cause = exc.__cause__
                    code = cause.code if isinstance(cause, SourceError) else None
                    if isinstance(exc, TransientSyncError):
                        # A file that kept answering 5xx through every retry. Charged to
                        # that file: one bad object aborting its page is how a single
                        # file cost an account 307 failed runs. If NOTHING on
                        # the page could be fetched it is the source that is down, and
                        # the run still fails below.
                        await self._emit_skip(source, source_object, "object_transient")
                        fetch_failure = fetch_failure or exc
                        continue
                    if code not in _OBJECT_SKIP_CODES:
                        raise
                    await self._emit_skip(source, source_object, f"object_{code.value}")
                    continue
                fetched += 1
                page_bytes += _content_bytes(content)
            page_bytes += _metadata_bytes(source_object)
            mappings.append((source_object, content, mapping))
        if fetch_failure is not None and not fetched:
            raise fetch_failure
        semaphore = asyncio.Semaphore(limits.max_concurrency)
        outcomes = {"ok": 0, "failed": 0}
        # An account-wide denial short-circuits every sibling still waiting on the
        # semaphore. Cancelling the TaskGroup is not enough: with no suspension
        # point between acquiring the semaphore and a synchronous ingest, an
        # already-scheduled sibling would run to completion before the cancellation
        # is delivered — letting objects ingest past a denied mapping.
        mapping_denied = False

        async def apply(
            source_object: SourceObject, content: SourceContent | None, mapping: MappingPlan
        ) -> None:
            nonlocal mapping_denied

            async def ingest_unless_denied() -> bool:
                # Checked at the last moment, after waiting for this source's
                # turn: a sibling may have hit the shared, account-wide denial
                # meanwhile, and then this object must not be ingested.
                if mapping_denied:
                    return False
                await self._ingest.ingest(source, source_object, content, mapping)
                return True

            async with semaphore:
                try:
                    ingested = await self._retry_call(
                        lambda: self._paced(run, ingest_unless_denied),
                        limits,
                        run,
                        cancel_event,
                    )
                except ObjectNotIngestibleError as refusal:
                    # About this object, not about the account. One file nothing
                    # can read must not cost an operator every other document
                    # beside it.
                    await self._emit_skip(source, source_object, refusal.reason)
                except MappingDeniedError:
                    # A denied mapping is an account-wide authorization verdict, not
                    # one bad object: every object shares the mapping. Charging it to
                    # one object let healthy siblings ingest and the run COMPLETE,
                    # advancing the cursor over data the operator never authorized —
                    # a silent authorization bypass. Flag the run and re-raise to
                    # abort fast on the first denied object, keeping mapping_denied.
                    mapping_denied = True
                    raise
                except (LeaseLostError, MappingPendingError, _CancellationError):
                    raise
                except SyncError as exc:
                    # An account-wide reason (revoked credential, rate limit, lost
                    # lease) ends the whole run; anything else is charged to this
                    # one object so the rest of the page still lands.
                    if exc.code in _FATAL_SYNC_CODES:
                        raise
                    outcomes["failed"] += 1
                    await self._emit_object_failed(source, source_object, exc.code)
                except Exception:  # reason: one object's unexpected error is not the account's
                    outcomes["failed"] += 1
                    await self._emit_object_failed(source, source_object, "ingest_error")
                else:
                    outcomes["ok"] += 1 if ingested else 0

        try:
            async with asyncio.TaskGroup() as group:
                for entry in mappings:
                    group.create_task(apply(*entry))
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None
        # Every object on the page failing unexpectedly is a broken store or source,
        # not a run of bad files: fail the sync rather than advance the cursor past
        # data that was never indexed. A page that only SKIPPED objects (typed as
        # un-takeable) is a healthy empty page and advances normally.
        if outcomes["failed"] and not outcomes["ok"]:
            raise SyncError("every object on the page failed to ingest")
        return _PageLanded(bytes=page_bytes, objects=outcomes["ok"], failed=outcomes["failed"])

    async def _finish_run(
        self,
        source: SourceDescription,
        limits: SyncLimits,
        run: _Run,
        cancel_event: asyncio.Event | None,
    ) -> None:
        """Let the port refresh source-wide derived state once, after the pages.

        A routing index or folder ontology describes the whole source, so the
        port rebuilds it here rather than per object. A port with no such state
        does not implement the hook.
        """
        finish = getattr(self._ingest, "finish_sync", None)
        if finish is None:
            return
        await self._retry_call(
            lambda: self._paced(run, lambda: finish(source)), limits, run, cancel_event
        )

    async def _finish_after_failure(self, source: SourceDescription, run: _Run) -> None:
        """Best effort: pages committed before a failure still reach the index.

        A source that fails on the same page every run would otherwise leave
        every earlier page out of its routing index indefinitely. A failure
        here is audited and never masks the run's own error.
        """
        finish = getattr(self._ingest, "finish_sync", None)
        if finish is None:
            return
        try:
            await self._paced(run, lambda: finish(source))
        except Exception as exc:  # reason: the run's own failure is the one reported
            await self._emit("finish_failed", source, {"error": type(exc).__name__})

    async def _paced(self, run: _Run, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Run one unit of this source's work inside its duty cycle."""
        async with run.pacer.unit():
            return await operation()

    async def _heartbeat(self, run: _Run, owner: asyncio.Task[Any] | None) -> None:
        """Renew the lease every third of its life, whatever the run is doing.

        Renewing only between units let one slow fetch or one long ingest outlive
        the lease with no contention at all (sweep D9). This runs beside the work
        on the event loop's own clock, the clock the store's lease expiry uses.
        A renewal the store refuses means another writer may hold the lease now:
        the run is stopped at once rather than discovering it at its next commit.
        A renewal that raises is retried on the next beat; the store's fencing
        still refuses any write made after the lease really lapsed.
        """
        while True:
            await asyncio.sleep(run.ttl / 3)
            try:
                renewed = await self._state.renew_lease(
                    run.agent_did,
                    run.source_id,
                    owner_id=run.owner_id,
                    fencing_token=run.fencing_token,
                    ttl_seconds=run.ttl,
                )
            except Exception:  # reason: a blip is retried; the fence protects the data
                _logger.warning("connected-data lease renewal failed; retrying", exc_info=True)
                continue
            if not renewed:
                run.lease_lost = True
                if owner is not None:
                    owner.cancel()
                return

    async def _defer(self, source: SourceDescription, run: _Run, exc: SyncError) -> bool:
        """End a run as deferred; True when the lease was already lost.

        Pages committed before it stay committed and the cursor is kept. The row
        reads ``idle`` with the reason, not ``failed``: the account is healthy.
        A rate limit means the provider named when to come back; an unavailable
        sync worker means the store's writer is restarting.
        """
        try:
            await self._set_status(
                run.agent_did,
                run.source_id,
                SyncStatus.IDLE,
                owner_id=run.owner_id,
                fencing_token=run.fencing_token,
                error_code=exc.code,
            )
        except LeaseLostError:
            await self._emit("lease_lost", source, {})
            return True
        if exc.code == _RATE_LIMITED:
            await self._emit("rate_limited", source, {"retry_after": exc.retry_after})
        else:
            await self._emit(
                "deferred", source, {"reason": exc.code, "retry_after": exc.retry_after}
            )
        return False

    def _elapsed(self, run: _Run) -> float:
        """Time this run has spent working: pacing rest does not count."""
        now: float = self._clock()
        return now - run.started - run.pacer.rested

    async def _register_live_datastore(
        self,
        source: SourceDescription,
        mapping: MappingPlan,
        limits: SyncLimits,
        run: _Run,
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
            lambda: register(source, self._source, mapping), limits, run, cancel_event
        )

    async def _retry_call(
        self,
        operation: Any,
        limits: SyncLimits,
        run: _Run,
        cancel_event: asyncio.Event | None,
    ) -> Any:
        for attempt in range(limits.retries + 1):
            try:
                return await operation()
            except (TransientSyncError, SourceError) as exc:
                if isinstance(exc, SourceError) and exc.code is not SourceFailureCode.TRANSIENT:
                    raise SyncError(
                        str(exc), code=str(exc.code), retry_after=exc.retry_after
                    ) from exc
                if attempt >= limits.retries:
                    raise TransientSyncError(str(exc), retry_after=exc.retry_after) from exc
                self._check_cancel(cancel_event)
                # One wait never outlasts a third of the lease; the provider's own
                # longer delay is honoured by the scheduler (deferral), not here.
                await self._sleep(
                    min(
                        max(exc.retry_after or 0.0, limits.retry_backoff_seconds * (attempt + 1)),
                        run.ttl / 3,
                    )
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
        budget_reached: bool | None = None,
    ) -> None:
        if not await self._state.set_status(
            agent_did,
            source_id,
            status,
            owner_id=owner_id,
            fencing_token=fencing_token,
            error_code=error_code,
            budget_reached=budget_reached,
        ):
            raise LeaseLostError()

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

    async def _emit_page(self, source: SourceDescription, landed: _PageLanded) -> None:
        """One summary per committed page, in place of one audit row per object fetched.

        A large account is tens of thousands of object reads; a row each flooded the
        chain (26.8k fetch lines per agent in three days) and buried the rows that
        matter. The grant is still re-checked before every read, and a refused or
        failed read still audits on its own; what is summarised is only the routine
        success. Per-object rows remain for the objects that were skipped or failed.
        """
        if self._audit is None:
            return
        result = self._audit(
            "connector.source.page",
            {
                "source": _safe_id(source.connection_id),
                "objects": landed.objects,
                "bytes": landed.bytes,
                "failed": landed.failed,
            },
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

    async def _emit_object_failed(
        self, source: SourceDescription, source_object: SourceObject, reason: str
    ) -> None:
        """One object that failed unexpectedly, named so an operator can see it.

        Distinct from a skip: a skip is an object the source or ingest typed as
        un-takeable (too large, gone, unreadable); a failure is an unexpected error
        charged to this one object so the rest of the page still lands. A page made
        entirely of failures is not survivable — see ``_ingest_page``.
        """
        await self._emit(
            "object_failed",
            source,
            {"object": _safe_id(source_object.object_id), "reason": reason},
        )

    @staticmethod
    def _check_cancel(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _CancellationError()


@dataclass(frozen=True)
class _PageLanded:
    """What one page put into the index: bytes taken, objects ingested, objects failed."""

    bytes: int
    objects: int
    failed: int


@dataclass
class _Run:
    """One run's lease identity, working-time origin and duty-cycle limiter."""

    agent_did: str
    source_id: str
    owner_id: str
    fencing_token: int
    ttl: float
    started: float
    pacer: DutyCycleLimiter
    #: Set by the heartbeat when the store refused a renewal.
    lease_lost: bool = False


class _CancellationError(Exception):
    pass


def _absorb_own_cancel() -> bool:
    """Take back the heartbeat's cancellation; False if someone else also cancelled."""
    task = asyncio.current_task()
    return task is None or task.uncancel() == 0


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
