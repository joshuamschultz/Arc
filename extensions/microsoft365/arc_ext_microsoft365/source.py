"""Outlook and OneDrive knowledge sources over the connection's Microsoft Graph client.

Both read through :class:`~arc_ext_microsoft365.native.graph.GraphClient` with the
connection's own credential handle, so knowledge sync and the interactive tools
share one wire client, one pinned host and one sign-in. Each is a separate stream
(``<instance>:outlook`` and ``<instance>:onedrive``) with its own cursor.

Outlook syncs one folder with Graph's message delta query: the first pass pages
through the folder, later passes return only what changed, including deletions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from arcagent.extension.credentials import CredentialRenewalError
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
    source_error_from_renewal,
)

from .native.graph import EgressRefusedError, GraphClient, GraphError, ToolError, segment
from .native.tools import TEXT_BODY

_CURSOR_VERSION = 2
_DELTA_SELECT = "id,subject,from,receivedDateTime,lastModifiedDateTime,changeKey,conversationId"
_MESSAGE_SELECT = (
    "id,subject,from,toRecipients,receivedDateTime,body,changeKey,lastModifiedDateTime"
)


class OutlookSourceAdapter:
    """One Outlook folder (Inbox by default) as an incremental mail source."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self._folder = "inbox"

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="outlook",
            account_id="microsoft365-account",
            data_shape=SourceDataShape.MAIL,
            display_name="Microsoft 365 Mail",
            supports_incremental=True,
            supports_deletes=True,
            root_locator=self._folder,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        payload = await _json_call(self._graph, "/me/mailFolders?$top=100&$select=id,displayName")
        out = [
            SourceResource(
                resource_id="inbox",
                label="Inbox",
                resource_kind="folder",
                selected=self._folder == "inbox",
            )
        ]
        for folder in payload.get("value", []):
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
        folder = request.root_locator or self._folder
        link = _outlook_link(request.checkpoint, folder)
        resumed = link is not None
        if link is None:
            link = f"/me/mailFolders/{segment(folder)}/messages/delta?$select={_DELTA_SELECT}"
        page_size = max(1, min(request.page_size, 200))
        payload = await _json_call(
            self._graph,
            link,
            headers={"Prefer": f"odata.maxpagesize={page_size}"},
            checkpoint=resumed,
        )
        # Graph returns mail newest-first, so mapping each message's timestamp
        # straight to a revision yields a DECREASING revision across the page and
        # ArcMemory silently stops re-indexing. Emit live messages in
        # ascending-revision order; deletions sort after the live run.
        messages = sorted(
            (value for value in payload.get("value", []) if isinstance(value, dict)),
            key=lambda value: (_is_removed(value), _outlook_revision(value)),
        )
        objects = tuple(_object(value) for value in messages)
        next_link = payload.get("@odata.nextLink")
        delta_link = payload.get("@odata.deltaLink")
        follow = next_link if isinstance(next_link, str) and next_link else delta_link
        if not isinstance(follow, str) or not follow:
            raise SourceError(SourceFailureCode.TRANSIENT, "Outlook returned no next page")
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=_outlook_checkpoint(folder, follow),
            has_more=bool(next_link),
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        item = await _json_call(
            self._graph,
            f"/me/messages/{segment(request.object_id)}?$select={_MESSAGE_SELECT}",
            headers=TEXT_BODY,
        )
        version = _outlook_version(item)
        if version != request.version:
            raise SourceError(
                SourceFailureCode.VERSION_CHANGED, "Outlook message changed during fetch"
            )
        body = _message_text(item).encode()
        if len(body) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "Outlook message exceeds byte limit")
        return SourceContent(
            object_id=request.object_id, version=version, media_type="text/plain", content=body
        )

    async def close_source(self) -> None:
        await self._graph.aclose()


