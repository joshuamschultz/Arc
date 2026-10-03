"""P18-3 — the native OAuth 2.0 security core every provider connect runs through.

One redirect flow for every provider (design O1): ``begin`` builds the provider's
consent URL, the provider sends the browser back to ArcUI's ``/oauth/callback``
page, and ``complete`` swaps the one-time code for tokens. This module holds the
parts where one mistake is a login-swap or a wrong-mailbox binding:

* **State is single-use and bound to the operator session that began it.**
  :class:`OAuthPendingLedger` pops a pending sign-in exactly once; a complete
  from another session is refused WITHOUT consuming it, so an attacker holding a
  callback cannot burn the operator's flow, and an attacker's own ``state`` can
  never be completed in a victim's session (CSRF / login swap).
* **PKCE S256 whenever the flow declares it.** :func:`build_authorize_url`
  refuses to build a PKCE flow's URL without a challenge (no silent downgrade).
* **The redirect URI is an input, never derived here.** Callers compute it from
  deployment config; :func:`checked_callback` refuses any pasted address whose
  scheme, host, port or path differ from it.
* **The signed-in account is verified before anything is stored.**
  :func:`verified_id_token_email` checks ``aud``, ``iss``, ``exp`` and
  ``email_verified`` on the ``id_token`` the token endpoint returned over TLS
  (OIDC Core 3.1.3.7 allows skipping the signature for that channel).

Token values stay wrapped in :class:`~arcagent.extension.secrets.Secret` from the
provider's answer to custody; no error message carries a code, a token, the
pasted address or provider-authored text beyond the OAuth ``error`` code.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from arcagent.core.errors import ExtensionError
from arcagent.extension.credentials import (
    TERMINAL_ERROR_CODES,
    CredentialRenewalError,
    RefreshRequest,
    RenewedCredential,
)
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.secrets import Secret

#: Refusal codes a surface branches on.
OAUTH_STATE_INVALID = "OAUTH_STATE_INVALID"
OAUTH_CALLBACK_INVALID = "OAUTH_CALLBACK_INVALID"
OAUTH_DECLINED = "OAUTH_DECLINED"
OAUTH_BUSY = "OAUTH_BUSY"
OAUTH_PKCE_REQUIRED = "OAUTH_PKCE_REQUIRED"
ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"

#: How long a begun sign-in may wait for its callback, and how many may wait at once.
PENDING_TTL_SECONDS = 600.0
PENDING_CAPACITY = 16

#: Whole-call bound on one token-endpoint POST.
_POST_TIMEOUT_SECONDS = 20.0

#: A pasted callback address longer than this is not a callback (LLM10).
_MAX_CALLBACK_LENGTH = 4096

#: Query keys a provider's callback may carry. Anything else is refused.
_CALLBACK_KEYS = frozenset(
    {
        "code",
        "state",
        "scope",
        "authuser",
        "hd",
        "prompt",
        "iss",
        "error",
        "error_description",
        "error_uri",
    }
)

#: ``id_token`` clock tolerance.
_ID_TOKEN_LEEWAY_SECONDS = 300


# --- token-endpoint requests -------------------------------------------------


@dataclass(frozen=True)
class TokenPost:
    """One POST to a token or revocation endpoint. Only the URL renders in a repr."""

    url: str
    form: dict[str, str] | None = field(default=None, repr=False)
    json_body: dict[str, str] | None = field(default=None, repr=False)
    basic_auth: tuple[str, str] | None = field(default=None, repr=False)
    bearer: str | None = field(default=None, repr=False)
    #: ``GET`` is for the one authenticated lookup a connect makes (the sites a token reaches).
    method: Literal["POST", "GET"] = "POST"


#: Sends a :class:`TokenPost`, returns ``(status_code, json_body)``. Injected so the
#: flow is testable without a socket.
PostToken = Callable[[TokenPost], Awaitable[tuple[int, dict[str, Any]]]]


async def send_token_post(call: TokenPost) -> tuple[int, dict[str, Any]]:
    """The one HTTP call to a token endpoint.

    A transport failure becomes ``ConnectionError`` (an ``OSError``) so callers
    classify it as retryable without knowing the HTTP client. A ``Retry-After``
    header in seconds is carried as ``payload["_retry_after"]``.
    """
    headers = {"Accept": "application/json"}
    extra: dict[str, Any] = {}
    if call.basic_auth is not None:
        extra["auth"] = httpx.BasicAuth(*call.basic_auth)
    if call.bearer is not None:
        headers["Authorization"] = f"Bearer {call.bearer}"
    try:
        async with httpx.AsyncClient(timeout=_POST_TIMEOUT_SECONDS) as client:
            if call.method == "GET":
                response = await client.get(call.url, headers=headers)
            else:
                response = await client.post(
                    call.url,
                    data=call.form,
                    json=call.json_body,
                    **extra,
                    headers=headers,
                )
    except httpx.TransportError as exc:
        raise ConnectionError(type(exc).__name__) from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, list):
        payload = {"items": payload}
    body: dict[str, Any] = payload if isinstance(payload, dict) else {}
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isdigit():
        body["_retry_after"] = int(retry_after)
    return response.status_code, body


def token_request(
    flow: OAuthFlow, grant: dict[str, str], *, client_id: str, client_secret: Secret
) -> TokenPost:
    """Place the client credentials where ``flow.client_auth`` says the provider wants them."""
    secret = client_secret.reveal()
    if flow.client_auth == "basic":
        return TokenPost(url=flow.token_url, form=grant, basic_auth=(client_id, secret))
    body = {**grant, "client_id": client_id}
    if secret:
        body["client_secret"] = secret
    if flow.client_auth == "post_json":
        return TokenPost(url=flow.token_url, json_body=body)
    return TokenPost(url=flow.token_url, form=body)


# --- PKCE and the authorize URL ----------------------------------------------


@dataclass(frozen=True)
class PkcePair:
    """An RFC 7636 verifier (kept server-side) and its S256 challenge (sent)."""

    verifier: Secret
    challenge: str


def new_pkce() -> PkcePair:
    """A fresh 64-character verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    return PkcePair(verifier=Secret(verifier), challenge=s256_challenge(verifier))


