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

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors.install import build_attachment

_BUNDLE = Path(__file__).resolve().parents[1] / "dropbox"
_TOKEN = "AT-live-xyz"


def _attachment() -> Any:
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    return build_attachment(
        manifest,
        _BUNDLE,
        {
            "app_key": Secret("app-key-1234"),
            "app_secret": Secret("app-secret-5678"),
            "refresh_token": Secret("refresh-abcd"),
        },
    )


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
