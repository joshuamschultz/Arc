"""Native OAuth2 authorization-code exchange for connectors (in-harness connect).

The host-login path signs a host BINARY in; a native OAuth connector has no
binary, so this is the exchange the harness runs itself. ``arc connector
authorize`` builds the provider's authorize URL from the stored client id, takes
the one-time code the provider shows, and swaps it here for a durable refresh
token — the only credential Arc stores for the connection. No short-lived access
token is ever persisted, and no adapter reimplements the exchange: it is driven
generically by the manifest ``[oauth]`` block.

``invalid_grant`` is terminal — an expired, used, or malformed code cannot be
retried, so the caller re-consents rather than looping. This mirrors
:data:`arcagent.extension.credentials.TERMINAL_ERROR_CODES` on the renewal side,
so both halves of a connection's life classify a dead grant the same way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from arcagent.core.errors import ExtensionError
from arcagent.extension.credentials import TERMINAL_ERROR_CODES
from arcagent.extension.manifest import OAuthFlow

#: POSTs form data with HTTP basic auth and returns ``(status_code, json_body)``.
#: Injected so the exchange is testable without a socket and the framework owns
#: the one HTTP client rather than this module opening its own.
PostForm = Callable[[str, dict[str, str], tuple[str, str]], Awaitable[tuple[int, dict[str, Any]]]]


@dataclass(frozen=True)
class OAuthTokens:
    """What a successful authorization-code exchange produced."""

    refresh_token: str
    access_token: str = ""
    expires_in: int = 0


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


def build_authorize_url(flow: OAuthFlow, *, client_id: str) -> str:
    """The provider URL the operator opens to consent, code flow, offline access.

    ``response_type=code`` is fixed — this module only implements the
    authorization-code grant. Everything provider-specific (``token_access_type=
    offline``, scopes) rides ``authorize_params`` from the manifest, so the URL is
    correct for any provider without this code knowing which one it is.
    """
    params = {"client_id": client_id, "response_type": "code", **flow.authorize_params}
    return f"{flow.authorize_url}?{urlencode(params)}"


async def exchange_authorization_code(
    flow: OAuthFlow, *, code: str, client_id: str, client_secret: str, post: PostForm
) -> OAuthTokens:
    """Swap a one-time authorization ``code`` for a durable refresh token.

    Raises :class:`OAuthExchangeError` on any non-200 or ``error`` body — a
    ``terminal`` one (``invalid_grant`` and the consent codes) means the code is
    dead and the operator must authorize again; anything else is a transient the
    caller may retry. A 200 with no ``refresh_token`` is its own terminal error:
    the app was authorized without offline access, so nothing durable was issued.
    """
    status, payload = await post(
        flow.token_url,
        {"grant_type": "authorization_code", "code": code},
        (client_id, client_secret),
    )
    if status != 200 or "error" in payload:
        error_code = str(payload.get("error") or f"http_{status}")
        detail = str(payload.get("error_description") or "")
        raise OAuthExchangeError(
            error_code=error_code,
            message=f"authorization-code exchange failed: {error_code} {detail}".strip(),
        )
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthExchangeError(
            error_code="no_refresh_token",
            message=(
                "the provider returned no refresh token — the app was authorized without offline "
                "access. Authorize again with token_access_type=offline."
            ),
        )
    return OAuthTokens(
        refresh_token=refresh_token,
        access_token=str(payload.get("access_token") or ""),
        expires_in=int(payload.get("expires_in") or 0),
    )


__all__ = [
    "OAuthExchangeError",
    "OAuthTokens",
    "PostForm",
    "build_authorize_url",
    "exchange_authorization_code",
]
