"""SPEC-083 T-1224 (COMP-029, REQ-512) — ``arc agent promotion run``: "Run now" from the CLI.

RED intent: ``arc agent promotion`` has no ``run`` verb yet, so argparse refuses
``run`` as an invalid choice — the command is absent.

Channel assumed (how the CLI reaches the RUNNING agent): the serve process
(``arc ui`` / ``arc.service``) holds the running agents in-process
(``app.state.embedded_agent_cache``); the CLI never builds a second, offline
agent. Like ``arc queue``, the CLI speaks to that process over ArcUI's
account-authenticated HTTP API:

    POST /api/auth/login  {"email", "password"}  -> {"token", "role"}
    POST /api/agents/{id}/memory/promotion/run   {"max_items"?}  (Bearer token)
         -> {"status", "evaluated", "promoted", "kept_private",
             "blocked_secret", "too_large", "deferred"}
    POST /api/auth/logout                        (always, even on failure)

Command surface assumed::

    arc agent promotion run <agent-dir-or-name> [--max-items N] [--json]
                            [--url URL] --email EMAIL

- ``<agent-dir-or-name>``: an agent directory resolves to its ``[agent].name``
  (the roster id ArcUI routes on); anything else is taken as the agent name and
  must be a single safe path segment.
- The password is read from a hidden prompt (``getpass``); there is no password
  or token flag (credentials on argv are shell history).
- Operator only: a non-operator session is refused before the run request.
- ``--max-items`` is validated client-side (1..5000) before any network call.
- Plain HTTP only to loopback; a remote URL must be HTTPS (checked before any
  credential is sent).
- The audit record ``memory.promotion.manual_run`` (actor, agent, cap; no
  content) is written server-side by the route — see
  ``packages/arcui/tests/test_memory_promotion_run_route.py``.

The server is faked at the network boundary (``httpx.MockTransport``), as in
``test_queue_command.py``.
"""

from __future__ import annotations

import getpass
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from arccli.commands.agent import agent_handler

TOKEN = "session-token-do-not-print-7781"
PASSWORD = "hidden-pass-5521"
EMAIL = "operator@example.com"
RUN_PATH = "/api/agents/olivia/memory/promotion/run"
RESULT = {
    "status": "completed",
    "evaluated": 12,
    "promoted": 3,
    "kept_private": 7,
    "blocked_secret": 1,
    "too_large": 1,
    "deferred": 40,
}


@dataclass
class _Server:
    """A fake ArcUI: records every request; answers login, run and logout."""

    role: str = "operator"
    run_status: int = 200
    run_body: dict[str, Any] = field(default_factory=lambda: dict(RESULT))
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
        if request.method == "POST" and path.endswith("/memory/promotion/run"):
            return httpx.Response(self.run_status, json=self.run_body)
        return httpx.Response(404, json={"error": "not found"})

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def runs(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/memory/promotion/run")]


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


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "team" / "olivia_agent"
    directory.mkdir(parents=True)
    (directory / "arcagent.toml").write_text(
        '[agent]\nname = "olivia"\norg = "local"\n\n[identity]\ndid = "did:arc:local:executor/olivia01"\n',
        encoding="utf-8",
    )
    return directory


def _run(*argv: str) -> int:
    """Invoke ``arc agent promotion run ...`` in-process; return the exit code."""
    try:
        agent_handler(["promotion", "run", *argv])
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode() or "{}")


# -- the happy path -------------------------------------------------------------


