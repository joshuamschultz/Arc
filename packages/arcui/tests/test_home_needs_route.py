"""``GET /api/home/needs`` — Home's aggregated "NEEDS YOU" panel (H-001).

The bug this closes: Home's "Needs you" panel only ever looked at failed runs
and review-status tasks, so a real pending approval or a gated capability sat
on their own dedicated pages while Home cheerfully reported "all caught up".
This route aggregates the same three operator-action queues those pages
already read from (``ApprovalStore``, ``arcagent.list_gated``, ``Observe.
tasks``) so Home can never disagree with them about what needs the operator.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arcgateway import team_roster
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import Task, TaskStore
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.observe import Observe
from arcui.routes.home import routes as home_routes


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Capability-inventory resolution can read the deployment's fleet-wide
    # arcagent.toml; pin it at the test tmp so nothing touches a real ~/.arc.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))


_AGENT_DID = "did:arc:test:exec/olivia"
_VALID_SKILL = (
    "---\n"
    "name: reporter\n"
    "version: 2.0.0\n"
    "description: does reporter\n"
    "triggers: [reporter]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)


def _build_ungated_agent(team_root: Path, name: str) -> None:
    """One agent workspace with a single UNSIGNED (gated) skill.

    Enterprise tier refuses an unsigned artifact outright, so it always lands
    in ``arcagent.list_gated``'s default (non-loaded) view — the exact set
    ``/api/trust/gated`` and this route both read.
    """
    agent_dir = team_root / name
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    key_dir = team_root / f"{name}-keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)

    (skills / "reporter").mkdir()
    (skills / "reporter" / "SKILL.md").write_text(_VALID_SKILL, encoding="utf-8")

    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        '[security]\ntier = "enterprise"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )


async def _seed_approval_store(backend: FakeBackend, *, seed: bool) -> ApprovalStore:
    await backend.start()
    store = ApprovalStore(backend)
    if seed:
        await store.create(
            PendingApproval(
                id="req1",
                agent_did=_AGENT_DID,
                agent_label="olivia",
                tool="send_message",
                legs=["external_comms"],
                call_hash="hash-1",
            )
        )
    return store


async def _seed_observe(backend: FakeBackend, *, seed: bool) -> Observe:
    await backend.start()
    if seed:
        store = TaskStore(backend)
        await store.create(
            Task(
                id="t1",
                title="write the report",
                status="review",
                creator_did=_AGENT_DID,
                owner_did=_AGENT_DID,
            )
        )
    return Observe(backend=backend)


def _make_app(
    tmp_path: Path,
    *,
    seed_approval: bool,
    seed_capability: bool,
    seed_review_task: bool,
) -> Starlette:
    team_root = tmp_path / "team"
    team_root.mkdir()
    if seed_capability:
        _build_ungated_agent(team_root, "olivia")

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=home_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.approval_store = asyncio.run(_seed_approval_store(FakeBackend(), seed=seed_approval))
    app.state.observe = asyncio.run(_seed_observe(FakeBackend(), seed=seed_review_task))
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return app


_VIEWER = {"Authorization": "Bearer viewer"}


def test_all_queues_empty_reports_zero_everywhere(tmp_path: Path) -> None:
    """Nothing pending anywhere -> every queue count is 0 and total is 0.

    This is the contract the frontend's "all caught up" message depends on:
    it must only ever show when EVERY queue, not just some of them, is empty.
    """
    app = _make_app(tmp_path, seed_approval=False, seed_capability=False, seed_review_task=False)
    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)

    assert resp.status_code == 200
    body = resp.json()
    assert body["approvals"] == {"count": 0, "items": []}
    assert body["capabilities"] == {"count": 0, "items": []}
    assert body["review_tasks"] == {"count": 0, "items": []}
    assert body["total"] == 0


def test_a_pending_approval_surfaces(tmp_path: Path) -> None:
    app = _make_app(tmp_path, seed_approval=True, seed_capability=False, seed_review_task=False)
    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)

    body = resp.json()
    assert body["approvals"]["count"] == 1
    assert body["approvals"]["items"][0]["id"] == "req1"
    assert body["approvals"]["items"][0]["agent_did"] == _AGENT_DID
    assert body["capabilities"]["count"] == 0
    assert body["review_tasks"]["count"] == 0
    assert body["total"] == 1


def test_a_gated_capability_surfaces(tmp_path: Path) -> None:
    """The gap this route closes: a capability stuck at /api/trust/gated
    reaches Home too — not just the dedicated Pending capabilities page."""
    app = _make_app(tmp_path, seed_approval=False, seed_capability=True, seed_review_task=False)
    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)

    body = resp.json()
    assert body["capabilities"]["count"] == 1
    item = body["capabilities"]["items"][0]
    assert item["name"] == "reporter"
    assert item["agent_id"] == "olivia"
    assert item["status"] != "loaded"
    assert body["approvals"]["count"] == 0
    assert body["review_tasks"]["count"] == 0
    assert body["total"] == 1


def test_a_task_awaiting_review_surfaces(tmp_path: Path) -> None:
    app = _make_app(tmp_path, seed_approval=False, seed_capability=False, seed_review_task=True)
    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)

    body = resp.json()
    assert body["review_tasks"]["count"] == 1
    assert body["review_tasks"]["items"][0]["id"] == "t1"
    assert body["review_tasks"]["items"][0]["status"] == "review"
    assert body["approvals"]["count"] == 0
    assert body["capabilities"]["count"] == 0
    assert body["total"] == 1


def test_all_three_queues_aggregate_together(tmp_path: Path) -> None:
    app = _make_app(tmp_path, seed_approval=True, seed_capability=True, seed_review_task=True)
    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)

    body = resp.json()
    assert body["approvals"]["count"] == 1
    assert body["capabilities"]["count"] == 1
    assert body["review_tasks"]["count"] == 1
    assert body["total"] == 3


def test_preview_items_are_capped_but_the_count_is_not(tmp_path: Path) -> None:
    """The count must stay the true total even once the preview list is capped
    — an operator must never see "all caught up" (or an undercount) just
    because more than the preview limit is pending."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    backend = FakeBackend()
    asyncio.run(backend.start())
    store = ApprovalStore(backend)
    for i in range(7):
        asyncio.run(
            store.create(
                PendingApproval(
                    id=f"req{i}",
                    agent_did=_AGENT_DID,
                    agent_label="olivia",
                    tool="send_message",
                    legs=["external_comms"],
                    call_hash=f"hash-{i}",
                )
            )
        )

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=home_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.approval_store = store
    app.state.observe = Observe(backend=FakeBackend())
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )

    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)
    body = resp.json()
    assert body["approvals"]["count"] == 7
    assert len(body["approvals"]["items"]) == 5
    assert body["total"] == 7


def test_a_missing_approval_store_degrades_to_empty_not_500(tmp_path: Path) -> None:
    """One plane being unavailable must not blank the other two queues."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=home_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.approval_store = None
    app.state.observe = asyncio.run(_seed_observe(FakeBackend(), seed=True))
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )

    resp = TestClient(app).get("/api/home/needs", headers=_VIEWER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["approvals"] == {"count": 0, "items": []}
    assert body["review_tasks"]["count"] == 1
