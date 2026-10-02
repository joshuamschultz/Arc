"""P18-2 — the dropbox attachment's side of the access-token handle contract.

Arc owns renewal. The attachment's whole duty is to ask for a bearer per request
and to tell the handle when the provider refused it, so the next ask renews.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.source import InspectSource
from arcagent.modules.connectors.install import build_attachment

from extensions.tests.fake_credential import FakeCredentialHandle

_BUNDLE = Path(__file__).resolve().parents[1] / "dropbox"


async def test_401_invalidates_and_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider 401 costs exactly one invalidate and one retry, then succeeds."""
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        if len(seen_tokens) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"account_id": "dbid:a", "email": "a@example.com"})

    real: Callable[..., httpx.AsyncClient] = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    handle = FakeCredentialHandle(bearer_values=["stale", "fresh"])
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(
        manifest,
        _BUNDLE,
        {},
        credential=handle,  # type: ignore[arg-type]  # structural stand-in for AccessTokenHandle
    )
    attachment = wrapper._delegate

    description = await attachment.inspect_source(InspectSource(connection_id="dbx:a"))
    await attachment.close_source()

    assert description.account_id == "dbid:a"
    assert handle.invalidations == 1
    assert handle.bearer_calls == 2
    assert seen_tokens == ["Bearer stale", "Bearer fresh"]