def s256_challenge(verifier: str) -> str:
    """``BASE64URL(SHA256(verifier))`` without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state() -> str:
    """An unguessable single-use ``state`` (43 characters)."""
    return secrets.token_urlsafe(32)


def scopes_for(flow: OAuthFlow, *, read_only: bool) -> list[str]:
    """The scopes to request: the read-only set when the connection is read-only."""
    if read_only and flow.scopes_read_only is not None:
        return list(flow.scopes_read_only)
    return list(flow.scopes)


def build_authorize_url(
    flow: OAuthFlow,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str | None,
    scopes: Sequence[str],
    login_hint: str = "",
) -> str:
    """The provider consent URL. The flow's own parameters cannot be overridden.

    Raises:
        ExtensionError: ``OAUTH_PKCE_REQUIRED`` when the flow declares PKCE and
            no challenge was given (a downgrade), or a challenge was given to a
            flow that does not use PKCE.
    """
    if flow.pkce != bool(code_challenge):
        raise ExtensionError(
            code=OAUTH_PKCE_REQUIRED,
            message="the sign-in link must carry a PKCE challenge exactly when the flow uses PKCE",
            details={"provider": flow.provider},
        )
    params: dict[str, str] = dict(flow.authorize_params)
    params.update({"client_id": client_id, "response_type": "code", "state": state})
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    if scopes:
        params["scope"] = flow.scope_separator.join(scopes)
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if login_hint:
        params["login_hint"] = login_hint
    return f"{flow.authorize_url}?{urlencode(params)}"


# --- the pending-authorization ledger ----------------------------------------


@dataclass(frozen=True)
class PendingAuthorization:
    """One begun sign-in waiting for its callback. Never persisted."""

    state: str
    instance: str
    session_id: str
    code_verifier: Secret | None
    redirect_uri: str
    created_at: float
    #: The account the connection is for, captured at begin ("" = not yet bound).
    intended_account: str = ""
    scopes: tuple[str, ...] = ()


def _state_invalid() -> ExtensionError:
    return ExtensionError(
        code=OAUTH_STATE_INVALID,
        message=(
            "this sign-in is unknown, expired, already used, or was started in another "
            "Arc session. Start the sign-in again from the connection card."
        ),
        details={},
    )


class OAuthPendingLedger:
    """In-memory, process-local, single-use; TTL 600 s; at most 16 waiting (LLM10)."""

    def __init__(
        self,
        *,
        ttl: float = PENDING_TTL_SECONDS,
        capacity: int = PENDING_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock
        self._pending: dict[str, PendingAuthorization] = {}

    def admit(self, pending: PendingAuthorization) -> None:
        """Hold ``pending`` until its callback. Refused when the ledger is full."""
        self._evict_expired()
        if len(self._pending) >= self._capacity:
            raise ExtensionError(
                code=OAUTH_BUSY,
                message="too many sign-ins are waiting; finish or let one expire first",
                details={},
            )
        if not pending.session_id or pending.state in self._pending:
            raise _state_invalid()
        self._pending[pending.state] = pending

    def take(self, state: str, *, session_id: str) -> PendingAuthorization:
        """Pop the pending sign-in for ``state`` — once, and only for its own session.

        A wrong session is refused WITHOUT consuming the entry, so a forged or
        swapped callback cannot burn the operator's own sign-in.
        """
        self._evict_expired()
        pending = self._pending.get(state) if state else None
        if pending is None or not session_id:
            raise _state_invalid()
        if not hmac.compare_digest(pending.session_id.encode(), session_id.encode()):
            raise _state_invalid()
        del self._pending[state]
        return pending

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._pending)

    def _evict_expired(self) -> None:
        now = self._clock()
        for state in [s for s, p in self._pending.items() if now - p.created_at > self._ttl]:
            del self._pending[state]


# --- the callback -------------------------------------------------------------


@dataclass(frozen=True)
class CallbackParams:
    """What a checked callback address carried. ``code`` is ``None`` iff ``error`` is set."""

    state: str
    code: Secret | None
    error: str | None = None


def _callback_invalid(reason: str) -> ExtensionError:
    return ExtensionError(
        code=OAUTH_CALLBACK_INVALID,
        message=f"that address is not this sign-in's callback: {reason}",
        details={},
    )


def _origin_and_path(url: str) -> tuple[str, str, int | None, str]:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    return parts.scheme.lower(), (parts.hostname or "").lower(), port, parts.path


def checked_callback(redirect_url: str, *, expected_redirect_uri: str) -> CallbackParams:
    """Parse a provider callback address, refusing anything that is not exactly ours.

    Scheme, host, port and path must equal ``expected_redirect_uri``'s; ``state``
    and ``code`` must each appear exactly once; no fragment, no userinfo, no
    unknown keys, no control characters, at most 4 KiB. Messages never echo the
    address: it carries a live code.

    Raises:
        ExtensionError: ``OAUTH_CALLBACK_INVALID``.
    """
    pasted = redirect_url.strip()
    if not pasted:
        raise _callback_invalid("it is empty")
    if len(pasted) > _MAX_CALLBACK_LENGTH:
        raise _callback_invalid("it is too long")
    if _unprintable(pasted, allow_space=False):
        raise _callback_invalid("it has spaces or control characters in it")
    parts = urlsplit(pasted)
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise _callback_invalid("it has unexpected parts")
    if _origin_and_path(pasted) != _origin_and_path(expected_redirect_uri):
        raise _callback_invalid("it does not come back to this Arc's sign-in page")
    query = _callback_query(parts.query)
    state = query.get("state", "")
    if not state:
        raise _callback_invalid("it carries no state")
    if "error" in query:
        return CallbackParams(state=state, code=None, error=query["error"])
    code = query.get("code", "")
    if not code:
        raise _callback_invalid("it carries no code")
    return CallbackParams(state=state, code=Secret(code))


def _callback_query(query: str) -> dict[str, str]:
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise _callback_invalid("it is damaged") from None
    names = [name for name, _ in pairs]
    if not set(names) <= _CALLBACK_KEYS:
        raise _callback_invalid("it has unexpected parameters")
    if len(names) != len(set(names)):
        raise _callback_invalid("it repeats a parameter")
    if any(_unprintable(text, allow_space=True) for pair in pairs for text in pair):
        raise _callback_invalid("it has unexpected characters")
    return dict(pairs)


def _unprintable(text: str, *, allow_space: bool) -> bool:
    """True for any control, format or separator character (a space only if not allowed)."""
    return any(
        unicodedata.category(character).startswith(("C", "Z"))
        for character in text
        if not (allow_space and character == " ")
    )


def declined(error: str) -> ExtensionError:
    """The refusal for a provider ``error=`` callback. Provider text is never echoed."""
    if error == "access_denied":
        return ExtensionError(
            code=OAUTH_DECLINED,
            message="You declined access. Nothing was stored.",
            details={"error_code": "access_denied"},
        )
    return ExtensionError(
        code=OAUTH_DECLINED,
        message="The provider did not grant access. Nothing was stored. Try again.",
        details={"error_code": "consent_required"},
    )


# --- the code exchange ----------------------------------------------------------


@dataclass(frozen=True)
class OAuthTokens:
    """What a successful authorization-code exchange produced."""

    refresh_token: Secret
    access_token: Secret
    expires_in: int
    scope: str | None = None
    id_token: str | None = field(default=None, repr=False)


class OAuthExchangeError(ExtensionError):
    """The code→token exchange failed. ``terminal`` decides whether a retry is sane."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(
            code="OAUTH_EXCHANGE_FAILED", message=message, details={"error_code": error_code}
        )
        self.error_code = error_code

    @property
    def terminal(self) -> bool:
        """True when only a fresh authorization (a new code) can fix this."""
        return self.error_code in TERMINAL_ERROR_CODES


