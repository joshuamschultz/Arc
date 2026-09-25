"""Queue CLI acts through the account-authenticated ArcUI coordinator."""

from __future__ import annotations

import httpx
import pytest

from arccli.commands import queue


def test_pause_logs_in_uses_versioned_api_and_logs_out(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/auth/login":
            assert '"password":"hidden-pass"' in body
            return httpx.Response(200, json={"token": "secret-session", "role": "operator"})
        assert request.headers["Authorization"] == "Bearer secret-session"
        if request.url.path == "/api/queue/pause":
            assert '"expected_revision":7' in body
            return httpx.Response(200, json={"revision": 8, "paused": True})
        return httpx.Response(200, json={"ok": True})

    original = httpx.Client
    monkeypatch.setattr(queue.getpass, "getpass", lambda _prompt: "hidden-pass")
    monkeypatch.setattr(
        queue.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    queue.queue_handler(["--email", "operator@example.com", "pause", "--expected-revision", "7"])
    assert [path for _, path, _ in calls] == [
        "/api/auth/login",
        "/api/queue/pause",
        "/api/auth/logout",
    ]
    assert "secret-session" not in capsys.readouterr().out


def test_cli_refuses_remote_http_before_sending_credentials(monkeypatch) -> None:
    monkeypatch.setattr(queue.getpass, "getpass", lambda _prompt: "hidden-pass")
    with pytest.raises(SystemExit):
        queue.queue_handler(
            ["--url", "http://example.com:8420", "--email", "operator@example.com", "status"]
        )


def test_cli_has_no_password_or_token_arguments() -> None:
    with pytest.raises(SystemExit):
        queue.queue_handler(["--email", "operator@example.com", "--password", "leak", "status"])
