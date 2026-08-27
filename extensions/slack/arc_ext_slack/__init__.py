"""Slack Web API adapter — the whole third-party side of the slack bundle.

Reads a user's Slack the way Slack recommends for an unattended reader: a USER
token (`xoxp-`), which sees exactly what the authenticating human sees — every
public channel, every private channel and DM they belong to — with no
per-conversation invite. A bot token cannot do this: a bot only reads
conversations it was added to and never sees a human's DMs. Slack user tokens do
NOT expire by default (token rotation is opt-in), so unlike the Dropbox bundle
there is no refresh-token dance: the operator supplies one durable token and the
connection keeps working.

Credential handling and transport mirror the other native bundles: the declared
secret arrives in the factory's context, resolved from Arc's secret store for
this connected instance and nowhere else; every request carries an explicit
timeout; and Slack's ``ok:false`` refusal is returned as an answer, never raised.

Knowledge model: one document per selected channel/DM. ``sync_source`` lists the
selected conversations with a version (the latest message ts); ``fetch_source``
renders that conversation's recent history to text, which arcmemory chunks and
indexes. This keeps the object count bounded (one per channel, not one per
message) while making the whole conversation searchable.
"""

from __future__ import annotations

import asyncio
import json
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

#: Connecting is held to a short leash — an unreachable host is reported at once,
#: not waited on — while a read already in flight is given room.
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

#: The one Slack Web API host. Every method is POST here with a Bearer token.
_API: Final = "https://slack.com/api"

#: Bounded retries for a 429 or a 5xx, honoring Retry-After.
_MAX_ATTEMPTS: Final = 4

#: How many recent messages a channel document renders, and the text cap on it —
#: so one busy channel cannot flood the model's context or a single sync.
_HISTORY_LIMIT: Final = 200
_MAX_DOC_CHARS: Final = 100_000

#: The conversation types a user token can enumerate.
_CONV_TYPES: Final = "public_channel,private_channel,mpim,im"

_STRING: Final[dict[str, str]] = {"type": "string"}