class OneDriveSourceAdapter:
    """Graph delta source for OneDrive, isolated from the Outlook source stream."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self._folder = "root"
        self._drive = ""
        self._account = ""

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        user = await _json_call(self._graph, "/me")
        self._account = str(user.get("id") or "")
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="onedrive",
            account_id=self._account or "microsoft365-onedrive-account",
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Microsoft OneDrive",
            supports_incremental=True,
            supports_deletes=True,
            root_locator=self._folder,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        drives = await _json_call(self._graph, "/me/drives")
        resources: list[SourceResource] = []
        for drive in drives.get("value", []):
            if not isinstance(drive, dict) or not (drive_id := str(drive.get("id") or "")):
                continue
            self._drive = self._drive or drive_id
            resources.append(
                SourceResource(
                    resource_id=f"{drive_id}:root",
                    label=str(drive.get("name") or "OneDrive"),
                    resource_kind="folder",
                )
            )
            children = await _json_call(self._graph, f"/drives/{segment(drive_id)}/root/children")
            for value in children.get("value", []):
                if not isinstance(value, dict) or "folder" not in value:
                    continue
                if identifier := str(value.get("id") or ""):
                    resources.append(
                        SourceResource(
                            resource_id=f"{drive_id}:{identifier}",
                            label=str(value.get("name") or identifier),
                            resource_kind="folder",
                        )
                    )
        return tuple(resources)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        if len(request.resource_ids) != 1:
            raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "select one OneDrive folder")
        self._folder = request.resource_ids[0]
        if ":" in self._folder:
            self._drive, self._folder = self._folder.split(":", 1)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        drive, folder = self._drive_folder()
        path = request.checkpoint or f"/drives/{segment(drive)}/items/{segment(folder)}/delta"
        response = await _json_call(self._graph, path, checkpoint=request.checkpoint is not None)
        values = response.get("value", [])
        objects = tuple(
            _onedrive_object(value, drive) for value in values if isinstance(value, dict)
        )
        next_checkpoint = str(
            response.get("@odata.nextLink") or response.get("@odata.deltaLink") or path
        )
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=next_checkpoint,
            has_more="@odata.nextLink" in response,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        drive, item_id = _split_drive_object(request.object_id)
        item = await _json_call(self._graph, f"/drives/{segment(drive)}/items/{segment(item_id)}")
        version = str(item.get("eTag") or "").strip('"')
        if version != request.version:
            raise SourceError(
                SourceFailureCode.VERSION_CHANGED, "OneDrive file changed during fetch"
            )
        response = await _call(
            self._graph, "GET", f"/drives/{segment(drive)}/items/{segment(item_id)}/content"
        )
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > request.max_bytes:
                    raise SourceError(
                        SourceFailureCode.TOO_LARGE, "OneDrive file exceeds byte limit"
                    )
                chunks.append(chunk)
        finally:
            await response.aclose()
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type=str(
                response.headers.get("Content-Type")
                or item.get("file", {}).get("mimeType")
                or "application/octet-stream"
            ),
            content=b"".join(chunks),
            metadata={
                "classification": str(
                    response.headers.get("x-ms-classification") or "unclassified"
                )
            },
        )

    async def close_source(self) -> None:
        await self._graph.aclose()

    def _drive_folder(self) -> tuple[str, str]:
        if not self._drive:
            if ":" not in self._folder:
                raise SourceError(SourceFailureCode.NOT_FOUND, "select a OneDrive folder")
            self._drive, self._folder = self._folder.split(":", 1)
        return self._drive, self._folder


def build_source_adapters(context: dict[str, Any]) -> dict[str, Any]:
    """Isolated mail and file streams for one Microsoft 365 connection."""
    credential = context.get("credential")
    cloud = str(context.get("cloud") or "")
    transport = context.get("transport")
    mail = GraphClient(credential, cloud=cloud, transport=transport)
    files = GraphClient(credential, cloud=cloud, transport=transport)
    return {"outlook": OutlookSourceAdapter(mail), "onedrive": OneDriveSourceAdapter(files)}


# --- the wire ------------------------------------------------------------------


async def _call(
    graph: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    checkpoint: bool = False,
) -> Any:
    """One Graph request, every failure mapped to a typed, redacted :class:`SourceError`."""
    try:
        if headers:
            response = await graph.request(method, path, headers=headers)
        else:
            response = await graph.request(method, path)
    except EgressRefusedError:
        code = SourceFailureCode.CHECKPOINT_INVALID if checkpoint else SourceFailureCode.TRANSIENT
        raise SourceError(code, "Microsoft Graph link is not on this connection's host") from None
    except GraphError as exc:
        raise _source_error(exc.status_code, exc.headers) from None
    except ToolError as exc:
        raise SourceError(SourceFailureCode.TRANSIENT, str(exc)) from None
    except CredentialRenewalError as exc:
        # Typed all the way: a credential only a person can fix is auth_required, not
        # a transient outage to retry every cycle.
        raise source_error_from_renewal(exc) from None
    except Exception as exc:
        raise _source_error(
            int(getattr(exc, "status_code", 0) or 0), getattr(exc, "headers", {})
        ) from None
    if response.status_code >= 400:
        await response.aclose()
        raise _source_error(response.status_code, response.headers)
    return response


async def _json_call(
    graph: Any, path: str, *, headers: dict[str, str] | None = None, checkpoint: bool = False
) -> dict[str, Any]:
    response = await _call(graph, "GET", path, headers=headers, checkpoint=checkpoint)
    try:
        if hasattr(response, "aread"):
            await response.aread()
        payload = response.json()
    except ValueError:
        payload = None
    finally:
        await response.aclose()
    if not isinstance(payload, dict):
        raise SourceError(SourceFailureCode.TRANSIENT, "Microsoft Graph returned invalid JSON")
    return payload


def _source_error(status: int, headers: Any) -> SourceError:
    if status == 401:
        return SourceError(SourceFailureCode.AUTH_REQUIRED, "Microsoft Graph request failed")
    if status == 410:
        # A delta token Graph no longer honours: start the folder over.
        return SourceError(SourceFailureCode.CHECKPOINT_INVALID, "Microsoft Graph delta expired")
    if status == 429:
        return SourceError(
            SourceFailureCode.RATE_LIMITED,
            "Microsoft Graph request failed",
            retry_after=_retry_after(headers),
        )
    if status == 404:
        return SourceError(SourceFailureCode.NOT_FOUND, "Microsoft Graph item not found")
    return SourceError(SourceFailureCode.TRANSIENT, "Microsoft Graph request failed")


# --- Outlook cursor and objects -------------------------------------------------


def _outlook_link(value: str | None, folder: str) -> str | None:
    """The link to follow from a checkpoint, or ``None`` to start the folder over."""
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = None
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or not isinstance(payload.get("folder"), str)
        or not isinstance(payload.get("link"), str)
        or not payload["link"]
    ):
        raise SourceError(SourceFailureCode.CHECKPOINT_INVALID, "invalid Outlook checkpoint")
    if payload["folder"] != folder:
        return None  # a different folder was selected: start it from the beginning
    return str(payload["link"])


def _outlook_checkpoint(folder: str, link: str) -> str:
    return json.dumps(
        {"v": _CURSOR_VERSION, "folder": folder, "link": link}, separators=(",", ":")
    )


def _is_removed(value: dict[str, Any]) -> bool:
    """Whether a Graph message payload is a deletion marker rather than a message."""
    return "@removed" in value or "deleted" in value


def _object(value: dict[str, Any]) -> SourceObject:
    identifier = str(value.get("id") or "")
    if not identifier:
        raise SourceError(SourceFailureCode.TRANSIENT, "Outlook returned a message without an id")
    deleted = _is_removed(value)
    return SourceObject(
        object_id=identifier,
        locator=str(value.get("conversationId") or identifier),
        kind=SourceObjectKind.DELETED if deleted else SourceObjectKind.FILE,
        version=_outlook_version(value),
        deleted=deleted,
        media_type=None if deleted else "text/plain",
        metadata={"classification": "unclassified", "revision": _outlook_revision(value)},
    )


def _message_text(item: dict[str, Any]) -> str:
    sender = (
        item.get("from", {}).get("emailAddress", {}) if isinstance(item.get("from"), dict) else {}
    )
    body = item.get("body")
    content = str(body.get("content") or "") if isinstance(body, dict) else ""
    lines = [
        f"Subject: {item.get('subject') or ''}",
        f"From: {sender.get('name') or ''} <{sender.get('address') or ''}>",
        f"Date: {item.get('receivedDateTime') or ''}",
        "",
        content,
    ]
    return "\n".join(lines)


def _outlook_version(value: dict[str, Any]) -> str:
    change_key = str(value.get("changeKey") or "")
    return change_key or str(_outlook_revision(value))


def _outlook_revision(value: dict[str, Any]) -> int:
    return _timestamp_revision(value, fallback=str(value.get("changeKey") or ""))


def _timestamp_revision(value: dict[str, Any], *, fallback: str) -> int:
    timestamp = str(value.get("lastModifiedDateTime") or "")
    if timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return max(1, int(parsed.astimezone(UTC).timestamp() * 1_000_000))
        except ValueError:
            pass
    digits = "".join(character for character in fallback if character.isdigit())
    return max(1, int(digits)) if digits else 1


# --- OneDrive objects -------------------------------------------------------------


def _onedrive_object(value: dict[str, Any], drive_id: str = "") -> SourceObject:
    identifier = str(value.get("id") or "")
    if not identifier:
        raise SourceError(SourceFailureCode.TRANSIENT, "OneDrive returned a file without an id")
    deleted = "deleted" in value
    name = str(value.get("name") or identifier)
    parent = str(value.get("parentReference", {}).get("path") or "")
    return SourceObject(
        object_id=f"{drive_id}:{identifier}" if drive_id else identifier,
        locator=str(value.get("webUrl") or f"{parent}/{name}"),
        kind=SourceObjectKind.DELETED if deleted else SourceObjectKind.FILE,
        version=str(value.get("eTag") or value.get("lastModifiedDateTime") or "deleted").strip(
            '"'
        ),
        deleted=deleted,
        media_type=str(value.get("file", {}).get("mimeType") or "application/octet-stream"),
        metadata={
            "classification": str(
                value.get("fileSystemInfo", {}).get("classification") or "unclassified"
            ),
            "revision": _timestamp_revision(value, fallback=str(value.get("eTag") or "")),
        },
    )


def _split_drive_object(value: str) -> tuple[str, str]:
    drive, separator, item = value.partition(":")
    if not separator or not drive or not item:
        raise SourceError(SourceFailureCode.NOT_FOUND, "invalid OneDrive object identifier")
    return drive, item


def _retry_after(headers: Any) -> float | None:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter("Retry-After") or getter("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
