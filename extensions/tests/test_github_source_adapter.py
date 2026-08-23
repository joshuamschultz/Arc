"""GitHub connected-source contract tests over the authorized CLI attachment."""

from __future__ import annotations

import json
from typing import Any

from arcagent.extension.attachment import ToolOutcome, ToolResult
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SyncSource,
)

from extensions.github.arc_ext_github import GitHubSourceAdapter


class _Attachment:
    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool == "github_repo_list":
            payload = [{"nameWithOwner": "arc/arc"}, {"nameWithOwner": "arc/docs"}]
        else:
            payload = [
                {
                    "number": 7,
                    "title": f"{tool} record",
                    "updatedAt": "2026-08-23T12:00:00Z",
                    "url": "https://github.test/arc/arc/7",
                }
            ]
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_github_source_selects_syncs_and_fetches_repository_records() -> None:
    adapter = GitHubSourceAdapter(_Attachment())

    description = await adapter.inspect_source(InspectSource(connection_id="github"))
    resources = await adapter.list_source_resources(ListSourceResources(connection_id="github"))
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="github", resource_ids=("arc/arc",))
    )
    page = await adapter.sync_source(SyncSource(connection_id="github", page_size=10))
    fetched = await adapter.fetch_source(
        FetchSourceObject(
            connection_id="github",
            object_id=page.objects[0].object_id,
            version=page.objects[0].version,
        )
    )

    assert description.account_id == "arc"
    assert description.data_shape.value == "document"
    assert {resource.resource_id for resource in resources} == {"arc/arc", "arc/docs"}
    assert {item.metadata["kind"] for item in page.objects} == {"issue", "pull"}
    assert all(int(item.metadata["revision"]) > 1 for item in page.objects)
    assert json.loads(fetched.content)["repository"] == "arc/arc"


class _PagedAttachment:
    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        count = min(int(args["limit"]), 450)
        if tool == "github_repo_list":
            payload = [{"nameWithOwner": f"arc/repo-{i}"} for i in range(count)]
        else:
            payload = [
                {
                    "number": i,
                    "title": f"{tool} {i}",
                    "updatedAt": "2026-08-23T12:00:00Z",
                }
                for i in range(count)
            ]
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_github_source_walks_collections_larger_than_one_page() -> None:
    adapter = GitHubSourceAdapter(_PagedAttachment())
    repos = await adapter.list_source_resources(ListSourceResources(connection_id="github"))
    assert len(repos) == 450
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="github", resource_ids=("arc/repo-0",))
    )
    page = await adapter.sync_source(SyncSource(connection_id="github", page_size=200))
    assert len(page.objects) == 200
    assert page.has_more
