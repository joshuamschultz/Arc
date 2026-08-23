"""Contract tests for OneDrive's independently synchronised Graph source.

The Microsoft 365 connection may expose Outlook and OneDrive simultaneously.  A
mail snapshot is not a OneDrive index: this suite holds the OneDrive sidecar to
the canonical source contract with a mocked Graph HTTP transport only.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pytest
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceError,
    SourceFailureCode,
    SourceObjectKind,
    SyncSource,
)

_ACCESS_TOKEN = "graph-access-token-must-never-escape"
_REFRESH_TOKEN = "graph-refresh-token-must-never-escape"


@dataclass
class _GraphReply:
    status_code: int = 200
    payload: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] = ()
    closed: bool = False

    def json(self) -> dict[str, Any]:
        return self.payload

    async def aiter_bytes(self) -> Iterable[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _GraphError(RuntimeError):
    def __init__(self, status: int, *, retry_after: str | None = None) -> None:
        super().__init__(f"Graph status {status}")
        self.status_code = status
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class _GraphTransport:
    """A deterministic Graph wire double keyed by exact endpoint suffixes."""

    def __init__(self, replies: Iterable[_GraphReply | Exception]) -> None:
        self._replies = iter(replies)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, path: str, **kwargs: Any) -> _GraphReply:
        self.calls.append((method, path, kwargs))
        reply = next(self._replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def aclose(self) -> None:
        self.closed = True


def _adapter(transport: _GraphTransport) -> Any:
    """Load the extension at test time so this suite names the intended sidecar."""
    module = importlib.import_module("extensions.microsoft365.arc_ext_microsoft365.source")
    cls = module.OneDriveSourceAdapter
    return cls(graph=transport)


async def test_drive_and_folder_resources_are_operator_selectable_and_scoped() -> None:
    transport = _GraphTransport(
        [
            _GraphReply(payload={"id": "user-a", "displayName": "Avery"}),
            _GraphReply(payload={"value": [{"id": "drive-a", "name": "OneDrive"}]}),
            _GraphReply(
                payload={
                    "value": [
                        {"id": "folder-design", "name": "Design", "folder": {}},
                        {"id": "file-readme", "name": "README.md", "file": {}},
                    ]
                }
            ),
        ]
    )
    adapter = _adapter(transport)

    description = await adapter.inspect_source(InspectSource(connection_id="m365:alpha:onedrive"))
    resources = await adapter.list_source_resources(
        ListSourceResources(connection_id="m365:alpha:onedrive")
    )
    await adapter.select_source_resources(
        SelectSourceResources(
            connection_id="m365:alpha:onedrive", resource_ids=("drive-a:folder-design",)
        )
    )

    assert description.connection_id == "m365:alpha:onedrive"
    assert description.source_kind == "onedrive"
    assert description.account_id == "user-a"
    assert description.supports_incremental and description.supports_deletes
    assert {resource.resource_id for resource in resources} >= {
        "drive-a:root",
        "drive-a:folder-design",
    }
    assert all("file-readme" not in resource.resource_id for resource in resources)
    paths = [path for _, path, _ in transport.calls]
    assert any(path.endswith("/me/drives") for path in paths)
    assert any(path.endswith("/drives/drive-a/root/children") for path in paths)


async def test_delta_paging_preserves_drive_item_identity_updates_moves_and_tombstones() -> None:
    transport = _GraphTransport(
        [
            _GraphReply(
                payload={
                    "value": [
                        {
                            "id": "item-1",
                            "name": "proposal.docx",
                            "eTag": '"etag-v1"',
                            "size": 7,
                            "file": {
                                "mimeType": (
                                    "application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document"
                                )
                            },
                            "parentReference": {"path": "/drive/root:/Design"},
                            "lastModifiedDateTime": "2026-08-23T12:00:00Z",
                            "fileSystemInfo": {"classification": "internal"},
                        }
                    ],
                    "@odata.nextLink": (
                        "/drives/drive-a/items/folder-design/delta?$skiptoken=page-2"
                    ),
                }
            ),
            _GraphReply(
                payload={
                    "value": [
                        {
                            "id": "item-1",
                            "name": "proposal-final.docx",
                            "eTag": '"etag-v2"',
                            "size": 9,
                            "file": {
                                "mimeType": (
                                    "application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document"
                                )
                            },
                            "parentReference": {"path": "/drive/root:/Archive"},
                        },
                        {"id": "item-removed", "name": "old.pdf", "deleted": {}},
                    ],
                    "@odata.deltaLink": "/drives/drive-a/items/folder-design/delta?token=final",
                }
            ),
        ]
    )
    adapter = _adapter(transport)
    await adapter.select_source_resources(
        SelectSourceResources(
            connection_id="m365:alpha:onedrive", resource_ids=("drive-a:folder-design",)
        )
    )

    first = await adapter.sync_source(
        SyncSource(connection_id="m365:alpha:onedrive", page_size=50)
    )
    second = await adapter.sync_source(
        SyncSource(
            connection_id="m365:alpha:onedrive", checkpoint=first.next_checkpoint, page_size=50
        )
    )

    assert first.has_more
    assert first.objects[0].object_id == "drive-a:item-1"
    assert first.objects[0].version == "etag-v1"
    assert first.objects[0].metadata["classification"] == "internal"
    assert second.objects[0].object_id == "drive-a:item-1"
    assert second.objects[0].version == "etag-v2"
    assert second.objects[0].locator.endswith("/Archive/proposal-final.docx")
    assert second.objects[1].kind is SourceObjectKind.DELETED
    assert second.objects[1].deleted
    assert second.next_checkpoint.endswith("token=final")


async def test_file_fetch_streams_with_a_byte_ceiling_and_preserves_graph_mime_metadata() -> None:
    reply = _GraphReply(
        headers={
            "Content-Type": "application/pdf",
            "ETag": '"etag-v2"',
            "x-ms-classification": "confidential",
        },
        chunks=(b"%PDF-", b"bytes"),
    )
    transport = _GraphTransport(
        [
            _GraphReply(
                payload={
                    "id": "item-pdf",
                    "eTag": '"etag-v2"',
                    "size": 10,
                    "file": {"mimeType": "application/pdf"},
                }
            ),
            reply,
        ]
    )
    adapter = _adapter(transport)
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="m365:alpha:onedrive", resource_ids=("drive-a:root",))
    )

    content = await adapter.fetch_source(
        FetchSourceObject(
            connection_id="m365:alpha:onedrive",
            object_id="drive-a:item-pdf",
            version="etag-v2",
            max_bytes=10,
        )
    )

    assert content.content == b"%PDF-bytes"
    assert content.media_type == "application/pdf"
    assert content.metadata["classification"] == "confidential"
    assert reply.closed
    assert any(
        path.endswith("/drives/drive-a/items/item-pdf/content") for _, path, _ in transport.calls
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, SourceFailureCode.AUTH_REQUIRED),
        (429, SourceFailureCode.RATE_LIMITED),
        (503, SourceFailureCode.TRANSIENT),
    ],
)
async def test_graph_failures_are_typed_retryable_and_redacted(
    status: int, expected: SourceFailureCode
) -> None:
    adapter = _adapter(_GraphTransport([_GraphError(status, retry_after="7")]))

    with pytest.raises(SourceError) as raised:
        await adapter.inspect_source(InspectSource(connection_id="m365:alpha:onedrive"))

    assert raised.value.code is expected
    assert raised.value.retry_after == (7.0 if status == 429 else None)
    assert _ACCESS_TOKEN not in raised.value.detail
    assert _REFRESH_TOKEN not in raised.value.detail


async def test_two_accounts_never_share_their_drive_scope_or_transport() -> None:
    first_transport = _GraphTransport(
        [
            _GraphReply(payload={"id": "user-a"}),
            _GraphReply(payload={"value": [{"id": "drive-a", "name": "A"}]}),
            _GraphReply(payload={"value": []}),
        ]
    )
    second_transport = _GraphTransport(
        [
            _GraphReply(payload={"id": "user-b"}),
            _GraphReply(payload={"value": [{"id": "drive-b", "name": "B"}]}),
            _GraphReply(payload={"value": []}),
        ]
    )
    first, second = _adapter(first_transport), _adapter(second_transport)

    first_description, second_description = (
        await first.inspect_source(InspectSource(connection_id="m365:first:onedrive")),
        await second.inspect_source(InspectSource(connection_id="m365:second:onedrive")),
    )
    await first.list_source_resources(
        ListSourceResources(connection_id=first_description.connection_id)
    )
    await second.list_source_resources(
        ListSourceResources(connection_id=second_description.connection_id)
    )
    await first.close_source()
    await second.close_source()

    assert first_description.account_id == "user-a"
    assert second_description.account_id == "user-b"
    assert all("drive-b" not in path for _, path, _ in first_transport.calls)
    assert all("drive-a" not in path for _, path, _ in second_transport.calls)
    assert first_transport.closed and second_transport.closed
