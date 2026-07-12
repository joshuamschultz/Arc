"""Agent schedule editor — PATCH edit (#20).

Operator-gated edit of an entry in the agent's ``schedules.json``. The agent's
scheduler engine re-reads that file every tick, so an atomic write here goes
live without IPC. Drives the real Starlette app with an on-disk agent root:
a viewer is refused (403), an unknown schedule/agent is 404, invalid cron /
bounds are refused (400, audited), and a legit edit lands atomically on disk
(200, audited) with the timing/enabled/prompt updated.
"""

from __future__ import annotations

import json
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

_SCHEDULES = [
    {
        "id": "sched_ffa77e980f06",
        "type": "cron",
        "prompt": "Summarize overnight alerts",
        "enabled": True,
        "expression": "40 10 * * *",
        "timeout_seconds": 300,
        "metadata": {"created_by": "agent", "run_count": 3},
    },
    {
        "id": "sched_interval01",
        "type": "interval",
        "prompt": "Poll the queue",
        "enabled": True,
        "every_seconds": 300,
        "timeout_seconds": 120,
        "metadata": {"created_by": "agent", "run_count": 0},
    },
]


def _build_team_dir(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    agent = root / "alpha_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\n[identity]\ndid = "did:arc:alpha"\n',
        encoding="utf-8",
    )
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "schedules.json").write_text(json.dumps(_SCHEDULES, indent=2), encoding="utf-8")
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
def ctx(tmp_path: Path) -> tuple[TestClient, Path]:
    team_root = _build_team_dir(tmp_path)
    return TestClient(_build_app(team_root)), team_root / "alpha_agent"


def _op() -> dict[str, str]:
    return {"Authorization": "Bearer op"}


def _viewer() -> dict[str, str]:
    return {"Authorization": "Bearer viewer"}


def _load(agent_dir: Path) -> list[dict]:
    return json.loads((agent_dir / "workspace" / "schedules.json").read_text())


def _entry(agent_dir: Path, sid: str) -> dict:
    return next(e for e in _load(agent_dir) if e["id"] == sid)


_URL = "/api/agents/alpha/schedules/sched_ffa77e980f06"


class TestEdit:
    def test_operator_edits_cron_and_prompt(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.patch(
            _URL,
            headers=_op(),
            json={"expression": "0 9 * * 1", "prompt": "Weekly Monday digest", "enabled": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["expression"] == "0 9 * * 1"
        assert body["prompt"] == "Weekly Monday digest"
        assert body["enabled"] is False
        on_disk = _entry(agent_dir, "sched_ffa77e980f06")
        assert on_disk["expression"] == "0 9 * * 1"
        assert on_disk["enabled"] is False
        # Untouched fields survive; the other schedule is left intact.
        assert on_disk["metadata"]["run_count"] == 3
        assert _entry(agent_dir, "sched_interval01")["every_seconds"] == 300

    def test_toggle_enabled_only(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.patch(_URL, headers=_op(), json={"enabled": False})
        assert resp.status_code == 200
        assert _entry(agent_dir, "sched_ffa77e980f06")["enabled"] is False

    def test_edit_interval_seconds(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.patch(
            "/api/agents/alpha/schedules/sched_interval01",
            headers=_op(),
            json={"every_seconds": 600},
        )
        assert resp.status_code == 200
        assert _entry(agent_dir, "sched_interval01")["every_seconds"] == 600

    def test_wrong_type_timing_field_ignored(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        # every_seconds on a cron schedule is not an editable field -> no edits.
        resp = client.patch(_URL, headers=_op(), json={"every_seconds": 600})
        assert resp.status_code == 400
        assert "no editable fields" in resp.json()["error"]

    def test_audits_applied(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            client.patch(_URL, headers=_op(), json={"enabled": False})
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "schedule.update"
            and e["details"]["outcome"] == "applied"
            and e["details"]["target"] == "schedule:sched_ffa77e980f06"
            for e in events
        )


class TestGuards:
    def test_viewer_forbidden(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.patch(_URL, headers=_viewer(), json={"enabled": False})
        assert resp.status_code == 403
        assert _entry(agent_dir, "sched_ffa77e980f06")["enabled"] is True

    def test_unknown_schedule_404(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.patch(
            "/api/agents/alpha/schedules/sched_nope", headers=_op(), json={"enabled": False}
        )
        assert resp.status_code == 404

    def test_invalid_cron_400(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.patch(_URL, headers=_op(), json={"expression": "not a cron"})
        assert resp.status_code == 400
        assert "cron" in resp.json()["error"]
        assert _entry(agent_dir, "sched_ffa77e980f06")["expression"] == "40 10 * * *"

    def test_interval_below_floor_400(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.patch(
            "/api/agents/alpha/schedules/sched_interval01",
            headers=_op(),
            json={"every_seconds": 5},
        )
        assert resp.status_code == 400

    def test_timeout_over_ceiling_400(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.patch(_URL, headers=_op(), json={"timeout_seconds": 99999})
        assert resp.status_code == 400

    def test_empty_prompt_400(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.patch(_URL, headers=_op(), json={"prompt": "   "})
        assert resp.status_code == 400

    def test_no_editable_fields_400(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.patch(_URL, headers=_op(), json={"id": "hacked", "type": "once"})
        assert resp.status_code == 400
