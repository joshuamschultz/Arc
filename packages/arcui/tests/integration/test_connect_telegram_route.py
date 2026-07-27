"""POST /api/agents/{id}/connect-telegram — operator-gated Telegram connect.

Drives the real Starlette app with an on-disk agent root: a viewer is refused (403),
a malformed token is refused (400) writing nothing, and a valid operator submit wires
the per-agent gateway block (200) with the token in the env file — never the config.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes

_GOOD_TOKEN = "8012345678:AAExampleExampleExampleExampleExample1"


def _build_team_dir(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    (root / "sales_agent").mkdir(parents=True)
    (root / "sales_agent" / "arcagent.toml").write_text(
        '[agent]\nname = "sales"\n[identity]\ndid = "did:arc:local:executor/7e3e"\n',
        encoding="utf-8",
    )
    return root


def _build_app(team_root: Path) -> Starlette:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    registry = AgentRegistry()
    app = Starlette(routes=[*agent_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.session_tracker = SessionTracker()
    app.state.team_root = team_root

    def _roster_provider() -> list[team_roster.RosterEntry]:
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(cfg_dir))
    team_root = _build_team_dir(tmp_path)
    return TestClient(_build_app(team_root)), cfg_dir


# The roster id is the agent's short name ([agent].name == "sales" for sales_agent/).
_URL = "/api/agents/sales/connect-telegram"


def test_viewer_is_refused(ctx: tuple[TestClient, Path]) -> None:
    client, _ = ctx
    r = client.post(
        _URL,
        json={"token": _GOOD_TOKEN, "user_id": 8293394811},
        headers={"Authorization": "Bearer viewer"},
    )
    assert r.status_code == 403


def test_bad_token_refused_writes_nothing(ctx: tuple[TestClient, Path]) -> None:
    client, cfg_dir = ctx
    r = client.post(
        _URL,
        json={"token": "not-a-token", "user_id": 1},
        headers={"Authorization": "Bearer op"},
    )
    assert r.status_code == 400
    assert not (cfg_dir / "arc.env").exists()


def test_operator_connect_wires_block_token_in_env(ctx: tuple[TestClient, Path]) -> None:
    client, cfg_dir = ctx
    r = client.post(
        _URL,
        json={"token": _GOOD_TOKEN, "user_id": 8293394811},
        headers={"Authorization": "Bearer op"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is True
    assert body["restart_required"] is True
    assert body["block"] == "sales_agent_telegram"
    # token in env, bound in config, NEVER the token itself in config
    assert _GOOD_TOKEN in (cfg_dir / "arc.env").read_text()
    gw = tomllib.loads((cfg_dir / "gateway.toml").read_text())
    assert gw["platforms"]["sales_agent_telegram"]["agent_did"] == "did:arc:local:executor/7e3e"
    assert _GOOD_TOKEN not in (cfg_dir / "gateway.toml").read_text()


def test_user_id_must_be_int(ctx: tuple[TestClient, Path]) -> None:
    client, _ = ctx
    r = client.post(
        _URL,
        json={"token": _GOOD_TOKEN, "user_id": "not-a-number"},
        headers={"Authorization": "Bearer op"},
    )
    assert r.status_code == 400
