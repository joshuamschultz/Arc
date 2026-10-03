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
            payload = {
                "values": [{"key": "ARC", "name": "Arc"}, {"key": "OPS", "name": "Operations"}]
            }
        elif tool == "jira_search_issues":
            project = args["jql"].split('"')[1]
            payload = {
                "issues": [
                    {
                        "key": f"{project}-1",
                        "fields": {
                            "summary": "First issue",
                            "updated": "2026-08-23T12:00:00Z",
                            "project": project,
                        },
                    }
                ],
                "nextPageToken": "",
            }
        elif tool == "jira_get_issue":
            payload = {"key": args["issue_key"], "fields": {"summary": "Fetched issue"}}
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
    # The search payload is indexed directly — no per-issue ``jira_get_issue``
    # (~7s each on a real account) during listing OR fetch. That N+1 is what put
    # a 657-issue account past the 900s deadline every hour.
    assert "jira_get_issue" not in [tool for tool, _ in attachment.calls]


async def test_jira_lists_each_project_once_across_pages() -> None:
    """A crawl lists the account once and pages it from memory.

    Re-running the whole per-project search on every page — the old behavior —
    multiplied the cost by the page count and never finished inside the deadline.
    """
    adapter = JiraSourceAdapter(_PagedJiraAttachment())
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="jira", resource_ids=("P0",))
    )

    first = await adapter.sync_source(SyncSource(connection_id="jira", page_size=200))
    searches_after_first = sum(
        1
        for tool, _ in adapter._attachment.calls
        if tool == "jira_search_issues"  # type: ignore[attr-defined]
    )
    await adapter.sync_source(
        SyncSource(connection_id="jira", page_size=200, checkpoint=first.next_checkpoint)
    )
    searches_after_second = sum(
        1
        for tool, _ in adapter._attachment.calls
        if tool == "jira_search_issues"  # type: ignore[attr-defined]
    )

    # Page two adds no new search calls: it pages the cached listing.
    assert searches_after_second == searches_after_first


async def test_jira_source_rejects_unavailable_project() -> None:
    adapter = JiraSourceAdapter(_Attachment())

    with pytest.raises(SourceError) as error:
        await adapter.select_source_resources(
            SelectSourceResources(connection_id="jira:primary", resource_ids=("NOPE",))
        )

    assert error.value.code.value == "not_found"


class _PagedJiraAttachment(_Attachment):
    """REST-shaped pages of 100 issues chained by ``nextPageToken`` (450 issues in all)."""

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool, args))
        if tool == "jira_list_projects":
            payload: Any = {
                "values": [{"key": f"P{i}", "name": f"Project {i}"} for i in range(450)]
            }
        elif tool == "jira_search_issues":
            project = args["jql"].split('"')[1]
            start = int(args.get("page_token") or 0)
            stop = min(start + int(args["limit"]), 450)
            payload = {
                "issues": [
                    {
                        "key": f"{project}-{i}",
                        "fields": {"summary": f"Issue {i}", "updated": "2026-08-23T12:00:00Z"},
                    }
                    for i in range(start, stop)
                ],
                "nextPageToken": str(stop) if stop < 450 else "",
            }
        else:
            payload = {"key": args.get("issue_key", "P-1")}
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=json.dumps(payload))


async def test_jira_source_walks_every_page_by_next_page_token() -> None:
    """A project's issues are walked by ``nextPageToken``, never by guessing a page size."""
    adapter = JiraSourceAdapter(_PagedJiraAttachment())
    resources = await adapter.list_source_resources(ListSourceResources(connection_id="jira"))
    assert len(resources) == 450
    await adapter.select_source_resources(
        SelectSourceResources(connection_id="jira", resource_ids=("P0",))
    )
    page = await adapter.sync_source(SyncSource(connection_id="jira", page_size=200))
    assert len(page.objects) == 200
    assert page.has_more
    searches = [a for tool, a in adapter._attachment.calls if tool == "jira_search_issues"]  # type: ignore[attr-defined]
    assert [call["limit"] for call in searches] == ["100"] * 5


def test_jira_manifest_declares_source_entrypoint() -> None:
    from pathlib import Path

    from arcagent.core.tier import Tier
    from arcagent.extension.manifest import load_manifest

    path = Path(__file__).resolve().parents[1] / "jira" / "extension.toml"
    manifest = load_manifest(path.read_text(encoding="utf-8"), tier=Tier.PERSONAL)
    assert manifest.config["source"]["entrypoint"] == "arc_ext_jira"


async def test_jira_completion_checkpoint_is_none_not_zero() -> None:
    """A finished crawl commits no cursor, so the next run is a full, reconciling pass."""
    adapter = JiraSourceAdapter(_Attachment())
    page = await adapter.sync_source(SyncSource(connection_id="jira", page_size=200))
    assert not page.has_more
    assert page.next_checkpoint is None
