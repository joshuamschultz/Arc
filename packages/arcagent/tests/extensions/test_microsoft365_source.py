"""Microsoft 365 knowledge sources over a fake Microsoft Graph (P18-3.M).

REQ-423 (mail) / REQ-424 (OneDrive) / COMP-006. The bundle's own
``build_source_adapters`` builds both streams on the connection's credential and
cloud; only Graph's HTTP is fake. Each synced object carries a MONOTONIC
``metadata["revision"]`` (Graph returns mail newest-first), and a OneDrive object is
fetchable with the very version sync handed out.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from packages.arcagent.tests.microsoft_fakes import FakeGraph, StaticCredential

from arcagent.extension.authoring import assert_connector_contract
from arcagent.extension.source import FetchSourceObject, SyncSource

_EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"
sys.path.insert(0, str(_EXTENSIONS_ROOT / "microsoft365"))

import arc_ext_microsoft365.source as ms  # the bundle is placed on sys.path just above

_CONNECTION = "ms365-test"
_GRAPH = "https://graph.microsoft.com/v1.0"


def _drive(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path.removeprefix("/v1.0")
    files = [
        {
            "id": "f1",
            "name": "a.txt",
            "file": {"mimeType": "text/plain"},
            "eTag": '"etag1"',
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
        },
        {
            "id": "f2",
            "name": "b.txt",
            "file": {"mimeType": "text/plain"},
            "eTag": '"etag2"',
            "lastModifiedDateTime": "2026-02-02T00:00:00Z",
        },
    ]
    if path == "/me/drives":
        return httpx.Response(200, json={"value": [{"id": "d1", "name": "OneDrive"}]})
    if path == "/drives/d1/root/children":
        return httpx.Response(200, json={"value": []})
    if path == "/drives/d1/items/root/delta":
        link = f"{_GRAPH}/drives/d1/items/root/delta?token=1"
        return httpx.Response(200, json={"value": files, "@odata.deltaLink": link})
    if path == "/drives/d1/items/f1":
        return httpx.Response(200, json=files[0])
    if path == "/drives/d1/items/f1/content":
        return httpx.Response(200, content=b"hello", headers={"Content-Type": "text/plain"})
    return None


@pytest.fixture
def adapters() -> dict[str, Any]:
    graph = FakeGraph(None, upn="josh@agency.gov", static_token="graph-access-token")
    for index, key in enumerate(("m3", "m2", "m1")):
        graph.add_message(key, subject=key, body=f"body {key}", sender="ann@agency.gov")
        graph.messages[key]["lastModifiedDateTime"] = f"2026-0{3 - index}-01T00:00:00Z"
    mail = graph.transport()

    def handler(request: httpx.Request) -> httpx.Response:
        answer = _drive(request)
        return answer if answer is not None else mail.handle_request(request)

    return ms.build_source_adapters(
        {
            "credential": StaticCredential("graph-access-token"),
            "cloud": "global",
            "transport": httpx.MockTransport(handler),
        }
    )


async def test_outlook_source_meets_the_connector_contract(adapters: dict[str, Any]) -> None:
    await assert_connector_contract(adapters["outlook"], connection_id=_CONNECTION)


async def test_onedrive_synced_object_is_fetchable_with_its_reported_version(
    adapters: dict[str, Any],
) -> None:
    onedrive = adapters["onedrive"]
    await onedrive.list_source_resources(None)
    page = await onedrive.sync_source(SyncSource(connection_id=_CONNECTION))
    assert {obj.object_id for obj in page.objects} >= {"d1:f1", "d1:f2"}
    assert all(obj.metadata.get("revision") for obj in page.objects)

    obj = next(item for item in page.objects if item.object_id == "d1:f1")
    content = await onedrive.fetch_source(
        FetchSourceObject(
            connection_id=_CONNECTION, object_id=obj.object_id, version=obj.version or ""
        )
    )
    assert content.content == b"hello"
