"""Google Drive REST over ``httpx.MockTransport``: files, change feed, export, download.

Stateful, so a test edits the "account" between syncs. It enforces what Drive
enforces and the Drive source depends on: a change feed addressed by page token
(a token the feed never issued is a 400), Google documents that only export, regular
files that only download, and exports over the export limit that answer 403.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"
PDF = "application/pdf"
EXPORT_LIMIT = 10 * 1024 * 1024
_EPOCH = datetime(2026, 10, 1, tzinfo=UTC)


class FakeDrive:
    """One account's Drive. ``add`` / ``edit`` / ``trash`` / ``unshare`` log changes."""

    def __init__(
        self,
        *,
        bearer_ok: Callable[[str], bool] | None = None,
        owner: str = "Ann Owner <ann@example.com>",
    ) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.log: list[dict[str, Any]] = []
        self.shared_drives: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.rejected = 0
        self._bearer_ok = bearer_ok
        self._owner = owner
        self._clock = 0

    # --- the account's side ----------------------------------------------------

    def add(
        self,
        file_id: str,
        name: str,
        mime: str,
        content: bytes | str = b"",
        *,
        parents: tuple[str, ...] = ("root",),
        drive_id: str = "",
        declared_size: int | None = None,
    ) -> None:
        body = content.encode() if isinstance(content, str) else content
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": list(parents),
            "content": body,
            "declared_size": declared_size,
            "trashed": False,
            "driveId": drive_id,
            "modifiedTime": self._tick(),
        }
        self._record(file_id)

    def edit(self, file_id: str, content: bytes | str) -> None:
        self.files[file_id]["content"] = content.encode() if isinstance(content, str) else content
        self.files[file_id]["modifiedTime"] = self._tick()
        self._record(file_id)

    def move(self, file_id: str, parents: tuple[str, ...]) -> None:
        self.files[file_id]["parents"] = list(parents)
        self.files[file_id]["modifiedTime"] = self._tick()
        self._record(file_id)

    def trash(self, file_id: str) -> None:
        self.files[file_id]["trashed"] = True
        self._record(file_id)

    def unshare(self, file_id: str) -> None:
        """The account loses access: the feed reports the file as removed."""
        del self.files[file_id]
        self.log.append({"changeType": "file", "fileId": file_id, "removed": True})

    # --- the wire --------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if self._bearer_ok is not None and not self._bearer_ok(bearer):
            self.rejected += 1
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        path = request.url.path.removeprefix("/drive/v3")
        params = request.url.params
        if path == "/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.log))})
        if path == "/changes":
            return self._changes(params.get("pageToken", ""))
        if path == "/drives":
            drives = [{"id": key, "name": name} for key, name in self.shared_drives.items()]
            return httpx.Response(200, json={"drives": drives})
        if path == "/files":
            return self._list(params)
        match = re.fullmatch(r"/files/([^/]+)(/export)?", path)
        if match is None:
            return _error(404, "notFound")
        return self._file(unquote(match.group(1)), bool(match.group(2)), params)

    def _tick(self) -> str:
        self._clock += 1
        return (_EPOCH + timedelta(minutes=self._clock)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _record(self, file_id: str) -> None:
        self.log.append({"changeType": "file", "fileId": file_id, "removed": False})

    def _meta(self, file: dict[str, Any]) -> dict[str, Any]:
        meta = {
            key: file[key]
            for key in ("id", "name", "mimeType", "parents", "trashed", "modifiedTime")
        }
        meta["webViewLink"] = f"https://drive.google.com/file/d/{file['id']}/view"
        meta["owners"] = [
            {
                "displayName": self._owner.split(" <")[0],
                "emailAddress": self._owner[:-1].split("<")[1],
            }
        ]
        meta["version"] = str(len(self.log))
        if file["driveId"]:
            meta["driveId"] = file["driveId"]
        if not file["mimeType"].startswith("application/vnd.google-apps."):
            size = file["declared_size"]
            meta["size"] = str(len(file["content"]) if size is None else size)
        return meta

    def _changes(self, token: str) -> httpx.Response:
        if not token.isdigit() or int(token) > len(self.log):
            return _error(400, "invalid", "Invalid Value")
        changes = []
        for entry in self.log[int(token) :]:
            change = dict(entry)
            file = self.files.get(entry["fileId"])
            if file is not None and not entry["removed"]:
                change["file"] = self._meta(file)
            changes.append(change)
        return httpx.Response(
            200, json={"changes": changes, "newStartPageToken": str(len(self.log))}
        )

    def _list(self, params: httpx.QueryParams) -> httpx.Response:
        query = params.get("q", "")
        parent = re.search(r"'([^']+)' in parents", query)
        mime = re.search(r"mimeType = '([^']+)'", query)
        drive_id = params.get("driveId", "")
        rows = [
            file
            for file in self.files.values()
            if not file["trashed"]
            and (parent is None or parent.group(1) in file["parents"])
            and (not drive_id or file["driveId"] == drive_id)
            and (mime is None or file["mimeType"] == mime.group(1))
        ]
        size = int(params.get("pageSize", "100"))
        start = int(params.get("pageToken") or 0)
        page = rows[start : start + size]
        body: dict[str, Any] = {"files": [self._meta(file) for file in page]}
        if start + size < len(rows):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    def _file(self, file_id: str, export: bool, params: httpx.QueryParams) -> httpx.Response:
        file = self.files.get(file_id)
        if file is None:
            return _error(404, "notFound", "File not found")
        if export:
            if not file["mimeType"].startswith("application/vnd.google-apps."):
                return _error(403, "fileNotExportable")
            if len(file["content"]) > EXPORT_LIMIT:
                return _error(403, "exportSizeLimitExceeded")
            return httpx.Response(200, content=file["content"])
        if params.get("alt") == "media":
            return httpx.Response(200, content=file["content"])
        return httpx.Response(200, json=self._meta(file))


def _error(status: int, reason: str, message: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": message, "errors": [{"reason": reason}]}},
    )
