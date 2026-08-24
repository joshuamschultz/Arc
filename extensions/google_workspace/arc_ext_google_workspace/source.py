"""Gmail source adapter over the already-authorized ``gog`` attachment."""

from __future__ import annotations

import json
import re
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

_CURSOR_VERSION = 1


class GmailSourceAdapter:
    """Synchronize a selected label through Gmail list and history cursors."""

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._selected = "INBOX"

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="gmail",
            account_id="gmail-account",
            data_shape=SourceDataShape.MAIL,
            display_name="Gmail",
            supports_incremental=True,
            supports_deletes=True,
            root_locator=self._selected,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        result = await self._attachment.invoke("google_gmail_labels", {})
        payload = _payload(result)
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
        cursor = _cursor(request.checkpoint)
        label = request.root_locator or self._selected
        if cursor is None or cursor["mode"] == "snapshot":
            return await self._sync_snapshot(request, label, cursor)
        return await self._sync_history(request, label, cursor)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        result = await self._attachment.invoke("google_gmail_message", {"id": request.object_id})
        payload = _payload(result)
        version = _revision(payload)
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

    async def _sync_snapshot(
        self, request: SyncSource, label: str, cursor: dict[str, str] | None
    ) -> SyncSourcePage:
        arguments: dict[str, str] = {"label": label, "limit": str(request.page_size)}
        if cursor is not None and (token := cursor.get("page_token")):
            arguments["page_token"] = token
        result = await self._attachment.invoke("google_gmail_messages", arguments)
        payload = _payload(result)
        objects = await self._message_objects(payload.get("messages", []))
        page_token = str(payload.get("nextPageToken") or "")
        if page_token:
            return SyncSourcePage(
                objects=objects,
                next_checkpoint=_encode_cursor("snapshot", page_token=page_token),
                has_more=True,
            )
        history_id = _revision_or_none(payload) or _latest_revision(objects)
        if history_id is None:
            return SyncSourcePage(objects=objects, next_checkpoint=_encode_cursor("snapshot"))
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=_encode_cursor("history", history_id=history_id),
        )

    async def _sync_history(
        self, request: SyncSource, label: str, cursor: dict[str, str]
    ) -> SyncSourcePage:
        arguments: dict[str, str] = {
            "label": label,
            "limit": str(request.page_size),
            "start_history_id": cursor["history_id"],
        }
        if token := cursor.get("page_token"):
            arguments["page_token"] = token
        result = await self._attachment.invoke("google_gmail_history", arguments)
        payload = _payload(result)
        objects = await self._history_objects(payload.get("history", []))
        next_history = _revision(payload) if payload.get("historyId") else cursor["history_id"]
        page_token = str(payload.get("nextPageToken") or "")
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=_encode_cursor(
                "history", history_id=next_history, page_token=page_token or None
            ),
            has_more=bool(page_token),
        )

    async def _message_objects(self, messages: Any) -> tuple[SourceObject, ...]:
        if not isinstance(messages, list):
            raise SourceError(SourceFailureCode.TRANSIENT, "Gmail returned invalid message list")
        objects: list[SourceObject] = []
        for message in messages:
            if not isinstance(message, dict) or not (message_id := str(message.get("id") or "")):
                raise SourceError(
                    SourceFailureCode.TRANSIENT, "Gmail returned a message without an id"
                )
            objects.append(_message_object(await self._message(message_id)))
        return tuple(objects)

    async def _history_objects(self, history: Any) -> tuple[SourceObject, ...]:
        if not isinstance(history, list):
            raise SourceError(SourceFailureCode.TRANSIENT, "Gmail returned invalid history")
        objects: dict[str, SourceObject] = {}
        for change in history:
            if not isinstance(change, dict):
                continue
            revision = _revision(change)
            for message_id in _deleted_message_ids(change):
                objects[message_id] = _deleted_object(message_id, revision)
            for message_id in _changed_message_ids(change):
                try:
                    objects[message_id] = _message_object(await self._message(message_id))
                except SourceError as error:
                    if error.code is not SourceFailureCode.NOT_FOUND:
                        raise
                    objects[message_id] = _deleted_object(message_id, revision)
        return tuple(objects.values())

    async def _message(self, message_id: str) -> dict[str, Any]:
        result = await self._attachment.invoke("google_gmail_message", {"id": message_id})
        payload = _payload(result)
        if not payload.get("id"):
            raise SourceError(SourceFailureCode.NOT_FOUND, "Gmail message is unavailable")
        return payload


