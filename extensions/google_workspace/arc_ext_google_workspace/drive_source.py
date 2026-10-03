"""Google Drive source adapter over the native REST attachment (Arc holds the credential).

A first sync pages the selected scope into a snapshot. While it runs, the account's
change-feed start token is held in the cursor, so nothing edited mid-snapshot is
missed. Every later sync reads the change feed from that token. A token Google no
longer accepts is a dead checkpoint (``CHECKPOINT_INVALID``): the coordinator starts
over from a snapshot.

Scope is what the operator chose: all of Drive, one shared drive, or folders. The
change feed is account-wide, so a changed file is kept only when it falls inside
the scope, and a file that left the scope is a tombstone.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final

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

from ._tool_json import tool_payload
from .native.drive_index import FOLDER_MIME, MAX_CONTENT_BYTES, SHORTCUT_MIME, indexable_media_type

_CURSOR_VERSION: Final = 1
#: Ingest refuses a change whose revision does not exceed the stored one, and a
#: removal carries no file to take a revision from, so it outranks every real one.
_DELETED_REVISION: Final = 2**62
_ALL: Final = "all"
_PAGE_CEILING: Final = 1000
#: Folders listed per call when picking what to sync. Deeper folders are reached by
#: choosing their parent: a folder's whole subtree is in scope.
_FOLDER_CHOICES: Final = 100
_SCOPE: Final = re.compile(r"^(?:all|drive:[^,\s]+|folder:[^,\s]+)$")
_BAD_TOKEN: Final = re.compile(r"(?<![\w-])(?:400|404|410)(?![\w-])|invalid.{0,12}token", re.I)
_NOT_FOUND: Final = re.compile(r"(?<![\w-])404(?![\w-])|not\s?found", re.IGNORECASE)


class DriveSourceAdapter:
    """Synchronize Drive through snapshot pages and the Changes API."""

    def __init__(self, attachment: Any, *, account: str = "") -> None:
        self._attachment = attachment
        self._account = account
        # Everything the account can see until an operator narrows it.
        self._selected: tuple[str, ...] = (_ALL,)

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="google_drive",
            account_id=self._account or "google-drive-account",
            data_shape=SourceDataShape.DOCUMENT,
            display_name="Google Drive",
            supports_incremental=True,
            supports_deletes=True,
            root_locator=",".join(self._selected),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        resources = [SourceResource(resource_id=_ALL, label="All of Drive", resource_kind="drive")]
        drives = await self._call("google_drive_drives", {})
        for drive in _entries(drives.get("drives")):
            if drive_id := str(drive.get("id") or ""):
                resources.append(
                    SourceResource(
                        resource_id=f"drive:{drive_id}",
                        label=str(drive.get("name") or drive_id),
                        resource_kind="shared_drive",
                    )
                )
        folders = await self._call(
            "google_drive_list",
            {
                "parent": "root",
                "query": f"mimeType = '{FOLDER_MIME}'",
                "max": str(_FOLDER_CHOICES),
            },
        )
        for folder in _entries(folders.get("files")):
            if folder_id := str(folder.get("id") or ""):
                resources.append(
                    SourceResource(
                        resource_id=f"folder:{folder_id}",
                        label=str(folder.get("name") or folder_id),
                        resource_kind="folder",
                    )
                )
        return tuple(
            resource.model_copy(update={"selected": resource.resource_id in self._selected})
            for resource in resources
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        offered = {
            item.resource_id
            for item in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }
        chosen = tuple(dict.fromkeys(request.resource_ids))
        if any(item not in offered for item in chosen):
            raise SourceError(
                SourceFailureCode.NOT_FOUND, "selected Drive folder or drive is unavailable"
            )
        self._selected = chosen

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        scopes = _parse_scopes(request.root_locator or ",".join(self._selected))
        label = ",".join(scopes)
        cursor = _cursor(request.checkpoint, label)
        size = min(request.page_size, _PAGE_CEILING)
        if cursor is None:
            return await self._start_snapshot(scopes, label, size)
        if cursor["mode"] == "snapshot":
            return await self._snapshot_page(cursor, label, size)
        return await self._changes_page(cursor, scopes, label, size)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        payload = await self._call(
            "google_drive_read",
            {
                "id": request.object_id,
                "max_bytes": str(min(request.max_bytes, MAX_CONTENT_BYTES)),
            },
        )
        file = payload.get("file")
        if not isinstance(file, dict) or file.get("trashed"):
            raise SourceError(SourceFailureCode.NOT_FOUND, "Drive file is unavailable")
        version = _version(file)
        if version != request.version:
            raise SourceError(SourceFailureCode.VERSION_CHANGED, "Drive file changed during fetch")
        skipped = payload.get("skipped")
        if skipped == "unsupported":
            raise SourceError(
                SourceFailureCode.UNSUPPORTED_CONTENT,
                f"Drive file type {file.get('mimeType')} is not indexable",
            )
        if skipped == "too_large":
            raise SourceError(SourceFailureCode.TOO_LARGE, "Drive file exceeds byte limit")
        content = _decode(payload.get("content"))
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "Drive file exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type=str(payload.get("media_type") or "text/plain"),
            content=content,
        )

    async def close_source(self) -> None:
        return None

    # --- snapshot --------------------------------------------------------------

    async def _start_snapshot(
        self, scopes: tuple[str, ...], label: str, size: int
    ) -> SyncSourcePage:
        # The feed's start token is taken BEFORE the first listing, so an edit made
        # while the snapshot runs is read by the changes that follow it.
        started = await self._call("google_drive_changes", {})
        token = str(started.get("startPageToken") or "")
        if not token:
            raise SourceError(SourceFailureCode.TRANSIENT, "Drive returned no change token")
        cursor = {"mode": "snapshot", "scope": label, "start": token, "queue": list(scopes)}
        return await self._snapshot_page(cursor, label, size)

    async def _snapshot_page(
        self, cursor: dict[str, Any], label: str, size: int
    ) -> SyncSourcePage:
        queue: list[str] = list(cursor["queue"])
        page_token = str(cursor.get("page_token") or "")
        objects: list[SourceObject] = []
        while queue and not objects:
            scope, _, scope_id = queue[0].partition(":")
            listing = await self._call(
                "google_drive_files",
                {"scope": scope, "id": scope_id, "limit": str(size), "page_token": page_token},
            )
            files = _entries(listing.get("files"))
            queue.extend(_subfolders(files) if scope == "folder" else ())
            objects.extend(_file_object(file) for file in files if _is_document(file))
            page_token = str(listing.get("nextPageToken") or "")
            if not page_token:
                queue.pop(0)
        if queue:
            return SyncSourcePage(
                objects=tuple(objects),
                next_checkpoint=_encode(
                    {**cursor, "queue": queue, "page_token": page_token}, drop=not page_token
                ),
                has_more=True,
            )
        return SyncSourcePage(
            objects=tuple(objects),
            next_checkpoint=_encode(
                {"mode": "changes", "scope": label, "page_token": cursor["start"]}
            ),
        )

    # --- changes ---------------------------------------------------------------

    async def _changes_page(
        self, cursor: dict[str, Any], scopes: tuple[str, ...], label: str, size: int
    ) -> SyncSourcePage:
        try:
            page = await self._call(
                "google_drive_changes", {"page_token": cursor["page_token"], "limit": str(size)}
            )
        except SourceError as error:
            raise _token_failure(error) from error
        placement = _Placement(self, scopes)
        objects: dict[str, SourceObject] = {}
        for change in _entries(page.get("changes")):
            if (item := await self._change_object(change, placement)) is not None:
                objects[item.object_id] = item
        following = str(page.get("nextPageToken") or "")
        token = following or str(page.get("newStartPageToken") or "")
        if not token:
            raise SourceError(SourceFailureCode.TRANSIENT, "Drive returned no change token")
        return SyncSourcePage(
            objects=tuple(objects.values()),
            next_checkpoint=_encode({"mode": "changes", "scope": label, "page_token": token}),
            has_more=bool(following),
        )

    async def _change_object(
        self, change: dict[str, Any], placement: _Placement
    ) -> SourceObject | None:
        if str(change.get("changeType") or "file") != "file":
            return None
        file_id = str(change.get("fileId") or "")
        file = change.get("file")
        if not file_id:
            return None
        if change.get("removed") or not isinstance(file, dict) or file.get("trashed"):
            # Removed also means "no longer visible to this account": a revoked share.
            return _tombstone(file_id, _version(file) if isinstance(file, dict) else "0")
        if not _is_document(file):
            return None
        if not await placement.contains(file):
            return _tombstone(file_id, _version(file))
        return _file_object(file)

    async def _call(self, tool: str, arguments: dict[str, str]) -> dict[str, Any]:
        result = await self._attachment.invoke(tool, arguments)
        return tool_payload(result, "Drive", list_key="files")


class _Placement:
    """Whether a changed file lies inside the selected scopes (parents are memoised)."""

    def __init__(self, adapter: DriveSourceAdapter, scopes: tuple[str, ...]) -> None:
        self._adapter = adapter
        self._everything = _ALL in scopes
        self._drives = {item.partition(":")[2] for item in scopes if item.startswith("drive:")}
        self._folders = {item.partition(":")[2] for item in scopes if item.startswith("folder:")}
        self._known: dict[str, bool] = {}

    async def contains(self, file: dict[str, Any]) -> bool:
        if self._everything or str(file.get("driveId") or "") in self._drives:
            return True
        return any([await self._under_folder(parent) for parent in _parents(file)])

    async def _under_folder(self, folder_id: str) -> bool:
        if folder_id in self._folders:
            return True
        if folder_id in self._known:
            return self._known[folder_id]
        self._known[folder_id] = False  # a parent chain has no cycles; this only guards one
        try:
            parent = await self._adapter._call("google_drive_file", {"id": folder_id})
        except SourceError as error:
            if error.code is not SourceFailureCode.TRANSIENT or not _NOT_FOUND.search(
                error.detail
            ):
                raise
            return False
        found = any([await self._under_folder(item) for item in _parents(parent)])
        self._known[folder_id] = found
        return found


def build_drive_adapter(context: dict[str, Any]) -> DriveSourceAdapter:
    return DriveSourceAdapter(context["attachment"], account=str(context.get("account") or ""))


def _entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parents(file: dict[str, Any]) -> list[str]:
    parents = file.get("parents")
    return [str(item) for item in parents] if isinstance(parents, list) else []


def _subfolders(files: Iterable[dict[str, Any]]) -> list[str]:
    return [
        f"folder:{file['id']}"
        for file in files
        if file.get("mimeType") == FOLDER_MIME and file.get("id")
    ]


def _is_document(file: dict[str, Any]) -> bool:
    """A file with content to index: not a folder, not a shortcut."""
    return bool(file.get("id")) and file.get("mimeType") not in {FOLDER_MIME, SHORTCUT_MIME}


def _version(file: dict[str, Any]) -> str:
    """The file's content version: its modified time (a share or a star leaves it alone)."""
    raw = str(file.get("modifiedTime") or file.get("version") or "")
    if not raw:
        raise SourceError(SourceFailureCode.TRANSIENT, "Drive returned a file without a version")
    return raw


