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


class OneDriveSourceAdapter:
    """Graph delta source for OneDrive, isolated from the Outlook source stream."""

    def __init__(self, attachment: Any | None = None, *, graph: Any | None = None) -> None:
        self._attachment = attachment
        self._graph = graph
        self._folder = "root"
        self._drive = ""
        self._account = ""

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        if self._graph is not None:
            user = await self._graph_request("GET", "/me")
            self._account = str(user.get("id") or "")
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="onedrive",
            account_id=self._account or "microsoft365-onedrive-account",
            display_name="Microsoft OneDrive",
            supports_incremental=self._graph is not None,
            supports_deletes=self._graph is not None,
            root_locator=self._folder,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        if self._graph is not None:
            drives = await self._graph_request("GET", "/me/drives")
            values: list[Any] = []
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
                children = await self._graph_request("GET", f"/drives/{drive_id}/root/children")
                values.extend(
                    [dict(item, _drive_id=drive_id) for item in children.get("value", [])]
                )
        else:
            result = await self._attachment.invoke("list-folder-files", {"folder": "root"})
            values = _json(result.content).get("value", _json(result.content).get("files", []))
            resources = [
                SourceResource(resource_id="root", label="OneDrive root", resource_kind="folder")
            ]
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict) or "folder" not in value:
                continue
            identifier = str(value.get("id") or "")
            drive_id = str(value.get("_drive_id") or self._drive)
            if identifier:
                resources.append(
                    SourceResource(
                        resource_id=f"{drive_id}:{identifier}" if drive_id else identifier,
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
        if self._graph is not None:
            drive, folder = self._drive_folder()
            path = request.checkpoint or f"/drives/{drive}/items/{folder}/delta"
            response = await self._graph_request("GET", path)
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
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint, has_more=False)
        result = await self._attachment.invoke(
            "list-folder-files", {"folder": request.root_locator or self._folder}
        )
        values = _json(result.content).get("value", _json(result.content).get("files", []))
        objects = tuple(
            _onedrive_object(value)
            for value in values
            if isinstance(value, dict) and value.get("file")
        )
        return SyncSourcePage(objects=objects, next_checkpoint="snapshot-1", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        if self._graph is not None:
            drive, item_id = _split_drive_object(request.object_id)
            item = await self._graph_request("GET", f"/drives/{drive}/items/{item_id}")
            version = str(item.get("eTag") or "").strip('"')
            if version != request.version:
                raise SourceError(
                    SourceFailureCode.VERSION_CHANGED, "OneDrive file changed during fetch"
                )
            response = await self._graph_response(
                "GET", f"/drives/{drive}/items/{item_id}/content"
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
        result = await self._attachment.invoke("get-onedrive-file", {"file_id": request.object_id})
        item = _json(result.content)
        version = str(item.get("eTag") or item.get("lastModifiedDateTime") or "1")
        if version != request.version:
            raise SourceError(
                SourceFailureCode.VERSION_CHANGED, "OneDrive file changed during fetch"
            )
        content = str(item.get("content") or item.get("text") or "").encode()
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "OneDrive file exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type=str(item.get("mimeType") or "text/plain"),
            content=content,
            metadata={"classification": "unclassified"},
        )

    async def close_source(self) -> None:
        if self._graph is not None:
            await self._graph.aclose()

    def _drive_folder(self) -> tuple[str, str]:
        if not self._drive:
            if ":" not in self._folder:
                raise SourceError(SourceFailureCode.NOT_FOUND, "select a OneDrive folder")
            self._drive, self._folder = self._folder.split(":", 1)
        return self._drive, self._folder

    async def _graph_request(self, method: str, path: str) -> dict[str, Any]:
        response = await self._graph_response(method, path)
        try:
            return response.json()
        finally:
            await response.aclose()

    async def _graph_response(self, method: str, path: str) -> Any:
        try:
            response = await self._graph.request(method, path)
        except Exception as exc:
            status = int(getattr(exc, "status_code", 0))
            code = (
                SourceFailureCode.AUTH_REQUIRED
                if status == 401
                else SourceFailureCode.RATE_LIMITED
                if status == 429
                else SourceFailureCode.TRANSIENT
            )
            retry = _retry_after(getattr(exc, "headers", {})) if status == 429 else None
            raise SourceError(code, "Microsoft Graph request failed", retry_after=retry) from exc
        if response.status_code >= 400:
            code = (
                SourceFailureCode.AUTH_REQUIRED
                if response.status_code == 401
                else SourceFailureCode.RATE_LIMITED
                if response.status_code == 429
                else SourceFailureCode.TRANSIENT
            )
            raise SourceError(
                code,
                "Microsoft Graph request failed",
                retry_after=_retry_after(response.headers)
                if response.status_code == 429
                else None,
            )
        return response


def build_source_adapter(context: dict[str, Any]) -> OutlookSourceAdapter:
    return OutlookSourceAdapter(context["attachment"])


def build_source_adapters(context: dict[str, Any]) -> dict[str, Any]:
    """Return isolated mail and file streams for a single Microsoft grant."""
    attachment = context["attachment"]
    return {
        "outlook": OutlookSourceAdapter(attachment),
        "onedrive": OneDriveSourceAdapter(attachment),
    }


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
        metadata={
            "classification": str(
                value.get("fileSystemInfo", {}).get("classification") or "unclassified"
            )
        },
    )


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
            )
        },
    )


def _split_drive_object(value: str) -> tuple[str, str]:
    drive, separator, item = value.partition(":")
    if not separator or not drive or not item:
        raise SourceError(SourceFailureCode.NOT_FOUND, "invalid OneDrive object identifier")
    return drive, item


def _retry_after(headers: Any) -> float | None:
    if not isinstance(headers, dict):
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _json(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SourceError(
            SourceFailureCode.TRANSIENT, "Microsoft 365 returned invalid JSON"
        ) from exc
    return loaded if isinstance(loaded, dict) else {"value": loaded}