class SlackAttachment:
    """Reaches the Slack Web API over HTTPS, through the hook methods."""

    #: A ``*:read`` scope maps to the conversation type it unlocks. If the token
    #: lacks one, that type is dropped and the connection still works with the
    #: rest — resilience over an all-or-nothing refusal.
    _SCOPE_TYPE: Final[dict[str, str]] = {
        "channels:read": "public_channel",
        "groups:read": "private_channel",
        "im:read": "im",
        "mpim:read": "mpim",
    }

    def __init__(self, *, user_token: str) -> None:
        self._token = user_token
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        self._selected: tuple[str, ...] = ()
        self._users: dict[str, str] = {}
        self._users_lock = asyncio.Lock()
        self._types: list[str] = _CONV_TYPES.split(",")
        self._dropped: set[str] = set()

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """One credential and no host prerequisite: the transport is httpx."""
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name="user_token",
                instruction="A Slack User OAuth Token (xoxp-) with the read scopes",
            )
        ]

    async def probe(self) -> ProbeResult:
        """auth.test — the cheapest call that proves the token and reach."""
        if not self._token:
            return ProbeResult(
                reachable=False,
                detail=(
                    "slack has no user_token — run 'arc connector auth <instance>' "
                    "and paste your Slack User OAuth Token (xoxp-)."
                ),
            )
        try:
            result = await self._call("auth.test", {})
        except httpx.HTTPStatusError as exc:
            return ProbeResult(reachable=False, detail=_refused(exc.response.status_code))
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(reachable=False, detail=f"Slack did not answer: {exc}")
        if not result.get("ok"):
            return ProbeResult(reachable=False, detail=_ok_error("auth.test", result))
        who = str(result.get("user") or "you")
        team = str(result.get("team") or "your workspace")
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"reached Slack as {who} in {team}",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Five verbs. Classification and tags match extension.toml exactly."""
        return [
            ToolSpec(
                name="slack_list_channels",
                description="List the channels, groups, and DMs you belong to.",
                input_schema=_schema({"limit": _STRING}),
                classification="read_only",
            ),
            ToolSpec(
                name="slack_read_channel",
                description="Read recent messages from a channel or DM by id.",
                input_schema=_schema({"channel": _STRING, "limit": _STRING}, required=["channel"]),
                classification="read_only",
            ),
            ToolSpec(
                name="slack_read_thread",
                description="Read a thread (parent + replies) by channel id and thread ts.",
                input_schema=_schema(
                    {"channel": _STRING, "ts": _STRING}, required=["channel", "ts"]
                ),
                classification="read_only",
            ),
            ToolSpec(
                name="slack_search",
                description="Search your Slack messages for a query.",
                input_schema=_schema({"query": _STRING, "limit": _STRING}, required=["query"]),
                classification="read_only",
            ),
            ToolSpec(
                name="slack_send_message",
                description="Post a message to a channel or DM.",
                input_schema=_schema(
                    {"channel": _STRING, "text": _STRING}, required=["channel", "text"]
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one verb. A Slack refusal is an answer, not a raise."""
        try:
            return ToolResult(tool=tool, content=await self._dispatch(tool, args))
        except KeyError:
            return _error(tool, f"slack has no tool named {tool!r}")
        except httpx.HTTPStatusError as exc:
            return _error(tool, f"slack answered {exc.response.status_code}: {exc.response.text}")
        except (httpx.HTTPError, ValueError) as exc:
            return _error(tool, f"slack call failed: {exc}")

    # --- connected source contract -------------------------------------------

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        """Identify the workspace behind one connection without exposing the token."""
        result = await self._source_call("auth.test", {})
        team_id = str(result.get("team_id") or "")
        if not team_id:
            raise SourceError(SourceFailureCode.TRANSIENT, "Slack returned no team id")
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="slack",
            account_id=team_id,
            display_name=str(result.get("team") or team_id),
            root_locator="",
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        """List the conversations the user may select for indexing."""
        del request
        resources: list[SourceResource] = []
        cursor = ""
        for _ in range(20):  # bound the enumeration (≤ 20k conversations)
            try:
                payload = await self._conversations(limit=1000, cursor=cursor)
            except httpx.HTTPStatusError as exc:
                raise _source_http_failure(exc.response) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise SourceError(SourceFailureCode.TRANSIENT, str(exc)) from exc
            for chan in payload.get("channels", []):
                if not isinstance(chan, dict):
                    continue
                cid = str(chan.get("id") or "")
                if cid:
                    resources.append(
                        SourceResource(
                            resource_id=cid,
                            label=_channel_label(chan),
                            resource_kind="channel",
                            locator=cid,
                        )
                    )
            cursor = str(_cursor(payload))
            if not cursor:
                break
        return tuple(resources)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        """Pin the selected conversation ids; sync/fetch operate on exactly these."""
        selected = tuple(cid for cid in request.resource_ids if cid)
        if not selected:
            raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "select at least one channel")
        self._selected = selected

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        """One page: every selected conversation as an object at its latest ts.

        The whole set fits one page (channels, not messages), so there is no
        cursor to carry — each sync is a fresh snapshot and the coordinator
        re-fetches only the conversations whose version (latest ts) advanced.

        The sync runs on a FRESH adapter instance, so ``self._selected`` (set by
        an earlier ``select_source_resources`` call) is empty here — fall back to
        every conversation the token can read, exactly as the github bundle falls
        back to all repos. Otherwise the sync would emit zero objects and download
        nothing.
        """
        channels = self._selected
        if not channels:
            resources = await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
            channels = tuple(resource.resource_id for resource in resources)
        objects: list[SourceObject] = []
        for cid in channels:
            latest = await self._latest_ts(cid)
            objects.append(
                SourceObject(
                    object_id=f"channel:{cid}",
                    locator=cid,
                    kind=SourceObjectKind.FILE,
                    version=latest or "0",
                    media_type="text/plain",
                    metadata={"channel": cid},
                )
            )
        return SyncSourcePage(objects=tuple(objects), next_checkpoint="", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        """Render a conversation's recent history to text under the byte ceiling."""
        cid = request.object_id.removeprefix("channel:")
        messages = await self._history(cid, _HISTORY_LIMIT)
        text = await self._render(cid, messages)
        body = text.encode("utf-8")
        if len(body) > request.max_bytes:
            body = body[: request.max_bytes]
        latest = messages[0].get("ts", "0") if messages else "0"
        return SourceContent(
            object_id=request.object_id,
            version=str(latest),
            media_type="text/plain",
            content=body,
            metadata={"channel": cid, "message_count": len(messages)},
        )

    async def close_source(self) -> None:
        """Release the shared connection pool owned by this attachment."""
        await self._client.aclose()

    # --- verbs ----------------------------------------------------------------

    async def _dispatch(self, tool: str, args: dict[str, Any]) -> str:
        """Route one verb to its request. ``KeyError`` means an undeclared name."""
        if tool == "slack_list_channels":
            limit = int(args["limit"]) if args.get("limit") else 200
            payload = await self._conversations(limit=limit, cursor="")
            out: dict[str, Any] = {"channels": payload.get("channels", [])}
            if self._dropped:
                out["note"] = (
                    "some conversation types are not readable with the current token "
                    f"scopes: {sorted(self._dropped)} — add them and reinstall to include them."
                )
            return _dump(out)
        if tool == "slack_read_channel":
            limit = int(args["limit"]) if args.get("limit") else 50
            messages = await self._history(str(args["channel"]), limit)
            return _dump({"channel": args["channel"], "messages": messages})
        if tool == "slack_read_thread":
            return _dump(
                await self._call(
                    "conversations.replies",
                    {"channel": str(args["channel"]), "ts": str(args["ts"])},
                )
            )
        if tool == "slack_search":
            params: dict[str, Any] = {"query": str(args["query"])}
            params["count"] = int(args["limit"]) if args.get("limit") else 20
            return _dump(await self._call("search.messages", params))
        if tool == "slack_send_message":
            return _dump(
                await self._call(
                    "chat.postMessage",
                    {"channel": str(args["channel"]), "text": str(args["text"])},
                )
            )
        raise KeyError(tool)

    async def _conversations(self, *, limit: int, cursor: str) -> dict[str, Any]:
        """conversations.list, narrowing types when a ``*:read`` scope is missing.

        Slack refuses the whole call if any requested type's scope is absent, so
        on a ``missing_scope`` it drops the mapped type (recorded in ``_dropped``)
        and retries — the connection lists what the token CAN see rather than
        failing entirely. Narrowing settles on the first page, before any cursor.
        """
        for _ in range(len(self._SCOPE_TYPE) + 1):
            if not self._types:
                raise ValueError("Slack has no readable conversation types for this token")
            params: dict[str, Any] = {
                "types": ",".join(self._types),
                "exclude_archived": "true",
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor
            payload = await self._raw("conversations.list", params)
            if payload.get("ok"):
                return payload
            if payload.get("error") == "missing_scope":
                removed = False
                for scope in str(payload.get("needed") or "").replace(" ", ",").split(","):
                    conv_type = self._SCOPE_TYPE.get(scope.strip())
                    if conv_type and conv_type in self._types:
                        self._types.remove(conv_type)
                        self._dropped.add(scope.strip())
                        removed = True
                if removed:
                    continue
            raise ValueError(_ok_error("conversations.list", payload))
        raise ValueError("Slack conversations.list: scope narrowing exhausted")

    # --- history + rendering --------------------------------------------------

    async def _history(self, channel: str, limit: int) -> list[dict[str, Any]]:
        """Recent messages for one conversation, newest first."""
        payload = await self._call(
            "conversations.history", {"channel": channel, "limit": min(limit, 1000)}
        )
        messages = payload.get("messages", [])
        return [m for m in messages if isinstance(m, dict)]

    async def _latest_ts(self, channel: str) -> str:
        """The ts of the newest message — the conversation's version."""
        payload = await self._source_call(
            "conversations.history", {"channel": channel, "limit": 1}
        )
        messages = payload.get("messages", [])
        if messages and isinstance(messages[0], dict):
            return str(messages[0].get("ts") or "0")
        return "0"

    async def _render(self, channel: str, messages: list[dict[str, Any]]) -> str:
        """Render messages oldest-first as 'name: text' lines for indexing."""
        lines: list[str] = [f"# Slack conversation {channel}", ""]
        for msg in reversed(messages):
            who = await self._user_name(str(msg.get("user") or msg.get("bot_id") or "unknown"))
            text = str(msg.get("text") or "").strip()
            if text:
                lines.append(f"{who}: {text}")
        rendered = "\n".join(lines)
        if len(rendered) > _MAX_DOC_CHARS:
            rendered = rendered[:_MAX_DOC_CHARS] + "\n…[truncated]"
        return rendered

    async def _user_name(self, user_id: str) -> str:
        """Resolve a user id to a display name, cached for the connection."""
        if not user_id or user_id == "unknown":
            return "unknown"
        async with self._users_lock:
            if user_id in self._users:
                return self._users[user_id]
        try:
            payload = await self._call("users.info", {"user": user_id})
        except (httpx.HTTPError, ValueError):
            return user_id
        profile = payload.get("user", {}) if isinstance(payload.get("user"), dict) else {}
        name = str(profile.get("real_name") or profile.get("name") or user_id)
        async with self._users_lock:
            self._users[user_id] = name
        return name

    # --- transport -------------------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One Slack Web API call. Raises ``ValueError`` on an ``ok:false`` refusal."""
        payload = await self._raw(method, params)
        if not payload.get("ok"):
            raise ValueError(_ok_error(method, payload))
        return payload

    async def _source_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A source-side call, mapping a refusal to a typed ``SourceError``."""
        try:
            payload = await self._raw(method, params)
        except httpx.HTTPStatusError as exc:
            raise _source_http_failure(exc.response) from exc
        except httpx.HTTPError as exc:
            raise SourceError(SourceFailureCode.TRANSIENT, "Slack transport failed") from exc
        if not payload.get("ok"):
            raise _source_ok_failure(method, payload)
        return payload

    async def _raw(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """POST one method, retrying a 429 / 5xx / ratelimited under a bounded budget."""
        for attempt in range(_MAX_ATTEMPTS):
            response = await self._client.post(
                f"{_API}/{method}",
                headers={"Authorization": f"Bearer {self._token}"},
                data={k: str(v) for k, v in params.items()},
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < _MAX_ATTEMPTS:
                    await asyncio.sleep(_retry_after(response, attempt))
                    continue
            response.raise_for_status()
            parsed = response.json()
            if not isinstance(parsed, dict):
                raise ValueError(f"Slack returned a non-object body for {method}")
            if parsed.get("error") == "ratelimited" and attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(_retry_after(response, attempt))
                continue
            return parsed
        raise ValueError(f"Slack retry budget exhausted for {method}")


def _refused(status: int) -> str:
    if status == 401:
        return (
            "Slack refused the token. The User OAuth Token is wrong or revoked — "
            "reinstall the app to your workspace and paste the new xoxp- token via "
            "'arc connector auth <instance>'."
        )
    if status == 403:
        return "Slack signed the token in but refused the call — a required scope is missing."
    return f"Slack answered {status}, so the connection could not be checked."


def _ok_error(method: str, payload: dict[str, Any]) -> str:
    err = str(payload.get("error") or "unknown_error")
    needed = payload.get("needed")
    hint = f" (needs scope: {needed})" if needed else ""
    return f"Slack {method} refused: {err}{hint}"


def _channel_label(chan: dict[str, Any]) -> str:
    name = chan.get("name")
    if isinstance(name, str) and name:
        prefix = "#" if chan.get("is_private") is False else "🔒"
        return f"{prefix}{name}"
    if chan.get("is_im"):
        return f"DM {chan.get('user') or chan.get('id')}"
    if chan.get("is_mpim"):
        return str(chan.get("name") or "group DM")
    return str(chan.get("id") or "channel")


def _cursor(payload: dict[str, Any]) -> str:
    meta = payload.get("response_metadata")
    if isinstance(meta, dict):
        return str(meta.get("next_cursor") or "")
    return ""


def _source_http_failure(response: httpx.Response) -> SourceError:
    status = response.status_code
    if status in (401, 403):
        return SourceError(SourceFailureCode.AUTH_REQUIRED, "Slack authorization was refused")
    if status == 429:
        return SourceError(
            SourceFailureCode.RATE_LIMITED,
            "Slack rate limit persisted after bounded retries",
            retry_after=_retry_after(response, _MAX_ATTEMPTS - 1),
        )
    if status >= 500:
        return SourceError(SourceFailureCode.TRANSIENT, "Slack service remained unavailable")
    return SourceError(SourceFailureCode.TRANSIENT, f"Slack refused source request ({status})")


def _source_ok_failure(method: str, payload: dict[str, Any]) -> SourceError:
    err = str(payload.get("error") or "")
    if err in ("not_authed", "invalid_auth", "token_revoked", "account_inactive"):
        return SourceError(SourceFailureCode.AUTH_REQUIRED, f"Slack {method}: {err}")
    if err == "ratelimited":
        return SourceError(SourceFailureCode.RATE_LIMITED, f"Slack {method}: rate limited")
    if err in ("channel_not_found", "not_in_channel"):
        return SourceError(SourceFailureCode.NOT_FOUND, f"Slack {method}: {err}")
    return SourceError(SourceFailureCode.TRANSIENT, _ok_error(method, payload))


def _retry_after(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return min(max(float(raw), 0.0), 60.0)
        except ValueError:
            pass
    return min(0.5 * float(2**attempt), 8.0)


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


def build_native_attachment(context: dict[str, Any]) -> SlackAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return SlackAttachment(user_token=_credential(context, "user_token"))
