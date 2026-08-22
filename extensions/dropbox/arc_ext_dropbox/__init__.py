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

import json
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

#: Seconds any one Dropbox request may take before it is abandoned.
_TIMEOUT: Final = 30.0

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

_STRING: Final[dict[str, str]] = {"type": "string"}


class DropboxAttachment:
    """Reaches the Dropbox files API over HTTPS, through the four hook methods."""

    def __init__(self, *, app_key: str, app_secret: str, refresh_token: str) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._refresh_token = refresh_token
        self._token = ""
        self._token_expiry = 0.0

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
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{_CONTENT}/2/files/download",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Dropbox-API-Arg": json.dumps({"path": path}),
                },
            )
        response.raise_for_status()
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
        token = await self._access_token()
        arg = json.dumps(
            {"path": _file_path(str(args["path"])), "mode": mode, "autorename": mode == "add"}
        )
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{_CONTENT}/2/files/upload",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Dropbox-API-Arg": arg,
                    "Content-Type": "application/octet-stream",
                },
                content=str(args["content"]).encode("utf-8"),
            )
        return _dump(_body(response))

    # --- transport -------------------------------------------------------------

    async def _rpc(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """One RPC call to api.dropboxapi.com. A ``None`` body sends the literal null."""
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_API, timeout=_TIMEOUT) as client:
            response = await client.post(
                path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                content="null" if body is None else json.dumps(body),
            )
        return _body(response)

    async def _access_token(self) -> str:
        """A live access token, minted from the refresh token and cached until expiry.

        The refresh token does not expire; the access token it mints lasts a few
        hours. Caching it means a burst of verbs shares one mint, and the slack
        means a token is renewed before it can die against a skewed clock.
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
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