def build_source_adapter(context: dict[str, Any]) -> GmailSourceAdapter:
    return GmailSourceAdapter(context["attachment"])


def _message_object(message: dict[str, Any]) -> SourceObject:
    object_id = str(message.get("id") or "")
    if not object_id:
        raise SourceError(SourceFailureCode.TRANSIENT, "Gmail returned a message without an id")
    version = _revision(message)
    return SourceObject(
        object_id=object_id,
        locator=str(message.get("threadId") or object_id),
        kind=SourceObjectKind.FILE,
        version=version,
        media_type="text/plain",
        metadata={"classification": "unclassified", "revision": int(version)},
    )


def _deleted_object(object_id: str, version: str) -> SourceObject:
    return SourceObject(
        object_id=object_id,
        locator=object_id,
        kind=SourceObjectKind.DELETED,
        version=version,
        deleted=True,
        metadata={"classification": "unclassified", "revision": int(version)},
    )


def _changed_message_ids(change: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for field in ("messagesAdded", "labelsAdded", "labelsRemoved"):
        for entry in change.get(field, []):
            if isinstance(entry, dict) and isinstance(entry.get("message"), dict):
                message_id = str(entry["message"].get("id") or "")
                if message_id:
                    values.append(message_id)
    return tuple(dict.fromkeys(values))


def _deleted_message_ids(change: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for entry in change.get("messagesDeleted", []):
        if isinstance(entry, dict) and isinstance(entry.get("message"), dict):
            message_id = str(entry["message"].get("id") or "")
            if message_id:
                values.append(message_id)
    return tuple(values)


def _cursor(value: str | None) -> dict[str, str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SourceError(
            SourceFailureCode.CHECKPOINT_INVALID, "invalid Gmail checkpoint"
        ) from exc
    if not isinstance(parsed, dict) or parsed.get("v") != _CURSOR_VERSION:
        raise SourceError(SourceFailureCode.CHECKPOINT_INVALID, "invalid Gmail checkpoint")
    mode = parsed.get("mode")
    if mode not in {"snapshot", "history"}:
        raise SourceError(SourceFailureCode.CHECKPOINT_INVALID, "invalid Gmail checkpoint")
    cursor = {key: str(item) for key, item in parsed.items() if isinstance(item, (str, int))}
    if mode == "history" and not cursor.get("history_id"):
        raise SourceError(SourceFailureCode.CHECKPOINT_INVALID, "invalid Gmail checkpoint")
    return cursor


def _encode_cursor(mode: str, **values: str | None) -> str:
    payload = {"v": _CURSOR_VERSION, "mode": mode}
    payload.update({key: value for key, value in values.items() if value})
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _revision(value: dict[str, Any]) -> str:
    raw = str(value.get("historyId") or value.get("internalDate") or value.get("id") or "")
    digits = "".join(re.findall(r"\d+", raw))
    if not digits:
        raise SourceError(SourceFailureCode.TRANSIENT, "Gmail returned no numeric revision")
    return str(int(digits))


def _revision_or_none(value: dict[str, Any]) -> str | None:
    try:
        return _revision(value)
    except SourceError:
        return None


def _latest_revision(objects: tuple[SourceObject, ...]) -> str | None:
    revisions = [int(item.version) for item in objects if item.version and item.version.isdigit()]
    return str(max(revisions)) if revisions else None


def _payload(result: Any) -> dict[str, Any]:
    """The tool's JSON, or the tool's own words about why there is none.

    A failed call returns readable prose in ``content``. Parsing that as JSON
    reported "Gmail returned invalid JSON" over every real cause — an expired
    grant, a revoked scope, a rate limit — and left an operator with nothing to
    act on.
    """
    if getattr(result, "outcome", None) is not None and str(result.outcome) != "ok":
        raise SourceError(SourceFailureCode.TRANSIENT, str(result.content)[:256])
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError as exc:
        detail = str(result.content)[:200].strip() or "an empty response"
        raise SourceError(
            SourceFailureCode.TRANSIENT, f"Gmail returned no JSON: {detail}"
        ) from exc
    return parsed if isinstance(parsed, dict) else {"messages": parsed}
