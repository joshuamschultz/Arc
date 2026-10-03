"""Fake Microsoft Entra ID token endpoint and Microsoft Graph for P18-3.M tests.

Only Microsoft's wire is fake. :class:`FakeEntra` is :class:`FakeOAuthProvider`
with an Entra-shaped id_token (``tid``, ``preferred_username``, tenant issuer) and a
token endpoint that answers only on ``https://<login host>/<tenant>/oauth2/v2.0/token``
— a request for any other tenant or host is refused, so a test passes only if Arc
bound the flow to the slot's tenant and cloud. Entra rotates refresh tokens.

:class:`FakeGraph` serves ``/v1.0`` over ``httpx.MockTransport``: every request must
carry an unexpired bearer the fake Entra issued (else 401) and must be on the
expected Graph host. It holds mail (with a delta feed), events and drive items.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from packages.arcagent.tests.oauth_fakes import FakeOAuthProvider

from arcagent.extension.oauth import TokenPost

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@dataclass
class FakeEntra(FakeOAuthProvider):
    """An Entra tenant's token endpoint and sign-in page."""

    client_id: str = CLIENT_ID
    client_secret: str = "entra-secret-value~1"
    tenant: str = TENANT
    login_host: str = "login.microsoftonline.com"
    rotate_refresh: bool = True
    #: Refresh posts that carried a ``scope`` parameter (Entra v2 expects one).
    refresh_scopes_seen: list[str] = field(default_factory=list)
    wrong_endpoint_posts: int = 0
    #: Claims every issued id_token carries on top of the honest ones (abuse tests).
    claims_override: dict[str, Any] = field(default_factory=dict)

    @property
    def token_url(self) -> str:
        return f"https://{self.login_host}/{self.tenant}/oauth2/v2.0/token"

    def consent(self, authorize_url: str, *, email: str, granted_scope: str | None = None) -> str:
        parts = urlsplit(authorize_url)
        assert parts.hostname == self.login_host, parts.hostname
        assert parts.path == f"/{self.tenant}/oauth2/v2.0/authorize", parts.path
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        assert query.get("code_challenge_method") == "S256"
        return super().consent(authorize_url, email=email, granted_scope=granted_scope)

    async def post(self, request: TokenPost) -> tuple[int, dict[str, Any]]:
        if request.url != self.token_url:
            self.wrong_endpoint_posts += 1
            return 400, {"error": "invalid_request"}
        body = dict(request.form or {})
        if body.get("grant_type") == "refresh_token" and body.get("scope"):
            self.refresh_scopes_seen.append(body["scope"])
        return await super().post(request)

    def id_token(self, email: str, **claims: Any) -> str:
        payload = {
            "iss": f"https://{self.login_host}/{self.tenant}/v2.0",
            "aud": self.client_id,
            "tid": self.tenant,
            "preferred_username": email,
            "exp": time.time() + 3600,
            **claims,
            **self.claims_override,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        return f"eyJhbGciOiJSUzI1NiJ9.{encoded}.c2ln"


class FakeGraph:
    """Microsoft Graph ``/v1.0`` for one signed-in user."""

    def __init__(
        self,
        entra: FakeOAuthProvider | None,
        *,
        upn: str,
        host: str = "graph.microsoft.com",
        static_token: str = "",
    ) -> None:
        self.entra = entra
        self.static_token = static_token
        self.upn = upn
        self.host = host
        self.calls: list[httpx.Request] = []
        self.rejected = 0
        self.messages: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.file_content: dict[str, bytes] = {}
        self.delta_generation = 0
        #: Scripted answers to send before the real one: (status, json, headers).
        self.script: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        self.last_bearer = ""

    # --- fixtures -------------------------------------------------------------

    def add_message(self, message_id: str, *, subject: str, body: str, sender: str) -> None:
        self.delta_generation += 1
        self.messages[message_id] = {
            "id": message_id,
            "subject": subject,
            "bodyPreview": body[:40],
            "body": {"contentType": "text", "content": body},
            "from": {"emailAddress": {"name": "Ann", "address": sender}},
            "toRecipients": [{"emailAddress": {"address": self.upn}}],
            "receivedDateTime": f"2026-10-0{min(9, len(self.messages) + 1)}T10:00:00Z",
            "lastModifiedDateTime": f"2026-10-0{min(9, len(self.messages) + 1)}T10:00:00Z",
            "changeKey": f"ck-{message_id}-{self.delta_generation}",
            "conversationId": f"conv-{message_id}",
            "isRead": False,
            "hasAttachments": False,
            "_generation": self.delta_generation,
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- the wire ----------------------------------------------------------------

    def _authorized(self, request: httpx.Request) -> bool:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.last_bearer = bearer
        if self.static_token:
            return bearer == self.static_token
        return self.entra is not None and self.entra.access_valid(bearer)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == self.host, request.url.host
        if not self._authorized(request):
            self.rejected += 1
            return httpx.Response(
                401,
                json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}},
            )
        if self.script:
            status, payload, headers = self.script.pop(0)
            return httpx.Response(status, json=payload, headers=headers)
        path = request.url.path.removeprefix("/v1.0")
        return self._route(request, path)

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/me":
            return httpx.Response(
                200, json={"id": "user-1", "userPrincipalName": self.upn, "mail": self.upn}
            )
        if path == "/me/mailFolders":
            return httpx.Response(
                200, json={"value": [{"id": "inbox-id", "displayName": "Inbox"}]}
            )
        if path.startswith("/me/mailFolders/") and path.endswith("/messages/delta"):
            return self._delta(request)
        if path.startswith("/me/mailFolders/") and path.endswith("/messages"):
            listed = sorted(self.messages.values(), key=lambda m: m["receivedDateTime"])
            return httpx.Response(200, json={"value": [_public(m) for m in reversed(listed)]})
        if path.startswith("/me/messages/"):
            found = self.messages.get(path.rsplit("/", 1)[-1])
            if found is None:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            return httpx.Response(200, json=_public(found))
        if path == "/me/sendMail" and request.method == "POST":
            self.sent.append(json.loads(request.content))
            return httpx.Response(202)
        if path == "/me/events" and request.method == "POST":
            event = json.loads(request.content)
            self.events.append(event)
            return httpx.Response(
                201, json={"id": "ev-1", "webLink": "https://outlook.example/ev"}
            )
        if path in ("/me/events", "/me/calendarView"):
            return httpx.Response(200, json={"value": self.events})
        return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": path}})

    def _delta(self, request: httpx.Request) -> httpx.Response:
        since = int(request.url.params.get("deltatoken") or 0)
        changed = [
            _public(message)
            for message in self.messages.values()
            if message["_generation"] > since
        ]
        gone = [{"id": key, "@removed": {"reason": "deleted"}} for key in self.removed]
        self.removed = []
        link = (
            f"https://{self.host}/v1.0/me/mailFolders/inbox/messages/delta"
            f"?deltatoken={self.delta_generation}"
        )
        return httpx.Response(200, json={"value": changed + gone, "@odata.deltaLink": link})


def _public(message: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in message.items() if not key.startswith("_")}


class StaticCredential:
    """A duck-typed credential handle serving one bearer; counts invalidations."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.invalidations = 0

    async def bearer(self) -> Any:
        from arcagent.extension.secrets import Secret

        return Secret(self.token)

    async def invalidate(self) -> None:
        self.invalidations += 1


__all__ = ["CLIENT_ID", "TENANT", "FakeEntra", "FakeGraph", "StaticCredential"]
