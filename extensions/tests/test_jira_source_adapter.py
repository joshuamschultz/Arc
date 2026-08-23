"""Jira connected-source contract tests over a fake authorized attachment."""

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

from extensions.jira.arc_ext_jira import JiraSourceAdapter


class _Attachment:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool, args))
        payload: Any
        if tool == "jira_list_projects":
            payload = [{"key": "ARC", "name": "Arc"}, {"key": "OPS", "name": "Operations"}]
        elif tool == "jira_search_issues":
            project = args["jql"].split('"')[1]
            payload = [
                {
                    "key": f"{project}-1",
                    "summary": "First issue",
                    "updated": "2026-08-23T12:00:00Z",
                    "self": f"https://jira.example/{project}-1",
                }
            ]
        elif tool == "jira_get_issue":
            payload = {"key": args["issue_key"], "summary": "Fetched issue"}
        else:
            raise AssertionError(tool)
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_jira_source_discovers_selects_syncs_and_fetches_projects() -> None:
    attachment = _Attachment()
    adapter = JiraSourceAdapter(attachment)

    description = await adapter.inspect_source(InspectSource(connection_id="jira:primary"))
    resources = await adapter.list_source_resources(
        ListSourceResources(connection_id="jira:primary")
    )
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="jira:primary", resource_ids=("ARC",))
    )
    page = await adapter.sync_source(SyncSource(connection_id="jira:primary", page_size=10))

    assert description.source_kind == "jira"
    assert description.data_shape.value == "document"
    assert {resource.resource_id for resource in resources} == {"ARC", "OPS"}
    assert len(page.objects) == 1
    assert int(page.objects[0].metadata["revision"]) > 1
    fetched = await adapter.fetch_source(
        FetchSourceObject(
            connection_id="jira:primary",
            object_id=page.objects[0].object_id,
            version=page.objects[0].version or "",
        )
    )
    assert json.loads(fetched.content)["key"] == "ARC-1"
    assert attachment.calls[-1][0] == "jira_get_issue"


async def test_jira_source_rejects_unavailable_project() -> None:
    adapter = JiraSourceAdapter(_Attachment())

    with pytest.raises(SourceError) as error:
        await adapter.select_source_resources(
            SelectSourceResources(connection_id="jira:primary", resource_ids=("NOPE",))
        )

    assert error.value.code.value == "not_found"


class _PagedJiraAttachment(_Attachment):
    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool == "jira_list_projects":
            count = min(int(args["limit"]), 450)
            payload = [{"key": f"P{i}", "name": f"Project {i}"} for i in range(count)]
        elif tool == "jira_search_issues":
            project = args["jql"].split('"')[1]
            count = min(int(args["limit"]), 450)
            payload = [
                {
                    "key": f"{project}-{i}",
                    "summary": f"Issue {i}",
                    "updated": "2026-08-23T12:00:00Z",
                }
                for i in range(count)
            ]
        else:
            payload = {"key": args.get("issue_key", "P-1")}
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_jira_source_walks_collections_larger_than_one_page() -> None:
    adapter = JiraSourceAdapter(_PagedJiraAttachment())
    resources = await adapter.list_source_resources(ListSourceResources(connection_id="jira"))
    assert len(resources) == 450
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="jira", resource_ids=("P0",))
    )
    page = await adapter.sync_source(SyncSource(connection_id="jira", page_size=200))
    assert len(page.objects) == 200
    assert page.has_more


def test_jira_manifest_declares_source_entrypoint() -> None:
    from pathlib import Path

    from arcagent.core.tier import Tier
    from arcagent.extension.manifest import load_manifest

    path = Path(__file__).resolve().parents[1] / "jira" / "extension.toml"
    manifest = load_manifest(path.read_text(encoding="utf-8"), tier=Tier.PERSONAL)
    assert manifest.config["source"]["entrypoint"] == "arc_ext_jira"
