"""One "needs you" inbox: pending pulse checks and unapproved schedules (UJ-9).

The DGX ``daily_briefing`` waited 19 times because a pending pulse check lived
only in Agent -> Pulse. ``GET /api/home/needs`` now carries it, and approving
goes through the pulse subsystem's own approve route (no second approval path).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from arcui.routes.home import routes as home_routes

from .test_pulse_approval_routes import APPROVE, _build_app, _op, _viewer

PULSE = "## daily_briefing\n- **Interval:** 60 min\n- **Action:** Brief the operator\n"


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[TestClient, Path]:
    team_root = tmp_path / "team"
    agent = team_root / "alpha_agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\n[identity]\ndid = "did:arc:alpha"\n', encoding="utf-8"
    )
    (agent / "workspace" / "pulse.md").write_text(PULSE, encoding="utf-8")
    app = _build_app(team_root, tmp_path)
    app.router.routes.extend(home_routes)
    return TestClient(app), agent / "workspace"


def _needs(client: TestClient) -> dict:
    return client.get("/api/home/needs", headers=_viewer()).json()


def test_pending_pulse_check_appears_in_the_inbox(ctx: tuple[TestClient, Path]) -> None:
    client, _ = ctx
    body = _needs(client)
    assert body["pulse"]["count"] == 1
    item = body["pulse"]["items"][0]
    assert item["agent_id"] == "alpha"
    assert item["check"] == "daily_briefing"
    assert item["action"] == "Brief the operator"
    assert len(item["definition_digest"]) == 64
    assert body["total"] >= 1


def test_approving_through_the_pulse_route_clears_it_and_makes_it_runnable(
    ctx: tuple[TestClient, Path],
) -> None:
    client, _ = ctx
    item = _needs(client)["pulse"]["items"][0]
    resp = client.post(
        APPROVE.replace("alpha", item["agent_id"]),
        headers=_op(),
        json={"check": item["check"], "definition_digest": item["definition_digest"]},
    )
    assert resp.status_code == 200
    body = _needs(client)
    assert body["pulse"] == {"count": 0, "items": []}
    status = client.get("/api/agents/alpha/pulse", headers=_op()).json()["checks"][0]
    assert status["approved"] is True


def test_an_unapproved_schedule_appears_in_the_inbox(ctx: tuple[TestClient, Path]) -> None:
    client, workspace = ctx
    (workspace / "schedules.json").write_text(
        json.dumps(
            [
                {"id": "s1", "name": "weekly", "approval": None},
                {"id": "s2", "name": "signed", "approval": {"revision": 1}},
            ]
        ),
        encoding="utf-8",
    )
    body = _needs(client)
    assert body["schedules"]["count"] == 1
    assert body["schedules"]["items"][0]["schedule_id"] == "s1"
    assert body["schedules"]["items"][0]["agent_id"] == "alpha"
