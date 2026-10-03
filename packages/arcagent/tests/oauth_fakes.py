"""A fake OAuth 2.0 provider (Google-shaped) for P18-3 tests and journeys.

Only the provider's HTTP is fake: tests hand :meth:`FakeOAuthProvider.post` to
``Connections(token_post=...)`` / ``open_custody(token_post=...)`` and drive the real
connect path. The fake enforces what a real provider enforces — a code is single
use, it is bound to the PKCE challenge and redirect URI of the consent that issued
it, and a refresh token can be revoked — so a test passes only if Arc's side is
right.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from arcagent.extension.oauth import TokenPost

GOOGLE_ISSUER = "https://accounts.google.com"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class _Consent:
    challenge: str
    redirect_uri: str
    email: str
    scope: str
    client_id: str


@dataclass
class FakeOAuthProvider:
    """Token endpoint + consent screen. Every value it issues is unique and traceable."""

    client_id: str = "cid-1234567890.apps.example"
    client_secret: str = "csecret-xyz"
    rotate_refresh: bool = False
    access_lifetime: int = 3600
    posts: list[TokenPost] = field(default_factory=list)
    exchanges: int = 0
    refreshes: int = 0
    revoked: set[str] = field(default_factory=set)
    issued_access: list[str] = field(default_factory=list)
    #: When set, each access token's expiry is tracked so a resource fake can 401 it.
    clock: Callable[[], datetime] | None = None
    expiry: dict[str, datetime] = field(default_factory=dict)
    _codes: dict[str, _Consent] = field(default_factory=dict)
    _refresh: dict[str, tuple[str, str]] = field(default_factory=dict)  # rt -> (email, scope)

    # --- consent screen --------------------------------------------------------

    def consent(self, authorize_url: str, *, email: str, granted_scope: str | None = None) -> str:
        """Sign in as ``email`` and allow; returns the address the browser lands on."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["client_id"] == self.client_id
        code = "4/0" + secrets.token_urlsafe(24)
        self._codes[code] = _Consent(
            challenge=query.get("code_challenge", ""),
            redirect_uri=query.get("redirect_uri", ""),
            email=email,
            scope=granted_scope if granted_scope is not None else query.get("scope", ""),
            client_id=query["client_id"],
        )
        landing = urlencode({"state": query["state"], "code": code, "scope": "x"})
        return f"{query.get('redirect_uri', 'https://provider.example/shown')}?{landing}"

    def decline(self, authorize_url: str) -> str:
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        landing = urlencode({"error": "access_denied", "state": query["state"]})
        return f"{query['redirect_uri']}?{landing}"

    # --- token endpoint ---------------------------------------------------------

    async def post(self, request: TokenPost) -> tuple[int, dict[str, Any]]:
        self.posts.append(request)
        body = dict(request.form or request.json_body or {})
        if request.form is not None and "token" in body and "grant_type" not in body:
            self.revoked.add(body["token"])
            return 200, {}
        if not self._client_ok(request, body):
            return 401, {"error": "invalid_client"}
        if body.get("grant_type") == "authorization_code":
            return self._exchange(body)
        if body.get("grant_type") == "refresh_token":
            return self._refresh_grant(body)
        return 400, {"error": "unsupported_grant_type"}

    def _client_ok(self, request: TokenPost, body: dict[str, str]) -> bool:
        if request.basic_auth is not None:
            return request.basic_auth == (self.client_id, self.client_secret)
        return body.get("client_id") == self.client_id and (
            body.get("client_secret") == self.client_secret
        )

    def _exchange(self, body: dict[str, str]) -> tuple[int, dict[str, Any]]:
        consent = self._codes.pop(body.get("code", ""), None)
        if consent is None:
            return 400, {"error": "invalid_grant"}
        verifier = body.get("code_verifier", "")
        if consent.challenge and _challenge(verifier) != consent.challenge:
            return 400, {"error": "invalid_grant"}
        if body.get("redirect_uri", "") != consent.redirect_uri:
            return 400, {"error": "redirect_uri_mismatch"}
        self.exchanges += 1
        refresh = "1//rt-" + secrets.token_urlsafe(24)
        self._refresh[refresh] = (consent.email, consent.scope)
        return 200, {
            **self._access(consent.scope),
            "refresh_token": refresh,
            "id_token": self.id_token(consent.email),
        }

    def _refresh_grant(self, body: dict[str, str]) -> tuple[int, dict[str, Any]]:
        token = body.get("refresh_token", "")
        if token in self.revoked or token not in self._refresh:
            return 400, {"error": "invalid_grant"}
        self.refreshes += 1
        email, scope = self._refresh[token]
        answer = self._access(scope)
        if self.rotate_refresh:
            del self._refresh[token]
            self.revoked.add(token)
            rotated = "1//rt-" + secrets.token_urlsafe(24)
            self._refresh[rotated] = (email, scope)
            answer["refresh_token"] = rotated
        return 200, answer

    def _access(self, scope: str) -> dict[str, Any]:
        access = "ya29." + secrets.token_urlsafe(24)
        self.issued_access.append(access)
        self.expiry[access] = self._now() + timedelta(seconds=self.access_lifetime)
        return {"access_token": access, "expires_in": self.access_lifetime, "scope": scope}

    def _now(self) -> datetime:
        return self.clock() if self.clock is not None else datetime.now(UTC)

    def access_valid(self, token: str) -> bool:
        """True for an unexpired access token this provider issued."""
        expires = self.expiry.get(token)
        return expires is not None and expires > self._now()

    def revoke_all(self) -> None:
        """The account owner removed the app: every refresh token is dead."""
        self.revoked.update(self._refresh)

    def id_token(self, email: str, **claims: Any) -> str:
        payload = {
            "iss": GOOGLE_ISSUER,
            "aud": self.client_id,
            "email": email,
            "email_verified": True,
            "exp": time.time() + 3600,
            **claims,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        return f"eyJhbGciOiJSUzI1NiJ9.{encoded}.c2ln"

    def secrets_seen(self) -> list[str]:
        """Every credential-shaped value this provider issued (for leak assertions)."""
        return [*self._refresh, *self.revoked, *self.issued_access]


class FakeGmail:
    """Gmail REST over ``httpx.MockTransport``: profile, labels, messages, history.

    Every request must carry a bearer the provider issued and has not expired;
    anything else answers 401, exactly as Google does.
    """

    def __init__(self, provider: FakeOAuthProvider, *, email: str) -> None:
        self.provider = provider
        self.email = email
        self.calls = 0
        self.rejected = 0
        self.messages: dict[str, dict[str, Any]] = {}
        self.history_id = 100
        self.history: list[dict[str, Any]] = []

    def add_message(self, message_id: str, *, subject: str, body: str) -> None:
        self.history_id += 1
        encoded = base64.urlsafe_b64encode(body.encode()).rstrip(b"=").decode()
        self.messages[message_id] = {
            "id": message_id,
            "threadId": f"t-{message_id}",
            "labelIds": ["INBOX"],
            "snippet": body[:40],
            "historyId": str(self.history_id),
            "internalDate": "1790000000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": "ann@example.com"},
                    {"name": "Date", "value": "Fri, 2 Oct 2026 10:00:00 +0000"},
                ],
                "body": {"size": len(body), "data": encoded},
            },
        }
        self.history.append(
            {
                "id": str(self.history_id),
                "messagesAdded": [{"message": {"id": message_id, "threadId": f"t-{message_id}"}}],
            }
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not self.provider.access_valid(bearer):
            self.rejected += 1
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        if path == "/history":
            since = int(request.url.params.get("startHistoryId") or 0)
            changes = [entry for entry in self.history if int(entry["id"]) > since]
            return httpx.Response(
                200, json={"history": changes, "historyId": str(self.history_id)}
            )
        return self._route(path)

    def _route(self, path: str) -> httpx.Response:
        if path == "/profile":
            return httpx.Response(
                200, json={"emailAddress": self.email, "historyId": str(self.history_id)}
            )
        if path == "/labels":
            return httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "INBOX"}]})
        if path == "/messages":
            listed = [{"id": key, "threadId": f"t-{key}"} for key in sorted(self.messages)]
            return httpx.Response(
                200, json={"messages": listed, "resultSizeEstimate": len(listed)}
            )
        if path.startswith("/messages/") and path.rsplit("/", 1)[-1] in self.messages:
            return httpx.Response(200, json=self.messages[path.rsplit("/", 1)[-1]])
        return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND"}})


__all__ = ["GOOGLE_ISSUER", "FakeGmail", "FakeOAuthProvider"]
