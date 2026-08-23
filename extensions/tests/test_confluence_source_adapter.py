"""Confluence connected-source lifecycle tests without opening a network socket."""

from __future__ import annotations

from typing import Any

from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SyncSource,
)

from extensions.confluence.arc_ext_confluence import ConfluenceAttachment


async def test_confluence_source_selects_syncs_and_fetches_pages() -> None:
    attachment = ConfluenceAttachment(
        base_url="https://arc.atlassian.net", email="arc@example.test", api_token="secret"
    )

    async def get(path: str, params: dict[str, str]) -> dict[str, Any]:
        if path.endswith("/space"):
            return {"results": [{"key": "ENG", "name": "Engineering"}]}
        if path.endswith("/content/search"):
            assert 'space in ("ENG")' in params["cql"]
            return {
                "results": [
                    {
                        "id": "42",
                        "title": "Arc Alpha",
                        "version": {"number": 3, "when": "2026-08-23T12:00:00Z"},
                        "_links": {"webui": "/spaces/ENG/pages/42"},
                    }
                ],
                "totalSize": 1,
            }
        return {
            "id": "42",
            "body": {"storage": {"value": "<p>Searchable Arc content</p>"}},
            "version": {"number": 3},
            "space": {"key": "ENG"},
        }

    attachment._get = get
    description = await attachment.inspect_source(InspectSource(connection_id="confluence"))
    resources = await attachment.list_source_resources(
        ListSourceResources(connection_id="confluence")
    )
    await attachment.select_source_resources(
        SelectSourceResources(connection_id="confluence", resource_ids=("ENG",))
    )
    page = await attachment.sync_source(SyncSource(connection_id="confluence"))
    fetched = await attachment.fetch_source(
        FetchSourceObject(
            connection_id="confluence", object_id="42", version=page.objects[0].version
        )
    )

    assert description.source_kind == "confluence"
    assert description.data_shape.value == "document"
    assert resources[0].resource_id == "ENG"
    assert page.objects[0].metadata["revision"] == 3
    assert fetched.content == b"<p>Searchable Arc content</p>"


async def test_confluence_source_walks_all_spaces() -> None:
    attachment = ConfluenceAttachment(
        base_url="https://arc.atlassian.net", email="arc@example.test", api_token="secret"
    )

    async def get(path: str, params: dict[str, str]) -> dict[str, Any]:
        if not path.endswith("/space"):
            return {}
        start = int(params.get("start", "0"))
        page = [
            {"key": f"S{i}", "name": f"Space {i}"}
            for i in range(start, min(start + 200, 450))
        ]
        return {"results": page, "totalSize": 450}

    attachment._get = get
    resources = await attachment.list_source_resources(
        ListSourceResources(connection_id="confluence")
    )
    assert len(resources) == 450
