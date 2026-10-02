"""Voice card routes: live status, listening toggle, typed wake word (item 13)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.config import WakeConfig
from arcgateway.connect import connect_voice
from arcgateway.team_roster import RosterEntry
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.agent_detail import connect_voice as voice_routes
from arcui.routes.agent_detail import routes

_DID = "did:arc:test:agent/alpha"


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        voice_routes, "emit_mutation_audit", lambda _request, **fields: recorded.append(fields)
    )
    return recorded


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AuthConfig, Path]:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    agent = tmp_path / "alpha"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(f'[identity]\ndid = "{_DID}"\n', encoding="utf-8")
    gateway = tmp_path / "config" / "gateway.toml"
    connect_voice(agent_did=_DID, gateway_config=gateway, env_file=tmp_path / "config" / "arc.env")

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="alpha",
            name="alpha",
            did=_DID,
            org=None,
            type=None,
            workspace_path=str(agent),
            model=None,
            provider=None,
            online=True,
            display_name="alpha",
            color="#000000",
            role_label="",
            hidden=False,
        )
    ]
    return TestClient(app), auth, gateway


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _noop(_draft: object) -> None:
    return None


def _attach_adapter(client: TestClient) -> VoiceAdapter:
    adapter = VoiceAdapter(
        on_message=_noop, agent_did=_DID, chat_id="olivia", engine_names=("whisper", "kokoro")
    )
    client.app.state.embedded_gateway = SimpleNamespace(adapters=[adapter])  # type: ignore[attr-defined]
    return adapter


def test_listening_requires_operator_and_audits(
    world: tuple[TestClient, AuthConfig, Path], audits: list[dict[str, Any]]
) -> None:
    client, auth, gateway = world
    denied = client.post(
        "/api/agents/alpha/voice/listening", json={"on": False}, headers=_hdr(auth.viewer_token)
    )
    assert denied.status_code == 403
    assert audits[-1]["operation"] == "voice_listening"
    assert audits[-1]["outcome"] == "denied"
    assert tomllib.loads(gateway.read_text())["platforms"]["voice"]["listening"] is True

    ok = client.post(
        "/api/agents/alpha/voice/listening", json={"on": False}, headers=_hdr(auth.operator_token)
    )
    assert ok.status_code == 200
    assert ok.json()["listening"] is False
    assert audits[-1]["operation"] == "voice_listening"
    assert audits[-1]["outcome"] == "allow"
    assert audits[-1]["detail"] == "off"
    assert tomllib.loads(gateway.read_text())["platforms"]["voice"]["listening"] is False


def test_listening_rejects_non_boolean(world: tuple[TestClient, AuthConfig, Path]) -> None:
    client, auth, _gw = world
    bad = client.post(
        "/api/agents/alpha/voice/listening", json={"on": "yes"}, headers=_hdr(auth.operator_token)
    )
    assert bad.status_code == 400


def test_live_status_absent_adapter_is_offline(world: tuple[TestClient, AuthConfig, Path]) -> None:
    client, auth, _gw = world
    body = client.get("/api/agents/alpha/voice", headers=_hdr(auth.viewer_token)).json()
    live = body["live"]
    assert live["client_connected"] is False
    assert live["state"] == "offline"
    assert live["adapter"]["up"] is False
    assert "Restart the gateway" in live["reason"]
    assert live["wake_words"] == ["olivia"]


def test_live_status_comes_from_the_hosted_adapter(
    world: tuple[TestClient, AuthConfig, Path],
) -> None:
    client, auth, _gw = world
    _attach_adapter(client)
    live = client.get("/api/agents/alpha/voice", headers=_hdr(auth.viewer_token)).json()["live"]
    assert live["client_connected"] is False
    assert live["adapter"]["up"] is False  # built but never connected in this test
    assert "adapter is not running" in live["reason"]
    assert live["engine"]["stt"] == "whisper"


async def test_toggle_is_applied_live_without_restart(
    world: tuple[TestClient, AuthConfig, Path],
) -> None:
    client, auth, _gw = world
    adapter = _attach_adapter(client)
    reply = client.post(
        "/api/agents/alpha/voice/listening", json={"on": False}, headers=_hdr(auth.operator_token)
    )
    assert reply.json()["applied_live"] is True
    assert adapter.listening is False


def test_wake_word_is_validated_persisted_and_honest(
    world: tuple[TestClient, AuthConfig, Path], audits: list[dict[str, Any]]
) -> None:
    client, auth, gateway = world
    bad = client.post(
        "/api/agents/alpha/voice/wake",
        json={"words": ["bad:word"]},
        headers=_hdr(auth.operator_token),
    )
    assert bad.status_code == 400
    assert "wake word" in bad.json()["error"]

    viewer = client.post(
        "/api/agents/alpha/voice/wake",
        json={"words": ["computer"]},
        headers=_hdr(auth.viewer_token),
    )
    assert viewer.status_code == 403

    ok = client.post(
        "/api/agents/alpha/voice/wake",
        json={"words": ["Computer"], "match": "exact"},
        headers=_hdr(auth.operator_token),
    )
    assert ok.status_code == 200
    assert "not a trained" in ok.json()["note"]
    stored = tomllib.loads(gateway.read_text())["platforms"]["voice"]["wake"]
    assert stored["words"] == ["computer"]
    assert stored["match"] == "exact"
    assert audits[-1]["operation"] == "voice_wake"
    assert WakeConfig.model_validate(stored).words == ["computer"]


def test_listening_refused_when_voice_not_connected_to_this_agent(
    world: tuple[TestClient, AuthConfig, Path],
) -> None:
    client, auth, gateway = world
    gateway.write_text("", encoding="utf-8")
    reply = client.post(
        "/api/agents/alpha/voice/listening", json={"on": True}, headers=_hdr(auth.operator_token)
    )
    assert reply.status_code == 409