async def exchange_authorization_code(
    flow: OAuthFlow,
    *,
    code: Secret,
    client_id: str,
    client_secret: Secret,
    redirect_uri: str,
    code_verifier: Secret | None,
    post: PostToken = send_token_post,
) -> OAuthTokens:
    """Swap a one-time authorization ``code`` for a refresh token and an access token.

    Raises:
        OAuthExchangeError: Any non-200 or ``error`` body, or a 200 that issued no
            refresh token (consent without offline access) or no access token.
    """
    if flow.pkce and code_verifier is None:
        raise ExtensionError(
            code=OAUTH_PKCE_REQUIRED,
            message="this sign-in has no PKCE verifier; start again",
            details={"provider": flow.provider},
        )
    grant = {"grant_type": "authorization_code", "code": code.reveal()}
    if redirect_uri:
        grant["redirect_uri"] = redirect_uri
    if code_verifier is not None:
        grant["code_verifier"] = code_verifier.reveal()
    request = token_request(flow, grant, client_id=client_id, client_secret=client_secret)
    try:
        status, payload = await post(request)
    except OSError as exc:
        raise OAuthExchangeError(
            error_code="provider_unavailable",
            message=f"could not reach the token endpoint: {type(exc).__name__}",
        ) from None
    if status != 200 or "error" in payload:
        error_code = _error_code(payload, status)
        raise OAuthExchangeError(
            error_code=error_code, message=f"the provider refused the sign-in: {error_code}"
        )
    return _tokens(payload)


