"""``/api/team/tasks`` + ``/api/agents/{id}/tasks`` — re-pointed to arcstore.

SPEC-056 Phase D (D1). Both routes exist today reading each agent's
``tasks.json`` (team_pages.py:183-191, agent_detail/sessions.py:143-149);
this file proves the re-pointed behavior — arcstore ``tasks`` collection rows,
stamped with the owning agent's ``agent_id`` on the fleet route, filtered by
owner DID on the per-agent route — while keeping the wire-stable
``TasksResponse`` shape (``{"tasks": [...]}``, PLAN D1 note).

The routes still read ``tasks.json`` today, so every test here fails RED
(missing arcstore-row content, not a 404/500) until re-pointed. Task seeding
uses ``arcstore.tasks.TaskStore`` against the SAME ``store/arcui.db`` that
``app.state.observe`` (wired below) reads — see test_observe_tasks.py for
why that's the correct seam.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from arcgateway import team_roster
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import Task, TaskStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.observe import Observe
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.team_pages import routes as team_routes

_CREATOR = "did:arc:test:human/operator"


async def _seed_store(data_dir: Path) -> TaskStore:
    """Open a TaskStore against the SAME db Observe reads (store/arcui.db)."""
    backend = SqliteBackend(data_dir / "store" / "arcui.db")
    await backend.start()
    return TaskStore(backend)


def _task(id_: str, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": id_,
        "title": f"Task {id_}",
        "creator_did": _CREATOR,
    }
    fields.update(overrides)
    return Task(**fields)


async def _seed(data_dir: Path, tasks: list[Task]) -> None:
    store = await _seed_store(data_dir)
    for t in tasks:
        await store.create(t)


def _build_team(tmp_path: Path, agents: list[tuple[str, str]]) -> Path:
    """Create ``tmp_path/team/<name>_agent`` with a real DID per agent.

    No ``tasks.json`` is written — the re-pointed routes must not need it.
    """
    root = tmp_path / "team"
    root.mkdir()
    for name, did in agents:
        agent_dir = root / f"{name}_agent"
        agent_dir.mkdir()
        (agent_dir / "arcagent.toml").write_text(
            f'[agent]\nname = "{name}"\norg = "research"\n'
            f'[identity]\ndid = "{did}"\n'
            '[llm]\nmodel = "openai/gpt-4o"\n',
            encoding="utf-8",
        )
        (agent_dir / "workspace").mkdir()
    return root


def _make_app(*, team_root: Path, data_dir: Path) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    registry = AgentRegistry()
    app = Starlette(routes=[*team_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.observe = Observe(data_dir=data_dir)
    app.state.team_root = team_root

    def _roster_provider() -> list[team_roster.RosterEntry]:
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


class TestFleetTasksFromArcstore:
    def test_serves_arcstore_rows_stamped_with_owning_agent_id(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "did:arc:alpha"), ("beta", "did:arc:beta")])
        asyncio.run(
            _seed(
                tmp_path,
                [_task("t1", owner_did="did:arc:alpha"), _task("t2", owner_did="did:arc:beta")],
            )
        )
        app, auth = _make_app(team_root=team, data_dir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/team/tasks", headers=_viewer(auth))

        assert resp.status_code == 200
        tasks = {t["id"]: t for t in resp.json()["tasks"]}
        assert set(tasks) == {"t1", "t2"}
        assert tasks["t1"]["agent_id"] == "alpha"
        assert tasks["t2"]["agent_id"] == "beta"

    def test_unowned_task_has_no_agent_id(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "did:arc:alpha")])
        asyncio.run(_seed(tmp_path, [_task("t1")]))  # unowned -> backlog
        app, auth = _make_app(team_root=team, data_dir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/team/tasks", headers=_viewer(auth))

        tasks = resp.json()["tasks"]
        assert tasks[0]["id"] == "t1"
        assert tasks[0]["agent_id"] is None

    def test_does_not_read_tasks_json_at_all(self, tmp_path):
        """A corrupt tasks.json must not affect the response — the route no
        longer reads it (source of truth is arcstore, PLAN D1)."""
        team = _build_team(tmp_path, [("alpha", "did:arc:alpha")])
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text(
            "not json at all", encoding="utf-8"
        )
        asyncio.run(_seed(tmp_path, [_task("t1", owner_did="did:arc:alpha")]))
        app, auth = _make_app(team_root=team, data_dir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/team/tasks", headers=_viewer(auth))

        assert resp.status_code == 200
        assert [t["id"] for t in resp.json()["tasks"]] == ["t1"]


class TestPerAgentTasksFromArcstore:
    def test_serves_only_that_agents_owned_tasks(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "did:arc:alpha"), ("beta", "did:arc:beta")])
        asyncio.run(
            _seed(
                tmp_path,
                [
                    _task("t1", owner_did="did:arc:alpha"),
                    _task("t2", owner_did="did:arc:beta"),
                ],
            )
        )
        app, auth = _make_app(team_root=team, data_dir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/agents/alpha/tasks", headers=_viewer(auth))

        assert resp.status_code == 200
        assert [t["id"] for t in resp.json()["tasks"]] == ["t1"]

    def test_unknown_agent_returns_404(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "did:arc:alpha")])
        app, auth = _make_app(team_root=team, data_dir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/agents/missing/tasks", headers=_viewer(auth))

        assert resp.status_code == 404


class TestTaskRowExposesRunLink:
    def test_run_id_field_present_for_cost_and_trace_link(self, tmp_path):
        """FR-11 — the card links run_id to the existing run/trace view; the
        field must ride through the fleet response unmodified."""
        team = _build_team(tmp_path, [("alpha", "did:arc:alpha")])
        asyncio.run(
            _seed(
                tmp_path,
                [_task("t1", owner_did="did:arc:alpha", status="in_progress", run_id="run-42")],
            )
        )
        app, auth = _make_app(team_root=team, data_dir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/team/tasks", headers=_viewer(auth))

        tasks = {t["id"]: t for t in resp.json()["tasks"]}
        assert tasks["t1"]["run_id"] == "run-42"

