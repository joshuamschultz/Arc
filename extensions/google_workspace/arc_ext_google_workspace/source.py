"""Gmail mailbox source adapter over the already-authorized ``gog`` attachment."""

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


class GmailSourceAdapter:
    """Read bounded Gmail messages through declared read-only connector verbs."""

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._selected = "INBOX"

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="gmail",
            account_id="gmail-account",
            display_name="Gmail",
            supports_incremental=False,
            supports_deletes=False,
            root_locator=self._selected,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        result = await self._attachment.invoke("google_gmail_labels", {})
        payload = _json(result.content)
        labels = payload.get("labels", payload if isinstance(payload, list) else [])
        resources = [
            SourceResource(
                resource_id="INBOX",
                label="Inbox",
                resource_kind="mailbox",
                selected=self._selected == "INBOX",
            )
        ]
        for label in labels if isinstance(labels, list) else []:
            if not isinstance(label, dict):
                continue
            label_id = str(label.get("id") or label.get("name") or "")
            if label_id:
                resources.append(
                    SourceResource(
                        resource_id=label_id,
                        label=str(label.get("name") or label_id),
                        resource_kind="label",
                        selected=self._selected == label_id,
                    )
                )
        return tuple(resources)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        if len(request.resource_ids) != 1:
            raise SourceError(
                SourceFailureCode.UNSUPPORTED_CONTENT, "select one Gmail mailbox or label"
            )
        if request.resource_ids[0] not in {
            item.resource_id
            for item in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }:
            raise SourceError(
                SourceFailureCode.NOT_FOUND, "selected Gmail resource is unavailable"
            )
        self._selected = request.resource_ids[0]

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint, has_more=False)
        result = await self._attachment.invoke(
            "google_gmail_messages",
            {"label": request.root_locator or self._selected, "limit": str(request.page_size)},
        )
        messages = _json(result.content).get("messages", [])
        objects = tuple(_message_object(item) for item in messages if isinstance(item, dict))
        return SyncSourcePage(objects=objects, next_checkpoint="snapshot-1", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        result = await self._attachment.invoke("google_gmail_message", {"id": request.object_id})
        payload = _json(result.content)
        version = str(payload.get("historyId") or payload.get("internalDate") or "1")
        if version != request.version:
            raise SourceError(
                SourceFailureCode.VERSION_CHANGED, "Gmail message changed during fetch"
            )
        body = str(payload.get("snippet") or payload.get("body") or json.dumps(payload)).encode()
        if len(body) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "Gmail message exceeds byte limit")
        return SourceContent(
            object_id=request.object_id, version=version, media_type="text/plain", content=body
        )

    async def close_source(self) -> None:
        return None


def build_source_adapter(context: dict[str, Any]) -> GmailSourceAdapter:
    return GmailSourceAdapter(context["attachment"])


def _message_object(message: dict[str, Any]) -> SourceObject:
    object_id = str(message.get("id") or "")
    if not object_id:
        raise SourceError(SourceFailureCode.TRANSIENT, "Gmail returned a message without an id")
    version = str(message.get("historyId") or message.get("internalDate") or "1")
    return SourceObject(
        object_id=object_id,
        locator=str(message.get("threadId") or object_id),
        kind=SourceObjectKind.FILE,
        version=version,
        media_type="text/plain",
        metadata={"classification": "unclassified"},
    )


def _json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SourceError(SourceFailureCode.TRANSIENT, "Gmail returned invalid JSON") from exc
    return parsed if isinstance(parsed, dict) else {"messages": parsed}
