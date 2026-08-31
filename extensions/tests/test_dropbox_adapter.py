"""What the Dropbox adapter puts on the wire, and what it does with a refresh token.

The conformance suite (``test_connector_bundles``) proves the manifest and the
adapter agree and that an unconfigured probe refuses by name. This file proves the
part only a live call exercises: that the adapter mints a short-lived access token
from the durable refresh token, caches it across calls, carries it as a bearer on
both Dropbox hosts, and reaches the files API for a read and a write.

httpx is mocked at the transport, so no socket is opened; every assertion is on
the request the shipped adapter actually built.
"""

from __future__ import annotations

import asyncio
import json
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
    SourceAdapter,
    SourceError,
    SourceFailureCode,
    SourceObjectKind,
    SyncSource,
)
from arcagent.modules.connectors.install import build_attachment

_BUNDLE = Path(__file__).resolve().parents[1] / "dropbox"
_TOKEN = "AT-live-xyz"


def _attachment() -> Any:
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(
        manifest,
        _BUNDLE,
        {
            "app_key": Secret("app-key-1234"),
            "app_secret": Secret("app-secret-5678"),
            "refresh_token": Secret("refresh-abcd"),
        },
    )
    return wrapper._delegate


class _Recorder:
    """Answers Dropbox's three hosts and remembers what each request carried."""

    def __init__(self) -> None:
        self.token_mints = 0
        self.list_body: dict[str, Any] | None = None
        self.upload_arg: str | None = None
        self.upload_body: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.dropbox.com/oauth2/token":
            self.token_mints += 1
            assert request.headers["authorization"].startswith("Basic "), "app auth missing"
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 14400})
        assert request.headers.get("authorization") == f"Bearer {_TOKEN}", url
        if url.endswith("/2/users/get_current_account"):
            return httpx.Response(200, json={"email": "josh@example.com"})
        if url.endswith("/2/files/list_folder"):
            self.list_body = json.loads(request.content)
            return httpx.Response(200, json={"entries": [{"name": "Arc"}]})
        if url.endswith("/2/files/upload"):
            self.upload_arg = request.headers.get("dropbox-api-arg")
            self.upload_body = request.content.decode("utf-8")
            return httpx.Response(200, json={"path_display": "/Notes/note.md"})
        return httpx.Response(404, json={"error": f"unmapped {url}"})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    rec = _Recorder()
    real: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(rec)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield rec


async def test_probe_mints_a_token_then_reads_the_account(recorder: _Recorder) -> None:
    result = await _attachment().probe()

    assert result.reachable
    assert "josh@example.com" in result.detail
    assert recorder.token_mints == 1


async def test_a_call_works_after_close_source_closed_the_client(recorder: _Recorder) -> None:
    """close_source() closes the shared httpx client (source-lifecycle teardown),
    but the SAME instance also serves the agent's interactive tools. A later call
    must recreate the client, not raise 'client has been closed'. Regression for a
    connection that probed green yet failed every tool call after a sync."""
    att = _attachment()
    assert (await att.probe()).reachable  # first use
    await att.close_source()  # a source operation releases the client
    # The connection must still be usable — the client is recreated on demand.
    assert (await att.probe()).reachable


async def test_a_malformed_refresh_token_is_named_not_hidden_behind_a_generic_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropbox's token endpoint answers 400 ``invalid_grant`` for a bad/malformed
    refresh token — the exact real failure. The probe must tell the operator to
    re-authorize, not surface the useless "answered 400, could not be checked".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "refresh token is malformed"},
            )
        return httpx.Response(500, json={"error": "token mint should have failed first"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    result = await _attachment().probe()

    assert not result.reachable
    detail = result.detail.lower()
    assert "refresh token" in detail and "authorize" in detail, result.detail


async def test_the_access_token_is_cached_across_calls(recorder: _Recorder) -> None:
    attachment = _attachment()

    await attachment.invoke("dropbox_account", {})
    await attachment.invoke("dropbox_list", {})

    assert recorder.token_mints == 1, "the refresh token was exchanged more than once"


async def test_list_reaches_list_folder_with_a_rooted_path(recorder: _Recorder) -> None:
    result = await _attachment().invoke("dropbox_list", {"path": "Meetings", "limit": "5"})

    assert result.outcome == "ok"
    assert recorder.list_body == {"path": "/Meetings", "recursive": False, "limit": 5}


async def test_upload_sends_content_to_the_content_host(recorder: _Recorder) -> None:
    result = await _attachment().invoke(
        "dropbox_upload", {"path": "/Notes/note.md", "content": "hello", "mode": "overwrite"}
    )

    assert result.outcome == "ok"
    assert recorder.upload_body == "hello"
    assert recorder.upload_arg is not None
    arg = json.loads(recorder.upload_arg)
    assert arg["path"] == "/Notes/note.md"
    assert arg["mode"] == "overwrite"


async def test_a_traversal_path_is_refused_before_any_request(recorder: _Recorder) -> None:
    result = await _attachment().invoke("dropbox_download", {"path": "/a/../../etc/passwd"})

    assert result.outcome == "error"
    assert "invalid Dropbox path" in result.content


def _mock_attachment(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> Any:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return _attachment()


async def test_source_sync_maps_initial_and_incremental_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 3600})
        if url.endswith("/2/files/list_folder"):
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            ".tag": "file",
                            "id": "id:one",
                            "path_display": "/one.pdf",
                            "rev": "r1",
                            "content_hash": "hash1",
                            "size": 12,
                        }
                    ],
                    "cursor": "c1",
                    "has_more": True,
                },
            )
        if url.endswith("/2/files/list_folder/continue"):
            assert json.loads(request.content) == {"cursor": "c1"}
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {".tag": "deleted", "path_lower": "/old.pdf"},
                        {
                            ".tag": "file",
                            "id": "id:one",
                            "path_display": "/moved.pdf",
                            "rev": "r2",
                        },
                    ],
                    "cursor": "c2",
                    "has_more": False,
                },
            )
        return httpx.Response(404)

    attachment = _mock_attachment(monkeypatch, handler)
    assert isinstance(attachment, SourceAdapter)
    first = await attachment.sync_source(SyncSource(connection_id="dbx:a", page_size=50))
    second = await attachment.sync_source(
        SyncSource(connection_id="dbx:a", checkpoint=first.next_checkpoint)
    )
    await attachment.close_source()

    assert first.has_more and first.next_checkpoint == "c1"
    assert first.objects[0].object_id == "id:one"
    assert first.objects[0].media_type == "application/pdf"
    assert second.next_checkpoint == "c2"
    assert second.objects[0].kind is SourceObjectKind.DELETED
    assert second.objects[1].object_id == "id:one"
    assert calls.count("https://api.dropbox.com/oauth2/token") == 1


