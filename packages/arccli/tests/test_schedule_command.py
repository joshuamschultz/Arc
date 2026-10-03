"""``arc schedule list|approve`` — re-approve a legacy schedule on the running agent.

The server is faked at the network boundary (``httpx.MockTransport``): the CLI logs
in, reads ``GET /api/agents/{id}/schedules`` and approves with
``POST /api/agents/{id}/schedules/{sid}/approve``. It never signs anything itself.
"""

from __future__ import annotations

import builtins
import getpass
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from arccli.commands.schedule import schedule_handler

TOKEN = "session-token-do-not-print-7781"
EMAIL = "operator@example.com"
LIST_PATH = "/api/agents/olivia/schedules"
APPROVE_PATH = "/api/agents/olivia/schedules/wf:nightly/approve"


def _row(sid: str, *, approved: bool, reason: str | None = None) -> dict[str, Any]:
    return {
        "id": sid,
        "type": "cron",
        "expression": "0 22 * * *",
        "enabled": approved,
        "approval": {"revision": 1} if approved else None,
        "metadata": {"disabled_reason": reason},
    }


@dataclass
class _Server:
    approve_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"token": TOKEN, "role": "operator"})
        if path == "/api/auth/logout":
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and path == LIST_PATH:
            return httpx.Response(
                200,
                json={
                    "schedules": [
                        _row("wf:nightly", approved=False, reason="unapproved"),
                        _row("s_ok", approved=True),
                    ]
                },
            )
        if request.method == "POST" and path == APPROVE_PATH:
            if self.approve_status != 200:
                return httpx.Response(self.approve_status, json={"error": "refused"})
            return httpx.Response(200, json={"approval": {"revision": 1}})
        return httpx.Response(404, json={"error": "not found"})

    def posts(self) -> list[str]:
        return [
            r.url.path for r in self.requests if r.method == "POST" and "approve" in r.url.path
        ]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    fake = _Server()
    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return original(transport=httpx.MockTransport(fake.respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": "hidden-pass")
    return fake


def _run(*argv: str) -> int:
    try:
        schedule_handler(list(argv))
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else (code if isinstance(code, int) else 1)
    return 0


def test_list_flags_the_unapproved_schedule(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("list", "--agent", "olivia", "--email", EMAIL) == 0
    out = capsys.readouterr().out
    assert "wf:nightly" in out and "Needs approval" in out
    assert "s_ok" in out


def test_approve_with_yes_posts_once(server: _Server) -> None:
    assert _run("approve", "wf:nightly", "--agent", "olivia", "--email", EMAIL, "--yes") == 0
    assert server.posts() == [APPROVE_PATH]


def test_approve_declined_posts_nothing(server: _Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "n")
    assert _run("approve", "wf:nightly", "--agent", "olivia", "--email", EMAIL) != 0
    assert server.posts() == []


def test_approve_refused_by_server_fails(server: _Server) -> None:
    server.approve_status = 403
    assert _run("approve", "wf:nightly", "--agent", "olivia", "--email", EMAIL, "--yes") != 0


def test_approve_requires_agent_and_email(server: _Server) -> None:
    assert _run("approve", "wf:nightly") != 0
    assert server.requests == []
