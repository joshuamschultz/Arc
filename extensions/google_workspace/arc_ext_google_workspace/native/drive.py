"""Drive listing over ``www.googleapis.com/drive/v3`` (read only)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .http import GoogleHttp, int_arg, text_arg

BASE: Final = "https://www.googleapis.com/drive/v3"
_FIELDS: Final = "files(id,name,mimeType,modifiedTime,size,webViewLink),nextPageToken"


def _quoted(value: str) -> str:
    """A Drive query string literal: backslash and quote escaped."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _query(args: Mapping[str, Any]) -> str:
    user_filter = text_arg(args, "query")
    parent = text_arg(args, "parent")
    clauses = ["trashed = false"]
    if parent:
        clauses.append(f"{_quoted(parent)} in parents")
    elif not user_filter:
        clauses.append("'root' in parents")
    if user_filter:
        clauses.append(f"({user_filter})")
    return " and ".join(clauses)


async def drive_list(http: GoogleHttp, args: Mapping[str, Any]) -> Any:
    """List or filter files, inside one folder when ``parent`` is given."""
    listing = await http.request(
        "GET",
        f"{BASE}/files",
        params={
            "q": _query(args),
            "pageSize": int_arg(args, "max", default=25, ceiling=100),
            "fields": _FIELDS,
        },
    )
    listing.setdefault("files", [])
    return listing
