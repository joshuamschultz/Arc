"""One async function per Gmail tool, over ``gmail.googleapis.com/gmail/v1/users/me``.

Every function takes the shared ``GmailContext`` and the tool's arguments and
returns plain JSON-able data; ``GoogleAttachment.invoke`` serialises it. Text the
mail's authors wrote (bodies, subjects, snippets) is framed as untrusted data
before it leaves here (LLM01).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from arcagent.extension.untrusted import frame_untrusted

from . import mime
from .http import (
    GoogleHttp,
    ToolError,
    drop_empty,
    encode_id,
    int_arg,
    list_arg,
    text_arg,
)

BASE: Final = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ATTACHMENT_BYTES: Final = 25 * 1024 * 1024
_SEARCH_CONCURRENCY: Final = 8
_REPLY_HEADERS: Final = ["Message-ID", "References", "Subject", "From", "To", "Cc", "Reply-To"]
_UNSAFE_FILE_NAME: Final = re.compile(r"[\x00-\x1f/\\]")


@dataclass(frozen=True)
class GmailContext:
    """What every Gmail tool needs: the HTTP helper, the account, the downloads folder."""

    http: GoogleHttp
    account: str
    download_dir: str


def _url(*segments: str) -> str:
    return "/".join([BASE, *segments])


def _framed(text: str) -> str:
    return frame_untrusted([("gmail", text)])


def _envelope(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The message in the shape the knowledge source reads (``_unwrap_message``)."""
    payload = raw.get("payload") or {}
    keys = ("id", "threadId", "labelIds", "snippet", "historyId", "internalDate")
    return {
        "message": {key: raw[key] for key in keys if key in raw},
        "headers": mime.message_headers(payload),
        "body": _framed(mime.message_body(payload)),
        "attachments": mime.message_attachments(payload),
    }


# --- labels -------------------------------------------------------------------


