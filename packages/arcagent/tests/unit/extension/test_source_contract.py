"""The vendor-neutral contract used by connected document and blob sources."""

from __future__ import annotations

from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceAdapter,
    SourceContent,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)


class _Source:
    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="fake",
            account_id="account",
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return SyncSourcePage(
            objects=(
                SourceObject(
                    object_id="id:1",
                    locator="/one.txt",
                    kind=SourceObjectKind.FILE,
                    version="1",
                ),
            ),
            next_checkpoint=request.checkpoint or "cursor",
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"one",
        )

    async def close_source(self) -> None:
        return None


def test_source_adapter_is_runtime_checkable() -> None:
    assert isinstance(_Source(), SourceAdapter)


def test_contract_values_are_frozen_and_reject_unknown_fields() -> None:
    request = SyncSource(connection_id="dropbox:one", page_size=200)
    assert request.page_size == 200
    assert request.checkpoint is None


def test_tombstone_cannot_claim_fetchable_content() -> None:
    deleted = SourceObject(
        object_id="id:gone",
        locator="/gone.pdf",
        kind=SourceObjectKind.DELETED,
        deleted=True,
    )
    assert deleted.deleted
    assert deleted.version is None
