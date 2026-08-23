"""Readwise Reader connected-source adapter over the authorized CLI attachment."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
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

_LOCATIONS = ("new", "later", "shortlist", "archive", "feed")


class ReadwiseSourceAdapter:
    """Index selected Readwise Reader documents as searchable text documents."""

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._selected: tuple[str, ...] = ("location:all",)
        self._content: dict[str, tuple[str, bytes]] = {}

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        await self._call("readwise_list_documents", {"limit": "1"})
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="readwise_reader",
            account_id="readwise",
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Readwise Reader",
            supports_incremental=False,
            supports_deletes=False,
            root_locator=",".join(self._selected),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        resources = [
            SourceResource(
                resource_id="location:all",
                label="All documents",
                resource_kind="library",
                locator="",
                selected="location:all" in self._selected,
            )
        ]
        resources.extend(
            SourceResource(
                resource_id=f"location:{location}",
                label=location.title(),
                resource_kind="location",
                locator=location,
                selected=f"location:{location}" in self._selected,
            )
            for location in _LOCATIONS
        )
        for tag in await self._tags():
            resources.append(
                SourceResource(
                    resource_id=f"tag:{tag}",
                    label=f"Tag: {tag}",
                    resource_kind="tag",
                    locator=tag,
                    selected=f"tag:{tag}" in self._selected,
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
            raise SourceError(
                SourceFailureCode.NOT_FOUND, "selected Readwise resource is unavailable"
            )
        self._selected = request.resource_ids

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        records: dict[str, dict[str, Any]] = {}
        for resource in self._selected:
            args: dict[str, Any] = {"limit": str(request.page_size)}
            if resource.startswith("location:") and resource != "location:all":
                args["location"] = resource.removeprefix("location:")
            elif resource.startswith("tag:"):
                args["tag"] = resource.removeprefix("tag:")
            for item in await self._call_all("readwise_list_documents", args):
                object_id = str(item.get("id") or "")
                if object_id:
                    records[object_id] = item
        values = list(records.values())
        start = int(request.checkpoint or "0")
        page = values[start : start + request.page_size]
        objects = tuple([await self._full_object(item) for item in page])
        next_checkpoint = str(start + len(page))
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=next_checkpoint if start + len(page) < len(values) else "0",
            has_more=start + len(page) < len(values),
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        cached = self._content.get(request.object_id)
        if cached is None:
            records = await self._call(
                "readwise_get_document", {"document_id": request.object_id}
            )
            payload: Any = records[0] if len(records) == 1 else records
            content = _document_content(payload)
            version = _version(content)
        else:
            version, content = cached
        if version != request.version:
            raise SourceError(
                SourceFailureCode.VERSION_CHANGED, "Readwise document changed during fetch"
            )
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "Readwise document exceeds byte limit")
        media_type = "text/html" if b"<html" in content[:512].lower() else "text/plain"
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type=media_type,
            content=content,
        )

    async def close_source(self) -> None:
        self._content.clear()

    async def _tags(self) -> tuple[str, ...]:
        records = await self._call("readwise_list_tags", {})
        tags: list[str] = []
        for record in records:
            name = str(record.get("name") or record.get("tag") or "")
            if name:
                tags.append(name)
        return tuple(dict.fromkeys(tags))

    async def _call(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._attachment.invoke(tool, args)
        if result.outcome is not ToolOutcome.OK:
            raise SourceError(SourceFailureCode.TRANSIENT, result.content)
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise SourceError(
                SourceFailureCode.TRANSIENT, "Readwise returned invalid JSON"
            ) from exc
        if isinstance(payload, dict):
            for key in ("results", "documents", "items", "tags"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        raise SourceError(
            SourceFailureCode.TRANSIENT, "Readwise returned an unsupported JSON shape"
        )

    async def _call_all(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk a document collection without silently accepting a CLI cap."""
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
                    f"Readwise returned a bounded page for {tool}; refusing a partial index",
                )
            previous = count
            requested *= 2
        raise SourceError(
            SourceFailureCode.TOO_LARGE,
            f"Readwise {tool} collection exceeds the safe synchronization bound",
        )

    def _object(self, document: dict[str, Any]) -> SourceObject:
        object_id = str(document.get("id") or "")
        if not object_id:
            raise SourceError(
                SourceFailureCode.TRANSIENT, "Readwise returned a document without an id"
            )
        content = _document_content(document)
        version = _version(content)
        self._content[object_id] = (version, content)
        return SourceObject(
            object_id=object_id,
            locator=str(document.get("url") or object_id),
            kind=SourceObjectKind.FILE,
            version=version,
            content_hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
            modified_at=str(document.get("updated_at") or document.get("updatedAt") or "") or None,
            media_type="text/plain",
            metadata={
                "title": str(document.get("title") or ""),
                "category": str(document.get("category") or ""),
                "location": str(document.get("location") or ""),
                "classification": "unclassified",
                "revision": _timestamp_revision(
                    document.get("updated_at") or document.get("updatedAt")
                ),
            },
        )

    async def _full_object(self, document: dict[str, Any]) -> SourceObject:
        document_id = str(document.get("id") or "")
        if not document_id:
            return self._object(document)
        details = await self._call(
            "readwise_get_document", {"document_id": document_id}
        )
        return self._object({**document, **details[0]} if details else document)


def _document_content(document: Any) -> bytes:
    if isinstance(document, dict):
        for key in ("content", "html", "summary", "text"):
            value = document.get(key)
            if isinstance(value, str) and value:
                return value.encode()
    return json.dumps(document, ensure_ascii=False, sort_keys=True).encode()


def _version(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _timestamp_revision(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 1
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def build_source_adapter(context: dict[str, Any]) -> ReadwiseSourceAdapter:
    """Build from the already policy-bound CLI attachment."""
    return ReadwiseSourceAdapter(context["attachment"])


__all__ = ["ReadwiseSourceAdapter", "build_source_adapter"]
