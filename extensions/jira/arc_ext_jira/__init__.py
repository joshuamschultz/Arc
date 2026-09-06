"""Jira connected-source adapter over the policy-bound ``acli`` attachment."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from arcagent.extension.attachment import ToolOutcome
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)


class JiraSourceAdapter:
    """Index explicitly selected Jira projects as searchable issue documents.

    The adapter only reaches Jira through the attachment already authorized by
    Arc. It never reads the ``acli`` credential store or constructs a network
    client of its own.
    """

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._projects: tuple[str, ...] = ()
        self._content: dict[str, tuple[str, bytes]] = {}
        # The whole issue set, listed ONCE at the start of a crawl and paged from
        # memory. Re-listing every project on every page — and fetching each issue
        # in its own `acli` call — put a 657-issue account past the 900s deadline
        # every hour (one issue view is ~7s; one search returns all 657 in ~0.5s).
        self._records: list[dict[str, Any]] | None = None

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        await self._call("jira_list_projects", {})
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="jira",
            account_id="jira",
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Jira",
            supports_incremental=False,
            supports_deletes=False,
            root_locator=",".join(self._projects),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        resources: list[SourceResource] = []
        # Not _call_all: this verb takes no arguments by design. Its argv pins
        # --paginate, which already returns every project the account can see,
        # and acli refuses --limit alongside it. Growing a page size here made
        # every call fail with "undeclared argument(s) limit".
        for project in await self._call("jira_list_projects", {}):
            key = str(project.get("key") or project.get("id") or "")
            if not key:
                continue
            name = str(project.get("name") or key)
            resources.append(
                SourceResource(
                    resource_id=key,
                    label=f"{name} ({key})",
                    resource_kind="project",
                    locator=key,
                    selected=key in self._projects,
                )
            )
        return tuple(resources)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        available = {
            resource.resource_id
            for resource in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }
        if not set(request.resource_ids).issubset(available):
            raise SourceError(SourceFailureCode.NOT_FOUND, "selected Jira project is unavailable")
        self._projects = request.resource_ids

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        start = int(request.checkpoint or "0")
        # List the whole account once, at the start of a crawl (or on a resumed
        # run whose cache is cold), then page it from memory. The search payload
        # is indexed directly — its ``--fields`` already carry the description, so
        # there is no per-issue ``jira_get_issue`` (~7s each) to make.
        if start == 0 or self._records is None:
            self._records = await self._list_all_records(request.connection_id)
        records = self._records
        page = records[start : start + request.page_size]
        objects = tuple(self._object(item) for item in page)
        end = start + len(page)
        has_more = end < len(records)
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=str(end) if has_more else "0",
            has_more=has_more,
        )

    async def _list_all_records(self, connection_id: str) -> list[dict[str, Any]]:
        """Every issue in every selected project, in one pass. ``_call_all`` grows
        the page size until a project's whole issue set comes back in one call."""
        projects = self._projects
        if not projects:
            projects = tuple(
                resource.resource_id
                for resource in await self.list_source_resources(
                    ListSourceResources(connection_id=connection_id)
                )
            )
        records: list[dict[str, Any]] = []
        for project in projects:
            records.extend(
                await self._call_all(
                    "jira_search_issues",
                    {"jql": f'project = "{project}" ORDER BY updated ASC'},
                )
            )
        return records

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        cached = self._content.get(request.object_id)
        if cached is None:
            details = await self._call("jira_get_issue", {"issue_key": request.object_id})
            payload: Any = details[0] if len(details) == 1 else details
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            version = _version(content)
        else:
            version, content = cached
        if version != request.version:
            raise SourceError(SourceFailureCode.VERSION_CHANGED, "Jira issue changed during fetch")
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "Jira issue exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type="application/json",
            content=content,
        )

    async def close_source(self) -> None:
        self._content.clear()
        self._records = None

    async def _call(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._attachment.invoke(tool, args)
        if result.outcome is not ToolOutcome.OK:
            raise SourceError(SourceFailureCode.TRANSIENT, result.content)
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise SourceError(SourceFailureCode.TRANSIENT, "Jira returned invalid JSON") from exc
        if isinstance(payload, dict):
            for key in ("issues", "values", "projects", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        raise SourceError(SourceFailureCode.TRANSIENT, "Jira returned an unsupported JSON shape")

    async def _call_all(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk a CLI collection by growing its page size, never accepting a cap."""
        requested = 200
        previous = -1
        while requested <= 100_000:
            payload = await self._call(tool, {**args, "limit": str(requested)})
            count = len(payload)
            if count < requested:
                return payload
            if count <= previous:
                raise SourceError(
                    SourceFailureCode.TRANSIENT,
                    f"Jira returned a bounded page for {tool}; refusing a partial index",
                )
            previous = count
            requested *= 2
        raise SourceError(
            SourceFailureCode.TOO_LARGE,
            f"Jira {tool} collection exceeds the safe synchronization bound",
        )

    def _object(self, issue: dict[str, Any]) -> SourceObject:
        object_id = str(issue.get("key") or issue.get("id") or "")
        if not object_id:
            raise SourceError(SourceFailureCode.TRANSIENT, "Jira returned an issue without a key")
        content = json.dumps(issue, ensure_ascii=False, sort_keys=True).encode()
        version = _version(content)
        self._content[object_id] = (version, content)
        return SourceObject(
            object_id=object_id,
            locator=str(issue.get("self") or issue.get("url") or object_id),
            kind=SourceObjectKind.FILE,
            version=version,
            content_hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
            modified_at=str(issue.get("updated") or "") or None,
            media_type="application/json",
            metadata={
                "project": str(issue.get("project", "")),
                "classification": "unclassified",
                "revision": _revision_for(issue),
            },
        )


def _version(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _timestamp_revision(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 1
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def _revision_for(issue: dict[str, Any]) -> int:
    """A monotonic revision for an issue.

    ArcMemory skips an unchanged object by its content ``version`` and only
    consults the revision when the content changed, where it must be strictly
    greater than what it stored. Jira's ``updated`` cannot be returned as a
    search field (``acli`` refuses it), so when it is absent the current time is
    used: a later crawl always carries a higher revision, so a changed issue is
    accepted while an unchanged one is skipped on its version before this matters.
    """
    updated = issue.get("updated")
    if isinstance(updated, str) and updated:
        return _timestamp_revision(updated)
    return int(datetime.now(tz=UTC).timestamp() * 1_000_000)


def build_source_adapter(context: dict[str, Any]) -> JiraSourceAdapter:
    """Build from the already policy-bound CLI attachment."""
    return JiraSourceAdapter(context["attachment"])


__all__ = ["JiraSourceAdapter", "build_source_adapter"]
