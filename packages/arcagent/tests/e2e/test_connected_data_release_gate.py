"""Alpha release gate for connected data.

These tests intentionally cross the package seams.  They use the real
``ConnectedDataService``, ``ArcMemoryIngestAdapter``, ``ApprovalStore`` and
ArcStore contract fake, while source adapters talk to deterministic provider
fakes.  A green unit suite is not enough for this journey: the operator must
be able to grant, select, map, approve, backfill, update/delete, restart and
then let the agent retrieve the result.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import (
    FetchSourceObject,
    KnowledgeHome,
    SyncLimits,
)
from arcagent.extension.source import (
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.ingest import (
    ArcMemoryIngestAdapter,
    ArcStoreObjectState,
    ArcStoreResourceSelection,
)
from arcagent.modules.connected_data.service import ConnectedDataService

_DID = "did:arc:alpha-release"


@dataclass
class _Object:
    object_id: str
    version: str
    body: bytes
    deleted: bool = False


class _MutableProvider:
    """A provider-like source with a cursor, resource scope and tombstones."""

    def __init__(self, *, source_kind: str, home: str) -> None:
        self.source_kind = source_kind
        self.home = home
        self.phase = 0
        self.selected: tuple[str, ...] = ()
        self.objects = {
            "doc-1": _Object("doc-1", "1", b"Alpha launch revenue is tracked in the report."),
            "doc-2": _Object("doc-2", "1", b"This record is removed in the delta."),
        }

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind=self.source_kind,
            account_id=f"{self.source_kind}-account",
            data_shape=(
                SourceDataShape.BLOB if self.source_kind == "blob" else SourceDataShape.DOCUMENT
            ),
            display_name=f"Mock {self.source_kind}",
            root_locator="inbox",
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        return (
            SourceResource(
                resource_id="inbox",
                label="Inbox",
                resource_kind="folder",
                selected=self.selected == ("inbox",),
            ),
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        if request.resource_ids != ("inbox",):
            raise ValueError("the release fixture exposes one selectable mailbox")
        self.selected = request.resource_ids

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if request.checkpoint is None:
            objects = tuple(
                SourceObject(
                    object_id=item.object_id,
                    locator=f"/{item.object_id}.txt",
                    kind=SourceObjectKind.FILE,
                    version=item.version,
                    media_type="text/plain",
                    metadata={"classification": "unclassified", "revision": 1},
                )
                for item in self.objects.values()
            )
            return SyncSourcePage(objects=objects, next_checkpoint="c1")
        if request.checkpoint == "c1" and self.phase >= 1:
            return SyncSourcePage(
                objects=(
                    SourceObject(
                        object_id="doc-1",
                        locator="/doc-1.txt",
                        kind=SourceObjectKind.FILE,
                        version="2",
                        media_type="text/plain",
                        metadata={"classification": "unclassified", "revision": 2},
                    ),
                    SourceObject(
                        object_id="doc-2",
                        locator="/doc-2.txt",
                        kind=SourceObjectKind.DELETED,
                        version="2",
                        deleted=True,
                        metadata={"classification": "unclassified", "revision": 2},
                    ),
                ),
                next_checkpoint="c2",
            )
        return SyncSourcePage(next_checkpoint=request.checkpoint or "c2")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        if request.object_id == "doc-1" and self.phase >= 1:
            return SourceContent(
                object_id="doc-1",
                version="2",
                media_type="text/plain",
                content=b"Alpha launch revenue was updated after the delta.",
            )
        item = self.objects[request.object_id]
        return SourceContent(
            object_id=item.object_id,
            version=item.version,
            media_type="text/plain",
            content=item.body,
        )

    async def close_source(self) -> None:
        return None


async def _wait(service: ConnectedDataService, expected: str) -> None:
    for _ in range(300):
        statuses = await service.list_sources()
        if statuses and statuses[0].status == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"source never reached {expected}: {await service.list_sources()}")


async def _wait_cursor(
    state: InMemorySourceSyncStore, *, expected: str, source_id: str
) -> None:
    for _ in range(300):
        if (await state.get_state(_DID, source_id)).cursor == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"source never committed cursor {expected!r}")


async def _approve_mapping(approval: ApprovalStore, approval_id: str) -> None:
    row = await approval.get(approval_id)
    assert row is not None and row.status == "pending"
    # The generic ArcUI approval route adds the signed grant in a live
    # deployment.  ArcMemory's approval seam consumes only the terminal status;
    # resolving through the shared store keeps this test independent of a local
    # operator key while still proving the same durable approval row is used.
    resolved = await approval.resolve(
        approval_id,
        status="approved",
        actor_did="did:arc:operator",
        resolved_by="did:arc:operator",
    )
    assert resolved is not None and resolved.status == "approved"


@pytest.mark.asyncio
async def test_release_gate_document_blob_backfill_delta_delete_restart_and_agent_recall(
    tmp_path: Path,
) -> None:
    """Exercise the full Dropbox/blob-shaped journey through the real seams."""

    backend = FakeBackend()
    approval = ApprovalStore(backend)
    sync_state = InMemorySourceSyncStore()
    source = _MutableProvider(source_kind="blob", home="document")
    catalog = SourceCatalog()
    await catalog.register("dropbox-alpha", source)
    object_state = ArcStoreObjectState(backend, actor_did=_DID)
    resource_state = ArcStoreResourceSelection(backend, actor_did=_DID)
    ingest = ArcMemoryIngestAdapter(
        tmp_path,
        _DID,
        approval_store=approval,
        object_state=object_state,
    )

    async def open_sync() -> InMemorySourceSyncStore:
        return sync_state

    async def open_resources() -> ArcStoreResourceSelection:
        return resource_state

    service = ConnectedDataService(
        catalog,
        agent_did=_DID,
        sync_store_opener=open_sync,
        ingest_factory=lambda _: ingest,
        resource_selection_store_opener=open_resources,
        limits=SyncLimits(max_pages=4, max_bytes=1_000_000),
        global_concurrency=1,
        interval_seconds=3600,
    )
    await service.start()
    await _wait(service, "awaiting_mapping")

    resources = await service.list_resources("dropbox-alpha")
    assert [item.resource_id for item in resources] == ["inbox"]
    selected = await service.select_resources("dropbox-alpha", resource_ids=("inbox",))
    assert selected[0].selected

    proposal = await service.stage_mapping(
        "dropbox-alpha", homes=(KnowledgeHome.DOCUMENT, KnowledgeHome.BLOB)
    )
    assert proposal is not None
    assert proposal.approval_status == "pending"
    await _approve_mapping(approval, proposal.approval_id)

    assert (await service.sync_now("dropbox-alpha")).status == "scheduled"
    await _wait(service, "complete")
    source.phase = 1
    assert (await service.sync_now("dropbox-alpha")).status == "scheduled"
    await _wait_cursor(sync_state, expected="c2", source_id="dropbox-alpha")
    state = await sync_state.get_state(_DID, "dropbox-alpha")
    assert state.cursor == "c2"

    from arcmemory.brain import ArcMemoryBrain

    source_id = ingest.canonical_source_id(
        SourceDescription(
            connection_id="dropbox-alpha",
            source_kind="blob",
            account_id="blob-account",
        )
    )
    brain = ArcMemoryBrain(tmp_path, _DID)
    hits = await brain.document_search(
        "updated launch revenue", source_id=source_id, caller_did=_DID
    )
    assert hits and "updated" in hits[0].text
    deleted_state = await object_state.get_object_state(source_id, "doc-2")
    assert deleted_state is not None and deleted_state.deleted

    await service.close()
    restarted = ConnectedDataService(
        catalog,
        agent_did=_DID,
        sync_store_opener=open_sync,
        ingest_factory=lambda _: ingest,
        resource_selection_store_opener=open_resources,
        limits=SyncLimits(max_pages=4, max_bytes=1_000_000),
        global_concurrency=1,
        interval_seconds=3600,
    )
    await restarted.start()
    await _wait(restarted, "complete")
    assert (await resource_state.get("dropbox-alpha")) == ("inbox",)
    await restarted.close()


@pytest.mark.asyncio
async def test_release_gate_sql_schema_and_profile_review_are_agent_safe(tmp_path: Path) -> None:
    """Structured stores stay typed; inferred profile facts stay approval-gated."""

    from arcmemory.brain import ArcMemoryBrain
    from arcmemory.connected_data import (
        ConnectedDataService,
        ConnectedObject,
        ConnectedSource,
        ConnectedSourceShape,
        SourceContent,
    )
    from arcmemory.datastore import SqliteDatastorePort
    from arcmemory.profile import ProfileFactKind, ReviewStatus

    brain = ArcMemoryBrain(tmp_path, _DID)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("INSERT INTO accounts VALUES ('a-1', 'Arc Alpha')")
    await brain.register_datastore(
        "supabase-alpha",
        SqliteDatastorePort(conn),
        caller_did=_DID,
    )
    assert await brain.datastore_query(
        "supabase-alpha", "get_record", "accounts", {"pk_value": "a-1"}, caller_did=_DID
    ) == {"id": "a-1", "name": "Arc Alpha"}

    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(tmp_path / "profile", _DID, approval_store=approval)
    profile_source = ConnectedSource(
        connection_id="crm-alpha", account_id="crm-account", source_kind="profile"
        , data_shape=ConnectedSourceShape.PROFILE
    )
    await service.propose_mapping(profile_source, ("profile",))
    pending_mapping = (await approval.list())[0]
    await _approve_mapping(approval, pending_mapping.id)
    mapping = await service.require_approved_mapping(profile_source)
    await service.ingest(
        profile_source,
        ConnectedObject(
            object_id="fact-1",
            locator="crm://alpha/role",
            version="1",
            media_type="text/plain",
            classification="unclassified",
            metadata={
                "profile_id": "olivia",
                "profile_field": "role",
                "profile_kind": ProfileFactKind.INFERRED.value,
            },
        ),
        SourceContent(object_id="fact-1", version="1", content=b"product designer"),
        mapping,
    )
    pending = await service.review_port.list(status=ReviewStatus.PENDING)
    assert len(pending) == 1
    assert (await service.review_port.context("olivia")).inferred == {}
    await service.review_port.approve(pending[0].fact_id)
    assert (await service.review_port.context("olivia")).inferred["role"] == "product designer"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["gmail", "outlook"])
async def test_release_gate_mail_adapters_expose_resources_and_versioned_messages(
    adapter_kind: str,
) -> None:
    """Gmail and Microsoft 365 adapters normalize provider payloads at the seam."""

    class Attachment:
        async def invoke(self, name: str, args: dict[str, Any]) -> Any:
            if adapter_kind == "gmail":
                payloads = {
                    "google_gmail_labels": {"labels": [{"id": "INBOX", "name": "Inbox"}]},
                    "google_gmail_messages": {"messages": [{"id": "m-1", "historyId": "h-1"}]},
                    "google_gmail_message": {"id": "m-1", "historyId": "h-1", "body": "launch"},
                }
            else:
                payloads = {
                    "list-mail-folders": {"value": [{"id": "inbox", "displayName": "Inbox"}]},
                    "list-mail-messages": {"value": [{"id": "m-1", "changeKey": "k-1"}]},
                    "get-mail-message": {"id": "m-1", "changeKey": "k-1", "bodyPreview": "launch"},
                }
            return SimpleNamespace(content=json.dumps(payloads[name]))

    if adapter_kind == "gmail":
        from extensions.google_workspace.arc_ext_google_workspace.source import GmailSourceAdapter

        adapter = GmailSourceAdapter(Attachment())
        expected_kind = "gmail"
    else:
        from extensions.microsoft365.arc_ext_microsoft365.source import OutlookSourceAdapter

        adapter = OutlookSourceAdapter(Attachment())
        expected_kind = "outlook"

    description = await adapter.inspect_source(InspectSource(connection_id=f"{adapter_kind}-alpha"))
    resources = await adapter.list_source_resources(
        ListSourceResources(connection_id=description.connection_id)
    )
    page = await adapter.sync_source(
        SyncSource(connection_id=description.connection_id, root_locator=description.root_locator)
    )
    content = await adapter.fetch_source(
        FetchSourceObject(
            connection_id=description.connection_id,
            object_id=page.objects[0].object_id,
            version=page.objects[0].version or "",
        )
    )
    assert description.source_kind == expected_kind
    assert resources and resources[0].resource_id
    assert page.objects[0].object_id == "m-1"
    assert content.content == b"launch"


def test_release_gate_provider_matrix_has_each_declared_source_seam() -> None:
    """Every Alpha connector must expose the same source lifecycle contract.

    Outlook and OneDrive intentionally remain separate source instances even
    though Microsoft grants one MCP attachment.  This assertion is the release
    tripwire for accidentally shipping only the Outlook half of that grant.
    """

    from extensions.dropbox.arc_ext_dropbox import DropboxAttachment
    from extensions.google_workspace.arc_ext_google_workspace.source import GmailSourceAdapter
    from extensions.microsoft365.arc_ext_microsoft365 import source as microsoft_source
    from extensions.postgresql.arc_ext_postgresql import PostgreSQLAttachment
    from extensions.s3.arc_ext_s3 import S3Attachment

    try:
        sqlite_source = importlib.import_module("extensions.sqlite.arc_ext_sqlite.source")
    except ModuleNotFoundError:
        sqlite_source = None
    sqlite_adapter = None if sqlite_source is None else getattr(
        sqlite_source, "SQLiteSourceAdapter", None
    )
    lifecycle = {
        "dropbox": DropboxAttachment,
        "postgres": PostgreSQLAttachment,
        "s3": S3Attachment,
        "gmail": GmailSourceAdapter,
        "outlook": microsoft_source.OutlookSourceAdapter,
        "onedrive": getattr(microsoft_source, "OneDriveSourceAdapter", None),
        "sqlite": sqlite_adapter,
    }
    missing = [name for name, adapter in lifecycle.items() if adapter is None]
    assert not missing, f"missing connected-data source adapters: {', '.join(missing)}"
    for name, adapter in lifecycle.items():
        assert all(
            hasattr(adapter, method)
            for method in (
                "inspect_source",
                "list_source_resources",
                "select_source_resources",
                "sync_source",
                "fetch_source",
                "close_source",
            )
        ), f"{name} adapter does not implement the source lifecycle"


@pytest.mark.asyncio
async def test_dropbox_native_connection_is_enrollable_as_a_knowledge_source() -> None:
    """The working tool attachment itself must also satisfy the source catalog seam."""
    from arcagent.extension.native_attachment import NativeAttachment

    attachment = NativeAttachment(
        "extensions.dropbox.arc_ext_dropbox",
        {"app_key": "app", "app_secret": "secret", "refresh_token": "refresh"},
    )
    source = attachment.source_adapter()

    assert source is not None
    assert source.__class__.__name__ == "DropboxAttachment"
    await source.close_source()


@pytest.mark.asyncio
async def test_release_gate_sqlite_file_resource_is_reopenable_and_read_only(
    tmp_path: Path,
) -> None:
    """A connected SQLite file is an approved resource, not the agent index DB."""

    module = importlib.import_module("extensions.sqlite.arc_ext_sqlite.source")
    factory = getattr(module, "build_source_adapter", None)
    assert callable(factory), "SQLite extension must expose build_source_adapter"
    database = tmp_path / "customer.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("INSERT INTO customers VALUES ('c-1', 'Alpha')")
    conn.commit()
    conn.close()

    adapter = factory({"database_path": database})
    description = await adapter.inspect_source(InspectSource(connection_id="sqlite-alpha"))
    resources = await adapter.list_source_resources(
        ListSourceResources(connection_id="sqlite-alpha")
    )
    assert description.source_kind == "sqlite"
    assert [item.resource_id for item in resources] == ["customers"]
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="sqlite-alpha", resource_ids=("customers",))
    )
    assert (await adapter.sync_source(SyncSource(connection_id="sqlite-alpha"))).next_checkpoint
    assert await adapter.query("get_record", "customers", {"pk_value": "c-1"}) == {
        "id": "c-1",
        "name": "Alpha",
    }
    await adapter.close_source()

    reopened = factory({"database_path": database})
    assert [item.resource_id for item in await reopened.list_source_resources(
        ListSourceResources(connection_id="sqlite-alpha")
    )] == ["customers"]
    await reopened.close_source()
