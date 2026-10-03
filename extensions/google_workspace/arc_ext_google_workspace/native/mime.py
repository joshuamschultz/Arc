"""Gmail message bodies in, RFC 5322 messages out.

Decoding walks the ``payload`` part tree Gmail returns: it prefers ``text/plain``,
falls back to ``text/html`` stripped to text, and lists every part that carries an
``attachmentId``. Building uses ``email.message.EmailMessage`` and refuses any
header value that holds a CR or LF, so a recipient or subject can never smuggle in
a second header (a hidden Bcc, say).
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

from .http import ToolError

_BLOCK_TAGS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})
_SKIPPED_TAGS = frozenset({"script", "style", "head"})


@dataclass(frozen=True)
class OutgoingAttachment:
    """One file to attach to an outgoing message."""

    filename: str
    mime_type: str
    data: bytes


def decode_base64url(data: str) -> bytes:
    """Gmail's base64url, with or without padding."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# --- decoding -----------------------------------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (line.strip() for line in "".join(self._chunks).splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(markup: str) -> str:
    """Tags stripped, scripts and styles dropped, blocks kept on their own lines."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    return extractor.text()


def _walk(part: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _part_text(part: Mapping[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    return decode_base64url(data).decode("utf-8", errors="replace")


def message_headers(payload: Mapping[str, Any]) -> dict[str, str]:
    """Header values by lowercased name; the first of a repeated header wins."""
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        headers.setdefault(str(header.get("name", "")).lower(), str(header.get("value", "")))
    return headers


def message_body(payload: Mapping[str, Any]) -> str:
    """The readable body: plain text when present, else the HTML as text."""
    plain: list[str] = []
    html: list[str] = []
    for part in _walk(payload):
        if part.get("filename"):
            continue
        mime = str(part.get("mimeType", ""))
        if mime == "text/plain":
            plain.append(_part_text(part))
        elif mime == "text/html":
            html.append(_part_text(part))
    if any(chunk.strip() for chunk in plain):
        return "\n".join(plain).strip()
    return html_to_text("\n".join(html))


def message_attachments(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every part that can be downloaded on its own."""
    found: list[dict[str, Any]] = []
    for part in _walk(payload):
        body = part.get("body") or {}
        if part.get("filename") and body.get("attachmentId"):
            found.append(
                {
                    "filename": part["filename"],
                    "mimeType": part.get("mimeType", ""),
                    "size": body.get("size", 0),
                    "attachmentId": body["attachmentId"],
                }
            )
    return found


# --- building -----------------------------------------------------------------


def check_header(name: str, value: str) -> str:
    """The value unchanged, or a tool error when it could carry a second header."""
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ToolError(f"{name} must not contain line breaks")
    return value


def _attach(message: EmailMessage, item: OutgoingAttachment) -> None:
    maintype, _, subtype = item.mime_type.partition("/")
    if not subtype:
        guessed = mimetypes.guess_type(item.filename)[0] or "application/octet-stream"
        maintype, _, subtype = guessed.partition("/")
    message.add_attachment(
        item.data,
        maintype=maintype,
        subtype=subtype,
        filename=check_header("filename", item.filename),
    )


def build_raw_message(
    headers: Mapping[str, str],
    body: str,
    attachments: list[OutgoingAttachment] | None = None,
) -> str:
    """A base64url RFC 5322 message ready for Gmail's ``raw`` field."""
    message = EmailMessage()
    try:
        for name, value in headers.items():
            if value:
                message[name] = check_header(name, value)
        message.set_content(body)
        for item in attachments or []:
            _attach(message, item)
    except ValueError as exc:
        raise ToolError(f"the message could not be built: {exc}") from exc
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