async def labels(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """List the account's labels."""
    return await ctx.http.request("GET", _url("labels"))


async def _label_index(ctx: GmailContext) -> dict[str, str]:
    """Casefolded id and name -> id, for every label on the account."""
    index: dict[str, str] = {}
    for label in (await labels(ctx, {})).get("labels", []):
        index[str(label["name"]).casefold()] = label["id"]
        index[str(label["id"]).casefold()] = label["id"]
    return index


async def resolve_labels(ctx: GmailContext, names: list[str]) -> list[str]:
    """Label ids for names or ids; an unknown label is a tool error."""
    if not names:
        return []
    index = await _label_index(ctx)
    missing = [name for name in names if name.casefold() not in index]
    if missing:
        raise ToolError(f"unknown Gmail label: {', '.join(missing)}")
    return [index[name.casefold()] for name in names]


async def label(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Read one label by id or name."""
    (label_id,) = await resolve_labels(ctx, [text_arg(args, "label", required=True)])
    return await ctx.http.request("GET", _url("labels", encode_id(label_id)))


# --- reading ------------------------------------------------------------------


async def _thread_summary(
    ctx: GmailContext, gate: asyncio.Semaphore, thread: Mapping[str, Any]
) -> str:
    async with gate:
        detail = await ctx.http.request(
            "GET",
            _url("threads", encode_id(thread["id"])),
            params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        )
    first = (detail.get("messages") or [{}])[0]
    headers = mime.message_headers(first.get("payload") or {})
    fields = [
        f"thread {thread['id']}",
        headers.get("date", ""),
        headers.get("from", ""),
        headers.get("subject", ""),
        str(thread.get("snippet", "")),
    ]
    return " | ".join(fields)


async def search(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Search by thread, then read each thread's sender, subject and date."""
    listing = await ctx.http.request(
        "GET",
        _url("threads"),
        params=drop_empty(
            {
                "q": text_arg(args, "query"),
                "maxResults": int_arg(args, "limit", default=20, ceiling=100),
                "pageToken": text_arg(args, "page_token"),
            }
        ),
    )
    threads = listing.get("threads") or []
    gate = asyncio.Semaphore(_SEARCH_CONCURRENCY)
    lines = await asyncio.gather(*(_thread_summary(ctx, gate, thread) for thread in threads))
    return {
        "threads": [{"id": thread["id"]} for thread in threads],
        "summary": _framed("\n".join(lines)) if lines else "",
        "nextPageToken": listing.get("nextPageToken", ""),
        "resultSizeEstimate": listing.get("resultSizeEstimate", 0),
    }


async def messages(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """List message ids for a query, a page at a time (Gmail's page maximum is 500)."""
    listing = await ctx.http.request(
        "GET",
        _url("messages"),
        params=drop_empty(
            {
                "q": text_arg(args, "query"),
                "maxResults": int_arg(args, "limit", default=100, ceiling=500),
                "pageToken": text_arg(args, "page_token"),
            }
        ),
    )
    listing.setdefault("messages", [])
    return listing


async def _get_message(ctx: GmailContext, message_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await ctx.http.request(
        "GET", _url("messages", encode_id(message_id)), params={"format": "full"}
    )
    return result


async def message(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Read one message as an envelope: headers, framed body, attachment list."""
    return _envelope(await _get_message(ctx, text_arg(args, "id", required=True)))


async def _get_thread(ctx: GmailContext, thread_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await ctx.http.request(
        "GET", _url("threads", encode_id(thread_id)), params={"format": "full"}
    )
    return result


async def thread(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Read a whole thread, one envelope per message."""
    raw = await _get_thread(ctx, text_arg(args, "thread_id", required=True))
    return {
        "thread": {"id": raw.get("id"), "historyId": raw.get("historyId")},
        "messages": [_envelope(item) for item in raw.get("messages") or []],
    }


async def thread_attachments(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """List every attachment in a thread without downloading any."""
    raw = await _get_thread(ctx, text_arg(args, "thread_id", required=True))
    found = [
        {"messageId": item["id"], **entry}
        for item in raw.get("messages") or []
        for entry in mime.message_attachments(item.get("payload") or {})
    ]
    return {"attachments": found}


async def history(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Mailbox changes since a history id. An aged-out id answers ``404 notFound``."""
    result = await ctx.http.request(
        "GET",
        _url("history"),
        params=drop_empty(
            {
                "startHistoryId": text_arg(args, "start_history_id", required=True),
                "maxResults": int_arg(args, "limit", default=100, ceiling=500),
                "pageToken": text_arg(args, "page_token"),
                "historyTypes": [
                    "messageAdded",
                    "messageDeleted",
                    "labelAdded",
                    "labelRemoved",
                ],
            }
        ),
    )
    result.setdefault("history", [])
    return result


# --- attachments --------------------------------------------------------------


def _checked_file_name(name: str) -> str:
    if (
        not name
        or name.startswith(".")
        or ".." in name
        or len(name) > 255
        or _UNSAFE_FILE_NAME.search(name)
    ):
        raise ToolError("file_name must be a plain file name (no folders, no leading dot)")
    return name


def _write_new_file(directory: str, name: str, data: bytes) -> str:
    """Create ``name`` in ``directory``; never follow a link, never overwrite."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ToolError("the downloads folder cannot be opened safely") from exc
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            file_fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError as exc:
            raise ToolError(f"{name} already exists in the downloads folder") from exc
        except OSError as exc:
            raise ToolError(f"{name} cannot be created in the downloads folder") from exc
        with os.fdopen(file_fd, "wb") as handle:
            handle.write(data)
    finally:
        os.close(dir_fd)
    return str(Path(directory) / name)


async def attachment(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Download one attachment into the downloads folder (at most 25 MiB)."""
    if not ctx.download_dir:
        raise ToolError("downloads are not enabled for this connection")
    name = _checked_file_name(text_arg(args, "file_name", required=True))
    data = await fetch_attachment(
        ctx,
        text_arg(args, "message_id", required=True),
        text_arg(args, "attachment_id", required=True),
    )
    path = await asyncio.to_thread(_write_new_file, ctx.download_dir, name, data)
    return {"path": path, "fileName": name, "bytes": len(data)}


async def fetch_attachment(ctx: GmailContext, message_id: str, attachment_id: str) -> bytes:
    """The decoded bytes of one attachment, refused above 25 MiB."""
    body = await ctx.http.request(
        "GET",
        _url("messages", encode_id(message_id), "attachments", encode_id(attachment_id)),
    )
    if int(body.get("size") or 0) > MAX_ATTACHMENT_BYTES:
        raise ToolError("the attachment is larger than 25 MB")
    data = mime.decode_base64url(body.get("data", ""))
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ToolError("the attachment is larger than 25 MB")
    return data


# --- drafts -------------------------------------------------------------------


async def drafts(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """List drafts a page at a time."""
    return await ctx.http.request(
        "GET",
        _url("drafts"),
        params=drop_empty(
            {
                "maxResults": int_arg(args, "limit", default=20, ceiling=100),
                "pageToken": text_arg(args, "page_token"),
            }
        ),
    )


async def _get_draft(ctx: GmailContext, draft_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await ctx.http.request(
        "GET", _url("drafts", encode_id(draft_id)), params={"format": "full"}
    )
    return result


async def draft_get(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Read one draft as an envelope."""
    raw = await _get_draft(ctx, text_arg(args, "draft_id", required=True))
    return {"id": raw.get("id"), "message": _envelope(raw.get("message") or {})}


async def _original_headers(ctx: GmailContext, message_id: str) -> dict[str, str]:
    """The reply-relevant headers of the message being answered."""
    raw = await ctx.http.request(
        "GET",
        _url("messages", encode_id(message_id)),
        params={"format": "metadata", "metadataHeaders": _REPLY_HEADERS},
    )
    return {"threadId": raw.get("threadId", ""), **mime.message_headers(raw.get("payload") or {})}


def _reply_headers(original: Mapping[str, str]) -> dict[str, str]:
    """In-Reply-To and References so the reply threads under the original."""
    message_id = original.get("message-id", "")
    references = " ".join(part for part in (original.get("references", ""), message_id) if part)
    return {"In-Reply-To": message_id, "References": references}


def _prefixed(prefix: str, subject: str) -> str:
    return subject if subject.casefold().startswith(prefix.casefold()) else f"{prefix} {subject}"


async def _compose(ctx: GmailContext, args: Mapping[str, Any]) -> tuple[str, str]:
    """``(raw, thread_id)`` for send and draft, threading under a replied-to message."""
    headers = {
        "To": text_arg(args, "to"),
        "Cc": text_arg(args, "cc"),
        "Bcc": text_arg(args, "bcc"),
        "Subject": text_arg(args, "subject"),
    }
    thread_id = text_arg(args, "thread_id")
    if reply_to := text_arg(args, "reply_to_message_id"):
        original = await _original_headers(ctx, reply_to)
        headers.update(_reply_headers(original))
        thread_id = thread_id or original["threadId"]
        if not headers["Subject"]:
            headers["Subject"] = _prefixed("Re:", original.get("subject", ""))
    return mime.build_raw_message(headers, text_arg(args, "body")), thread_id


def _message_resource(raw: str, thread_id: str) -> dict[str, str]:
    return drop_empty({"raw": raw, "threadId": thread_id})


def _require_recipient(args: Mapping[str, Any]) -> None:
    text_arg(args, "to", required=True)


async def draft(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Create a draft. Nothing is sent."""
    raw, thread_id = await _compose(ctx, args)
    return await ctx.http.request(
        "POST", _url("drafts"), body={"message": _message_resource(raw, thread_id)}
    )


async def _existing_fields(ctx: GmailContext, draft_id: str) -> dict[str, Any]:
    """The stored draft's recipients, subject and body, to fill what an update omits."""
    payload = (await _get_draft(ctx, draft_id)).get("message", {}).get("payload") or {}
    headers = mime.message_headers(payload)
    return {
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "body": mime.message_body(payload),
    }


async def draft_update(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Replace a draft; fields left out keep their stored value."""
    draft_id = text_arg(args, "draft_id", required=True)
    merged = await _existing_fields(ctx, draft_id)
    merged.update({key: args[key] for key in merged if args.get(key) is not None})
    raw, _ = await _compose(ctx, merged)
    return await ctx.http.request(
        "PUT",
        _url("drafts", encode_id(draft_id)),
        body={"id": draft_id, "message": {"raw": raw}},
    )


async def draft_delete(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Delete a draft for good."""
    draft_id = text_arg(args, "draft_id", required=True)
    await ctx.http.request("DELETE", _url("drafts", encode_id(draft_id)))
    return {"deleted": draft_id}


# --- labels on messages and threads -------------------------------------------


async def _modify(
    ctx: GmailContext, kind: str, item_id: str, add: list[str], remove: list[str]
) -> Any:
    if not add and not remove:
        raise ToolError("add or remove must name at least one label")
    body = {
        "addLabelIds": await resolve_labels(ctx, add),
        "removeLabelIds": await resolve_labels(ctx, remove),
    }
    return await ctx.http.request("POST", _url(kind, encode_id(item_id), "modify"), body=body)


async def modify(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Add or remove labels on one message."""
    return await _modify(
        ctx,
        "messages",
        text_arg(args, "message_id", required=True),
        list_arg(args, "add"),
        list_arg(args, "remove"),
    )


async def thread_modify(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Add or remove labels on every message of a thread."""
    return await _modify(
        ctx,
        "threads",
        text_arg(args, "thread_id", required=True),
        list_arg(args, "add"),
        list_arg(args, "remove"),
    )


async def mark_read(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Remove UNREAD."""
    return await _modify(
        ctx, "messages", text_arg(args, "message_id", required=True), [], ["UNREAD"]
    )


async def mark_unread(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Add UNREAD."""
    return await _modify(
        ctx, "messages", text_arg(args, "message_id", required=True), ["UNREAD"], []
    )


async def archive(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Remove INBOX."""
    return await _modify(
        ctx, "messages", text_arg(args, "message_id", required=True), [], ["INBOX"]
    )


async def trash(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Move a message to Trash."""
    message_id = text_arg(args, "message_id", required=True)
    return await ctx.http.request("POST", _url("messages", encode_id(message_id), "trash"))


async def untrash(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Take a message back out of Trash."""
    message_id = text_arg(args, "message_id", required=True)
    return await ctx.http.request("POST", _url("messages", encode_id(message_id), "untrash"))


# --- sending ------------------------------------------------------------------


async def _send_raw(ctx: GmailContext, raw: str, thread_id: str) -> Any:
    return await ctx.http.request(
        "POST", _url("messages", "send"), body=_message_resource(raw, thread_id)
    )


async def send(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Send a message now."""
    _require_recipient(args)
    raw, thread_id = await _compose(ctx, args)
    return await _send_raw(ctx, raw, thread_id)


async def draft_send(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Send an existing draft now."""
    draft_id = text_arg(args, "draft_id", required=True)
    return await ctx.http.request("POST", _url("drafts", "send"), body={"id": draft_id})


def _addresses(*values: str) -> list[str]:
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _without_account(account: str, addresses: list[str]) -> list[str]:
    own = account.casefold()
    return [item for item in addresses if own not in item.casefold()] if own else addresses


async def _reply(ctx: GmailContext, args: Mapping[str, Any], *, everyone: bool) -> Any:
    original = await _original_headers(ctx, text_arg(args, "message_id", required=True))
    sender = original.get("reply-to") or original.get("from", "")
    to = text_arg(args, "to") or sender
    cc = text_arg(args, "cc")
    if everyone:
        pool = _addresses(original.get("to", ""), original.get("cc", ""))
        cc = ", ".join(_without_account(ctx.account, pool))
    headers = {
        "To": to,
        "Cc": cc,
        "Subject": _prefixed("Re:", original.get("subject", "")),
        **_reply_headers(original),
    }
    raw = mime.build_raw_message(headers, text_arg(args, "body", required=True))
    return await _send_raw(ctx, raw, original["threadId"])


async def reply(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Reply to the sender, in the thread."""
    return await _reply(ctx, args, everyone=False)


async def reply_all(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Reply to everyone on the message, in the thread."""
    return await _reply(ctx, args, everyone=True)


async def _forwarded_files(
    ctx: GmailContext, message_id: str, payload: Mapping[str, Any]
) -> list[mime.OutgoingAttachment]:
    files: list[mime.OutgoingAttachment] = []
    total = 0
    for entry in mime.message_attachments(payload):
        data = await fetch_attachment(ctx, message_id, entry["attachmentId"])
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ToolError("the forwarded attachments are larger than 25 MB in total")
        files.append(mime.OutgoingAttachment(entry["filename"], entry["mimeType"], data))
    return files


def _forward_body(note: str, headers: Mapping[str, str], body: str) -> str:
    lines = [
        "---------- Forwarded message ---------",
        f"From: {headers.get('from', '')}",
        f"Date: {headers.get('date', '')}",
        f"Subject: {headers.get('subject', '')}",
        f"To: {headers.get('to', '')}",
        "",
        body,
    ]
    return "\n".join(([note, ""] if note else []) + lines)


async def forward(ctx: GmailContext, args: Mapping[str, Any]) -> Any:
    """Forward a message, with its attachments, to new recipients."""
    _require_recipient(args)
    message_id = text_arg(args, "message_id", required=True)
    original = await _get_message(ctx, message_id)
    payload = original.get("payload") or {}
    headers = mime.message_headers(payload)
    outgoing = {
        "To": text_arg(args, "to"),
        "Cc": text_arg(args, "cc"),
        "Bcc": text_arg(args, "bcc"),
        "Subject": _prefixed("Fwd:", headers.get("subject", "")),
    }
    body = _forward_body(text_arg(args, "note"), headers, mime.message_body(payload))
    files = await _forwarded_files(ctx, message_id, payload)
    return await _send_raw(ctx, mime.build_raw_message(outgoing, body, files), "")
