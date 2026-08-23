"""GitHub CLI-backed connected document source."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

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


class GitHubSourceAdapter:
    """Index selected repositories' issues and pull requests."""

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._repos: tuple[str, ...] = ()
        self._content: dict[str, tuple[str, bytes]] = {}

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        repos = await self._call("github_repo_list", {"limit": "1"})
        account = (
            str(repos[0].get("nameWithOwner", "github")).split("/", 1)[0]
            if repos
            else "github"
        )
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="github",
            account_id=account,
            data_shape=SourceDataShape.DOCUMENT,
            display_name=f"GitHub ({account})",
            supports_incremental=False,
            supports_deletes=False,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        repos = await self._call_all("github_repo_list", {})
        return tuple(
            SourceResource(
                resource_id=str(repo["nameWithOwner"]),
                label=str(repo["nameWithOwner"]),
                resource_kind="repository",
                locator=str(repo["nameWithOwner"]),
            )
            for repo in repos
            if repo.get("nameWithOwner")
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        resources = await self.list_source_resources(
            ListSourceResources(connection_id=request.connection_id)
        )
        allowed = {resource.resource_id for resource in resources}
        if not set(request.resource_ids).issubset(allowed):
            raise ValueError("invalid GitHub repository selection")
        self._repos = request.resource_ids

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        resources = await self.list_source_resources(
            ListSourceResources(connection_id=request.connection_id)
        )
        repos = self._repos or tuple(resource.resource_id for resource in resources)
        start = int(request.checkpoint or "0")
        records: list[tuple[str, str, dict[str, Any]]] = []
        for repo in repos:
            for kind, tool in (("issue", "github_issue_list"), ("pull", "github_pr_list")):
                args = {"repo": repo, "state": "all", "limit": "1000"}
                for item in await self._call_all(tool, args):
                    records.append((repo, kind, item))
        page_records = records[start : start + request.page_size]
        objects = tuple(self._object(repo, kind, item) for repo, kind, item in page_records)
        next_index = start + len(page_records)
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=str(next_index) if next_index < len(records) else "0",
            has_more=next_index < len(records),
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        version, content = self._content[request.object_id]
        if version != request.version:
            raise ValueError("GitHub object version changed")
        if len(content) > request.max_bytes:
            raise ValueError("GitHub object exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type="application/json",
            content=content,
            metadata={},
        )

    async def close_source(self) -> None:
        self._content.clear()

    async def _call(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._attachment.invoke(tool, args)
        if str(result.outcome) != "ok":
            raise RuntimeError(result.content)
        parsed = json.loads(result.content)
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    async def _call_all(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Read a complete CLI collection, refusing a provider-imposed hard cap.

        ``gh`` exposes a limit but no offset. Increasing the requested limit is its
        supported pagination mechanism; a constant response after increasing the
        limit means the provider capped the result and must not be treated as a
        complete collection.
        """
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
                    f"GitHub returned a bounded page for {tool}; refusing a partial index",
                )
            previous = count
            requested *= 2
        raise SourceError(
            SourceFailureCode.TOO_LARGE,
            f"GitHub {tool} collection exceeds the safe synchronization bound",
        )

    def _object(self, repo: str, kind: str, item: dict[str, Any]) -> SourceObject:
        number = str(item.get("number", ""))
        object_id = f"{repo}:{kind}:{number}"
        content = json.dumps(
            {"repository": repo, "kind": kind, **item}, ensure_ascii=False
        ).encode()
        version = str(item.get("updatedAt") or hashlib.sha256(content).hexdigest())
        self._content[object_id] = (version, content)
        return SourceObject(
            object_id=object_id,
            locator=str(item.get("url") or object_id),
            kind=SourceObjectKind.FILE,
            version=version,
            content_hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
            modified_at=str(item.get("updatedAt") or "") or None,
            media_type="application/json",
            metadata={
                "repository": repo,
                "kind": kind,
                "classification": "unclassified",
                "revision": _timestamp_revision(item.get("updatedAt")),
            },
        )


def build_source_adapter(context: dict[str, Any]) -> GitHubSourceAdapter:
    """Build from the already policy-bound CLI attachment."""
    return GitHubSourceAdapter(context["attachment"])


def _timestamp_revision(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 1
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


__all__ = ["GitHubSourceAdapter", "build_source_adapter"]