async def test_binary_fetch_preserves_bytes_and_refuses_a_changed_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = b"%PDF-\x00\xffcontent"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 3600})
        return httpx.Response(
            200,
            headers={
                "Dropbox-API-Result": json.dumps(
                    {
                        "id": "id:pdf",
                        "rev": "r2",
                        "path_display": "/report.pdf",
                        "content_hash": "hash2",
                    }
                )
            },
            content=binary,
        )

    attachment = _mock_attachment(monkeypatch, handler)
    content = await attachment.fetch_source(
        FetchSourceObject(connection_id="dbx:a", object_id="id:pdf", version="r2", max_bytes=50)
    )
    with pytest.raises(SourceError) as changed:
        await attachment.fetch_source(
            FetchSourceObject(connection_id="dbx:a", object_id="id:pdf", version="r1")
        )
    await attachment.close_source()

    assert content.content == binary
    assert content.media_type == "application/pdf"
    assert changed.value.code is SourceFailureCode.VERSION_CHANGED


async def test_retry_is_bounded_and_a_401_refreshes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_mints = 0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, token_mints
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            token_mints += 1
            return httpx.Response(
                200, json={"access_token": f"token-{token_mints}", "expires_in": 3600}
            )
        calls += 1
        if calls == 1:
            return httpx.Response(401)
        if calls == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={"account_id": "dbid:a", "email": "a@example.com"},
        )

    attachment = _mock_attachment(monkeypatch, handler)
    description = await attachment.inspect_source(InspectSource(connection_id="dbx:a"))
    await attachment.close_source()

    assert description.account_id == "dbid:a"
    assert token_mints == 2
    assert calls == 3


async def test_checkpoint_reset_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 3600})
        return httpx.Response(409, json={"error_summary": "reset/invalid_cursor"})

    attachment = _mock_attachment(monkeypatch, handler)
    with pytest.raises(SourceError) as raised:
        await attachment.sync_source(SyncSource(connection_id="dbx:a", checkpoint="stale"))
    await attachment.close_source()
    assert raised.value.code is SourceFailureCode.CHECKPOINT_INVALID


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, SourceFailureCode.RATE_LIMITED), (503, SourceFailureCode.TRANSIENT)],
)
async def test_retry_budget_is_bounded(
    monkeypatch: pytest.MonkeyPatch, status: int, code: SourceFailureCode
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 3600})
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "0"})

    attachment = _mock_attachment(monkeypatch, handler)
    with pytest.raises(SourceError) as raised:
        await attachment.inspect_source(InspectSource(connection_id="dbx:a"))
    await attachment.close_source()

    assert raised.value.code is code
    assert calls == 3


async def test_binary_fetch_enforces_the_byte_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            return httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 3600})
        return httpx.Response(
            200,
            headers={
                "Dropbox-API-Result": json.dumps(
                    {"id": "id:large", "rev": "r1", "path_display": "/large.bin"}
                )
            },
            content=b"0123456789",
        )

    attachment = _mock_attachment(monkeypatch, handler)
    with pytest.raises(SourceError) as raised:
        await attachment.fetch_source(
            FetchSourceObject(
                connection_id="dbx:a", object_id="id:large", version="r1", max_bytes=5
            )
        )
    await attachment.close_source()
    assert raised.value.code is SourceFailureCode.TOO_LARGE


async def test_concurrent_connections_and_token_mints_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mints: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.dropbox.com/oauth2/token":
            body = request.content.decode()
            account = "a" if "refresh-abcd" in body else "b"
            mints[account] = mints.get(account, 0) + 1
            return httpx.Response(
                200, json={"access_token": f"token-{account}", "expires_in": 3600}
            )
        token = request.headers["authorization"].removeprefix("Bearer token-")
        return httpx.Response(
            200, json={"account_id": f"dbid:{token}", "email": f"{token}@example.com"}
        )

    first = _mock_attachment(monkeypatch, handler)
    second = _attachment()
    second._refresh_token = "refresh-other"
    results = await asyncio.gather(
        first.inspect_source(InspectSource(connection_id="dbx:a")),
        first.inspect_source(InspectSource(connection_id="dbx:a")),
        second.inspect_source(InspectSource(connection_id="dbx:b")),
    )
    await first.close_source()
    await second.close_source()

    assert [result.account_id for result in results] == ["dbid:a", "dbid:a", "dbid:b"]
    assert mints == {"a": 1, "b": 1}
