"""P18-3 §8.1 — the OAuth security core: state, session binding, PKCE, callback, id_token."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.credentials import CredentialRenewalError, RefreshRequest
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.oauth import (
    ACCOUNT_MISMATCH,
    OAUTH_BUSY,
    OAUTH_CALLBACK_INVALID,
    OAUTH_DECLINED,
    OAUTH_PKCE_REQUIRED,
    OAUTH_STATE_INVALID,
    OAuthExchangeError,
    OAuthPendingLedger,
    PendingAuthorization,
    TokenPost,
    build_authorize_url,
    checked_callback,
    declined,
    exchange_authorization_code,
    missing_scopes,
    new_pkce,
    new_state,
    refresh_access_token,
    require_account,
    revoke,
    s256_challenge,
    scopes_for,
    verified_id_token_email,
)
from arcagent.extension.secrets import Secret

REDIRECT = "http://127.0.0.1:8420/oauth/callback"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]


def _flow(**overrides: Any) -> OAuthFlow:
    values: dict[str, Any] = {
        "provider": "google",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "refresh_token_secret": "refresh_token",
        "scopes": ["openid", "email", "https://www.googleapis.com/auth/gmail.modify"],
        "scopes_read_only": ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"],
        "client_auth": "post_form",
        "account": "openid_email",
        "id_token_issuers": GOOGLE_ISSUERS,
        "authorize_params": {"access_type": "offline", "prompt": "consent"},
        "revoke_url": "https://oauth2.googleapis.com/revoke",
    }
    values.update(overrides)
    return OAuthFlow(**values)


def _pending(state: str, session: str = "sess-A", *, at: float = 0.0) -> PendingAuthorization:
    return PendingAuthorization(
        state=state,
        instance="blackarc",
        session_id=session,
        code_verifier=Secret("v" * 64),
        redirect_uri=REDIRECT,
        created_at=at,
    )


def _id_token(**claims: Any) -> str:
    base = {
        "iss": "https://accounts.google.com",
        "aud": "client-123",
        "email": "Josh@BlackArcIndustrial.com",
        "email_verified": True,
        "exp": time.time() + 3600,
    }
    base.update(claims)
    payload = base64.urlsafe_b64encode(json.dumps(base).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"


# --- begin -------------------------------------------------------------------------


def test_begin_builds_url_with_pkce_state_and_configured_redirect() -> None:
    flow = _flow()
    pkce = new_pkce()
    state = new_state()
    url = build_authorize_url(
        flow,
        client_id="client-123",
        redirect_uri=REDIRECT,
        state=state,
        code_challenge=pkce.challenge,
        scopes=scopes_for(flow, read_only=True),
        login_hint="josh@blackarcindustrial.com",
    )
    query = parse_qs(urlsplit(url).query)
    assert len(state) >= 43
    assert query["state"] == [state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [s256_challenge(pkce.verifier.reveal())]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["scope"] == ["openid email https://www.googleapis.com/auth/gmail.readonly"]
    assert query["login_hint"] == ["josh@blackarcindustrial.com"]
    assert query["access_type"] == ["offline"]
    assert pkce.verifier.reveal() not in url, "the verifier never leaves the server"
    assert len(pkce.verifier.reveal()) == 64


def test_read_write_connection_asks_for_the_full_scopes() -> None:
    assert "https://www.googleapis.com/auth/gmail.modify" in scopes_for(_flow(), read_only=False)


def test_pkce_downgrade_is_refused() -> None:
    with pytest.raises(ExtensionError) as caught:
        build_authorize_url(
            _flow(),
            client_id="c",
            redirect_uri=REDIRECT,
            state="s",
            code_challenge=None,
            scopes=[],
        )
    assert caught.value.code == OAUTH_PKCE_REQUIRED


def test_manifest_cannot_override_flow_owned_parameters() -> None:
    for name in ("redirect_uri", "state", "code_challenge_method", "client_id"):
        with pytest.raises(ValueError):
            _flow(authorize_params={name: "x"})


def test_manifest_endpoints_must_be_https_without_userinfo() -> None:
    for url in ("http://oauth2.googleapis.com/token", "https://u:p@oauth2.googleapis.com/token"):
        with pytest.raises(ValueError):
            _flow(token_url=url)


# --- the ledger -------------------------------------------------------------------


def test_complete_is_single_use_and_session_bound() -> None:
    ledger = OAuthPendingLedger()
    ledger.admit(_pending("state-1", "sess-A", at=time.monotonic()))
    with pytest.raises(ExtensionError) as wrong:
        ledger.take("state-1", session_id="sess-B")
    assert wrong.value.code == OAUTH_STATE_INVALID
    assert len(ledger) == 1, "a wrong session must not burn the operator's sign-in"
    assert ledger.take("state-1", session_id="sess-A").instance == "blackarc"
    with pytest.raises(ExtensionError) as replay:
        ledger.take("state-1", session_id="sess-A")
    assert replay.value.code == OAUTH_STATE_INVALID


def test_forged_and_empty_state_or_session_are_refused() -> None:
    ledger = OAuthPendingLedger()
    ledger.admit(_pending("state-1"))
    for state, session in (("forged", "sess-A"), ("", "sess-A"), ("state-1", "")):
        with pytest.raises(ExtensionError) as caught:
            ledger.take(state, session_id=session)
        assert caught.value.code == OAUTH_STATE_INVALID


def test_pending_expires_after_ten_minutes_and_caps_at_sixteen() -> None:
    now = [1000.0]
    ledger = OAuthPendingLedger(clock=lambda: now[0])
    ledger.admit(_pending("old", at=now[0]))
    now[0] += 601
    with pytest.raises(ExtensionError):
        ledger.take("old", session_id="sess-A")
    for index in range(16):
        ledger.admit(_pending(f"s{index}", at=now[0]))
    with pytest.raises(ExtensionError) as full:
        ledger.admit(_pending("one-too-many", at=now[0]))
    assert full.value.code == OAUTH_BUSY
    now[0] += 601
    ledger.admit(_pending("after-expiry", at=now[0]))


# --- the callback -------------------------------------------------------------------


def test_checked_callback_accepts_exactly_our_address() -> None:
    params = checked_callback(
        f"{REDIRECT}?state=abc&code=4/0Ab&scope=email+openid&authuser=0&prompt=consent",
        expected_redirect_uri=REDIRECT,
    )
    assert params.state == "abc"
    assert params.code is not None and params.code.reveal() == "4/0Ab"


_BAD_CALLBACKS = [
    "http://evil:8420/oauth/callback?state=abc&code=SECRETCODE",
    "http://127.0.0.1:9999/oauth/callback?state=abc&code=SECRETCODE",
    "http://127.0.0.1:8420/other?state=abc&code=SECRETCODE",
    "https://127.0.0.1:8420/oauth/callback?state=abc&code=SECRETCODE",
    "http://127.0.0.1:8420/oauth/callback?state=abc&code=SECRETCODE&code=SECRETCODE2",
    "http://127.0.0.1:8420/oauth/callback?state=abc&state=x&code=SECRETCODE",
    "http://127.0.0.1:8420/oauth/callback?state=abc&code=SECRET%0ACODE",
    "http://127.0.0.1:8420/oauth/callback?state=abc&code=SECRETCODE&next=http://evil",
    "http://127.0.0.1:8420/oauth/callback?state=abc&code=SECRETCODE#frag",
    "http://u:p@127.0.0.1:8420/oauth/callback?state=abc&code=SECRETCODE",
    "http://127.0.0.1:8420/oauth/callback?code=SECRETCODE",
    "http://127.0.0.1:8420/oauth/callback?state=abc",
    "http://127.0.0.1:8420/oauth/callback?state=abc&code=SECRETCODE" + "&scope=" + "a" * 5120,
    "",
]


@pytest.mark.parametrize("pasted", _BAD_CALLBACKS)
def test_checked_callback_rejects_other_host_port_path_and_duplicates(pasted: str) -> None:
    with pytest.raises(ExtensionError) as caught:
        checked_callback(pasted, expected_redirect_uri=REDIRECT)
    assert caught.value.code == OAUTH_CALLBACK_INVALID
    assert "SECRETCODE" not in caught.value.message
    assert "evil" not in caught.value.message


def test_provider_error_param_maps_to_declined() -> None:
    params = checked_callback(
        f"{REDIRECT}?error=access_denied&state=abc", expected_redirect_uri=REDIRECT
    )
    assert params.code is None and params.error == "access_denied"
    refusal = declined(params.error)
    assert refusal.code == OAUTH_DECLINED
    assert refusal.message.startswith("You declined access")
    other = declined("<script>alert(1)</script>")
    assert "<script>" not in other.message


# --- token endpoint ---------------------------------------------------------------------


def _capture(status: int, body: dict[str, Any]) -> tuple[list[TokenPost], Any]:
    seen: list[TokenPost] = []

    async def post(request: TokenPost) -> tuple[int, dict[str, Any]]:
        seen.append(request)
        return status, dict(body)

    return seen, post


_GRANT = {
    "refresh_token": "rt-1",
    "access_token": "at-1",
    "expires_in": 3599,
    "scope": "openid email",
}


@pytest.mark.parametrize("mode", ["basic", "post_form", "post_json"])
async def test_client_auth_modes_put_client_credentials_in_the_right_place(mode: str) -> None:
    seen, post = _capture(200, _GRANT)
    flow = _flow(client_auth=mode)
    await exchange_authorization_code(
        flow,
        code=Secret("the-code"),
        client_id="cid",
        client_secret=Secret("csecret"),
        redirect_uri=REDIRECT,
        code_verifier=Secret("verifier"),
        post=post,
    )
    request = seen[0]
    body = request.form if request.form is not None else request.json_body
    assert body is not None
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "the-code"
    assert body["redirect_uri"] == REDIRECT
    assert body["code_verifier"] == "verifier"
    if mode == "basic":
        assert request.basic_auth == ("cid", "csecret") and "client_secret" not in body
    else:
        assert request.basic_auth is None
        assert body["client_id"] == "cid" and body["client_secret"] == "csecret"
    assert (request.json_body is not None) is (mode == "post_json")
    assert "csecret" not in repr(request) and "the-code" not in repr(request)


async def test_exchange_returns_wrapped_tokens_and_id_token() -> None:
    _, post = _capture(200, {**_GRANT, "id_token": "a.b.c"})
    tokens = await exchange_authorization_code(
        _flow(),
        code=Secret("c"),
        client_id="cid",
        client_secret=Secret("s"),
        redirect_uri=REDIRECT,
        code_verifier=Secret("v"),
        post=post,
    )
    assert tokens.refresh_token.reveal() == "rt-1"
    assert tokens.access_token.reveal() == "at-1"
    assert tokens.expires_in == 3599 and tokens.id_token == "a.b.c"
    assert "rt-1" not in repr(tokens) and "at-1" not in repr(tokens)


async def test_exchange_without_verifier_on_pkce_flow_is_refused() -> None:
    seen, post = _capture(200, _GRANT)
    with pytest.raises(ExtensionError) as caught:
        await exchange_authorization_code(
            _flow(),
            code=Secret("c"),
            client_id="cid",
            client_secret=Secret("s"),
            redirect_uri=REDIRECT,
            code_verifier=None,
            post=post,
        )
    assert caught.value.code == OAUTH_PKCE_REQUIRED and not seen


async def test_a_dead_code_is_terminal_and_provider_text_is_not_echoed() -> None:
    _, post = _capture(400, {"error": "invalid_grant", "error_description": "<b>bad</b>"})
    with pytest.raises(OAuthExchangeError) as caught:
        await exchange_authorization_code(
            _flow(),
            code=Secret("c"),
            client_id="cid",
            client_secret=Secret("s"),
            redirect_uri=REDIRECT,
            code_verifier=Secret("v"),
            post=post,
        )
    assert caught.value.terminal and "<b>" not in caught.value.message


async def test_no_refresh_token_is_refused() -> None:
    _, post = _capture(200, {"access_token": "a", "expires_in": 10})
    with pytest.raises(OAuthExchangeError) as caught:
        await exchange_authorization_code(
            _flow(),
            code=Secret("c"),
            client_id="cid",
            client_secret=Secret("s"),
            redirect_uri=REDIRECT,
            code_verifier=Secret("v"),
            post=post,
        )
    assert caught.value.error_code == "no_refresh_token"


async def test_refresh_uses_the_flow_client_auth_and_classifies_invalid_grant() -> None:
    seen, post = _capture(400, {"error": "invalid_grant"})
    request = RefreshRequest(
        flow=_flow(), refresh_token=Secret("rt"), client_id="cid", client_secret=Secret("cs")
    )
    with pytest.raises(CredentialRenewalError) as caught:
        await refresh_access_token(request, post=post)
    assert caught.value.error_code == "invalid_grant" and caught.value.terminal
    assert seen[0].form is not None and seen[0].form["client_secret"] == "cs"
    assert seen[0].form["grant_type"] == "refresh_token"


async def test_revoke_is_best_effort() -> None:
    seen, post = _capture(200, {})
    assert await revoke(_flow(), token=Secret("rt"), post=post)
    assert seen[0].form == {"token": "rt"}

    async def broken(_request: TokenPost) -> tuple[int, dict[str, Any]]:
        raise ConnectionError("down")

    assert not await revoke(_flow(), token=Secret("rt"), post=broken)
    assert not await revoke(_flow(revoke_url=None), token=Secret("rt"), post=post)


def test_missing_scopes_detects_granular_consent_drop() -> None:
    asked = ["openid", "email", "cal"]
    assert missing_scopes(asked, "openid email") == ("cal",)
    assert missing_scopes(asked, None) == ()


# --- the account --------------------------------------------------------------------------


def test_id_token_claims_checked() -> None:
    good = verified_id_token_email(_id_token(), client_id="client-123", issuers=GOOGLE_ISSUERS)
    assert good == "josh@blackarcindustrial.com"
    bad = [
        _id_token(aud="someone-else"),
        _id_token(iss="https://evil.example"),
        _id_token(email_verified=False),
        _id_token(email_verified="true"),
        _id_token(exp=time.time() - 3600),
        _id_token(email=None),
        "not-a-jwt",
        None,
    ]
    for token in bad:
        with pytest.raises(ExtensionError) as caught:
            verified_id_token_email(token, client_id="client-123", issuers=GOOGLE_ISSUERS)
        assert caught.value.code == ACCOUNT_MISMATCH


def test_audience_list_requires_matching_azp() -> None:
    token = _id_token(aud=["client-123", "other"], azp="other")
    with pytest.raises(ExtensionError):
        verified_id_token_email(token, client_id="client-123", issuers=GOOGLE_ISSUERS)
    ok = _id_token(aud=["client-123", "other"], azp="client-123")
    assert verified_id_token_email(ok, client_id="client-123", issuers=GOOGLE_ISSUERS)


def test_wrong_account_is_refused_and_blank_intended_accepts() -> None:
    require_account("josh@blackarcindustrial.com", intended="Josh@BlackArcIndustrial.com ")
    require_account("anyone@example.com", intended="")
    with pytest.raises(ExtensionError) as caught:
        require_account("hello@joshuaschultz.com", intended="josh@blackarcsystems.com")
    assert caught.value.code == ACCOUNT_MISMATCH
    assert "hello@" not in caught.value.message, "emails are never echoed"
