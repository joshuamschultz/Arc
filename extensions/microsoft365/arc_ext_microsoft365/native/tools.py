"""The ten Microsoft 365 tools, one async function each, over :class:`GraphClient`.

Every text a mailbox, calendar or drive owner did not write themselves — subjects,
bodies, previews, names, file text — is framed as untrusted data before it leaves
here (LLM01). Graph asked for mail bodies as plain text (``Prefer:
outlook.body-content-type="text"``), so no HTML reaches the model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from arcagent.extension.untrusted import frame_untrusted

from .graph import GraphClient, ToolError, segment

TEXT_BODY: Final = {"Prefer": 'outlook.body-content-type="text"'}
_MAIL_SELECT: Final = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,"
    "isRead,hasAttachments,conversationId,parentFolderId,webLink"
)
_EVENT_SELECT: Final = "id,subject,start,end,location,organizer,attendees,isAllDay,webLink"
_FILE_SELECT: Final = "id,name,size,file,folder,lastModifiedDateTime,webUrl,parentReference"
_FOLDER_SELECT: Final = "id,displayName,parentFolderId,totalItemCount,unreadItemCount"
_EMAIL: Final = re.compile(r"^[^@\s<>\",;:]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
_MAX_RECIPIENTS: Final = 50
_MAX_TEXT_FILE_BYTES: Final = 1024 * 1024
_TEXT_TYPES: Final = ("text/", "application/json", "application/xml", "application/csv")
_WELL_KNOWN_FOLDERS: Final = frozenset(
    {"inbox", "sentitems", "drafts", "deleteditems", "archive", "junkemail"}
)


def _framed(text: str) -> str:
    return frame_untrusted([("microsoft365", text)]) if text else ""


def text_arg(args: Mapping[str, Any], name: str, *, required: bool = False) -> str:
    """A string argument, stripped. A missing required one is a tool error."""
    value = args.get(name)
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ToolError(f"{name} is required")
    if any(not character.isprintable() for character in text if character not in "\n\t"):
        raise ToolError(f"{name} has control characters in it")
    return text


def int_arg(args: Mapping[str, Any], name: str, *, default: int, ceiling: int) -> int:
    """An integer argument clamped to ``0..ceiling`` (``skip``) or ``1..ceiling``."""
    raw = args.get(name)
    if raw is None or raw == "":
        return min(default, ceiling)
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a whole number") from None
    floor = 0 if name == "skip" else 1
    return max(floor, min(number, ceiling))


def _address(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return ""
    email = entry.get("emailAddress")
    if not isinstance(email, Mapping):
        return ""
    name, address = str(email.get("name") or ""), str(email.get("address") or "")
    return f"{name} <{address}>" if name and name != address else address


def _addresses(entries: Any) -> list[str]:
    return [_address(entry) for entry in entries or [] if _address(entry)]


def _recipients(args: Mapping[str, Any], name: str, *, required: bool) -> list[dict[str, Any]]:
    """Comma-separated addresses → Graph recipients. Each must look like one address."""
    listed = [item.strip() for item in text_arg(args, name, required=required).split(",")]
    addresses = [item for item in listed if item]
    if len(addresses) > _MAX_RECIPIENTS:
        raise ToolError(f"{name} names more than {_MAX_RECIPIENTS} recipients")
    for address in addresses:
        if not _EMAIL.fullmatch(address):
            raise ToolError(f"{name} has something that is not an email address in it")
    return [{"emailAddress": {"address": address}} for address in addresses]


def _folder(args: Mapping[str, Any]) -> str:
    folder = text_arg(args, "folder") or "inbox"
    return folder.lower() if folder.lower() in _WELL_KNOWN_FOLDERS else folder


def _message_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "conversation_id": raw.get("conversationId"),
        "received": raw.get("receivedDateTime"),
        "from": _framed(_address(raw.get("from"))),
        "to": _framed(", ".join(_addresses(raw.get("toRecipients")))),
        "subject": _framed(str(raw.get("subject") or "")),
        "preview": _framed(str(raw.get("bodyPreview") or "")),
        "is_read": raw.get("isRead"),
        "has_attachments": raw.get("hasAttachments"),
        "link": raw.get("webLink"),
    }


def _page(payload: Mapping[str, Any], items: list[dict[str, Any]], skip: int) -> dict[str, Any]:
    more = bool(payload.get("@odata.nextLink"))
    return {"items": items, "more": more, "next_skip": skip + len(items) if more else None}


async def list_mail_messages(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """Messages in one folder, newest first; optional Outlook search text."""
    top = int_arg(args, "top", default=25, ceiling=50)
    skip = int_arg(args, "skip", default=0, ceiling=100_000)
    search = text_arg(args, "search")
    params: dict[str, str | int] = {"$top": top, "$select": _MAIL_SELECT}
    if search:
        # $search cannot be combined with $orderby or $skip (Graph rejects it).
        params["$search"] = '"' + search.replace('"', "") + '"'
    else:
        params["$orderby"] = "receivedDateTime desc"
        params["$skip"] = skip
    payload = await graph.get_json(
        f"/me/mailFolders/{segment(_folder(args))}/messages", params=params
    )
    items = [_message_summary(raw) for raw in payload.get("value", []) if isinstance(raw, dict)]
    return _page(payload, items, skip)


async def get_mail_message(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """One message with its body as plain text."""
    message_id = text_arg(args, "message_id", required=True)
    raw = await graph.send_json(
        "GET",
        f"/me/messages/{segment(message_id)}",
        params={"$select": _MAIL_SELECT + ",body"},
        headers=TEXT_BODY,
    )
    body = raw.get("body")
    text = str(body.get("content") or "") if isinstance(body, Mapping) else ""
    return {
        **_message_summary(raw),
        "cc": _framed(", ".join(_addresses(raw.get("ccRecipients")))),
        "body": _framed(text),
    }


async def list_mail_folders(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """The mailbox's top-level folders."""
    del args
    payload = await graph.get_json(
        "/me/mailFolders", params={"$top": 100, "$select": _FOLDER_SELECT}
    )
    folders = [
        {
            "id": raw.get("id"),
            "name": _framed(str(raw.get("displayName") or "")),
            "total": raw.get("totalItemCount"),
            "unread": raw.get("unreadItemCount"),
        }
        for raw in payload.get("value", [])
        if isinstance(raw, dict)
    ]
    return {"folders": folders}


