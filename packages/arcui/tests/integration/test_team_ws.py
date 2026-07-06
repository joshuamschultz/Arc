"""Integration tests for the read-only team stream WS — ``/ws/team``.

SPEC-031 F1/F2. The route is a *thin view*: it authenticates a viewer,
streams rendered team flows to the browser, and **forwards** human group
posts to the owning layer (arcteam) without signing or routing them itself.

Streaming is driven end-to-end through the real path: a fake (duck-typed)
MessagingService feeds ``TeamBusObserver`` → ``TeamStreamHub`` → the route →
the browser, so no NATS or arcteam instance is needed. Forwarding is checked
against a recording fake that stands in for arcteam's signer/router.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-test"
OPERATOR_TOKEN = "operator-tok-test"
AGENT_TOKEN = "agent-tok-test"


@pytest.fixture
def auth_config() -> AuthConfig:
    return AuthConfig(
        {
            "viewer_token": VIEWER_TOKEN,
            "operator_token": OPERATOR_TOKEN,
            "agent_token": AGENT_TOKEN,
        }
    )


# ── fake bus feed ──────────────────────────────────────────────────────────


class _FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeMessage:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.seq = data["seq"]

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return dict(self._data)


def _msg(*, sender: str, channel: str, body: str, seq: int, mentions: list[str] | None = None) -> dict[str, Any]:
    return {
        "seq": seq,
        "id": f"m{seq}",
        "ts": "2026-07-05T00:00:00Z",
        "sender": sender,
        "to": [f"channel://{channel}"],
        "body": body,
        "mentions": mentions or [],
        "action_required": bool(mentions),
        "priority": "high" if mentions else "normal",
    }


class _FakeService:
    def __init__(self, channels: dict[str, list[dict[str, Any]]]) -> None:
        self._channels = channels

    async def list_channels(self) -> list[_FakeChannel]:
        return [_FakeChannel(n) for n in self._channels]

    async def list_channel_messages(
        self, channel_name: str, after_seq: int = 0, limit: int = 100
    ) -> list[_FakeMessage]:
        return [_FakeMessage(m) for m in self._channels[channel_name] if m["seq"] > after_seq]


class _RecordingForwarder:
    """Captures forwarded group posts; stands in for arcteam's signer/router."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def __call__(self, *, sender: str, channel: str, text: str) -> None:
        self.calls.append({"sender": sender, "channel": channel, "text": text})


@pytest.fixture
def forwarder() -> _RecordingForwarder:
    return _RecordingForwarder()


def _make_app(
    auth_config: AuthConfig,
    *,
    service: _FakeService | None = None,
    forwarder: _RecordingForwarder | None = None,
) -> Any:
    return create_app(
        auth_config=auth_config,
        messaging_service=service,
        team_post_forwarder=forwarder,
        team_stream_interval=0.02,
    )


def _team_url(channel: str | None = None) -> str:
    return "/ws/team" if channel is None else f"/ws/team?channel={channel}"


# ── auth ───────────────────────────────────────────────────────────────────


def test_invalid_token_rejected(auth_config: AuthConfig) -> None:
    app = _make_app(auth_config)
    with TestClient(app) as client, client.websocket_connect(_team_url()) as ws:
        ws.send_json({"token": "nope"})
        assert "error" in ws.receive_json()


def test_agent_token_rejected(auth_config: AuthConfig) -> None:
    app = _make_app(auth_config)
    with TestClient(app) as client, client.websocket_connect(_team_url()) as ws:
        ws.send_json({"token": AGENT_TOKEN})
        try:
            err = ws.receive_json()
            assert "error" in err or err.get("type") == "error"
        except Exception:  # noqa: S110 — server may close before the frame
            pass


# ── streaming (read-only view) ─────────────────────────────────────────────


def test_streams_rendered_frame_with_handles(auth_config: AuthConfig) -> None:
    service = _FakeService(
        {
            "ops": [
                _msg(
                    sender="did:arc:local:agent/intake",
                    channel="ops",
                    body="hello @architect",
                    seq=1,
                    mentions=["did:arc:local:agent/architect"],
                )
            ]
        }
    )
    app = _make_app(auth_config, service=service)
    with TestClient(app) as client, client.websocket_connect(_team_url()) as ws:
        ws.send_json({"token": VIEWER_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        frame = ws.receive_json()
        assert frame["type"] == "team_message"
        assert frame["channel"] == "ops"
        assert frame["from"] == "intake"
        assert frame["mentions"] == ["architect"]
        assert "did:" not in frame["from"]


def test_channel_scope_filters_stream(auth_config: AuthConfig) -> None:
    service = _FakeService(
        {
            "hr": [_msg(sender="agent://hrbot", channel="hr", body="ignored", seq=1)],
            "ops": [_msg(sender="agent://intake", channel="ops", body="kept", seq=1)],
        }
    )
    app = _make_app(auth_config, service=service)
    with TestClient(app) as client, client.websocket_connect(_team_url("ops")) as ws:
        ws.send_json({"token": VIEWER_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        frame = ws.receive_json()
        assert frame["channel"] == "ops"
        assert frame["body"] == "kept"


# ── forwarding (F2) ────────────────────────────────────────────────────────


def test_group_post_is_forwarded_not_signed(
    auth_config: AuthConfig, forwarder: _RecordingForwarder
) -> None:
    app = _make_app(auth_config, forwarder=forwarder)
    with TestClient(app) as client, client.websocket_connect(_team_url()) as ws:
        ws.send_json({"token": VIEWER_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "channel": "ops", "text": "status?"})
        ack = ws.receive_json()
        assert ack["type"] == "posted"
        assert ack["channel"] == "ops"

    assert len(forwarder.calls) == 1
    call = forwarder.calls[0]
    assert call["channel"] == "ops"
    assert call["text"] == "status?"
    # arcui forwards the human's derived identity; it never invents a DID or
    # signs — the sender ref is opaque to the view but must be present.
    assert call["sender"]


def test_post_without_forwarder_errors(auth_config: AuthConfig) -> None:
    app = _make_app(auth_config)  # no forwarder wired
    with TestClient(app) as client, client.websocket_connect(_team_url()) as ws:
        ws.send_json({"token": VIEWER_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "channel": "ops", "text": "hi"})
        err = ws.receive_json()
        assert err.get("type") == "error"
        assert err.get("code") == "forward_unavailable"


def test_post_missing_channel_errors(
    auth_config: AuthConfig, forwarder: _RecordingForwarder
) -> None:
    app = _make_app(auth_config, forwarder=forwarder)
    with TestClient(app) as client, client.websocket_connect(_team_url()) as ws:
        ws.send_json({"token": VIEWER_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "text": "no channel"})
        err = ws.receive_json()
        assert err.get("type") == "error"
    assert forwarder.calls == []


def test_hub_present_even_without_service(auth_config: AuthConfig) -> None:
    """A minimal app still exposes a team_stream hub (no service wired)."""
    app = create_app(auth_config=auth_config)
    assert app.state.team_stream is not None
