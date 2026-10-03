"""P18-3.M — tenant-bound, cloud-bound OAuth flows (Microsoft Entra ID shape).

The core never names a vendor: a manifest declares endpoint templates with
``{login_host}`` and ``{tenant}`` and a closed table of clouds; the deployment's app
slot holds a tenant GUID and a cloud KEY. These tests pin that the slot can never
point a token at a host the signed manifest did not declare, that the id_token must
come from the slot's tenant, and that a refresh token minted for one tenant is never
sent to another.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher

from arcagent.core.errors import ExtensionError
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credentials import (
    CredentialRenewalError,
    RefreshRequest,
    RenewalPlanner,
    RenewedCredential,
)
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.oauth import (
    ACCOUNT_MISMATCH,
    TokenPost,
    bind_flow,
    missing_scopes,
    refresh_access_token,
    verified_id_token_username,
)
from arcagent.extension.oauth_apps import OAuthApp, OAuthAppStore
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore

TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

CLOUDS = {
    "global": {
        "label": "Commercial / GCC",
        "login_host": "login.microsoftonline.com",
        "api_host": "graph.microsoft.com",
    },
    "usgov": {
        "label": "GCC High",
        "login_host": "login.microsoftonline.us",
        "api_host": "graph.microsoft.us",
    },
    "dod": {
        "label": "DoD",
        "login_host": "login.microsoftonline.us",
        "api_host": "dod-graph.microsoft.us",
    },
}


def _flow(**overrides: Any) -> OAuthFlow:
    values: dict[str, Any] = {
        "provider": "microsoft",
        "authorize_url": "https://{login_host}/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://{login_host}/{tenant}/oauth2/v2.0/token",
        "refresh_token_secret": "refresh_token",
        "scopes": ["openid", "profile", "offline_access", "User.Read", "Mail.Read"],
        "client_auth": "post_form",
        "account": "openid_username",
        "id_token_issuers": ["https://{login_host}/{tenant}/v2.0"],
        "app_tenant": True,
        "clouds": CLOUDS,
        "default_cloud": "global",
        "refresh_scopes": True,
    }
    values.update(overrides)
    return OAuthFlow.model_validate(values)


def _app(tenant: str = TENANT, cloud: str = "global") -> OAuthApp:
    return OAuthApp(
        provider="microsoft",
        client_id=CLIENT,
        client_secret=Secret("s3cret"),
        tenant_id=tenant,
        cloud=cloud,
    )


# --- the manifest: templates and the closed cloud table ----------------------


def test_bind_flow_fills_the_tenant_and_the_clouds_login_host() -> None:
    bound = bind_flow(_flow(), _app())
    assert bound.token_url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert bound.authorize_url.startswith(f"https://login.microsoftonline.com/{TENANT}/")
    assert bound.id_token_issuers == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]


@pytest.mark.parametrize(
    ("cloud", "host"),
    [("usgov", "login.microsoftonline.us"), ("dod", "login.microsoftonline.us")],
)
def test_government_clouds_bind_to_their_own_authority(cloud: str, host: str) -> None:
    bound = bind_flow(_flow(), _app(cloud=cloud))
    assert bound.token_url == f"https://{host}/{TENANT}/oauth2/v2.0/token"


def test_an_unknown_cloud_key_is_refused_at_bind() -> None:
    with pytest.raises(ExtensionError) as refused:
        bind_flow(_flow(), _app(cloud="evil.example"))
    assert refused.value.code == "OAUTH_APP_INVALID"


def test_a_tenant_bound_flow_refuses_a_slot_without_a_tenant() -> None:
    with pytest.raises(ExtensionError) as refused:
        bind_flow(_flow(), _app(tenant=""))
    assert refused.value.code == "OAUTH_APP_INVALID"


def test_a_flow_without_placeholders_binds_to_itself() -> None:
    plain = OAuthFlow(
        provider="google",
        authorize_url="https://accounts.example/auth",
        token_url="https://accounts.example/token",
        refresh_token_secret="refresh_token",
    )
    google_app = OAuthApp(provider="google", client_id="cid", client_secret=Secret("x"))
    assert bind_flow(plain, google_app) == plain


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_url": "https://{login_host}/{tenant}/{other}/token"},  # unknown placeholder
        {"clouds": {}, "default_cloud": ""},  # {login_host} with no clouds
        {"app_tenant": False},  # {tenant} with no tenant in the slot
        {"default_cloud": "nowhere"},
        {"clouds": {"global": {**CLOUDS["global"], "login_host": "evil.example/x"}}},
        {"clouds": {"global": {**CLOUDS["global"], "api_host": "https://graph.microsoft.com"}}},
        {"clouds": {"Bad Key": CLOUDS["global"]}, "default_cloud": "Bad Key"},
    ],
)
def test_a_manifest_cannot_declare_an_open_endpoint(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _flow(**overrides)


# --- the signed-in account: tenant, issuer, audience --------------------------


def _id_token(**claims: Any) -> str:
    payload = {
        "aud": CLIENT,
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "preferred_username": "Josh@Agency.gov",
        "exp": time.time() + 3600,
        **claims,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{encoded}.c2ln"


def _verify(token: str) -> str:
    bound = bind_flow(_flow(), _app())
    return verified_id_token_username(
        token, client_id=CLIENT, issuers=bound.id_token_issuers, tenant=TENANT
    )


def test_username_comes_from_preferred_username_casefolded() -> None:
    assert _verify(_id_token()) == "josh@agency.gov"


def test_email_is_the_fallback_when_there_is_no_preferred_username() -> None:
    assert _verify(_id_token(preferred_username=None, email="Ann@Agency.gov")) == "ann@agency.gov"


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": OTHER_TENANT},
        {"iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"},
        {"iss": "https://login.microsoftonline.com/common/v2.0"},
        {"aud": "another-app"},
        {"exp": time.time() - 3600},
        {"preferred_username": None},
        {"preferred_username": "no-at-sign"},
    ],
)
def test_an_id_token_from_another_tenant_issuer_or_app_is_refused(claims: dict[str, Any]) -> None:
    with pytest.raises(ExtensionError) as refused:
        _verify(_id_token(**claims))
    assert refused.value.code == ACCOUNT_MISMATCH


# --- granted scopes ------------------------------------------------------------


def test_scopes_match_resource_qualified_and_case_insensitively() -> None:
    requested = ["openid", "profile", "offline_access", "User.Read", "Mail.Read", "Mail.Send"]
    granted = "https://graph.microsoft.com/user.read https://graph.microsoft.com/Mail.Read"
    assert missing_scopes(requested, granted) == ("Mail.Send",)


# --- refresh --------------------------------------------------------------------


async def test_refresh_re_sends_the_scopes_and_targets_the_bound_tenant() -> None:
    seen: list[TokenPost] = []

    async def post(call: TokenPost) -> tuple[int, dict[str, Any]]:
        seen.append(call)
        return 200, {"access_token": "a1", "expires_in": 3600, "refresh_token": "r1"}

    request = RefreshRequest(
        flow=bind_flow(_flow(), _app()),
        refresh_token=Secret("r0"),
        client_id=CLIENT,
        client_secret=Secret("s3cret"),
    )
    renewed = await refresh_access_token(request, post=post)
    assert renewed.refresh_token is not None and renewed.refresh_token.reveal() == "r1"
    assert seen[0].url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert seen[0].form is not None
    assert seen[0].form["scope"].split() == _flow().scopes


# --- the app slot ----------------------------------------------------------------


async def test_app_slot_keeps_tenant_and_cloud_and_shows_them() -> None:
    store = OAuthAppStore(InterleavingBackend(), make_cipher())
    await store.put(
        "microsoft",
        client_id=CLIENT,
        client_secret="s3cret",
        tenant_id=TENANT.upper(),
        cloud="global",
        actor_did="did:arc:operator:test",
    )
    app = await store.get("microsoft")
    assert app is not None and app.tenant_id == TENANT and app.cloud == "global"
    status = await store.status("microsoft")
    assert status.tenant_id == TENANT and status.cloud == "global"


@pytest.mark.parametrize(
    "tenant",
    ["common", "organizations", "consumers", "agency.onmicrosoft.com", "../x", TENANT + "0"],
)
async def test_app_slot_refuses_anything_but_a_tenant_guid(tenant: str) -> None:
    store = OAuthAppStore(InterleavingBackend(), make_cipher())
    with pytest.raises(ExtensionError) as refused:
        await store.put(
            "microsoft",
            client_id=CLIENT,
            client_secret="s3cret",
            tenant_id=tenant,
            cloud="global",
            actor_did="did:arc:operator:test",
        )
    assert refused.value.code == "OAUTH_APP_INVALID"


# --- the renewer: a token minted for tenant A is never sent for slot B -----------


async def test_renewer_refuses_when_the_slot_now_points_at_another_tenant() -> None:
    backend = InterleavingBackend()
    clock_now = datetime(2026, 10, 3, tzinfo=UTC)
    rows = CredentialRowStore(backend, make_cipher(), clock=lambda: clock_now)
    state = ConnectionStateStore(backend)
    await rows.put_grant(
        "work_mail",
        refresh_field="refresh_token",
        refresh_token="r0",
        access_token="a0",
        issued_at=clock_now - timedelta(hours=2),
        expires_at=clock_now - timedelta(hours=1),
        scope=None,
        actor_did="did:arc:operator:test",
        fields={"tenant_id": TENANT, "cloud": "global"},
    )
    await state.create(
        ConnectionRecord(connection="work_mail", custody="arc"), actor_did="did:arc:operator:test"
    )
    calls: list[RefreshRequest] = []

    async def refresh(request: RefreshRequest) -> RenewedCredential:
        calls.append(request)
        return RenewedCredential(access_token=Secret("a1"), expires_in=3600)

    async def slot_b(_flow: OAuthFlow) -> OAuthApp:
        return _app(tenant=OTHER_TENANT)

    async def opener() -> Any:
        return backend

    planner = RenewalPlanner(
        rows=rows,
        refresh=refresh,
        health=StoreHealthReporter(opener),
        owner_id="renewer",
        client=slot_b,
        state=state,
        clock=lambda: clock_now,
    )
    with pytest.raises(CredentialRenewalError) as refused:
        await planner.ensure_fresh("work_mail", flow=_flow())
    assert refused.value.terminal
    assert calls == []
    record = await state.get("work_mail")
    assert record is not None and record.status != "healthy"
