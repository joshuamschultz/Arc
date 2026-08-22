"""The native OAuth authorization-code exchange the harness runs at connect time.

Covers the two things only a live exchange exercises — the authorize URL a
provider will accept, and swapping a one-time code for a durable refresh token —
plus the failure the operator actually hits (a dead code), all against an
injected POST so no socket is opened.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from arcagent.core.tier import Tier
from arcagent.extension.manifest import OAuthFlow, load_manifest
from arcagent.extension.oauth import (
    OAuthExchangeError,
    build_authorize_url,
    exchange_authorization_code,
)

_FLOW = OAuthFlow(
    authorize_url="https://www.dropbox.com/oauth2/authorize",
    token_url="https://api.dropbox.com/oauth2/token",
    client_id_secret="app_key",
    client_secret_secret="app_secret",
    refresh_token_secret="refresh_token",
    authorize_params={"token_access_type": "offline"},
)


def test_authorize_url_asks_for_a_code_with_offline_access() -> None:
    url = build_authorize_url(_FLOW, client_id="app-key-123")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == _FLOW.authorize_url
    assert query["client_id"] == ["app-key-123"]
    assert query["response_type"] == ["code"], "only the authorization-code grant is built"
    assert query["token_access_type"] == ["offline"], "a durable refresh token needs offline"


async def test_a_code_is_swapped_for_a_refresh_token() -> None:
    calls: list[tuple[str, dict[str, str], tuple[str, str]]] = []

    async def post(url: str, data: dict[str, str], auth: tuple[str, str]) -> tuple[int, dict[str, Any]]:
        calls.append((url, data, auth))
        return 200, {"refresh_token": "rt-durable", "access_token": "at-short", "expires_in": 14400}

    tokens = await exchange_authorization_code(
        _FLOW, code="one-time-code", client_id="ak", client_secret="as", post=post
    )

    assert tokens.refresh_token == "rt-durable"
    url, data, auth = calls[0]
    assert url == _FLOW.token_url
    assert data == {"grant_type": "authorization_code", "code": "one-time-code"}
    assert auth == ("ak", "as"), "the app key/secret authenticate the exchange"


async def test_a_dead_code_is_a_terminal_error_not_a_retry() -> None:
    async def post(url: str, data: dict[str, str], auth: tuple[str, str]) -> tuple[int, dict[str, Any]]:
        return 400, {"error": "invalid_grant", "error_description": "code doesn't exist or has expired"}

    with pytest.raises(OAuthExchangeError) as exc:
        await exchange_authorization_code(
            _FLOW, code="expired", client_id="ak", client_secret="as", post=post
        )
    assert exc.value.terminal, "invalid_grant means authorize again, never retry the dead code"
    assert "invalid_grant" in str(exc.value)


async def test_offline_access_missing_yields_no_refresh_token_error() -> None:
    async def post(url: str, data: dict[str, str], auth: tuple[str, str]) -> tuple[int, dict[str, Any]]:
        return 200, {"access_token": "at-only", "expires_in": 14400}  # no refresh_token

    with pytest.raises(OAuthExchangeError) as exc:
        await exchange_authorization_code(
            _FLOW, code="c", client_id="ak", client_secret="as", post=post
        )
    assert exc.value.error_code == "no_refresh_token"
    assert "offline" in str(exc.value).lower()


def test_manifest_oauth_must_name_declared_secrets() -> None:
    toml = """
[extension]
name = "demo"
version = "1.0.0"
description = "d"
attachment = "native"

[config.native]
entrypoint = "demo_ext"

[[secrets]]
name = "app_key"

[[secrets]]
name = "app_secret"

[oauth]
authorize_url = "https://x/authorize"
token_url = "https://x/token"
client_id_secret = "app_key"
client_secret_secret = "app_secret"
refresh_token_secret = "refresh_token"
"""
    with pytest.raises(ValueError, match="refresh_token_secret"):
        load_manifest(toml, tier=Tier.PERSONAL)