def _error_code(payload: dict[str, Any], status: int) -> str:
    """The OAuth ``error`` code if it is a plain token, else ``http_<status>``."""
    raw = payload.get("error")
    if isinstance(raw, str) and raw.replace("_", "").isalnum() and len(raw) <= 64:
        return raw
    return f"http_{status}"


def _tokens(payload: dict[str, Any]) -> OAuthTokens:
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise OAuthExchangeError(
            error_code="no_refresh_token",
            message=(
                "the provider returned no refresh token: the app was authorized without "
                "offline access. Connect again."
            ),
        )
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthExchangeError(
            error_code="no_access_token", message="the provider returned no access token"
        )
    scope = payload.get("scope")
    id_token = payload.get("id_token")
    return OAuthTokens(
        refresh_token=Secret(refresh),
        access_token=Secret(access),
        expires_in=_positive_int(payload.get("expires_in")),
        scope=scope if isinstance(scope, str) else None,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
    )


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


#: Scopes that ask for a behaviour (a refresh token), not a permission: a provider
#: need not list them in the grant. The exchange already refuses a grant with no
#: refresh token, so the behaviour is proven by its result.
_REQUEST_ONLY_SCOPES = frozenset({"offline_access"})


def missing_scopes(requested: Sequence[str], granted: str | None) -> tuple[str, ...]:
    """The requested scopes the grant lacks (granular consent can drop some).

    A provider that answers no ``scope`` granted what was asked (RFC 6749 5.1).
    """
    if granted is None:
        return ()
    have = set(granted.split())
    return tuple(
        scope for scope in requested if scope not in have and scope not in _REQUEST_ONLY_SCOPES
    )


