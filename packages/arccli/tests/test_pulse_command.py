"""``arc pulse status|approve`` — review and approve pulse checks on the running agent.

Like ``arc agent promotion run``, the CLI never builds an offline agent: it logs in
to the serve process over ArcUI's HTTP API, reads ``GET /api/agents/{id}/pulse``,
and approves with ``POST /api/agents/{id}/pulse/approve {check, definition_digest}``.
The server is faked at the network boundary (``httpx.MockTransport``).
"""

from __future__ import annotations

import builtins
import getpass
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from arccli.commands.pulse import pulse_handler

TOKEN = "session-token-do-not-print-7781"
PASSWORD = "hidden-pass-5521"
EMAIL = "operator@example.com"
LIST_PATH = "/api/agents/olivia/pulse"
APPROVE_PATH = "/api/agents/olivia/pulse/approve"


def _check(name: str, status: str = "unapproved", diff: str = "+action: Sweep") -> dict[str, Any]:
    return {
        "name": name,
        "interval_minutes": 5,
        "action": "Sweep",
        "definition_digest": f"digest-{name}",
        "status": status,
        "approved": status == "approved",
        "stale": status == "changes_pending",
        "approved_revision": 1 if status != "unapproved" else None,
        "last_revision_ran": None,
        "diff": diff,
    }


@dataclass
class _Server:
    role: str = "operator"
    checks: list[dict[str, Any]] = field(
        default_factory=lambda: [
            _check("health"),
            _check("inbox", "changes_pending", "-action: Old\n+action: New"),
            _check("done", "approved", ""),
        ]
    )
    approve_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"token": TOKEN, "role": self.role})
        if path == "/api/auth/logout":
            return httpx.Response(200, json={"ok": True})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthenticated"})
        if request.method == "GET" and path == LIST_PATH:
            return httpx.Response(200, json={"checks": self.checks, "authority_available": True})
        if request.method == "POST" and path == APPROVE_PATH:
            if self.approve_status != 200:
                return httpx.Response(self.approve_status, json={"error": "pulse check changed"})
            return httpx.Response(200, json={"revision": 2, "approved": True})
        return httpx.Response(404, json={"error": "not found"})

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def approvals(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == APPROVE_PATH]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    fake = _Server()
    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return original(transport=httpx.MockTransport(fake.respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": PASSWORD)
    return fake


def _run(*argv: str) -> int:
    try:
        pulse_handler(list(argv))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def _answer(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": reply)


# -- status -----------------------------------------------------------------------


def test_status_with_minimum_args_lists_every_check(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("status", "--agent", "olivia", "--email", EMAIL) == 0

    assert server.paths == ["/api/auth/login", LIST_PATH, "/api/auth/logout"]
    out = capsys.readouterr().out
    assert "health" in out and "unapproved" in out
    assert "inbox" in out and "changes pending" in out
    assert "done" in out and "approved" in out


def test_status_json_prints_the_checks(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("status", "--agent", "olivia", "--email", EMAIL, "--json") == 0

    wire = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in wire["checks"]] == ["health", "inbox", "done"]


# -- approve ----------------------------------------------------------------------


def test_approve_yes_posts_each_pending_check_with_the_digest_it_listed(
    server: _Server,
) -> None:
    assert _run("approve", "--agent", "olivia", "--email", EMAIL, "--yes") == 0

    assert server.approvals() == [
        {"check": "health", "definition_digest": "digest-health"},
        {"check": "inbox", "definition_digest": "digest-inbox"},
    ]
    assert server.paths[-1] == "/api/auth/logout"


def test_approve_check_limits_to_one_check(server: _Server) -> None:
    assert _run("approve", "--agent", "olivia", "--email", EMAIL, "--check", "inbox", "--yes") == 0

    assert server.approvals() == [{"check": "inbox", "definition_digest": "digest-inbox"}]


def test_approve_shows_the_diff_and_declining_posts_nothing(
    server: _Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answer(monkeypatch, "n")

    code = _run("approve", "--agent", "olivia", "--email", EMAIL, "--check", "inbox")

    assert code != 0
    assert server.approvals() == []
    assert "-action: Old" in capsys.readouterr().out


def test_approve_confirmed_posts(server: _Server, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, "y")

    assert _run("approve", "--agent", "olivia", "--email", EMAIL, "--check", "health") == 0

    assert server.approvals() == [{"check": "health", "definition_digest": "digest-health"}]


def test_approve_with_nothing_pending_posts_nothing(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.checks = [_check("done", "approved", "")]

    assert _run("approve", "--agent", "olivia", "--email", EMAIL, "--yes") == 0

    assert server.approvals() == []
    assert "nothing" in capsys.readouterr().out.lower()


def test_approve_unknown_check_is_refused(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run("approve", "--agent", "olivia", "--email", EMAIL, "--check", "ghost", "--yes")

    assert code != 0
    assert server.approvals() == []
    assert "ghost" in capsys.readouterr().err


def test_a_server_refusal_is_reported_and_fails(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.approve_status = 409

    code = _run("approve", "--agent", "olivia", "--email", EMAIL, "--yes")

    assert code != 0
    assert "pulse check changed" in capsys.readouterr().err


# -- minimum args and refusals ----------------------------------------------------


def test_bare_group_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run() == 0
    assert "approve" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["status", "approve"])
def test_missing_agent_or_email_is_a_clean_error_before_any_request(
    server: _Server, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    assert _run(verb) != 0
    assert _run(verb, "--agent", "olivia") != 0
    assert _run(verb, "--email", EMAIL) != 0

    assert server.requests == []
    assert "--agent" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["../olivia", "olivia?x=1", "olivia#frag"])
def test_an_unsafe_agent_name_is_refused_before_any_request(server: _Server, name: str) -> None:
    assert _run("approve", "--agent", name, "--email", EMAIL, "--yes") != 0

    assert server.requests == []


def test_a_viewer_session_cannot_approve(server: _Server) -> None:
    server.role = "viewer"

    assert _run("approve", "--agent", "olivia", "--email", EMAIL, "--yes") != 0

    assert server.approvals() == []


def test_session_token_and_password_are_never_printed(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    _run("approve", "--agent", "olivia", "--email", EMAIL, "--yes")

    captured = capsys.readouterr()
    for secret in (TOKEN, PASSWORD):
        assert secret not in captured.out and secret not in captured.err
