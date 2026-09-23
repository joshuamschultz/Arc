"""Tests for fleet (team-level) HTTP routes — SPEC-022 Phase 2 task 2.3.

Each endpoint aggregates per-agent data from the gateway-driven roster
and ``team/<agent>/`` filesystem reads. Routes are pure read; no writes
to ``team/`` (acceptance criterion 15) — verified end-to-end by SHA-256
snapshot tests in Phase 8.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from arcgateway import team_roster
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import Task, TaskStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.observe import Observe
from arcui.registry import AgentRegistry
from arcui.routes.team_pages import routes as team_routes
from arcui.types import AgentRegistration


async def _seed_tasks(backend: FakeBackend, tasks: list[Task]) -> None:
    """Seed tasks into the arcstore mutable plane `Observe.tasks()` reads.

    SPEC-056 Phase D re-pointed `/api/team/tasks` off `tasks.json` (which
    `_build_team` still writes for the fixture's other endpoints, but the
    tasks route no longer reads) onto arcstore — this seeds the real source
    of truth.
    """
    await backend.start()
    store = TaskStore(backend)
    for task in tasks:
        await store.create(task)


def _write_worm_audit(
    data_dir: Path,
    *,
    seq: int,
    actor_did: str,
    action: str = "gateway.fs.read",
    target: str = "tool:x",
    outcome: str = "allow",
    extra: dict[str, object] | None = None,
) -> None:
    """Append one signed-chain record to the durable WORM file arcstore mirrors."""
    worm = data_dir / "worm"
    worm.mkdir(parents=True, exist_ok=True)
    line = {
        "seq": seq,
        "event_hash": f"hash-{seq}",
        "prev_hash": f"hash-{seq - 1}" if seq else "",
        "signature": "sig",
        "event": {
            "ts": f"2026-05-31T00:00:{seq:02d}+00:00",
            "actor_did": actor_did,
            "action": action,
            "target": target,
            "outcome": outcome,
            "extra": extra,
        },
    }
    with (worm / "audit-chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _make_app(
    team_root: Path | None = None, backend: FakeBackend | None = None
) -> tuple[Starlette, AuthConfig, AgentRegistry]:
    auth = AuthConfig(
        {
            "viewer_token": "viewer",
            "operator_token": "operator",
        }
    )
    registry = AgentRegistry()
    app = Starlette(routes=team_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.observe = Observe(backend=backend) if backend is not None else Observe()
    app.state.team_root = team_root

    def _roster_provider() -> list[team_roster.RosterEntry]:
        if app.state.team_root is None:
            return []
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=app.state.team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth, registry


def _build_team(tmp_path: Path, agents: list[tuple[str, str]]) -> Path:
    """Create ``tmp_path/team/<name>_agent`` for each (name, policy_md) pair."""
    root = tmp_path / "team"
    root.mkdir()
    for name, policy in agents:
        agent_dir = root / f"{name}_agent"
        agent_dir.mkdir()
        (agent_dir / "arcagent.toml").write_text(
            f'[agent]\nname = "{name}"\norg = "research"\n'
            f'[identity]\ndid = "did:arc:{name}"\n'
            '[llm]\nmodel = "openai/gpt-4o"\n',
            encoding="utf-8",
        )
        ws = agent_dir / "workspace"
        ws.mkdir()
        if policy:
            (ws / "policy.md").write_text(policy, encoding="utf-8")
        # tasks.json + skills/ for those endpoints
        (ws / "tasks.json").write_text(
            f'[{{"id": "{name}-t1", "title": "{name} task", "status": "open"}}]',
            encoding="utf-8",
        )
        # Loader convention: skills are SKILL.md folders under the skills/
        # subdir of a capabilities root (workspace/capabilities/skills/<name>/).
        skill_dir = ws / "capabilities" / "skills" / f"{name}_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}_skill\ndescription: skill of {name}\n---\nbody\n",
            encoding="utf-8",
        )
    return root


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


# ---------------------------------------------------------------------------
# /api/team/roster
# ---------------------------------------------------------------------------


class TestRoster:
    def test_empty_when_no_team_root(self):
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    def test_lists_agents(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        assert resp.status_code == 200
        ids = {a["agent_id"] for a in resp.json()["agents"]}
        assert ids == {"alpha", "beta"}

    def test_overlays_online_status(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, registry = _make_app(team_root=team)
        registry.register(
            "alpha",
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                connected_at="2026-04-29T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["online"] is True
        assert agents["beta"]["online"] is False

    def test_roster_has_no_degraded_field(self, tmp_path):
        """Task #19 — the roster's ``degraded`` field was derived from
        ``~/.arcagent/agent-state.json``, a file only the non-canonical
        ``scripts/arc-stack.sh`` ever wrote. Under the embedded-gateway
        architecture nothing writes that file, so ``degraded`` was always
        stale/false for every real deployment. Removed outright rather than
        kept as a permanently-false placeholder (no fallback shim) — online
        status is already derived live from ``app.state.agent_registry``
        (see ``embedded_agents.py``), which is the only honest signal that
        exists today.
        """
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, registry = _make_app(team_root=team)
        registry.register(
            "alpha",
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                connected_at="2026-07-10T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert "degraded" not in agents["alpha"]

    def test_roster_carries_canonical_identity_shape(self, tmp_path):
        # H-007: every roster row ships the same {host, platform, type,
        # short_id, name} shape the SPA's one shared component renders,
        # parsed from the row's own DID and joined by DID — not by matching
        # names client-side.
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        alpha = next(a for a in resp.json()["agents"] if a["agent_id"] == "alpha")
        identity = alpha["identity"]
        assert identity["did"] == "did:arc:alpha"
        assert identity["name"] == "alpha"


# ---------------------------------------------------------------------------
# /api/team/policy/{bullets,stats}
# ---------------------------------------------------------------------------


class TestFleetPolicy:
    def test_bullets_aggregated_with_agent_id(self, tmp_path):
        policy_alpha = (
            "- [P01] Be helpful {score:8, uses:5, reviewed:2026-04-01, "
            "created:2026-01-01, source:s1}\n"
        )
        policy_beta = (
            "- [P01] Be careful {score:6, uses:2, reviewed:2026-04-15, "
            "created:2026-02-01, source:s2}\n"
        )
        team = _build_team(tmp_path, [("alpha", policy_alpha), ("beta", policy_beta)])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/policy/bullets", headers=_viewer(auth))
        assert resp.status_code == 200
        bullets = resp.json()["bullets"]
        # Each bullet stamped with agent_id so the UI can badge it
        agents_with_p01 = {b["agent_id"] for b in bullets if b["id"] == "P01"}
        assert agents_with_p01 == {"alpha", "beta"}

    def test_stats_aggregates_across_fleet(self, tmp_path):
        policy_alpha = (
            "- [P01] A {score:8, uses:1, reviewed:2026-04-01, created:2026-01-01, source:s1}\n"
            "- [P02] B {score:1, uses:1, reviewed:2026-04-01, created:2026-01-01, source:s1}\n"
        )
        policy_beta = (
            "- [P03] C {score:6, uses:1, reviewed:2026-04-01, created:2026-01-01, source:s2}\n"
        )
        team = _build_team(tmp_path, [("alpha", policy_alpha), ("beta", policy_beta)])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/policy/stats", headers=_viewer(auth))
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total"] == 3
        assert stats["active"] == 2
        assert stats["retired"] == 1
        # avg over active = (8+6)/2 = 7
        assert abs(stats["avg_score"] - 7.0) < 0.01
        per_agent = {a["agent_id"]: a for a in stats["per_agent"]}
        assert per_agent["alpha"]["total"] == 2
        assert per_agent["beta"]["total"] == 1


# ---------------------------------------------------------------------------
# /api/team/tasks
# ---------------------------------------------------------------------------


class TestFleetTasks:
    def test_create_and_list_work_without_fleet_messaging(
        self, tmp_path, arcstore_backend: FakeBackend
    ) -> None:
        from arcui.routes.tasks import routes as mutation_routes

        app, auth, _ = _make_app(backend=arcstore_backend)
        app.router.routes.extend(mutation_routes)
        app.state.task_store = TaskStore(arcstore_backend)
        app.state.agent_mail = None
        app.state.roster_provider = lambda: (_ for _ in ()).throw(RuntimeError("fleet down"))
        client = TestClient(app)
        operator = {"Authorization": f"Bearer {auth.operator_token}"}
        unowned = client.post("/api/team/tasks", headers=operator, json={"title": "Backlog"})
        assert unowned.status_code == 201
        assert unowned.json()["owner_notification"] == "not_applicable"
        owned = client.post(
            "/api/team/tasks",
            headers=operator,
            json={"title": "Assigned", "owner_did": "did:arc:alpha"},
        )
        assert owned.status_code == 201
        assert owned.json()["owner_notification"] == "unavailable"
        listed = client.get("/api/team/tasks", headers=_viewer(auth))
        assert listed.status_code == 200
        assert {task["id"] for task in listed.json()["tasks"]} == {
            unowned.json()["id"],
            owned.json()["id"],
        }
        assert listed.json()["facets"]["total"] == 2

    def test_large_history_filters_and_off_page_relationships(
        self, tmp_path, arcstore_backend: FakeBackend
    ) -> None:
        """Global facets and page projections include rows beyond a thousand-item history."""
        team = _build_team(tmp_path, [("alpha", "")])
        tasks = [
            Task(
                id="old-dependency",
                title="Dependency",
                creator_did="did:arc:alpha",
                status="done",
                owner_did="did:arc:old",
                tags=["rare"],
            )
        ]
        tasks.extend(
            Task(
                id=f"closed-{index:04d}",
                title="Closed",
                creator_did="did:arc:alpha",
                status="done",
            )
            for index in range(1100)
        )
        tasks.extend(
            [
                Task(
                    id="parent",
                    title="Parent",
                    creator_did="did:arc:alpha",
                    status="todo",
                    blocked_by=["old-dependency"],
                ),
                Task(
                    id="child",
                    title="Child",
                    creator_did="did:arc:alpha",
                    status="todo",
                    parent_id="parent",
                ),
            ]
        )
        asyncio.run(_seed_tasks(arcstore_backend, tasks))
        app, auth, _ = _make_app(team_root=team, backend=arcstore_backend)
        client = TestClient(app)
        head = client.get("/api/team/tasks?limit=2", headers=_viewer(auth)).json()
        assert head["facets"]["total"] == 1103
        assert head["facets"]["owners"]["did:arc:old"] == 1
        assert head["facets"]["tags"]["rare"] == 1
        assert head["facets"]["blocked"] == 0
        assert head["projections"]["parent"]["blocked"] is False
        assert head["projections"]["parent"]["dependencies"]["old-dependency"]["status"] == "done"
        assert head["projections"]["parent"]["child_total"] == 1
        rare = client.get(
            "/api/team/tasks?owner_did=did%3Aarc%3Aold&tag=rare",
            headers=_viewer(auth),
        ).json()
        assert [task["id"] for task in rare["tasks"]] == ["old-dependency"]
        assert rare.get("next_cursor") is None
        filtered = client.get("/api/team/tasks?status=done&limit=1", headers=_viewer(auth)).json()
        assert filtered["tasks"][0]["status"] == "done"
        assert (
            client.get(
                "/api/team/tasks?status=todo&cursor=" + filtered["next_cursor"],
                headers=_viewer(auth),
            ).status_code
            == 400
        )
        for bad in ("done' OR true --", "backlog; DROP TABLE mutable_records"):
            assert (
                client.get(
                    "/api/team/tasks", params={"status": bad}, headers=_viewer(auth)
                ).status_code
                == 400
            )

    def test_board_pages_closed_history_and_keeps_open_tasks(
        self, tmp_path, arcstore_backend: FakeBackend
    ) -> None:
        team = _build_team(tmp_path, [("alpha", "")])
        tasks = [
            Task(id=f"done-{index:03d}", title="Done", creator_did="did:arc:alpha", status="done")
            for index in range(12)
        ]
        tasks.append(Task(id="open", title="Open", creator_did="did:arc:alpha", status="todo"))
        asyncio.run(_seed_tasks(arcstore_backend, tasks))
        app, auth, _ = _make_app(team_root=team, backend=arcstore_backend)
        client = TestClient(app)
        first = client.get("/api/team/tasks?limit=5", headers=_viewer(auth)).json()
        assert [task["id"] for task in first["tasks"]] == ["open"]
        assert first["next_cursor"] is not None
        cursor: str | None = None
        seen: set[str] = set()
        for expected_closed in (5, 5, 2):
            path = "/api/team/tasks?limit=5"
            path += f"&cursor={cursor or first['next_cursor']}"
            response = client.get(path, headers=_viewer(auth))
            assert response.status_code == 200
            body = response.json()
            assert all(task["status"] == "done" for task in body["tasks"])
            closed = {task["id"] for task in body["tasks"] if task["status"] == "done"}
            assert len(closed) == expected_closed
            assert not seen & closed
            seen |= closed
            cursor = body.get("next_cursor")
        assert cursor is None
        assert len(seen) == 12
        summary = client.get("/api/team/tasks/summary?window=24h", headers=_viewer(auth))
        assert summary.status_code == 200
        assert summary.json() == {"counts": {"done": 12, "todo": 1}, "total": 13}

    def test_board_rejects_invalid_cursor(self, tmp_path) -> None:
        app, auth, _ = _make_app(team_root=_build_team(tmp_path, [("alpha", "")]))
        for cursor in ("invalid!", "__8", "W1wiaGlzdG9yeVwiLG51bGxd"):
            response = TestClient(app).get(
                f"/api/team/tasks?limit=5&cursor={cursor}", headers=_viewer(auth)
            )
            assert response.status_code == 400

    def test_aggregates_tasks_with_agent_id(self, tmp_path, arcstore_backend: FakeBackend):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        asyncio.run(
            _seed_tasks(
                arcstore_backend,
                [
                    Task(
                        id="alpha-t1",
                        title="alpha task",
                        creator_did="did:arc:alpha",
                        owner_did="did:arc:alpha",
                    ),
                    Task(
                        id="beta-t1",
                        title="beta task",
                        creator_did="did:arc:beta",
                        owner_did="did:arc:beta",
                    ),
                ],
            )
        )
        app, auth, _ = _make_app(team_root=team, backend=arcstore_backend)
        client = TestClient(app)
        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        ids = {t["id"] for t in tasks}
        assert ids == {"alpha-t1", "beta-t1"}
        for t in tasks:
            assert "agent_id" in t

    def test_tasks_window_scopes_to_recently_touched(
        self, tmp_path, arcstore_backend: FakeBackend, monkeypatch
    ) -> None:
        """The Home summary counts tasks touched within its requested window."""
        import arcstore.backends.memory as memory_backend

        team = _build_team(tmp_path, [("alpha", "")])
        stale_ts = "2020-01-01T00:00:00+00:00"
        monkeypatch.setattr(memory_backend, "_now", lambda: stale_ts)
        asyncio.run(
            _seed_tasks(
                arcstore_backend,
                [Task(id="stale-t1", title="old", creator_did="did:arc:alpha")],
            )
        )
        monkeypatch.undo()
        asyncio.run(
            _seed_tasks(
                arcstore_backend,
                [Task(id="fresh-t1", title="new", creator_did="did:arc:alpha")],
            )
        )

        app, auth, _ = _make_app(team_root=team, backend=arcstore_backend)
        client = TestClient(app)

        resp = client.get("/api/team/tasks/summary?window=1h", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"counts": {"backlog": 1}, "total": 1}

        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert {t["id"] for t in resp.json()["tasks"]} == {"stale-t1", "fresh-t1"}

    def test_tasks_invalid_window_400(self, tmp_path) -> None:
        app, auth, _ = _make_app(team_root=_build_team(tmp_path, [("alpha", "")]))
        client = TestClient(app)
        resp = client.get("/api/team/tasks/summary?window=bogus", headers=_viewer(auth))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/team/tools-skills
# ---------------------------------------------------------------------------


class TestFleetToolsSkills:
    def test_skills_directory(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        assert "skills" in body
        assert "tools" in body
        skill_names = {s["name"] for s in body["skills"]}
        assert "alpha_skill" in skill_names
        assert "beta_skill" in skill_names

    def test_tools_matrix_uses_live_registrations(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, registry = _make_app(team_root=team)
        registry.register(
            "alpha",
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                tools=["fs.read", "search"],
                connected_at="2026-04-29T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        tools = resp.json()["tools"]
        names = {t["name"] for t in tools}
        assert "fs.read" in names
        assert "search" in names

    def test_tools_matrix_populated_without_live_registration(self, tmp_path):
        """Read-on-demand: no agent is live-registered, yet the fleet tools
        matrix is not empty — tools come from the durable enumeration
        (builtins/modules/disk), the same set the per-agent Tools tab shows.
        Regression for the '0 tools' fleet matrix while every agent had them."""
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, registry = _make_app(team_root=team)
        assert registry.get("alpha") is None  # nothing live-registered
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        names = {t["name"] for t in tools}
        assert names, "fleet tools matrix is empty without a live registration"
        assert names & {"read", "write", "edit", "bash"}, (
            f"expected builtin tools in the fleet matrix, got {sorted(names)[:10]}"
        )
        assert all("alpha" in t["agents"] for t in tools)


# ---------------------------------------------------------------------------
# /api/team/audit
# ---------------------------------------------------------------------------


class TestFleetAudit:
    def test_returns_recent_events(self, tmp_path, _isolated_arc_data_dir: Path):
        # Real path: the fleet audit tab reads the durable signed chain
        # (arcstore WORM mirror) through Observe.audit — no buffer.
        from arcui.server import create_app

        for i in range(3):
            _write_worm_audit(_isolated_arc_data_dir, seq=i, actor_did=f"did:arc:a{i}")

        team = _build_team(tmp_path, [("alpha", "")])
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
            resp = client.get("/api/team/audit", headers=_viewer(auth))
        assert resp.status_code == 200
        assert len(resp.json()["events"]) == 3

    def test_limit_param(self, tmp_path, _isolated_arc_data_dir: Path):
        from arcui.server import create_app

        for i in range(50):
            _write_worm_audit(_isolated_arc_data_dir, seq=i, actor_did="did:arc:alpha")

        team = _build_team(tmp_path, [("alpha", "")])
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
            resp = client.get("/api/team/audit?limit=10", headers=_viewer(auth))
        assert len(resp.json()["events"]) == 10

    def test_rows_carry_actor_identity_and_readable_fields(
        self, tmp_path, _isolated_arc_data_dir: Path
    ):
        """H-022: a row names WHO (roster-joined identity), WHAT tool/target,
        and WHY (the policy pipeline's reason) — not just raw DID/action/outcome.
        """
        from arcui.server import create_app

        _write_worm_audit(
            _isolated_arc_data_dir,
            seq=0,
            actor_did="did:arc:alpha",
            action="policy.evaluate",
            target="memory.write",
            outcome="deny",
            extra={"reason": "tool not on the agent allowlist"},
        )

        team = _build_team(tmp_path, [("alpha", "")])
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
            resp = client.get("/api/team/audit", headers=_viewer(auth))
        assert resp.status_code == 200
        event = resp.json()["events"][0]

        # WHO — roster-joined by DID (H-007), not a raw truncated DID.
        assert event["identity"]["did"] == "did:arc:alpha"
        assert event["identity"]["name"] == "alpha"
        assert event["actor"] == "alpha"
        # WHAT / WHY / outcome.
        assert event["action_label"] == "Tool policy check"
        assert event["target_label"] == "memory.write"
        assert event["decision"] == "Denied"
        assert event["reason"] == "tool not on the agent allowlist"
        # Raw stored fields untouched — this is a read projection, not a rewrite.
        assert event["actor_did"] == "did:arc:alpha"
        assert event["action"] == "policy.evaluate"
        assert event["outcome"] == "deny"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_roster_requires_token(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, _, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/roster")
        assert resp.status_code == 401


# Coverage — error / edge branches


class TestEdgeCases:
    def test_audit_invalid_limit(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/audit?limit=abc", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_tasks_skips_malformed_json(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        # Corrupt one tasks.json — fleet endpoint should still answer with what it can.
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text("garbage", encoding="utf-8")
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json()["tasks"] == []
        assert resp.json()["facets"]["total"] == 0

    def test_tasks_object_root_returns_empty(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text(
            '{"single": "object"}', encoding="utf-8"
        )
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert resp.json()["tasks"] == []
        assert resp.json()["facets"]["total"] == 0

    def test_no_skills_dir_for_one_agent(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        # alpha has skills/. add a beta agent without skills/
        beta = team / "beta_agent"
        beta.mkdir()
        (beta / "arcagent.toml").write_text(
            '[agent]\nname = "beta"\n[identity]\ndid = "did:arc:beta"\n',
            encoding="utf-8",
        )
        (beta / "workspace").mkdir()
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()["skills"]}
        assert "alpha_skill" in names
        # beta has no skills, but its absence is silent — no crash.
