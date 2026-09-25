"""SPEC-082 COMP-008 (T-1091 RED) — a terminal sync failure backs off and escalates.

REQ-427: when a sync fails with ``AUTH_REQUIRED`` (or another terminal credential
failure), the system SHALL mark connection health as needs-attention and notify
the operator, and SHALL NOT silently retry the full sync on the next timer
against the same dead credential.

Today the monitor loop (``service._monitor_loop``) re-schedules EVERY registered
source on every ``interval_seconds`` tick, with no health state and no backoff.
A source whose credential is revoked therefore fails, is rescheduled, fails
again, on and on — hammering a dead credential every interval forever, while the
operator is never told and the connection's health is never marked. The intended
new surface is ``arcagent.modules.connected_data.health``.

These tests fail RED because there is no health marking and no backoff: the dead
source is re-synced on every tick and its surfaced status is ``failed``, not
``needs_attention``.
"""

from __future__ import annotations

import asyncio

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.service import ConnectedDataService


class _AuthRevokedSource:
    """A source whose credential has been revoked — only a person can fix it."""

    def __init__(self) -> None:
        self.sync_attempts = 0
        self.closed = False

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="email",
            account_id="mailbox",
            data_shape=SourceDataShape.MAIL,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.sync_attempts += 1
        raise SourceError(
            SourceFailureCode.AUTH_REQUIRED,
            'oauth2: "invalid_grant" "Token has been expired or revoked."',
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise AssertionError("a revoked source must never fetch content")

    async def close_source(self) -> None:
        self.closed = True


class _ApprovedIngest:
    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="approval-1",
            homes=(KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT),
            revision="r1",
            content_hash="h1",
        )

    async def ingest(self, source, source_object, content, mapping) -> None:  # type: ignore[no-untyped-def]
        return None

    def allowed_homes(self, source: SourceDescription) -> tuple[KnowledgeHome, ...]:
        return (KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)

    def canonical_source_id(self, source: SourceDescription) -> str:
        return "source-mailbox"

    async def complete_snapshot(self, source, object_ids, mapping) -> None:  # type: ignore[no-untyped-def]
        return None

    async def reset_source(self, source: SourceDescription) -> None:
        return None

    async def purge_source(self, source: SourceDescription) -> None:
        return None


async def _ready(value: InMemorySourceSyncStore) -> InMemorySourceSyncStore:
    return value


def _service(catalog: SourceCatalog) -> ConnectedDataService:
    return ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=lambda: _ready(InMemorySourceSyncStore()),
        ingest_factory=lambda _: _ApprovedIngest(),
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=0.01,
    )


async def _wait_until(predicate, *, turns: int = 400):  # type: ignore[no-untyped-def]
    for _ in range(turns):
        if await predicate():
            return True
        await asyncio.sleep(0)
    return False


@pytest.mark.asyncio
async def test_a_dead_credential_is_not_re_synced_every_tick() -> None:
    """A revoked credential must be backed off, not hammered every interval (REQ-427)."""
    catalog = SourceCatalog()
    source = _AuthRevokedSource()
    await catalog.register("mail", source)
    service = _service(catalog)
    await service.start()

    # Wait for the first (failing) sync to land.
    async def synced_at_least_once() -> bool:
        return source.sync_attempts >= 1

    reached = await _wait_until(synced_at_least_once)
    assert reached, "the source never attempted its first sync"

    # Give the monitor many more ticks; a backed-off source must not run again.
    await asyncio.sleep(0.15)
    attempts = source.sync_attempts
    await service.close()

    assert attempts == 1, (
        "a dead credential was re-synced on the timer instead of being backed off; "
        f"sync_source ran {attempts} times"
    )


@pytest.mark.asyncio
async def test_terminal_failure_surfaces_needs_attention_health() -> None:
    """A terminal auth failure must surface as needs-attention, not a bare 'failed'.

    'failed' reads as a transient blip a retry might clear; 'needs_attention' tells
    the operator (and the connector panel) that only a human re-consent will fix it.
    """
    catalog = SourceCatalog()
    await catalog.register("mail", _AuthRevokedSource())
    service = _service(catalog)
    await service.start()

    async def is_needs_attention() -> bool:
        statuses = await service.list_sources()
        if not statuses:
            return False
        status = statuses[0]
        return "needs_attention" in (status.status, status.detail)

    reached = await _wait_until(is_needs_attention)
    final = (await service.list_sources())[0]
    await service.close()

    assert reached, (
        "a terminal AUTH_REQUIRED failure was not surfaced as needs_attention health; "
        f"the source's surfaced status was {final.status!r} (detail={final.detail!r})"
    )


class _RevokedThenRestoredSource(_AuthRevokedSource):
    """Revoked on the first sync; the operator re-signs in OUTSIDE Arc before the next.

    The shape of a Google account reconnected through ``gog``: the credential lives
    in the binary's own keyring, so nothing in Arc changes when it comes back and
    no operator action in Arc ever clears the latch.
    """

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.sync_attempts += 1
        if self.sync_attempts == 1:
            raise SourceError(
                SourceFailureCode.AUTH_REQUIRED,
                'oauth2: "invalid_grant" "Token has been expired or revoked."',
            )
        return SyncSourcePage(objects=(), next_checkpoint="c1")


@pytest.mark.asyncio
async def test_a_dead_credential_is_rechecked_and_resumes_once_it_works_again() -> None:
    """Backed off is not latched forever: a slow recheck finds the restored credential."""
    catalog = SourceCatalog()
    source = _RevokedThenRestoredSource()
    await catalog.register("mail", source)
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=lambda: _ready(InMemorySourceSyncStore()),
        ingest_factory=lambda _: _ApprovedIngest(),
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=0.01,
        terminal_recheck_seconds=0.05,
    )
    await service.start()

    async def recovered() -> bool:
        statuses = await service.list_sources()
        return (
            source.sync_attempts >= 2
            and bool(statuses)
            and statuses[0].status != ("needs_attention")
        )

    reached = False
    for _ in range(100):
        if await recovered():
            reached = True
            break
        await asyncio.sleep(0.01)
    final = (await service.list_sources())[0]
    await service.close()

    assert reached, (
        "a credential restored outside Arc was never rechecked — the source stayed "
        f"latched ({source.sync_attempts} attempt(s), status {final.status!r})"
    )


def test_a_recheck_that_fails_again_backs_off_again_without_a_second_notice() -> None:
    from arcagent.modules.connected_data.health import ConnectionHealthTracker

    now = [0.0]
    tracker = ConnectionHealthTracker(recheck_after=60.0, clock=lambda: now[0])
    assert tracker.note_terminal_failure("mail") is True
    assert tracker.is_backed_off("mail")
    now[0] = 61.0
    assert not tracker.is_backed_off("mail"), "the recheck window passed"
    assert tracker.note_terminal_failure("mail") is False, "one notice per outage"
    assert tracker.is_backed_off("mail")
    tracker.clear("mail")
    assert not tracker.is_backed_off("mail")
