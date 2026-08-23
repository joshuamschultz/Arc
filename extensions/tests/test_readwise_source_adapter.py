"""Readwise Reader connected-source contract tests over a fake attachment."""

from __future__ import annotations

import json
from typing import Any

import pytest
from arcagent.extension.attachment import ToolOutcome, ToolResult
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceError,
    SyncSource,
)

from extensions.readwise_reader.arc_ext_readwise_reader import ReadwiseSourceAdapter


class _Attachment:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool, args))
        if tool == "readwise_list_tags":
            payload: Any = [{"name": "research"}, {"name": "arc"}]
        elif tool == "readwise_list_documents":
            payload = [
                {
                    "id": "doc-1",
                    "title": "Arc notes",
                    "url": "https://example.test/arc",
                    "location": args.get("location", "new"),
                    "content": "Searchable Arc content",
                    "updated_at": "2026-08-23T12:00:00Z",
                }
            ]
        elif tool == "readwise_get_document":
            payload = {"id": args["document_id"], "content": "Searchable Arc content"}
        else:
            raise AssertionError(tool)
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_readwise_source_discovers_selects_syncs_and_fetches_documents() -> None:
    attachment = _Attachment()
    adapter = ReadwiseSourceAdapter(attachment)

    description = await adapter.inspect_source(InspectSource(connection_id="readwise:primary"))
    resources = await adapter.list_source_resources(
        ListSourceResources(connection_id="readwise:primary")
    )
    await adapter.select_source_resources(
        SelectSourceResources(
            connection_id="readwise:primary", resource_ids=("tag:research",)
        )
    )
    page = await adapter.sync_source(SyncSource(connection_id="readwise:primary", page_size=10))

    assert description.source_kind == "readwise_reader"
    assert description.data_shape.value == "document"
    assert {resource.resource_id for resource in resources} >= {
        "location:all",
        "location:archive",
        "tag:research",
    }
    assert page.objects[0].object_id == "doc-1"
    assert int(page.objects[0].metadata["revision"]) > 1
    fetched = await adapter.fetch_source(
        FetchSourceObject(
            connection_id="readwise:primary",
            object_id="doc-1",
            version=page.objects[0].version or "",
        )
    )
    assert fetched.content == b"Searchable Arc content"


async def test_readwise_source_rejects_unavailable_resource() -> None:
    adapter = ReadwiseSourceAdapter(_Attachment())

    with pytest.raises(SourceError) as error:
        await adapter.select_source_resources(
            SelectSourceResources(connection_id="readwise:primary", resource_ids=("tag:nope",))
        )

    assert error.value.code.value == "not_found"


class _PagedReadwiseAttachment(_Attachment):
    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool == "readwise_list_tags":
            payload: Any = [{"name": "research"}]
        elif tool == "readwise_list_documents":
            count = min(int(args["limit"]), 450)
            payload = [
                {
                    "id": f"doc-{i}",
                    "title": f"Document {i}",
                    "content": f"content {i}",
                    "updated_at": "2026-08-23T12:00:00Z",
                }
                for i in range(count)
            ]
        else:
            payload = {"id": args["document_id"], "content": "content"}
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_readwise_source_walks_collections_larger_than_one_page() -> None:
    adapter = ReadwiseSourceAdapter(_PagedReadwiseAttachment())
    page = await adapter.sync_source(SyncSource(connection_id="readwise", page_size=200))
    assert len(page.objects) == 200
    assert page.has_more


def test_readwise_manifest_declares_source_entrypoint() -> None:
    from pathlib import Path

    from arcagent.core.tier import Tier
    from arcagent.extension.manifest import load_manifest

    path = Path(__file__).resolve().parents[1] / "readwise_reader" / "extension.toml"
    manifest = load_manifest(path.read_text(encoding="utf-8"), tier=Tier.PERSONAL)
    assert manifest.config["source"]["entrypoint"] == "arc_ext_readwise_reader"
