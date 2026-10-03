"""A fake Atlassian 3LO provider and Jira REST site for P18-3 (3.T) tests.

Only Atlassian's HTTP is fake. The provider enforces what Atlassian enforces: no
PKCE, JSON token bodies, ROTATING refresh tokens (a used token is dead, answered
``403 invalid_grant``), and ``accessible-resources`` listing the sites a token
reaches. The Jira fake answers only for a cloud id the bearer was issued for and
401s an expired or unknown access token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from packages.arcagent.tests.oauth_fakes import FakeOAuthProvider

from arcagent.extension.oauth import TokenPost

SITE_A = ("cloud-aaaa-1111", "https://acme.atlassian.net")
SITE_B = ("cloud-bbbb-2222", "https://other.atlassian.net")


@dataclass
class FakeAtlassian(FakeOAuthProvider):
    """Token endpoint, consent screen and ``accessible-resources`` in one."""

    client_id: str = "atl-client-1234567890"
    client_secret: str = "atl-secret-xyz"
    rotate_refresh: bool = True
    sites: list[tuple[str, str]] = field(default_factory=lambda: [SITE_A])
    resource_gets: int = 0

    async def post(self, request: TokenPost) -> tuple[int, dict[str, Any]]:
        if request.method == "GET":
            return self._resources(request)
        status, body = await super().post(request)
        if status == 400 and body.get("error") == "invalid_grant":
            return 403, body  # Atlassian answers a dead or reused refresh token 403
        return status, body

    def _resources(self, request: TokenPost) -> tuple[int, dict[str, Any]]:
        self.resource_gets += 1
        if request.bearer is None or not self.access_valid(request.bearer):
            return 401, {}
        return 200, {
            "items": [
                {"id": cloud_id, "url": url, "name": url.split("//")[1], "scopes": []}
                for cloud_id, url in self.sites
            ]
        }


class FakeJira:
    """Jira REST v3 for one provider's tokens, over ``httpx.MockTransport``."""

    def __init__(self, provider: FakeAtlassian, *, cloud_id: str = SITE_A[0]) -> None:
        self.provider = provider
        self.cloud_id = cloud_id
        self.requests: list[httpx.Request] = []
        self.rejected = 0
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: list[tuple[str, dict[str, Any]]] = []

    def add_issue(self, key: str, *, summary: str, description: str = "") -> None:
        self.issues[key] = {
            "id": str(1000 + len(self.issues)),
            "key": key,
            "fields": {
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                    ],
                },
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
                "updated": "2026-10-02T10:00:00.000+0000",
                "project": {"key": key.split("-")[0]},
            },
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def bearers(self) -> list[str]:
        return [r.headers.get("authorization", "").removeprefix("Bearer ") for r in self.requests]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not self.provider.access_valid(bearer):
            self.rejected += 1
            return httpx.Response(401, json={"message": "Unauthorized"})
        prefix = f"/ex/jira/{self.cloud_id}/rest/api/3/"
        path = request.url.path
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "no such site"})
        return self._route(request.method, path.removeprefix(prefix), request)

    def _route(self, method: str, path: str, request: httpx.Request) -> httpx.Response:
        if path == "myself":
            return httpx.Response(200, json={"accountId": "acc-1", "displayName": "Ann"})
        if path == "search/jql":
            start = int(request.url.params.get("nextPageToken") or 0)
            size = int(request.url.params.get("maxResults") or 50)
            ordered = list(self.issues.values())
            chunk = ordered[start : start + size]
            more = start + size < len(ordered)
            body: dict[str, Any] = {"issues": chunk}
            if more:
                body["nextPageToken"] = str(start + size)
            return httpx.Response(200, json=body)
        if path == "project/search":
            return httpx.Response(
                200,
                json={"values": [{"id": "1", "key": "ARC", "name": "Arc"}], "isLast": True},
            )
        if path.startswith("issue/") and method == "GET" and path.count("/") == 1:
            found = self.issues.get(path.split("/")[1])
            if found is None:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
            return httpx.Response(200, json=found)
        if path.endswith("/comment") and method == "POST":
            self.comments.append((path.split("/")[1], json.loads(request.content)))
            return httpx.Response(201, json={"id": "c-1"})
        return httpx.Response(404, json={"errorMessages": [f"no route {method} {path}"]})
