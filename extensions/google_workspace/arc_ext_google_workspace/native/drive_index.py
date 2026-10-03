"""Drive reads for the connected-data source: files, changes, content (read only).

The listing tools in ``drive`` answer an agent's question. These answer an
indexer's: page every file in a scope, page the account's change feed from a
token, and read one file's text. They are separate tools so the agent-facing
listing keeps its small shape.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, Final

from .drive import BASE, quoted
from .http import GoogleApiError, GoogleHttp, ToolError, drop_empty, encode_id, int_arg, text_arg

FOLDER_MIME: Final = "application/vnd.google-apps.folder"
SHORTCUT_MIME: Final = "application/vnd.google-apps.shortcut"

#: Google-native documents have no bytes of their own; ``files.export`` turns them
#: into text. A sheet exports as CSV, which the plain-text extractor reads.
_EXPORT_MIME: Final = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

#: Regular files the document extractors read: pdf, docx, xlsx, md, txt, html.
_DOWNLOAD_MIMES: Final = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/markdown",
        "text/plain",
        "text/html",
    }
)

#: The existing per-object cap of connected-data ingestion.
MAX_CONTENT_BYTES: Final = 10 * 1024 * 1024

_FILE_FIELDS: Final = (
    "id,name,mimeType,modifiedTime,size,version,webViewLink,trashed,driveId,parents,"
    "owners(displayName,emailAddress)"
)
_CHANGE_FIELDS: Final = (
    f"changes(changeType,removed,fileId,file({_FILE_FIELDS})),nextPageToken,newStartPageToken"
)
_LIST_FIELDS: Final = f"files({_FILE_FIELDS}),nextPageToken"
_ALL_DRIVES: Final = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


def indexable_media_type(mime_type: str) -> str | None:
    """The media type this file is indexed as, or None when nothing can read it."""
    if mime_type in _EXPORT_MIME:
        # An exported sheet is CSV, which is plain text to the extractors.
        return "text/plain"
    return mime_type if mime_type in _DOWNLOAD_MIMES else None


def _scope_query(scope: str, scope_id: str) -> tuple[str, dict[str, str]]:
    if scope == "drive":
        return "trashed = false", {"corpora": "drive", "driveId": scope_id, **_ALL_DRIVES}
    if scope == "folder":
        query = f"{quoted(scope_id)} in parents and trashed = false"
        return query, {"corpora": "allDrives", **_ALL_DRIVES}
    return "trashed = false", {"corpora": "allDrives", **_ALL_DRIVES}


async def drive_files(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """One page of files for indexing: everything, one shared drive, or one folder."""
    scope = text_arg(args, "scope") or "all"
    if scope not in {"all", "drive", "folder"}:
        raise ToolError("scope must be all, drive or folder")
    scope_id = text_arg(args, "id", required=scope != "all")
    query, scoping = _scope_query(scope, scope_id)
    listing = await http.request(
        "GET",
        f"{BASE}/files",
        params=drop_empty(
            {
                "q": query,
                "pageSize": int_arg(args, "limit", default=200, ceiling=1000),
                "pageToken": text_arg(args, "page_token"),
                "fields": _LIST_FIELDS,
                **scoping,
            }
        ),
    )
    listing.setdefault("files", [])
    return listing


async def drive_changes(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """Changes since a page token; with no token, the token to start from."""
    token = text_arg(args, "page_token")
    if not token:
        return await http.request(
            "GET", f"{BASE}/changes/startPageToken", params={"supportsAllDrives": "true"}
        )
    page = await http.request(
        "GET",
        f"{BASE}/changes",
        params=drop_empty(
            {
                "pageToken": token,
                "pageSize": int_arg(args, "limit", default=200, ceiling=1000),
                "includeRemoved": "true",
                "restrictToMyDrive": "false",
                "fields": _CHANGE_FIELDS,
                **_ALL_DRIVES,
            }
        ),
    )
    page.setdefault("changes", [])
    return page


async def drive_file(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """One file's metadata (no content)."""
    return await _metadata(http, text_arg(args, "id", required=True))


async def drive_drives(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """The shared drives this account can see."""
    listing = await http.request(
        "GET",
        f"{BASE}/drives",
        params={
            "pageSize": int_arg(args, "limit", default=100, ceiling=100),
            "fields": "drives(id,name)",
        },
    )
    listing.setdefault("drives", [])
    return listing


async def drive_read(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """One file's metadata and indexable content, or why it is not indexable.

    Google documents are exported to text; regular files of a type the extractors
    read are downloaded. ``skipped`` names a per-file refusal (``unsupported`` or
    ``too_large``) so the caller records it against this file alone. Content is
    base64 so every file type travels the same way.
    """
    file_id = text_arg(args, "id", required=True)
    cap = int_arg(args, "max_bytes", default=MAX_CONTENT_BYTES, ceiling=MAX_CONTENT_BYTES)
    meta = await _metadata(http, file_id)
    mime_type = str(meta.get("mimeType") or "")
    media_type = indexable_media_type(mime_type)
    if media_type is None:
        return {"file": meta, "skipped": "unsupported"}
    if int(meta.get("size") or 0) > cap:
        return {"file": meta, "skipped": "too_large"}
    try:
        data = await _content(http, file_id, mime_type)
    except GoogleApiError as exc:
        if "exportSizeLimitExceeded" in str(exc):
            return {"file": meta, "skipped": "too_large"}
        raise
    if len(data) > cap:
        return {"file": meta, "skipped": "too_large"}
    return {
        "file": meta,
        "media_type": media_type,
        "content": base64.b64encode(data).decode("ascii"),
    }


async def _metadata(http: GoogleHttp, file_id: str) -> Any:
    return await http.request(
        "GET",
        f"{BASE}/files/{encode_id(file_id)}",
        params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
    )


async def _content(http: GoogleHttp, file_id: str, mime_type: str) -> bytes:
    exported = _EXPORT_MIME.get(mime_type)
    if exported is not None:
        return await http.request_bytes(
            "GET", f"{BASE}/files/{encode_id(file_id)}/export", params={"mimeType": exported}
        )
    return await http.request_bytes(
        "GET",
        f"{BASE}/files/{encode_id(file_id)}",
        params={"alt": "media", "supportsAllDrives": "true"},
    )
