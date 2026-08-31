"""Dropbox files API adapter — the whole third-party side of the dropbox bundle.

Speaks OAuth2 the way Dropbox recommends for an unattended app. The operator
supplies three values once: the app key and secret that identify the app, and a
refresh token that does not expire. From those this adapter mints a short-lived
access token on demand (POST oauth2/token, grant_type=refresh_token) and caches
it until just before it expires, so a connection made once keeps working with no
further sign-in — the property the old `dbxcli` short-lived token could not hold.

Credential handling and the transport rules mirror the confluence bundle's, for
the same reasons: the declared secrets arrive in the factory's context, resolved
from Arc's secret store for this connected instance and from nowhere else; every
request carries an explicit timeout; and a path is data carried in the request
body or the `Dropbox-API-Arg` header, never spliced into a URL.

Dropbox splits its API across two hosts: RPC calls (list, search, metadata) go to
api.dropboxapi.com with a JSON body; file bytes (download, upload) go to
content.dropboxapi.com with the arguments in a header and the content as the raw
body. Both are reached with the same bearer token.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from typing import Any, Final

import httpx
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
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

#: One flat 30 seconds covered both reaching Dropbox and reading a file from it,
#: so any document that took longer than half a minute to download failed the
#: whole sync with a ReadTimeout. Connecting is still held to a short leash —
#: an unreachable host should be reported at once, not waited on — while a
#: transfer already in progress is given room. The per-sync ``max_seconds``
#: ceiling still bounds the run as a whole.
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=300.0, write=120.0, pool=10.0)

#: Where a refresh token is exchanged for a short-lived access token.
_OAUTH_ENDPOINT: Final = "https://api.dropbox.com/oauth2/token"

#: The RPC host (list, search, metadata, account) and the content host (bytes).
_API: Final = "https://api.dropboxapi.com"
_CONTENT: Final = "https://content.dropboxapi.com"

#: Refresh a cached access token this many seconds before it actually expires, so
#: a token never dies mid-request against a clock that is a little off.
_EXPIRY_SLACK: Final = 60.0

#: A downloaded file is returned as text; anything past this is truncated so a
#: single large file cannot flood the model's context. The marker names the cut.
_MAX_DOWNLOAD_CHARS: Final = 100_000
_MAX_ATTEMPTS: Final = 3

_STRING: Final[dict[str, str]] = {"type": "string"}


class DropboxAttachment:
    """Reaches the Dropbox files API over HTTPS, through the four hook methods."""

    def __init__(self, *, app_key: str, app_secret: str, refresh_token: str) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._refresh_token = refresh_token
        self._token = ""
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()
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
        """Three credentials and no host prerequisite: the transport is httpx."""
        return [
            Requirement(kind=RequirementKind.CREDENTIAL, name=name, instruction=instruction)
            for name, instruction in (
                ("app_key", "The Dropbox app's App key, from its Settings tab"),
                ("app_secret", "The Dropbox app's App secret, from its Settings tab"),
                ("refresh_token", "A Dropbox refresh token from an offline authorization"),
            )
        ]

    async def probe(self) -> ProbeResult:
        """Read the current account. The cheapest call that proves auth and reach."""
        missing = self._missing()
        if missing:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"dropbox has no credential for {', '.join(missing)} — "
                    f"run 'arc connector auth <instance>' to supply them."
                ),
            )
        try:
            account = await self._rpc("/2/users/get_current_account", None)
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
        """Eight verbs. Classification and tags match extension.toml exactly."""
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
        if tool == "dropbox_delete":
            return _dump(
                await self._rpc("/2/files/delete_v2", {"path": _file_path(str(args["path"]))})
            )
        raise KeyError(tool)

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
            token = await self._access_token()
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
                self._token = ""
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

    async def _access_token(self) -> str:
        """A live access token, minted from the refresh token and cached until expiry.

        The refresh token does not expire; the access token it mints lasts a few
        hours. Caching it means a burst of verbs shares one mint, and the slack
        means a token is renewed before it can die against a skewed clock.
        """
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            response = await self._http.post(
                _OAUTH_ENDPOINT,
                data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                auth=(self._app_key, self._app_secret),
            )
            payload = _body(response)
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                msg = "Dropbox returned no access token for the refresh token"
                raise ValueError(msg)
            self._token = token
            lifetime = float(payload.get("expires_in", 14400)) - _EXPIRY_SLACK
            self._token_expiry = time.monotonic() + lifetime
            return token

    def _missing(self) -> list[str]:
        """Which of the three credentials this attachment does not have."""
        held = {
            "app_key": self._app_key,
            "app_secret": self._app_secret,
            "refresh_token": self._refresh_token,
        }
        return sorted(name for name, value in held.items() if not value)


def _refused(status: int) -> str:
    """What the operator should do about the status Dropbox answered the probe with.

    Its own copy, deliberately: this folder imports nothing from Arc but the
    hook's value types, which is what makes the bundle deletable. 401 means the
    refresh token or the app key/secret pair is wrong or revoked — Arc mints the
    access token itself, so a bad token here is one of those three. 403 means the
    app is not permitted the scope a verb needs — the account signed in, but the
    app was not granted files access on its Permissions tab.
    """
    if status == 400:
        # Dropbox's OAuth2 token endpoint answers 400 invalid_grant for a bad,
        # expired, malformed, or truncated refresh token — the actual failure an
        # operator hits, and the one the generic message below hid.
        return (
            "Dropbox rejected the refresh token (invalid_grant): it is malformed, expired, "
            "or revoked. Re-authorize the app (token_access_type=offline) and paste the new "
            "refresh token — a valid one is ~64 characters."
        )
    if status == 401:
        return (
            "Dropbox refused to mint an access token. The refresh token may be revoked, "
            "or the app key and app secret may not be the pair that issued it. Re-authorize "
            "the app (token_access_type=offline) and paste the new refresh token, or check "
            "the key and secret on the app's Settings tab."
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
    return SourceObject(
        object_id=object_id,
        locator=locator,
        kind=kind,
        version=_optional_string(entry.get("rev")),
        content_hash=_optional_string(entry.get("content_hash")),
        size=_optional_int(entry.get("size")),
        modified_at=_optional_string(entry.get("server_modified")),
        media_type=_media_type(locator) if kind is SourceObjectKind.FILE else None,
        metadata=entry,
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


def _schema(
    properties: dict[str, dict[str, str]], *, required: list[str] | None = None
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


def _credential(context: dict[str, Any], key: str) -> str:
    """One declared credential out of the context Arc resolved from its secret store."""
    return str(context.get(key) or "")


def build_native_attachment(context: dict[str, Any]) -> DropboxAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return DropboxAttachment(
        app_key=_credential(context, "app_key"),
        app_secret=_credential(context, "app_secret"),
        refresh_token=_credential(context, "refresh_token"),
    )
