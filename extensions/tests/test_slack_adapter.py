"""What the Slack adapter puts on the wire, and how it turns a conversation into
a Knowledge document.

The conformance suite (``test_connector_bundles``) proves the manifest and the
adapter agree and that an unconfigured probe refuses by name. This file proves the
part only a live call exercises: the bearer user token on every request, an
``ok:false`` refusal returned as an answer (not raised), the read verbs, and the
source seams that make a channel searchable.

httpx is mocked at the transport, so no socket is opened; every assertion is on
the request the shipped adapter actually built.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceAdapter,
    SyncSource,
)
from arcagent.modules.connectors.install import build_attachment

_BUNDLE = Path(__file__).resolve().parents[1] / "slack"
_TOKEN = "xoxp-live-token"


def _attachment() -> Any:
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(manifest, _BUNDLE, {"user_token": Secret(_TOKEN)})
    return wrapper._delegate


def _params(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))


class _Recorder:
    """Answers Slack Web API methods and remembers what each request carried."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.last: dict[str, dict[str, str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        self.calls.append(method)
        assert request.headers.get("authorization") == f"Bearer {_TOKEN}", method
        self.last[method] = _params(request)
        if method == "auth.test":
            return httpx.Response(
                200, json={"ok": True, "user": "josh", "team": "Acme", "team_id": "T123"}
            )
        if method == "conversations.list":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C1", "name": "deals", "is_private": False},
                        {"id": "D1", "is_im": True, "user": "U9"},
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if method == "conversations.history":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": "200.0", "user": "U1", "text": "second"},
                        {"ts": "100.0", "user": "U2", "text": "first"},
                    ],
                },
            )
        if method == "conversations.replies":
            return httpx.Response(200, json={"ok": True, "messages": [{"ts": "100.0"}]})
        if method == "users.info":
            uid = _params(request)["user"]
            return httpx.Response(200, json={"ok": True, "user": {"real_name": f"User {uid}"}})
        if method == "search.messages":
            return httpx.Response(200, json={"ok": True, "messages": {"matches": []}})
        if method == "chat.postMessage":
            return httpx.Response(200, json={"ok": True, "ts": "300.0"})
        return httpx.Response(200, json={"ok": False, "error": f"unmapped_{method}"})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    rec = _Recorder()
    real: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(rec)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield rec


async def test_probe_reaches_auth_test_as_the_user(recorder: _Recorder) -> None:
    result = await _attachment().probe()
    assert result.reachable
    assert "josh" in result.detail and "Acme" in result.detail
    assert recorder.calls == ["auth.test"]


async def test_a_revoked_token_is_named_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    result = await _attachment().probe()
    assert not result.reachable
    assert "invalid_auth" in result.detail


async def test_list_channels_reaches_conversations_list(recorder: _Recorder) -> None:
    result = await _attachment().invoke("slack_list_channels", {"limit": "5"})
    assert result.outcome == "ok"
    assert recorder.last["conversations.list"]["limit"] == "5"
    assert "public_channel" in recorder.last["conversations.list"]["types"]


async def test_search_passes_the_query(recorder: _Recorder) -> None:
    result = await _attachment().invoke("slack_search", {"query": "budget", "limit": "3"})
    assert result.outcome == "ok"
    assert recorder.last["search.messages"] == {"query": "budget", "count": "3"}


async def test_send_message_posts_channel_and_text(recorder: _Recorder) -> None:
    result = await _attachment().invoke(
        "slack_send_message", {"channel": "C1", "text": "hello team"}
    )
    assert result.outcome == "ok"
    assert recorder.last["chat.postMessage"] == {"channel": "C1", "text": "hello team"}


async def test_an_ok_false_refusal_is_an_error_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error": "missing_scope", "needed": "search:read"}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    result = await _attachment().invoke("slack_search", {"query": "x"})
    assert result.outcome == "error"
    assert "missing_scope" in result.content and "search:read" in result.content


async def test_source_seams_make_a_channel_a_document(recorder: _Recorder) -> None:
    a = _attachment()
    assert isinstance(a, SourceAdapter)

    desc = await a.inspect_source(InspectSource(connection_id="c"))
    assert desc.source_kind == "slack" and desc.account_id == "T123"

    resources = await a.list_source_resources(ListSourceResources(connection_id="c"))
    assert {r.resource_id for r in resources} == {"C1", "D1"}

    await a.select_source_resources(SelectSourceResources(connection_id="c", resource_ids=("C1",)))
    page = await a.sync_source(SyncSource(connection_id="c"))
    assert len(page.objects) == 1
    obj = page.objects[0]
    assert obj.object_id == "channel:C1" and obj.version == "200.0"

    content = await a.fetch_source(
        FetchSourceObject(connection_id="c", object_id="channel:C1", version="200.0")
    )
    text = content.content.decode("utf-8")
    assert "first" in text and "second" in text  # rendered oldest-first
    assert content.version == "200.0"
    await a.close_source()
