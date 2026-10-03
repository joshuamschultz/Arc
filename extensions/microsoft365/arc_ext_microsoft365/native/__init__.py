# ruff: noqa: E501  (the tool table carries the manifest descriptions verbatim)
"""Microsoft 365 over native Microsoft Graph: Outlook mail, calendar, OneDrive.

Arc owns the OAuth side (Entra ID, tenant-bound, sealed custody). This attachment
asks its credential handle for a fresh bearer at the header site of every request
and never mints or stores a token. Tool names are the ten the manifest has always
allowed; what answers them changed from an npm MCP server to Graph directly.

The Graph host comes from the connection's ``cloud`` field (a KEY the connect
stored from the signed manifest's cloud table), never from a URL anyone typed.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
from arcagent.core.errors import ArcAgentError
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)

from . import tools
from .graph import GraphClient, ToolError

_MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
_INTEGER_ARGUMENTS: Final = frozenset({"top", "skip"})
_MISMATCH_DETAIL: Final = "signed in as a different account"

Handler = Callable[[GraphClient, Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class _Tool:
    """One row of the static tool table."""

    name: str
    description: str
    classification: str
    capability_tags: tuple[str, ...]
    handler: Handler
    arguments: tuple[tuple[str, str, int | None, bool], ...]  # name, text, max, required


_TOOLS: Final[tuple[_Tool, ...]] = (
    _Tool(
        "list-mail-messages",
        "List Outlook messages in the signed-in mailbox.",
        "read_only",
        (),
        tools.list_mail_messages,
        (
            (
                "folder",
                "A folder id from list-mail-folders, or inbox, sentitems, drafts, archive. Default inbox.",
                None,
                False,
            ),
            (
                "search",
                "Outlook search text, e.g. invoice from:ann. Cannot be combined with skip.",
                None,
                False,
            ),
            ("top", "How many messages to return (1-50).", 50, False),
            ("skip", "How many to skip, from next_skip of the previous page.", None, False),
        ),
    ),
    _Tool(
        "get-mail-message",
        "Read one Outlook message, including its body.",
        "read_only",
        (),
        tools.get_mail_message,
        (("message_id", "The message id from list-mail-messages.", None, True),),
    ),
    _Tool(
        "list-mail-folders",
        "List the mail folders in the mailbox.",
        "read_only",
        (),
        tools.list_mail_folders,
        (),
    ),
    _Tool(
        "send-mail",
        "Send an Outlook email to the named recipients.",
        "state_modifying",
        ("network_egress",),
        tools.send_mail,
        (
            ("to", "Recipients, comma-separated email addresses.", None, True),
            ("cc", "CC recipients, comma-separated.", None, False),
            ("subject", "Subject line.", None, True),
            ("body", "Plain-text body.", None, False),
        ),
    ),
    _Tool(
        "list-calendar-events",
        "List calendar events.",
        "read_only",
        (),
        tools.list_calendar_events,
        (
            ("top", "How many events to return (1-50).", 50, False),
            ("skip", "How many to skip, from next_skip of the previous page.", None, False),
        ),
    ),
    _Tool(
        "get-calendar-view",
        "Read the calendar over an explicit time window, recurrences expanded.",
        "read_only",
        (),
        tools.get_calendar_view,
        (
            ("start", "Window start, ISO 8601, e.g. 2026-10-05T00:00:00Z.", None, True),
            ("end", "Window end, ISO 8601.", None, True),
            ("top", "How many occurrences to return (1-100).", 100, False),
        ),
    ),
    _Tool(
        "create-calendar-event",
        "Create a calendar event. Attendees are invited by email.",
        "state_modifying",
        ("network_egress",),
        tools.create_calendar_event,
        (
            ("subject", "Event title.", None, True),
            ("start", "Start, ISO 8601 local time, e.g. 2026-10-05T09:00:00.", None, True),
            ("end", "End, ISO 8601 local time.", None, True),
            (
                "time_zone",
                "Time zone of start and end, e.g. Eastern Standard Time. Default UTC.",
                None,
                False,
            ),
            (
                "attendees",
                "Attendee email addresses, comma-separated. Each is emailed an invitation.",
                None,
                False,
            ),
            ("location", "Where it happens.", None, False),
            ("body", "Plain-text description.", None, False),
        ),
    ),
    _Tool(
        "list-folder-files",
        "List files in a OneDrive folder.",
        "read_only",
        (),
        tools.list_folder_files,
        (
            ("folder", "A folder id, or root (the default).", None, False),
            ("top", "How many items to return (1-200).", 200, False),
        ),
    ),
    _Tool(
        "get-onedrive-file",
        "Read one OneDrive file by identifier.",
        "read_only",
        (),
        tools.get_onedrive_file,
        (("file_id", "The file id from list-folder-files or search-onedrive-files.", None, True),),
    ),
    _Tool(
        "search-onedrive-files",
        "Search OneDrive files by name and content.",
        "read_only",
        (),
        tools.search_onedrive_files,
        (
            ("query", "Words to search for.", None, True),
            ("top", "How many files to return (1-100).", 100, False),
        ),
    ),
)


def _input_schema(tool: _Tool) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, description, maximum, _required in tool.arguments:
        if name in _INTEGER_ARGUMENTS:
            prop: dict[str, Any] = {"type": "integer", "minimum": 0 if name == "skip" else 1}
            if maximum is not None:
                prop["maximum"] = maximum
        else:
            prop = {"type": "string"}
        properties[name] = {**prop, "description": description}
    required = [name for name, _text, _max, needed in tool.arguments if needed]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _spec(tool: _Tool) -> ToolSpec:
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        input_schema=_input_schema(tool),
        classification=tool.classification,  # type: ignore[arg-type]  # table holds the two literals
        capability_tags=list(tool.capability_tags),
    )


class MicrosoftAttachment:
    """The Microsoft 365 tools for one connection, as an ``ExtensionAttachment``."""

    def __init__(
        self,
        credential: Any,
        account: str = "",
        cloud: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._account = account.strip()
        self._cloud = cloud.strip()
        self._transport = transport
        self._credential = credential
        self._graph: GraphClient | None = None
        self._tools = {tool.name: tool for tool in _TOOLS}

    def _client(self) -> GraphClient:
        """Built on first use, so an unknown cloud is a tool error, not a load failure."""
        if self._graph is None:
            self._graph = GraphClient(
                self._credential, cloud=self._cloud, transport=self._transport
            )
        return self._graph

    def requirements(self) -> list[Requirement]:
        """No typed credential and no host prerequisite: Arc holds the sign-in."""
        return []

    async def probe(self) -> ProbeResult:
        """``GET /me``: proves the token, the tenant's reach, and that it is the bound user."""
        try:
            user = await self._client().get_json(
                "/me", params={"$select": "userPrincipalName,mail"}
            )
        except ArcAgentError as exc:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"microsoft365 has no usable credential ({exc.code}): "
                    "click Connect on its card to sign in."
                ),
            )
        except ToolError as exc:
            return ProbeResult(reachable=False, detail=f"microsoft365: {exc}")
        names = {str(user.get(key) or "").casefold() for key in ("userPrincipalName", "mail")}
        if self._account and self._account.casefold() not in names:
            return ProbeResult(reachable=False, detail=_MISMATCH_DETAIL)
        return ProbeResult(
            reachable=True, tools=await self.describe_tools(), detail="reached Microsoft 365"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Every tool, from the static table."""
        return [_spec(tool) for tool in _TOOLS]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one tool. A failure is an ERROR result the agent can read, never a raise."""
        definition = self._tools.get(tool)
        if definition is None:
            return _error(tool, f"unknown tool {tool!r}")
        try:
            data = await definition.handler(self._client(), args)
        except (ToolError, ArcAgentError) as exc:
            return _error(tool, _failure_text(exc))
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(content.encode()) > _MAX_OUTPUT_BYTES:
            return _error(tool, "The result is larger than 16 MiB. Ask for a smaller page.")
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=content)


def _failure_text(exc: Exception) -> str:
    """Text safe to show: the handle's own code, or the sanitized Graph error."""
    if isinstance(exc, ArcAgentError):
        return f"Microsoft 365 credential unavailable ({exc.code})"
    return str(exc)


def _error(tool: str, content: str) -> ToolResult:
    return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=content)


def build_native_attachment(context: dict[str, Any]) -> MicrosoftAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return MicrosoftAttachment(
        context["credential"],
        account=str(context.get("account") or ""),
        cloud=str(context.get("cloud") or ""),
        transport=context.get("transport"),
    )


__all__ = ["MicrosoftAttachment", "build_native_attachment"]