async def send_mail(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """Send one plain-text message now (saved to Sent Items)."""
    message = {
        "subject": text_arg(args, "subject", required=True),
        "body": {"contentType": "Text", "content": text_arg(args, "body")},
        "toRecipients": _recipients(args, "to", required=True),
        "ccRecipients": _recipients(args, "cc", required=False),
    }
    await graph.send_json(
        "POST", "/me/sendMail", body={"message": message, "saveToSentItems": True}
    )
    return {"sent": True, "to": len(message["toRecipients"]), "cc": len(message["ccRecipients"])}


def _event(raw: Mapping[str, Any]) -> dict[str, Any]:
    location = raw.get("location")
    return {
        "id": raw.get("id"),
        "subject": _framed(str(raw.get("subject") or "")),
        "start": raw.get("start"),
        "end": raw.get("end"),
        "all_day": raw.get("isAllDay"),
        "location": _framed(
            str(location.get("displayName") or "") if isinstance(location, Mapping) else ""
        ),
        "organizer": _framed(_address(raw.get("organizer"))),
        "attendees": _framed(", ".join(_addresses(raw.get("attendees")))),
        "link": raw.get("webLink"),
    }


async def list_calendar_events(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """Events on the default calendar, most recently started first (recurrences unexpanded)."""
    top = int_arg(args, "top", default=25, ceiling=50)
    skip = int_arg(args, "skip", default=0, ceiling=100_000)
    payload = await graph.get_json(
        "/me/events",
        params={
            "$top": top,
            "$skip": skip,
            "$select": _EVENT_SELECT,
            "$orderby": "start/dateTime desc",
        },
    )
    items = [_event(raw) for raw in payload.get("value", []) if isinstance(raw, dict)]
    return _page(payload, items, skip)


def _when(args: Mapping[str, Any], name: str) -> str:
    value = text_arg(args, name, required=True)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"{name} must be an ISO 8601 date-time, e.g. 2026-10-05T09:00:00"
        ) from None
    return value


async def get_calendar_view(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """Every occurrence between two times, recurrences expanded."""
    top = int_arg(args, "top", default=50, ceiling=100)
    payload = await graph.get_json(
        "/me/calendarView",
        params={
            "startDateTime": _when(args, "start"),
            "endDateTime": _when(args, "end"),
            "$top": top,
            "$select": _EVENT_SELECT,
            "$orderby": "start/dateTime",
        },
    )
    items = [_event(raw) for raw in payload.get("value", []) if isinstance(raw, dict)]
    return {"items": items, "more": bool(payload.get("@odata.nextLink"))}


async def create_calendar_event(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """Create an event; Exchange emails an invitation to each attendee."""
    zone = text_arg(args, "time_zone") or "UTC"
    if not re.fullmatch(r"[A-Za-z0-9_+\-/ ]{1,64}", zone):
        raise ToolError("time_zone must be a time zone name such as UTC or Eastern Standard Time")
    event: dict[str, Any] = {
        "subject": text_arg(args, "subject", required=True),
        "start": {"dateTime": _when(args, "start"), "timeZone": zone},
        "end": {"dateTime": _when(args, "end"), "timeZone": zone},
        "body": {"contentType": "Text", "content": text_arg(args, "body")},
        "attendees": [
            {**recipient, "type": "required"}
            for recipient in _recipients(args, "attendees", required=False)
        ],
    }
    location = text_arg(args, "location")
    if location:
        event["location"] = {"displayName": location}
    created = await graph.send_json("POST", "/me/events", body=event)
    return {"created": True, "id": created.get("id"), "link": created.get("webLink")}


def _file(raw: Mapping[str, Any]) -> dict[str, Any]:
    file_info = raw.get("file")
    return {
        "id": raw.get("id"),
        "name": _framed(str(raw.get("name") or "")),
        "is_folder": "folder" in raw,
        "size": raw.get("size"),
        "mime_type": file_info.get("mimeType") if isinstance(file_info, Mapping) else None,
        "modified": raw.get("lastModifiedDateTime"),
        "link": raw.get("webUrl"),
    }


async def list_folder_files(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """The children of a OneDrive folder (``root`` by default)."""
    folder = text_arg(args, "folder") or "root"
    top = int_arg(args, "top", default=50, ceiling=200)
    path = (
        "/me/drive/root/children"
        if folder == "root"
        else f"/me/drive/items/{segment(folder)}/children"
    )
    payload = await graph.get_json(path, params={"$top": top, "$select": _FILE_SELECT})
    items = [_file(raw) for raw in payload.get("value", []) if isinstance(raw, dict)]
    return {"items": items, "more": bool(payload.get("@odata.nextLink"))}


async def get_onedrive_file(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """One file's details, and its text when it is a text file of at most 1 MiB."""
    file_id = text_arg(args, "file_id", required=True)
    raw = await graph.get_json(
        f"/me/drive/items/{segment(file_id)}", params={"$select": _FILE_SELECT}
    )
    described = _file(raw)
    mime = str(described.get("mime_type") or "")
    reported = raw.get("size")
    size = reported if isinstance(reported, int) else _MAX_TEXT_FILE_BYTES + 1
    if described["is_folder"] or not mime.startswith(_TEXT_TYPES) or size > _MAX_TEXT_FILE_BYTES:
        return {**described, "text": None, "note": "Only text files up to 1 MiB are read here."}
    response = await graph.request("GET", f"/me/drive/items/{segment(file_id)}/content")
    try:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_TEXT_FILE_BYTES:
                raise ToolError("the file is larger than 1 MiB")
    finally:
        await response.aclose()
    return {**described, "text": _framed(content.decode("utf-8", errors="replace"))}


async def search_onedrive_files(graph: GraphClient, args: Mapping[str, Any]) -> dict[str, Any]:
    """Search the user's OneDrive by file name and content."""
    query = text_arg(args, "query", required=True)
    top = int_arg(args, "top", default=25, ceiling=100)
    escaped = query.replace("'", "''")
    payload = await graph.get_json(
        f"/me/drive/root/search(q='{segment(escaped)}')",
        params={"$top": top, "$select": _FILE_SELECT},
    )
    items = [_file(raw) for raw in payload.get("value", []) if isinstance(raw, dict)]
    return {"items": items, "more": bool(payload.get("@odata.nextLink"))}


__all__ = [
    "create_calendar_event",
    "get_calendar_view",
    "get_mail_message",
    "get_onedrive_file",
    "list_calendar_events",
    "list_folder_files",
    "list_mail_folders",
    "list_mail_messages",
    "search_onedrive_files",
    "send_mail",
]