# --- the signed-in account -------------------------------------------------------


def _mismatch(reason: str) -> ExtensionError:
    return ExtensionError(
        code=ACCOUNT_MISMATCH,
        message=f"the signed-in account could not be accepted: {reason}. Nothing was stored.",
        details={},
    )


def verified_id_token_email(
    id_token: str | None,
    *,
    client_id: str,
    issuers: Sequence[str],
    now: Callable[[], float] = time.time,
) -> str:
    """The verified, casefolded email an ``id_token`` names, or ``ACCOUNT_MISMATCH``.

    The token came straight from the token endpoint over TLS, so its signature
    need not be checked (OIDC Core 3.1.3.7); its claims must: ``aud`` (and
    ``azp`` when ``aud`` is a list) is our client, ``iss`` is an allowed issuer,
    it has not expired, and the email is verified.
    """
    claims = _id_token_claims(id_token)
    if not _audience_ok(claims, client_id):
        raise _mismatch("the sign-in was issued to a different app")
    if claims.get("iss") not in set(issuers):
        raise _mismatch("the sign-in came from an unexpected issuer")
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires + _ID_TOKEN_LEEWAY_SECONDS < now():
        raise _mismatch("the sign-in has expired")
    if claims.get("email_verified") is not True:
        raise _mismatch("the account's email address is not verified")
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email or _unprintable(email, allow_space=False):
        raise _mismatch("the sign-in names no email address")
    return email.casefold()


def _id_token_claims(id_token: str | None) -> dict[str, Any]:
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise _mismatch("the provider returned no usable identity token")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError):
        raise _mismatch("the provider returned an unreadable identity token") from None
    if not isinstance(claims, dict):
        raise _mismatch("the provider returned an unreadable identity token")
    return claims


def _audience_ok(claims: dict[str, Any], client_id: str) -> bool:
    audience = claims.get("aud")
    if isinstance(audience, str):
        return hmac.compare_digest(audience, client_id)
    if isinstance(audience, list) and client_id in audience:
        return claims.get("azp") == client_id
    return False


def require_account(resolved: str, *, intended: str) -> None:
    """Refuse a sign-in as anyone but the connection's intended account (blank = any)."""
    if intended and resolved.casefold() != intended.strip().casefold():
        raise _mismatch(
            "you signed in as a different account than this connection is for. "
            "Sign in again and choose the right account"
        )


@dataclass(frozen=True)
class SiteBinding:
    """The one site a connect bound: its provider id and its host."""

    cloud_id: str
    site: str


async def resolve_site(
    access_token: Secret,
    *,
    resources_url: str,
    site: str,
    cloud_id: str,
    post: PostToken = send_token_post,
) -> SiteBinding:
    """The site this token may reach and this connection is for, or ``ACCOUNT_MISMATCH``.

    ``resources_url`` lists what the new token can see. The connection's ``site``
    (a host such as ``acme.atlassian.net``) picks one; a blank ``site`` is accepted
    only when exactly one is visible. A connection that already holds a
    ``cloud_id`` may only be reconnected to that same site: consent given as a
    different organisation's account must never rebind it. The refusal names site
    HOSTS, never ids.
    """
    try:
        status, payload = await post(
            TokenPost(url=resources_url, bearer=access_token.reveal(), method="GET")
        )
    except OSError:
        raise _mismatch("the provider's site list could not be reached") from None
    items = payload.get("items")
    if status != 200 or not isinstance(items, list):
        raise _mismatch("the provider did not list the sites this sign-in can reach")
    sites = [_site_of(item) for item in items]
    sites = [found for found in sites if found is not None]
    wanted = site.strip().casefold().removeprefix("https://").rstrip("/")
    matches = [found for found in sites if not wanted or found.site == wanted]
    if len(matches) != 1:
        visible = ", ".join(sorted({found.site for found in sites})) or "none"
        raise _mismatch(
            f"this sign-in reaches these sites: {visible}. "
            "Set this connection's site to one of them, then connect again"
        )
    if cloud_id and matches[0].cloud_id != cloud_id:
        raise _mismatch("you signed in to a different site than this connection is bound to")
    return matches[0]