def test_run_logs_in_posts_to_the_running_agent_and_logs_out(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run("olivia", "--email", EMAIL)

    assert code == 0, capsys.readouterr().err
    assert server.paths == ["/api/auth/login", RUN_PATH, "/api/auth/logout"]
    login = _body(server.requests[0])
    assert login == {"email": EMAIL, "password": PASSWORD}
    assert server.runs()[0].method == "POST"


def test_run_without_max_items_sends_no_cap(server: _Server) -> None:
    assert _run("olivia", "--email", EMAIL) == 0

    assert "max_items" not in _body(server.runs()[0])


def test_max_items_is_sent_as_an_integer(server: _Server) -> None:
    assert _run("olivia", "--max-items", "2000", "--email", EMAIL) == 0

    assert _body(server.runs()[0]) == {"max_items": 2000}


def test_prints_status_and_every_count(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("olivia", "--email", EMAIL) == 0

    out = capsys.readouterr().out
    assert "completed" in out
    for name in ("evaluated", "promoted", "kept_private", "deferred"):
        pattern = rf"{name.replace('_', '[ _]')}\W+{RESULT[name]}\b"
        assert re.search(pattern, out, re.IGNORECASE), f"{name} count missing from:\n{out}"


def test_json_prints_the_result_object(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("olivia", "--json", "--email", EMAIL) == 0

    assert json.loads(capsys.readouterr().out) == RESULT


def test_non_completed_status_is_reported_verbatim(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """Federal / disabled runs are results, not errors: the operator sees why."""
    server.run_body = {**{k: 0 for k in RESULT if k != "status"}, "status": "tier_forbidden"}

    code = _run("olivia", "--email", EMAIL)

    assert code == 0
    assert "tier_forbidden" in capsys.readouterr().out


def test_session_token_and_password_are_never_printed(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("olivia", "--json", "--email", EMAIL) == 0

    captured = capsys.readouterr()
    for secret in (TOKEN, PASSWORD):
        assert secret not in captured.out
        assert secret not in captured.err


# -- agent addressing -------------------------------------------------------------


def test_an_agent_directory_resolves_to_its_roster_name(server: _Server, agent_dir: Path) -> None:
    assert _run(str(agent_dir), "--email", EMAIL) == 0

    assert server.runs()[0].url.path == RUN_PATH


@pytest.mark.parametrize(
    "name", ["../olivia", "olivia/../../keys", "olivia?x=1", "olivia#frag", ""]
)
def test_an_unsafe_agent_name_is_refused_before_any_request(
    server: _Server, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """A name must not steer the request onto another route (path/query injection)."""
    code = _run(name, "--email", EMAIL)

    assert code != 0
    assert server.requests == []
    assert "name" in capsys.readouterr().err.lower()


# -- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize("cap", ["0", "-1", "5001", "abc", "2.5"])
def test_out_of_range_max_items_is_refused_before_any_request(
    server: _Server, capsys: pytest.CaptureFixture[str], cap: str
) -> None:
    code = _run("olivia", "--max-items", cap, "--email", EMAIL)

    assert code != 0
    assert server.requests == []
    assert "max-items" in capsys.readouterr().err.lower()


def test_max_items_at_the_ceiling_is_accepted(server: _Server) -> None:
    assert _run("olivia", "--max-items", "5000", "--email", EMAIL) == 0

    assert _body(server.runs()[0]) == {"max_items": 5000}


def test_a_viewer_session_is_refused_before_the_run_request(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.role = "viewer"

    code = _run("olivia", "--email", EMAIL)

    assert code != 0
    assert server.runs() == []
    assert server.paths[-1] == "/api/auth/logout"
    assert "operator" in capsys.readouterr().err.lower()


def test_remote_plain_http_is_refused_before_credentials_are_sent(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run("olivia", "--url", "http://example.com:8420", "--email", EMAIL)

    assert code != 0
    assert server.requests == []
    assert "https" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("flag", ["--password", "--token"])
def test_there_is_no_credential_flag(
    server: _Server, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    code = _run("olivia", flag, "leak-me", "--email", EMAIL)

    assert code != 0
    assert server.requests == []
    assert flag in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, "Operator role required"),
        (404, "Agent not found"),
        (422, "max_items must be between 1 and 5000"),
        (503, "agent is not running"),
    ],
)
def test_a_server_refusal_exits_nonzero_with_its_message_and_still_logs_out(
    server: _Server, capsys: pytest.CaptureFixture[str], status: int, error: str
) -> None:
    server.run_status = status
    server.run_body = {"error": error}

    code = _run("olivia", "--email", EMAIL)

    assert code != 0
    captured = capsys.readouterr()
    assert error in captured.err
    assert "Traceback" not in captured.err
    assert server.paths[-1] == "/api/auth/logout"


def test_an_unreachable_server_exits_nonzero_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = httpx.Client

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return original(transport=httpx.MockTransport(refuse), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": PASSWORD)

    code = _run("olivia", "--email", EMAIL)

    assert code != 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert re.search(r"unavailable|unreachable|connect", err, re.IGNORECASE), err
