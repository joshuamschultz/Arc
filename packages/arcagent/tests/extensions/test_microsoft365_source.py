"""SPEC-082 T-1101 (RED) — Microsoft 365 source indexing over a fake MCP server.

REQ-423 (mail) / REQ-424 (OneDrive) / COMP-006. Against a fake ``ms-365-mcp-server``
(the MCP transport is stubbed with scripted tool replies), the Microsoft source
adapters must list and sync mail and OneDrive objects, and each synced object must
carry a MONOTONIC ``metadata["revision"]`` so ArcMemory keeps re-indexing changes —
the connector-authoring contract from :mod:`arcagent.extension.authoring`.

``source.py`` already *computes* a revision, so this drives the end-to-end
sync→object / sync→fetch paths that are currently untested and pins the two clauses
that genuinely fail today (T-1102 fixes both in ``source.py``):

- **PRIMARY RED — monotonic-revision clause (Outlook).** Microsoft Graph returns
  mail newest-first by default. ``OutlookSourceAdapter`` maps each message's
  ``lastModifiedDateTime`` straight to ``metadata["revision"]`` with no ordering
  guarantee, so one page carries a *decreasing* revision and ArcMemory silently
  stops re-indexing. ``assert_connector_contract`` refuses it.
- **SECONDARY RED — sync→fetch round trip (OneDrive).** A synced OneDrive object
  reports ``version`` = its eTag stripped of quotes, but ``fetch_source`` compares
  against the UNSTRIPPED eTag, so an object can never be fetched with the very
  version sync just handed out — every fetch raises ``VERSION_CHANGED``.

The OneDrive listing/sync half already works; the ``*_lists_files`` test is a green
anchor that scopes the two REDs to precise defects rather than "nothing runs".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from arcagent.extension.authoring import assert_connector_contract
from arcagent.extension.mcp_attachment import McpAttachment, McpResilience
from arcagent.extension.source import FetchSourceObject, SyncSource

_EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"
sys.path.insert(0, str(_EXTENSIONS_ROOT / "microsoft365"))

import arc_ext_microsoft365.source as ms  # the bundle is placed on sys.path just above

_CONNECTION = "ms365-test"

#: Microsoft Graph returns mail newest-first (receivedDateTime desc) by default, so
#: the revisions this yields are strictly *decreasing* across the page.
_MAIL_NEWEST_FIRST = {
    "value": [
        {"id": "m3", "lastModifiedDateTime": "2026-03-03T00:00:00Z", "conversationId": "c3",
         "bodyPreview": "three"},
        {"id": "m2", "lastModifiedDateTime": "2026-02-02T00:00:00Z", "conversationId": "c2",
         "bodyPreview": "two"},
        {"id": "m1", "lastModifiedDateTime": "2026-01-01T00:00:00Z", "conversationId": "c1",
         "bodyPreview": "one"},
    ]
}

#: OneDrive files, whose Graph eTags are quoted strings (as real eTags always are).
_FILES = {
    "value": [
        {"id": "f1", "name": "a.txt", "file": {"mimeType": "text/plain"},
         "eTag": '"etag1"', "lastModifiedDateTime": "2026-01-01T00:00:00Z"},
        {"id": "f2", "name": "b.txt", "file": {"mimeType": "text/plain"},
         "eTag": '"etag2"', "lastModifiedDateTime": "2026-02-02T00:00:00Z"},
    ]
}

_REPLIES: dict[str, Any] = {
    "list-mail-messages": _MAIL_NEWEST_FIRST,
    "list-mail-folders": {"value": [{"id": "inbox", "displayName": "Inbox"}]},
    "list-folder-files": _FILES,
    "get-onedrive-file": {"id": "f1", "eTag": '"etag1"', "content": "hello", "mimeType": "text/plain"},
    "get-mail-message": {"id": "m1", "changeKey": "", "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                         "bodyPreview": "one"},
}


class _FakeMs365Server:
    """Stubs the ms-365-mcp-server transport with scripted tool replies."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self._replies = replies

    def requirements(self) -> list[Any]:
        return []

    async def send(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if message["method"] == "tools/call":
            name = message["params"]["name"]
            payload = self._replies.get(name, {"value": []})
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "isError": False,
                },
            }
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"resultType": "complete"}}

    async def close(self) -> None:
        return None


def _adapters() -> dict[str, Any]:
    """The Microsoft source adapters, wired to the fake broker exactly as the loader does."""
    attachment = McpAttachment(
        _FakeMs365Server(_REPLIES), resilience=McpResilience(backoff_seconds=0.0)
    )
    return ms.build_source_adapters({"attachment": attachment})


async def test_outlook_source_emits_monotonic_revision_on_realistic_mail_order() -> None:
    """PRIMARY RED (monotonic-revision clause): newest-first mail yields a decreasing
    revision, which stops ArcMemory re-indexing. The authoring contract refuses it."""
    outlook = _adapters()["outlook"]

    await assert_connector_contract(outlook, connection_id=_CONNECTION)


async def test_onedrive_synced_object_is_fetchable_with_its_reported_version() -> None:
    """SECONDARY RED (sync→fetch): the version sync reports (eTag, quotes stripped) does
    not match what fetch compares against (raw eTag), so fetch raises VERSION_CHANGED."""
    onedrive = _adapters()["onedrive"]

    page = await onedrive.sync_source(SyncSource(connection_id=_CONNECTION))
    assert page.objects, "OneDrive sync must surface file objects"
    obj = page.objects[0]

    content = await onedrive.fetch_source(
        FetchSourceObject(connection_id=_CONNECTION, object_id=obj.object_id, version=obj.version or "")
    )

    assert content.object_id == obj.object_id


async def test_onedrive_source_lists_files_each_with_a_revision() -> None:
    """Green anchor: the OneDrive sync path already surfaces file objects each carrying
    a revision — this half is wired today; the two REDs above are the gaps T-1102 closes."""
    onedrive = _adapters()["onedrive"]

    page = await onedrive.sync_source(SyncSource(connection_id=_CONNECTION))

    assert {obj.object_id for obj in page.objects} >= {"f1", "f2"}
    assert all(obj.metadata.get("revision") for obj in page.objects)