def _site_of(item: object) -> SiteBinding | None:
    if not isinstance(item, dict):
        return None
    identifier, url = item.get("id"), item.get("url")
    if not isinstance(identifier, str) or not isinstance(url, str):
        return None
    host = urlsplit(url).hostname
    if not identifier or "/" in identifier or not host:
        return None
    return SiteBinding(cloud_id=identifier, site=host.casefold())


# --- refresh and revoke ------------------------------------------------------------


async def refresh_access_token(
    request: RefreshRequest, *, post: PostToken = send_token_post
) -> RenewedCredential:
    """Exchange a refresh token for a fresh access token (RFC 6749 section 6).

    The provider's answer is classified so the caller knows whether a retry is sane:

    * 400 ``invalid_grant``: terminal, the refresh token is dead (reconnect).
    * 401 ``invalid_client``: terminal ``consent_required``; the app secret was
      rotated and the operator must set the new one.
    * 429 / 5xx / a transport failure: retryable (``Retry-After`` carried).
    * any other 4xx: terminal ``auth_required``.
    """
    grant = {"grant_type": "refresh_token", "refresh_token": request.refresh_token.reveal()}
    token_post = token_request(
        request.flow, grant, client_id=request.client_id, client_secret=request.client_secret
    )
    try:
        status, payload = await post(token_post)
    except OSError as exc:
        raise CredentialRenewalError(
            error_code="provider_unavailable",
            message=f"could not reach the token endpoint: {type(exc).__name__}",
        ) from None
    if status == 200 and "error" not in payload:
        return _renewed(payload)
    raise _refresh_error(status, payload)


def _renewed(payload: dict[str, Any]) -> RenewedCredential:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise CredentialRenewalError(
            error_code="provider_unavailable", message="the provider returned no access token"
        )
    rotated = payload.get("refresh_token")
    scope = payload.get("scope")
    return RenewedCredential(
        access_token=Secret(access),
        expires_in=_positive_int(payload.get("expires_in")),
        refresh_token=Secret(rotated) if isinstance(rotated, str) and rotated else None,
        scope=scope if isinstance(scope, str) else None,
    )


def _refresh_error(status: int, payload: dict[str, Any]) -> CredentialRenewalError:
    error = _error_code(payload, status)
    retry_after = payload.get("_retry_after")
    if status == 429 or status >= 500:
        code = "rate_limited" if status == 429 else "provider_unavailable"
        hint = float(retry_after) if isinstance(retry_after, (int, float)) else None
        return CredentialRenewalError(
            error_code=code, message=f"token refresh answered {status}", retry_after=hint
        )
    if error == "invalid_grant":
        code = "invalid_grant"
    elif error == "invalid_client" or status == 401:
        code = "consent_required"
    else:
        code = "auth_required"
    return CredentialRenewalError(error_code=code, message=f"token refresh refused: {error}")


async def revoke(flow: OAuthFlow, *, token: Secret, post: PostToken = send_token_post) -> bool:
    """Best-effort revocation at the provider. Never raises; True when it answered 200."""
    if flow.revoke_url is None:
        return False
    if flow.revoke_style == "bearer":
        request = TokenPost(url=flow.revoke_url, bearer=token.reveal())
    else:
        request = TokenPost(url=flow.revoke_url, form={"token": token.reveal()})
    try:
        status, _ = await post(request)
    except OSError:
        return False
    return status == 200


__all__ = [
    "ACCOUNT_MISMATCH",
    "OAUTH_BUSY",
    "OAUTH_CALLBACK_INVALID",
    "OAUTH_DECLINED",
    "OAUTH_PKCE_REQUIRED",
    "OAUTH_STATE_INVALID",
    "CallbackParams",
    "OAuthExchangeError",
    "OAuthPendingLedger",
    "OAuthTokens",
    "PendingAuthorization",
    "PkcePair",
    "PostToken",
    "TokenPost",
    "build_authorize_url",
    "checked_callback",
    "declined",
    "exchange_authorization_code",
    "missing_scopes",
    "new_pkce",
    "new_state",
    "refresh_access_token",
    "require_account",
    "revoke",
    "s256_challenge",
    "scopes_for",
    "send_token_post",
    "token_request",
    "verified_id_token_email",
]
