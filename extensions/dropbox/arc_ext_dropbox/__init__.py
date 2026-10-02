"""Dropbox files API adapter — the whole third-party side of the dropbox bundle.

Dropbox speaks OAuth2, and Arc owns that side: it holds the refresh token, renews
the short-lived access token, and commits the renewed one to sealed custody. This
adapter never mints or stores a token. It asks its credential handle for a fresh
bearer at the header site of every request, and on a 401 it tells the handle to
invalidate so the next ask forces one renewal.

The transport rules mirror the confluence bundle's: every request carries an
explicit timeout, and a path is data carried in the request body or the
`Dropbox-API-Arg` header, never spliced into a URL.

Dropbox splits its API across two hosts: RPC calls (list, search, metadata) go to
api.dropboxapi.com with a JSON body; file bytes (download, upload) go to
content.dropboxapi.com with the arguments in a header and the content as the raw
body. Both are reached with the same bearer token.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
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

if TYPE_CHECKING:
    from arcagent.extension.credential_broker import AccessTokenHandle

#: One flat 30 seconds covered both reaching Dropbox and reading a file from it,
#: so any document that took longer than half a minute to download failed the
#: whole sync with a ReadTimeout. Connecting is still held to a short leash —
#: an unreachable host should be reported at once, not waited on — while a
#: transfer already in progress is given room. The per-sync ``max_seconds``
#: ceiling still bounds the run as a whole.
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=300.0, write=120.0, pool=10.0)

#: A verb that moves file bytes may legitimately run as long as the transport lets
#: it. The tool registry's 30 second default cut ``dropbox_upload`` off ("TOOL_TIMEOUT")
#: long before this transport gave up, so the tool bound is the transport's own,
#: read from it rather than restated.
_TRANSFER_TOOL_TIMEOUT: Final = int(max(_TIMEOUT.read or 0.0, _TIMEOUT.write or 0.0))

#: The RPC host (list, search, metadata, account) and the content host (bytes).
_API: Final = "https://api.dropboxapi.com"
_CONTENT: Final = "https://content.dropboxapi.com"

#: A downloaded file is returned as text; anything past this is truncated so a
#: single large file cannot flood the model's context. The marker names the cut.
_MAX_DOWNLOAD_CHARS: Final = 100_000
_MAX_ATTEMPTS: Final = 3

_STRING: Final[dict[str, str]] = {"type": "string"}

#: Bounds on ``dropbox_archive_copy``: files per call and bytes per file.
_ARCHIVE_MAX_FILES: Final = 500
_ARCHIVE_MAX_FILE_BYTES: Final = 100 * 1024 * 1024
#: The Dropbox folder transcripts are filed under; stripped to find the meeting name.
_ARCHIVE_SOURCE_FOLDER: Final = "Meetings"


class DropboxAttachment:
    """Reaches the Dropbox files API over HTTPS, through the four hook methods."""

    def __init__(self, credential: AccessTokenHandle) -> None:
        self._credential = credential
        self._source_root = ""
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    @property
    def _http(self) -> httpx.AsyncClient:
        """The shared httpx client, recreated if a prior ``close_source`` closed it.

        ONE ``DropboxAttachment`` serves BOTH seams: the connected-source
        lifecycle (``close_source`` releases the client after a sync) AND the
        agent's interactive tools (``invoke``). ``close_source`` closing the
        shared client left every later tool call raising "client has been closed"
        even though the connection card still probed green from a fresh instance.
        Recreating on demand lets the two seams coexist on one long-lived instance
        — a connection stays usable after any source operation.
        """
        if self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """No sensitive field and no host prerequisite: Arc holds the credential."""
        return []

    async def probe(self) -> ProbeResult:
        """Read the current account. The cheapest call that proves auth and reach."""
        try:
            account = await self._rpc("/2/users/get_current_account", None)
        except ExtensionError as exc:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"dropbox has no usable credential ({exc.code}) — "
                    f"run 'arc connector authorize <instance>' to connect it."
                ),
            )
        except httpx.HTTPStatusError as exc:
            return ProbeResult(reachable=False, detail=_refused(exc.response.status_code))
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(reachable=False, detail=f"Dropbox did not answer: {exc}")
        name = _account_name(account)
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"reached Dropbox as {name}",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Nine verbs. Classification and tags match extension.toml exactly."""
        return [
            ToolSpec(
                name="dropbox_list",
                description="List files and folders under a Dropbox path (empty = root).",
                input_schema=_schema({"path": _STRING, "recursive": _STRING, "limit": _STRING}),
                classification="read_only",
            ),
            ToolSpec(
                name="dropbox_search",
                description="Search the account for files and folders matching a query.",
                input_schema=_schema({"query": _STRING, "limit": _STRING}, required=["query"]),
                classification="read_only",
            ),
            ToolSpec(
                name="dropbox_download",
                description="Read a file's text content by path.",
                input_schema=_schema({"path": _STRING}, required=["path"]),
                classification="read_only",
                timeout_seconds=_TRANSFER_TOOL_TIMEOUT,
            ),
            ToolSpec(
                name="dropbox_account",
                description="Read the connected account's identity and storage use.",
                input_schema=_schema({}),
                classification="read_only",
            ),
            ToolSpec(
                name="dropbox_upload",
                description="Write a text file to a Dropbox path. 'add' keeps an "
                "existing file (autorenames); 'overwrite' replaces it.",
                input_schema=_schema(
                    {"path": _STRING, "content": _STRING, "mode": _STRING},
                    required=["path", "content"],
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
                timeout_seconds=_TRANSFER_TOOL_TIMEOUT,
            ),
            ToolSpec(
                name="dropbox_create_folder",
                description="Create a folder at a Dropbox path.",
                input_schema=_schema({"path": _STRING}, required=["path"]),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
            ToolSpec(
                name="dropbox_move",
                description="Move or rename a file or folder.",
                input_schema=_schema(
                    {"from_path": _STRING, "to_path": _STRING},
                    required=["from_path", "to_path"],
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
            ToolSpec(
                name="dropbox_archive_copy",
                description="Copy meeting files server-side into dest_root/<meeting>/<name>. "
                "Skips identical copies, refuses to overwrite different ones.",
                input_schema=_schema(
                    {
                        "files": {"type": "array", "items": {"type": "string"}},
                        "dest_root": _STRING,
                        "max_file_bytes": _STRING,
                    },
                    required=["files", "dest_root"],
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
                timeout_seconds=_TRANSFER_TOOL_TIMEOUT,
            ),
            ToolSpec(
                name="dropbox_delete",
                description="Delete a file or folder.",
                input_schema=_schema({"path": _STRING}, required=["path"]),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one verb. A Dropbox refusal is an answer, not a raise."""
        try:
            return ToolResult(tool=tool, content=await self._dispatch(tool, args))
        except KeyError:
            return _error(tool, f"dropbox has no tool named {tool!r}")
        except ExtensionError as exc:
            return _error(tool, f"dropbox has no usable credential ({exc.code})")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return _error(tool, f"dropbox answered {status}: {exc.response.text}")
        except (httpx.HTTPError, ValueError) as exc:
            return _error(tool, f"dropbox call failed: {exc}")

    # --- connected source contract --------------------------------------------

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        """Identify the account behind one connection without exposing credentials."""
        account = await self._source_rpc("/2/users/get_current_account", None)
        account_id = str(account.get("account_id") or "")
        if not account_id:
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox returned no account ID")
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="dropbox",
            account_id=account_id,
            display_name=_account_name(account),
            root_locator=self._source_root,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        """List the root and immediate folders available for operator selection."""
        del request
        payload = await self._source_rpc(
            "/2/files/list_folder", {"path": "", "recursive": False, "limit": 2_000}
        )
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox returned invalid resources")
        resources = [
            SourceResource(
                resource_id="root", label="Dropbox root", resource_kind="folder", locator=""
            )
        ]
        resources.extend(
            SourceResource(
                resource_id=str(entry.get("id") or entry.get("path_lower") or ""),
                label=str(entry.get("name") or entry.get("path_display") or "folder"),
                resource_kind="folder",
                locator=str(entry.get("path_display") or entry.get("path_lower") or ""),
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get(".tag") == "folder"
        )
        return tuple(resource for resource in resources if resource.resource_id)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        """Pin one selected subtree; one Dropbox cursor cannot safely merge roots."""
        if len(request.resource_ids) != 1:
            raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "select one Dropbox folder")
        selected = request.resource_ids[0]
        if selected == "root":
            self._source_root = ""
            return
        resources = await self.list_source_resources(
            ListSourceResources(connection_id=request.connection_id)
        )
        matching = next(
            (resource for resource in resources if resource.resource_id == selected), None
        )
        if matching is None:
            raise SourceError(
                SourceFailureCode.NOT_FOUND, "selected Dropbox folder is unavailable"
            )
        self._source_root = matching.locator

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        """Return one initial or incremental page and its opaque Dropbox cursor."""
        if request.checkpoint is None:
            body: dict[str, Any] = {
                "path": _folder_path(request.root_locator),
                "recursive": True,
                "include_deleted": False,
                "limit": request.page_size,
            }
            payload = await self._source_rpc("/2/files/list_folder", body)
        else:
            payload = await self._source_rpc(
                "/2/files/list_folder/continue", {"cursor": request.checkpoint}
            )
        cursor = payload.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox returned no sync cursor")
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox returned invalid entries")
        return SyncSourcePage(
            objects=tuple(_source_object(entry) for entry in entries if isinstance(entry, dict)),
            next_checkpoint=cursor,
            has_more=bool(payload.get("has_more")),
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        """Download an exact revision as raw bytes under the caller's byte ceiling."""
        path = (
            request.object_id
            if request.object_id.startswith("id:")
            else _file_path(request.object_id)
        )
        try:
            response = await self._source_request(
                "POST",
                f"{_CONTENT}/2/files/download",
                headers={"Dropbox-API-Arg": json.dumps({"path": path})},
                stream=True,
            )
        except ExtensionError as exc:
            raise _credential_failure(exc) from exc
        except httpx.HTTPStatusError as exc:
            raise _source_http_failure(exc.response) from exc
        except httpx.HTTPError as exc:
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox transport failed") from exc
        try:
            metadata = _download_metadata(response)
            actual_version = str(metadata.get("rev") or "")
            if actual_version != request.version:
                raise SourceError(
                    SourceFailureCode.VERSION_CHANGED,
                    f"Dropbox object changed from revision {request.version}",
                )
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > request.max_bytes:
                    raise SourceError(
                        SourceFailureCode.TOO_LARGE,
                        f"Dropbox object exceeds {request.max_bytes} bytes",
                    )
        finally:
            await response.aclose()
        locator = str(metadata.get("path_display") or metadata.get("name") or path)
        return SourceContent(
            object_id=str(metadata.get("id") or request.object_id),
            version=actual_version,
            media_type=_media_type(locator),
            content=bytes(content),
            content_hash=_optional_string(metadata.get("content_hash")),
            metadata=metadata,
        )

    async def close_source(self) -> None:
        """Release the shared connection pool owned by this attachment."""
        await self._client.aclose()

    # --- verbs ----------------------------------------------------------------

    async def _dispatch(self, tool: str, args: dict[str, Any]) -> str:
        """Route one verb to its request. ``KeyError`` means an undeclared name."""
        if tool == "dropbox_list":
            body: dict[str, Any] = {
                "path": _folder_path(str(args.get("path", ""))),
                "recursive": str(args.get("recursive", "")).lower() in ("true", "1", "yes"),
            }
            if args.get("limit"):
                body["limit"] = int(args["limit"])
            return _dump(await self._rpc("/2/files/list_folder", body))
        if tool == "dropbox_search":
            options = {"max_results": int(args["limit"])} if args.get("limit") else {}
            return _dump(
                await self._rpc(
                    "/2/files/search_v2", {"query": str(args["query"]), "options": options}
                )
            )
        if tool == "dropbox_download":
            return await self._download(_file_path(str(args["path"])))
        if tool == "dropbox_account":
            return _dump(await self._rpc("/2/users/get_current_account", None))
        if tool == "dropbox_upload":
            return await self._upload(args)
        if tool == "dropbox_create_folder":
            path = _file_path(str(args["path"]))
            return _dump(await self._rpc("/2/files/create_folder_v2", {"path": path}))
        if tool == "dropbox_move":
            return _dump(
                await self._rpc(
                    "/2/files/move_v2",
                    {
                        "from_path": _file_path(str(args["from_path"])),
                        "to_path": _file_path(str(args["to_path"])),
                    },
                )
            )
        if tool == "dropbox_archive_copy":
            return _dump(await self._archive_copy(args))
        if tool == "dropbox_delete":
            return _dump(
                await self._rpc("/2/files/delete_v2", {"path": _file_path(str(args["path"]))})
            )
        raise KeyError(tool)

    async def _archive_copy(self, args: dict[str, Any]) -> dict[str, Any]:
        """Copy listed meeting files server-side into ``dest_root/<meeting>/<name>``.

        Deterministic and bounded, for workflow tool nodes: no bytes round-trip
        through Arc. An identical archived copy (same Dropbox content hash) is
        skipped, a different one is a conflict and is never overwritten, and a
        failing file does not stop the rest — the error is raised at the end so
        the runner retries, and a retry only has the failed files left to do.
        """
        entries = args.get("files")
        if not isinstance(entries, list):
            msg = "files must be a list of Dropbox paths"
            raise ValueError(msg)
        if len(entries) > _ARCHIVE_MAX_FILES:
            msg = f"too many files in one call: {len(entries)} > {_ARCHIVE_MAX_FILES}"
            raise ValueError(msg)
        dest_root = _file_path(str(args.get("dest_root", ""))).rstrip("/")
        if not dest_root:
            msg = "dest_root must be a folder path"
            raise ValueError(msg)
        max_bytes = int(args.get("max_file_bytes") or _ARCHIVE_MAX_FILE_BYTES)
        archived: list[str] = []
        skipped: list[str] = []
        failures: list[str] = []
        for raw in sorted({_archive_entry_path(entry) for entry in entries}):
            try:
                relative, copied = await self._archive_one(raw, dest_root, max_bytes)
            except (ValueError, httpx.HTTPError) as exc:
                failures.append(f"{raw}: {exc}")
                continue
            (archived if copied else skipped).append(relative)
        if failures:
            raise ValueError("; ".join(failures)[:2000])
        return {
            "status": "archived",
            "count": len(archived) + len(skipped),
            "archived": sorted(archived),
            "skipped": sorted(skipped),
        }

    async def _archive_one(self, raw: str, dest_root: str, max_bytes: int) -> tuple[str, bool]:
        """Archive one file. Returns (meeting-relative name, whether it was newly copied)."""
        source = _file_path(raw)
        parts = [p for p in source.split("/") if p]
        if parts and parts[0] == _ARCHIVE_SOURCE_FOLDER:
            parts = parts[1:]
        if not parts:
            msg = "not a meeting file"
            raise ValueError(msg)
        meeting = parts[0] if len(parts) > 1 else parts[0].rsplit(".", 1)[0]
        relative = f"{meeting}/{parts[-1]}"
        target = f"{dest_root}/{relative}"
        origin = await self._metadata(source)
        if origin is None or origin.get(".tag") != "file":
            msg = "source file missing"
            raise ValueError(msg)
        if int(origin.get("size", 0)) > max_bytes:
            msg = f"file too large (> {max_bytes} bytes)"
            raise ValueError(msg)
        existing = await self._metadata(target)
        if existing is not None:
            if existing.get("content_hash") == origin.get("content_hash"):
                return relative, False
            msg = "conflict: an archived copy already differs"
            raise ValueError(msg)
        await self._rpc(
            "/2/files/copy_v2", {"from_path": source, "to_path": target, "autorename": False}
        )
        return relative, True

    async def _metadata(self, path: str) -> dict[str, Any] | None:
        """A path's metadata, or None when Dropbox says there is nothing there."""
        try:
            return await self._rpc("/2/files/get_metadata", {"path": path})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409 and "not_found" in exc.response.text:
                return None
            raise

    async def _download(self, path: str) -> str:
        """A file's bytes as text, truncated so one large file cannot flood context."""
        response = await self._request(
            "POST",
            f"{_CONTENT}/2/files/download",
            headers={"Dropbox-API-Arg": json.dumps({"path": path})},
        )
        text = response.content.decode("utf-8", errors="replace")
        if len(text) > _MAX_DOWNLOAD_CHARS:
            dropped = len(text) - _MAX_DOWNLOAD_CHARS
            return f"{text[:_MAX_DOWNLOAD_CHARS]}…[+{dropped} chars truncated]"
        return text

    async def _upload(self, args: dict[str, Any]) -> str:
        """Write text to a path. ``mode`` chooses add-and-autorename or overwrite."""
        mode = str(args.get("mode", "add")).lower()
        if mode not in ("add", "overwrite"):
            msg = f"upload mode must be 'add' or 'overwrite', not {mode!r}"
            raise ValueError(msg)
        arg = json.dumps(
            {"path": _file_path(str(args["path"])), "mode": mode, "autorename": mode == "add"}
        )
        response = await self._request(
            "POST",
            f"{_CONTENT}/2/files/upload",
            headers={"Dropbox-API-Arg": arg, "Content-Type": "application/octet-stream"},
            content=str(args["content"]).encode("utf-8"),
        )
        return _dump(_body(response))

    # --- transport -------------------------------------------------------------

    async def _rpc(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """One RPC call to api.dropboxapi.com. A ``None`` body sends the literal null."""
        response = await self._request(
            "POST",
            f"{_API}{path}",
            headers={"Content-Type": "application/json"},
            content="null" if body is None else json.dumps(body),
        )
        return _body(response)

    async def _source_rpc(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return await self._rpc(path, body)
        except ExtensionError as exc:
            raise _credential_failure(exc) from exc
        except httpx.HTTPStatusError as exc:
            raise _source_http_failure(exc.response) from exc
        except httpx.HTTPError as exc:
            raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox transport failed") from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: str | bytes | None = None,
    ) -> httpx.Response:
        return await self._source_request(method, url, headers=headers, content=content)

    async def _source_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: str | bytes | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        refreshed = False
        for attempt in range(_MAX_ATTEMPTS):
            token = (await self._credential.bearer()).reveal()
            client = self._http
            request = client.build_request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", **headers},
                content=content,
            )
            response = await client.send(request, stream=stream)
            if response.status_code == 401 and not refreshed:
                await response.aclose()
                await self._credential.invalidate()
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < _MAX_ATTEMPTS:
                    delay = _retry_after(response, attempt)
                    await response.aclose()
                    await asyncio.sleep(delay)
                    continue
            if stream and response.is_error:
                await response.aread()
                await response.aclose()
            response.raise_for_status()
            return response
        raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox retry budget exhausted")


def _refused(status: int) -> str:
    """What the operator should do about the status Dropbox answered the probe with.

    Its own copy, deliberately: this folder imports nothing from Arc but the
    hook's value types, which is what makes the bundle deletable. 401 means the
    access token was refused even after Arc renewed it, so the grant behind it was
    revoked or the app credentials changed. 403 means the
    app is not permitted the scope a verb needs — the account signed in, but the
    app was not granted files access on its Permissions tab.
    """
    if status in (400, 401):
        return (
            "Dropbox refused the access token even after a renewal. The authorization may "
            "be revoked, or the app key and secret may have changed. Reconnect the account "
            "with 'arc connector authorize <instance>'."
        )
    if status == 403:
        return (
            "Dropbox signed the app in but refused the call. The app is missing a permission "
            "it needs — open its Permissions tab, tick files.metadata.read, files.content.read "
            "and files.content.write, click Submit, then re-authorize to get a fresh token."
        )
    return f"Dropbox answered {status}, so the connection could not be checked."


def _account_name(account: dict[str, Any]) -> str:
    """A human label for the connected account, however Dropbox shaped the reply."""
    email = account.get("email")
    if isinstance(email, str) and email:
        return email
    name = account.get("name")
    if isinstance(name, dict):
        display = name.get("display_name")
        if isinstance(display, str) and display:
            return display
    return "the connected account"


def _folder_path(value: str) -> str:
    """A list_folder path: '' for the root, else a validated absolute path."""
    text = value.strip()
    if not text or text == "/":
        return ""
    return _file_path(text)


def _file_path(value: str) -> str:
    """An absolute Dropbox path, refusing the traversal a path must never carry."""
    text = value.strip()
    if ".." in text:
        msg = f"invalid Dropbox path {text!r}"
        raise ValueError(msg)
    return text if text.startswith("/") else f"/{text}"


def _archive_entry_path(entry: Any) -> str:
    """The path in a files entry: a string, or an object carrying ``path``/``path_display``."""
    if isinstance(entry, str) and entry:
        return entry
    if isinstance(entry, dict):
        for key in ("path", "path_display"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    msg = f"unreadable files entry: {str(entry)[:80]!r}"
    raise ValueError(msg)


def _body(response: httpx.Response) -> dict[str, Any]:
    """The response as a JSON object, raising for status and for a non-object body."""
    response.raise_for_status()
    if not response.content:
        return {"status": response.status_code}
    parsed = response.json()
    if not isinstance(parsed, dict):
        return {"result": parsed}
    return parsed


def _source_object(entry: dict[str, Any]) -> SourceObject:
    """Translate Dropbox metadata into the canonical source record."""
    tag = str(entry.get(".tag") or "")
    locator = str(entry.get("path_display") or entry.get("path_lower") or entry.get("name") or "")
    if tag == "deleted":
        return SourceObject(
            object_id=f"path:{str(entry.get('path_lower') or locator).lower()}",
            locator=locator,
            kind=SourceObjectKind.DELETED,
            deleted=True,
            metadata=entry,
        )
    kind = SourceObjectKind.FOLDER if tag == "folder" else SourceObjectKind.FILE
    object_id = str(entry.get("id") or f"path:{locator.lower()}")
    # A monotonic revision so a CHANGED file re-indexes. Dropbox's ``rev`` is the
    # content version but is not monotonic, and ArcMemory refuses an update whose
    # revision is not strictly greater than the stored one — so without this a
    # file was ingested once and every later edit was rejected as out of order.
    # ``server_modified`` moves forward on every edit.
    metadata = {**entry, "revision": _modified_revision(entry.get("server_modified"))}
    return SourceObject(
        object_id=object_id,
        locator=locator,
        kind=kind,
        version=_optional_string(entry.get("rev")),
        content_hash=_optional_string(entry.get("content_hash")),
        size=_optional_int(entry.get("size")),
        modified_at=_optional_string(entry.get("server_modified")),
        media_type=_media_type(locator) if kind is SourceObjectKind.FILE else None,
        metadata=metadata,
    )


def _download_metadata(response: httpx.Response) -> dict[str, Any]:
    raw = response.headers.get("dropbox-api-result", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(
            SourceFailureCode.TRANSIENT, "Dropbox returned no file metadata"
        ) from exc
    if not isinstance(parsed, dict):
        raise SourceError(SourceFailureCode.TRANSIENT, "Dropbox returned invalid file metadata")
    return parsed


def _source_http_failure(response: httpx.Response) -> SourceError:
    status = response.status_code
    if status in (400, 401, 403):
        return SourceError(SourceFailureCode.AUTH_REQUIRED, "Dropbox authorization was refused")
    if status == 404:
        return SourceError(SourceFailureCode.NOT_FOUND, "Dropbox object was not found")
    if status == 429:
        return SourceError(
            SourceFailureCode.RATE_LIMITED,
            "Dropbox rate limit persisted after bounded retries",
            retry_after=_retry_after(response, _MAX_ATTEMPTS - 1),
        )
    if status == 409:
        text = response.text.lower()
        if "reset" in text or "invalid_cursor" in text:
            return SourceError(SourceFailureCode.CHECKPOINT_INVALID, "Dropbox cursor is invalid")
        if "not_found" in text:
            return SourceError(SourceFailureCode.NOT_FOUND, "Dropbox object was not found")
    if status >= 500:
        return SourceError(SourceFailureCode.TRANSIENT, "Dropbox service remained unavailable")
    return SourceError(SourceFailureCode.TRANSIENT, f"Dropbox refused source request ({status})")


def _retry_after(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return min(max(float(raw), 0.0), 30.0)
        except ValueError:
            pass
    return min(0.25 * float(2**attempt), 2.0)


def _media_type(locator: str) -> str:
    return mimetypes.guess_type(locator)[0] or "application/octet-stream"


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _modified_revision(value: Any) -> int:
    """A monotonic revision from Dropbox's ``server_modified`` ISO timestamp.

    Returns 1 when absent or unparseable — a file with no modified time never
    re-indexes on edit, but it also never wrongly blocks its own first ingest.
    """
    if not isinstance(value, str) or not value:
        return 1
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)
    except ValueError:
        return 1


#: Credential errors only a person can fix. Anything else (a busy renewal, a
#: provider outage during renewal, a stale token) is transient and retried.
_TERMINAL_CREDENTIAL_CODES: Final = frozenset(
    {"CREDENTIAL_MISSING", "CREDENTIAL_UNREADABLE", "CREDENTIAL_NOT_GRANTED"}
)


def _credential_failure(exc: ExtensionError) -> SourceError:
    """Map a credential-handle refusal onto the source taxonomy without over-escalating."""
    terminal = exc.code in _TERMINAL_CREDENTIAL_CODES or bool(getattr(exc, "terminal", False))
    code = SourceFailureCode.AUTH_REQUIRED if terminal else SourceFailureCode.TRANSIENT
    return SourceError(code, f"Dropbox credential unavailable ({exc.code})")


def _schema(
    properties: dict[str, dict[str, Any]], *, required: list[str] | None = None
) -> dict[str, Any]:
    """One tool's input schema, closed to anything the verb did not declare."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _dump(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


def _error(tool: str, content: str) -> ToolResult:
    return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=content)


def build_native_attachment(context: dict[str, Any]) -> DropboxAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return DropboxAttachment(context["credential"])
