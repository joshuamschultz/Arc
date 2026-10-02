"""arc gateway voice-status / voice-listen / voice-wake / connect-voice --wake (item 13).

The CLI speaks to the running ArcUI over its operator HTTP API, so the server is
faked at the network boundary (``httpx.MockTransport``), as in
``test_agent_promotion_run.py``. Every test uses the bare minimum arguments first:
the untested shape is the bare call.
"""

from __future__ import annotations

import getpass
import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from arccli.commands.gateway_connect import (
    gateway_connect_voice_handler,
    gateway_voice_listen_handler,
    gateway_voice_status_handler,
    gateway_voice_wake_handler,
)

TOKEN = "session-token-do-not-print-7781"
EMAIL = "operator@example.com"
LIVE = {
    "enabled": True,
    "live": {
        "state": "offline",
        "listening": True,
        "reason": "No mic client is connected. Run arc-voice on the box with the microphone.",
        "wake_words": ["olivia"],
        "wake_mode": "stt",
        "adapter": {"up": True, "reason": ""},
        "client": {"up": False, "reason": "No mic client is connected."},
        "engine": {"up": True, "reason": ""},
    },
}


@dataclass
class _Server:
    status: dict[str, Any] = field(default_factory=lambda: dict(LIVE))
    requests: list[httpx.Request] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"token": TOKEN, "role": "operator"})
        if path == "/api/auth/logout":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/agents/olivia/voice" and request.method == "GET":
            return httpx.Response(200, json=self.status)
        if path == "/api/agents/olivia/voice/listening":
            return httpx.Response(200, json={"listening": False, "applied_live": True})
        if path == "/api/agents/olivia/voice/wake":
            words = json.loads(request.content)["words"]
            return httpx.Response(
                200, json={"wake": {"words": words}, "note": "Not a trained wake-word model."}
            )
        return httpx.Response(404, json={"error": "not found"})

    def posted(self, suffix: str) -> dict[str, Any]:
        match = next(r for r in self.requests if r.url.path.endswith(suffix))
        return json.loads(match.content)


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


def _run(handler: Callable[[list[str]], None], *argv: str) -> int:
    try:
        handler(list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def test_voice_status_minimum_args_shows_each_part_and_the_reason(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(gateway_voice_status_handler, "olivia", "--email", EMAIL) == 0
    out = capsys.readouterr().out
    assert "OFFLINE" in out
    assert "mic client" in out and "DOWN" in out
    assert "gateway" in out and "speech engine" in out
    assert "listening      ON" in out
    assert "olivia" in out
    assert "arc-voice" in out  # the plain reason says what to run


def test_voice_status_not_connected_is_one_plain_line(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.status = {"enabled": False}
    assert _run(gateway_voice_status_handler, "olivia", "--email", EMAIL) == 0
    assert "not connected" in capsys.readouterr().out


def test_voice_listen_off_posts_false(server: _Server, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(gateway_voice_listen_handler, "olivia", "off", "--email", EMAIL) == 0
    assert server.posted("/voice/listening") == {"on": False}
    assert "OFF" in capsys.readouterr().out


def test_voice_listen_rejects_a_bad_state(server: _Server) -> None:
    assert _run(gateway_voice_listen_handler, "olivia", "maybe", "--email", EMAIL) != 0
    assert not any(r.url.path.endswith("/listening") for r in server.requests)


def test_voice_wake_minimum_args_sends_words_and_prints_the_honest_note(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(gateway_voice_wake_handler, "olivia", "computer", "--email", EMAIL) == 0
    assert server.posted("/voice/wake") == {"words": ["computer"]}
    out = capsys.readouterr().out
    assert "computer" in out
    assert "Not a trained" in out


def test_voice_commands_never_print_the_session_token(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(gateway_voice_status_handler, "olivia", "--email", EMAIL)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "hidden-pass" not in captured.out + captured.err


def _agent(tmp_path: Path) -> Path:
    directory = tmp_path / "olivia_agent"
    directory.mkdir()
    (directory / "arcagent.toml").write_text(
        '[agent]\nname = "olivia"\n[identity]\ndid = "did:arc:local:executor/olivia01"\n',
        encoding="utf-8",
    )
    return directory


def test_connect_voice_bare_call_writes_listening_on_and_default_wake(tmp_path: Path) -> None:
    gw = tmp_path / "gateway.toml"
    code = _run(
        gateway_connect_voice_handler,
        "--agent",
        str(_agent(tmp_path)),
        "--gateway-config",
        str(gw),
        "--env-file",
        str(tmp_path / "arc.env"),
    )
    assert code == 0
    voice = tomllib.loads(gw.read_text())["platforms"]["voice"]
    assert voice["listening"] is True
    assert voice["wake"]["words"] == ["olivia"]


def test_connect_voice_wake_flag_is_validated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gw = tmp_path / "gateway.toml"
    argv = [
        "--agent",
        str(_agent(tmp_path)),
        "--gateway-config",
        str(gw),
        "--env-file",
        str(tmp_path / "arc.env"),
    ]
    assert _run(gateway_connect_voice_handler, *argv, "--wake", "bad:word") == 1
    assert not gw.exists()
    assert "wake word" in capsys.readouterr().err

    assert (
        _run(gateway_connect_voice_handler, *argv, "--wake", "jarvis", "--wake", "hey jarvis") == 0
    )
    assert tomllib.loads(gw.read_text())["platforms"]["voice"]["wake"]["words"] == [
        "jarvis",
        "hey jarvis",
    ]
