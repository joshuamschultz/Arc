"""SPEC-082 COMP-007 (T-1089 RED) — the sync path renews credentials proactively.

REQ-426: while a connection has a renewable credential, the system SHALL
proactively renew it via ``CredentialLifecycle.ensure_fresh`` before expiry
(single-writer, per-account lock) FROM THE SYNC/MONITOR LOOP — not only
reactively on a 401.

``CredentialLifecycle.ensure_fresh`` exists and is fully unit-tested in
``tests/unit/extension/test_credentials.py``, but it has NO production caller:
nothing on the connected-data sync/monitor path invokes it. So a token silently
ages out mid-sync and every connection breaks at least once per credential
lifetime.

These tests spy on ``CredentialLifecycle.ensure_fresh`` and drive a real sync
through ``ConnectedDataService``. They fail RED because the sync path never
touches the renewal seam.

BUILDER NOTE (RED-wave handoff): the contract asserted here is "a real sync of a
registered source consults ``CredentialLifecycle.ensure_fresh``, and a terminal
renewal failure stops the sync before it runs against a dead credential." If the
GREEN wiring gates renewal on a per-connection credential account, the wiring
must derive that account from the registered source so this minimal source still
triggers it — do not weaken these assertions to make them pass.
"""

from __future__ import annotations

import asyncio

import pytest
from arcstore.source_sync import InMemorySourceSyncStore

import arcagent.extension.credentials as credmod
from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.credentials import CredentialRenewalError
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.service import ConnectedDataService


class _CountingSource:
    """A one-object source that records how many times it was actually synced."""

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
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint, has_more=False)
        return SyncSourcePage(
            objects=(
                SourceObject(
                    object_id="message-1",
                    locator="inbox/message-1",
                    kind=SourceObjectKind.FILE,
                    version="1",
                    media_type="text/plain",
                ),
            ),
            next_checkpoint="cursor-1",
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"launch notes",
        )

    async def close_source(self) -> None:
        self.closed = True


class _ApprovedIngest:
    def __init__(self) -> None:
        self.ingested: list[str] = []

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="approval-1",
            homes=(KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT),
            revision="r1",
            content_hash="h1",
        )

    async def ingest(self, source, source_object, content, mapping) -> None:  # type: ignore[no-untyped-def]
        self.ingested.append(source_object.object_id)

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


def _service(catalog: SourceCatalog, ingest: _ApprovedIngest) -> ConnectedDataService:
    return ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=lambda: _ready(InMemorySourceSyncStore()),
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=60,
    )


async def _wait_for_status(service: ConnectedDataService, expected: str) -> None:
    for _ in range(200):
        statuses = await service.list_sources()
        if statuses and statuses[0].status == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"source did not reach {expected!r}: {await service.list_sources()!r}")


@pytest.mark.asyncio
async def test_sync_path_consults_proactive_credential_renewal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A real sync of a registered source must invoke ``ensure_fresh`` (REQ-426)."""
    calls: list[str] = []

    async def spy(self, account, *, renew, caller_did):  # type: ignore[no-untyped-def]
        calls.append(getattr(account, "connection", getattr(account, "key", "unknown")))
        return False

    monkeypatch.setattr(credmod.CredentialLifecycle, "ensure_fresh", spy)

    catalog = SourceCatalog()
    await catalog.register("mail", _CountingSource())
    service = _service(catalog, _ApprovedIngest())
    await service.start()
    await _wait_for_status(service, "complete")
    await service.close()

    assert calls, (
        "the connected-data sync path never invoked CredentialLifecycle.ensure_fresh; "
        "credentials are only renewed reactively on a 401 (REQ-426 unmet)"
    )


@pytest.mark.asyncio
async def test_terminal_renewal_failure_stops_the_sync_before_it_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A dead credential must abort the sync, not run against a rotted token.

    When proactive renewal fails terminally (``invalid_grant`` — only a human can
    fix it), the sync must not proceed to hammer the source with a credential the
    authorization server has already rejected.
    """

    async def terminal(self, account, *, renew, caller_did):  # type: ignore[no-untyped-def]
        raise CredentialRenewalError(error_code="invalid_grant", message="operator must re-consent")

    monkeypatch.setattr(credmod.CredentialLifecycle, "ensure_fresh", terminal)

    catalog = SourceCatalog()
    source = _CountingSource()
    await catalog.register("mail", source)
    service = _service(catalog, _ApprovedIngest())
    await service.start()
    # Let the monitor loop run its first pass over the registered source.
    for _ in range(200):
        if source.sync_attempts or (await service.list_sources())[0].status != "inspecting":
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    await service.close()

    assert source.sync_attempts == 0, (
        "the sync ran against a credential whose proactive renewal failed terminally; "
        f"sync_source was called {source.sync_attempts} times"
    )