def _revision(file: dict[str, Any]) -> int:
    """A number that only grows as the file changes: its modified time in milliseconds."""
    try:
        stamp = datetime.fromisoformat(str(file.get("modifiedTime") or ""))
    except ValueError:
        digits = "".join(re.findall(r"\d+", str(file.get("version") or "")))
        return int(digits) if digits else 0
    return int(stamp.timestamp() * 1000)


def _owner(file: dict[str, Any]) -> str:
    owners = _entries(file.get("owners"))
    if not owners:
        return ""
    name, email = str(owners[0].get("displayName") or ""), str(owners[0].get("emailAddress") or "")
    return f"{name} <{email}>" if name and email else name or email


def _file_object(file: dict[str, Any]) -> SourceObject:
    mime_type = str(file.get("mimeType") or "")
    size = file.get("size")
    metadata: dict[str, Any] = {
        "classification": "unclassified",
        "revision": _revision(file),
        "title": str(file.get("name") or file["id"]),
        "url": str(file.get("webViewLink") or ""),
        "owner": _owner(file),
    }
    return SourceObject(
        object_id=str(file["id"]),
        locator=str(file.get("name") or file["id"]),
        kind=SourceObjectKind.FILE,
        version=_version(file),
        size=int(size) if size is not None else None,
        modified_at=str(file.get("modifiedTime") or "") or None,
        # An unreadable type keeps its own, so the skip names what it was.
        media_type=indexable_media_type(mime_type) or mime_type or None,
        metadata={key: value for key, value in metadata.items() if value != ""},
    )


