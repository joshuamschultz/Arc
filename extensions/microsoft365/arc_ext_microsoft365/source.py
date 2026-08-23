"""Outlook mailbox source adapter over the declared Microsoft MCP attachment."""

from __future__ import annotations

import json
from typing import Any

from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)


class OutlookSourceAdapter:
    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._folder = "inbox"

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="outlook",
            account_id="microsoft365-account",
            display_name="Microsoft 365 Mail",
            supports_incremental=False,
            supports_deletes=False,
            root_locator=self._folder,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        result = await self._attachment.invoke("list-mail-folders", {})
        folders = _json(result.content).get("value", _json(result.content).get("folders", []))
        out = [
            SourceResource(
                resource_id="inbox",
                label="Inbox",
                resource_kind="folder",
                selected=self._folder == "inbox",
            )
        ]
        for folder in folders if isinstance(folders, list) else []:
            if isinstance(folder, dict) and (identifier := str(folder.get("id") or "")):
                out.append(
                    SourceResource(
                        resource_id=identifier,
                        label=str(folder.get("displayName") or identifier),
                        resource_kind="folder",
                        selected=self._folder == identifier,
                    )
                )
        return tuple(out)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        if len(request.resource_ids) != 1:
            raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "select one Outlook folder")
        self._folder = request.resource_ids[0]

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint, has_more=False)
        result = await self._attachment.invoke(
            "list-mail-messages",
            {"folder": request.root_locator or self._folder, "top": request.page_size},
        )
        messages = _json(result.content).get("value", [])
        objects = tuple(_object(value) for value in messages if isinstance(value, dict))
        return SyncSourcePage(objects=objects, next_checkpoint="snapshot-1", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        result = await self._attachment.invoke(
            "get-mail-message", {"message_id": request.object_id}
        )
        item = _json(result.content)
        version = str(item.get("changeKey") or item.get("lastModifiedDateTime") or "1")
        if version != request.version:
            raise SourceError(
                SourceFailureCode.VERSION_CHANGED, "Outlook message changed during fetch"
            )
        body = str(
            item.get("bodyPreview") or item.get("body", {}).get("content") or json.dumps(item)
        ).encode()
        if len(body) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "Outlook message exceeds byte limit")
        return SourceContent(
            object_id=request.object_id, version=version, media_type="text/plain", content=body
        )

    async def close_source(self) -> None:
        return None


def build_source_adapter(context: dict[str, Any]) -> OutlookSourceAdapter:
    return OutlookSourceAdapter(context["attachment"])


def _object(value: dict[str, Any]) -> SourceObject:
    identifier = str(value.get("id") or "")
    if not identifier:
        raise SourceError(SourceFailureCode.TRANSIENT, "Outlook returned a message without an id")
    return SourceObject(
        object_id=identifier,
        locator=str(value.get("conversationId") or identifier),
        kind=SourceObjectKind.FILE,
        version=str(value.get("changeKey") or value.get("lastModifiedDateTime") or "1"),
        media_type="text/plain",
        metadata={"classification": "unclassified"},
    )


def _json(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SourceError(
            SourceFailureCode.TRANSIENT, "Microsoft 365 returned invalid JSON"
        ) from exc
    return loaded if isinstance(loaded, dict) else {"value": loaded}