def _tombstone(object_id: str, version: str) -> SourceObject:
    return SourceObject(
        object_id=object_id,
        locator=object_id,
        kind=SourceObjectKind.DELETED,
        version=version,
        deleted=True,
        metadata={"classification": "unclassified", "revision": _DELETED_REVISION},
    )


def _decode(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SourceError(
            SourceFailureCode.TRANSIENT, "Drive returned unreadable content"
        ) from exc


def _parse_scopes(locator: str) -> tuple[str, ...]:
    scopes = tuple(item for item in locator.split(",") if item)
    if not scopes or not all(_SCOPE.match(item) for item in scopes):
        raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "invalid Drive selection")
    return scopes


def _cursor(value: str | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _invalid_checkpoint() from exc
    if not isinstance(parsed, dict) or parsed.get("v") != _CURSOR_VERSION:
        raise _invalid_checkpoint()
    # A different selection is a different corpus: start again from a snapshot.
    if parsed.get("scope") != label:
        raise _invalid_checkpoint()
    if parsed.get("mode") == "changes" and parsed.get("page_token"):
        return parsed
    if parsed.get("mode") == "snapshot" and parsed.get("start") and parsed.get("queue"):
        return parsed
    raise _invalid_checkpoint()


def _encode(values: dict[str, Any], *, drop: bool = False) -> str:
    payload = {"v": _CURSOR_VERSION, **values}
    if drop:
        payload.pop("page_token", None)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _invalid_checkpoint() -> SourceError:
    return SourceError(SourceFailureCode.CHECKPOINT_INVALID, "invalid Drive checkpoint")


def _token_failure(error: SourceError) -> SourceError:
    """A page token Google rejects is a dead checkpoint, not a passing blip.

    Only the changes call carries a token, so only here does a 400, 404 or 410
    mean "start over from a snapshot". Retried as transient it would be tried
    every cycle forever while the index went stale.
    """
    if error.code is SourceFailureCode.TRANSIENT and _BAD_TOKEN.search(error.detail):
        return SourceError(
            SourceFailureCode.CHECKPOINT_INVALID, f"Drive page token rejected: {error.detail}"
        )
    return error
