"""alpha-2 P8 — arcskill operator controls in ArcUI.

Routes under ``/api/agents/{id}/skills/{skill_name}/``:

* ``GET  improver``            — read model (state, candidates, scores, gate verdicts)
* ``POST improve?dry_run=1``   — preview: diff + gate verdict, no write (operator)
* ``POST improve?dry_run=0``   — apply (operator, ``confirm: true``), optionally the
  previewed candidate via ``preview_id``; the running agent's adapter applies it
  through the SAME gate / authorization / apply path
* ``POST evals/run``           — run the golden suite now (operator)
* ``POST evals/regen``         — regenerate machine anchors (operator, ``confirm``)

Every POST — accepted, refused or failed — writes one audit record. The real app
and the real skill bundle are used; the running agent is the only fake.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arctrust.identity import AgentIdentity
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER = {"Authorization": "Bearer viewer-tok-improver"}
OPERATOR = {"Authorization": "Bearer operator-tok-improver"}
SKILL = "planner"
_SKILL_MD = (
    "---\nname: planner\nversion: 1.0.0\ndescription: does planning\n"
    "triggers: [planner]\ntools: [reload]\n---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)
_CASES = "def test_alpha():\n    assert 1\n\ndef test_beta():\n    assert 1\n"
_PREVIEW = {
    "status": "preview",
    "skill_name": SKILL,
    "reason": "strict improvement: fixed failing case(s), no regression",
    "preview_id": "p" * 32,
    "candidate_id": "abc123def456",
    "generation": 2,
    "diff": "--- current/SKILL.md\n+++ candidate/SKILL.md\n-old\n+new\n",
    "scores": {"accuracy": 4.0},
    "seed_scores": {"accuracy": 3.0},
    "stop_reason": "max_iterations",
    "iterations_run": 3,
    "gate": {
        "accepted": True,
        "reason": "strict improvement: fixed failing case(s), no regression",
        "before_pass": 1,
        "after_pass": 2,
        "newly_passing": 1,
    },
    "approval_required": False,
    "secret_internal_field": "must not leak",
}


@dataclass
class _Adapter:
    improve_result: dict[str, Any] = field(default_factory=lambda: dict(_PREVIEW))
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: BaseException | None = None

    async def improve_now(
        self, *, skill_name: str, dry_run: bool, preview_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "improve_now",
                {"skill_name": skill_name, "dry_run": dry_run, "preview_id": preview_id},
            )
        )
        if self.error is not None:
            raise self.error
        return dict(self.improve_result)

    async def run_evals(self, *, skill_name: str) -> dict[str, Any]:
        self.calls.append(("run_evals", {"skill_name": skill_name}))
        return {
            "status": "completed",
            "skill_name": skill_name,
            "reason": "",
            "total": 2,
            "passed": 1,
            "failed": 1,
            "cases": [
                {
                    "case_id": "evals/test_g.py::test_alpha",
                    "passed": True,
                    "detail": "",
                    "gate_type": "exact_match",
                    "provenance": "human",
                },
                {
                    "case_id": "evals/test_g.py::test_beta",
                    "passed": False,
                    "detail": "AssertionError",
                    "gate_type": "exact_match",
                    "provenance": "human",
                },
            ],
        }

    async def regen_evals(self, *, skill_name: str) -> dict[str, Any]:
        self.calls.append(("regen_evals", {"skill_name": skill_name}))
        return {
            "status": "completed",
            "skill_name": skill_name,
            "reason": "",
            "total": 5,
            "adopted": 3,
            "quarantined": [{"nodeid": "evals/x::test_q", "reason": "flaky"}],
        }


@dataclass
class _RunningAgent:
    adapter: _Adapter = field(default_factory=_Adapter)
    registered_tools: list[Any] = field(default_factory=list)
    reloads: int = 0

    async def skill_adapter(self) -> _Adapter:
        return self.adapter

    async def reload_or_raise(self) -> None:
        self.reloads += 1


@dataclass
class _Sink:
    def close(self) -> None:
        return None


@dataclass
class _Worm:
    """Duck-types MutationWormWriter (``.sink`` is closed at app shutdown)."""

    fields: list[Any] = field(default_factory=list)
    sink: _Sink = field(default_factory=_Sink)

    def write(self, fields: Any) -> None:
        self.fields.append(fields)

    def records(self, operation: str) -> list[dict[str, Any]]:
        dumped = [f.model_dump(mode="json") for f in self.fields]
        return [d for d in dumped if d["operation"] == operation]


@dataclass
class World:
    app: Any
    agent: _RunningAgent
    worm: _Worm
    skill_dir: Path
    workspace: Path
    did: str

    def set_running(self, agent: Any | None) -> None:
        self.app.state.embedded_agent_cache = {} if agent is None else {self.did: agent}


def _roster(agent_dir: Path, did: str) -> list[RosterEntry]:
    return [
        RosterEntry(
            agent_id="tester",
            name="tester",
            did=did,
            org="arc",
            type="agent",
            workspace_path=str(agent_dir),
            model="claude-3-5-sonnet",
            provider="anthropic",
            online=True,
            display_name="Tester",
            color="#1abc9c",
            role_label="Test",
            hidden=False,
        )
    ]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))
    team_root = tmp_path / "team"
    agent_dir = team_root / "tester_agent"
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True)
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    (agent_dir / "arcagent.toml").write_text(
        '[agent]\nname = "tester"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{workspace}"\n[llm]\nmodel = "test/model"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    skill_dir = agent_dir / "capabilities" / "skills" / SKILL
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (skill_dir / "evals" / "test_g.py").write_text(_CASES, encoding="utf-8")

    auth = AuthConfig(
        {"viewer_token": "viewer-tok-improver", "operator_token": "operator-tok-improver"}
    )
    app = create_app(
        team_root=team_root, auth_config=auth, data_dir=tmp_path / "data", workspace_dir=workspace
    )
    app.state.roster_provider = lambda: _roster(agent_dir, identity.did)
    worm = _Worm()
    app.state.audit_worm = worm
    agent = _RunningAgent()
    w = World(app, agent, worm, skill_dir, workspace, identity.did)
    w.set_running(agent)
    yield w


def _url(tail: str) -> str:
    return f"/api/agents/tester/skills/{SKILL}/{tail}"


def _bundle(w: World) -> dict[str, bytes]:
    return {p.as_posix(): p.read_bytes() for p in w.skill_dir.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# GET improver — the read model
# ---------------------------------------------------------------------------


def _seed_improver_state(workspace: Path) -> None:
    base = workspace / "skill_traces" / SKILL
    (base / "candidates").mkdir(parents=True)
    (base / "candidates" / "manifest.json").write_text(
        json.dumps(
            {
                "active_candidate_id": "abc123def456",
                "generation": 1,
                "frontier": ["abc123def456"],
                "candidates": {
                    "abc123def456": {
                        "generation": 1,
                        "parent_id": "seed",
                        "scores": {"accuracy": 4.5},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (base / "index.json").write_text(
        json.dumps({"total_traces": 7, "success_count": 5, "failure_count": 2}), encoding="utf-8"
    )
    (base / "gate_log.jsonl").write_text(
        json.dumps(
            {
                "skill_name": SKILL,
                "source": "auto",
                "kind": "prose",
                "accepted": False,
                "reason": "regression on 1 golden case(s)",
                "outcome": "rejected",
                "candidate_id": "fff000",
                "before_pass": 2,
                "after_pass": 1,
                "newly_passing": 0,
                "ts": "2026-10-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_viewer_reads_state_candidates_scores_and_gate_verdicts(world: World) -> None:
    _seed_improver_state(world.workspace)
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.get(_url("improver"), headers=VIEWER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skill_name"] == SKILL
    assert body["lifecycle_state"] == "active"
    assert body["live"] is True
    assert body["traces"] == {"total": 7, "success": 5, "failure": 2}
    assert body["candidates"][0]["candidate_id"] == "abc123def456"
    assert body["candidates"][0]["scores"] == {"accuracy": 4.5}
    assert body["gate_log"][0]["reason"] == "regression on 1 golden case(s)"
    assert body["gate_log"][0]["outcome"] == "rejected"
    assert body["suite"]["total"] == 2


def test_improver_of_an_untouched_skill_is_empty_and_writes_nothing(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(None)
        resp = client.get(_url("improver"), headers=VIEWER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["candidates"] == []
    assert resp.json()["live"] is False
    assert not (world.workspace / "skill_traces").exists()


def test_improver_of_an_unknown_skill_is_404(world: World) -> None:
    with TestClient(world.app) as client:
        resp = client.get("/api/agents/tester/skills/ghost/improver", headers=VIEWER)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST improve — preview and apply
# ---------------------------------------------------------------------------


def test_dry_run_returns_diff_and_gate_verdict_and_makes_no_write(world: World) -> None:
    before = _bundle(world)
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("improve?dry_run=1"), json={}, headers=OPERATOR)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "preview"
    assert body["diff"].startswith("--- current/SKILL.md")
    assert body["gate"]["accepted"] is True
    assert body["preview_id"] == "p" * 32
    assert "secret_internal_field" not in body
    assert world.agent.adapter.calls == [
        ("improve_now", {"skill_name": SKILL, "dry_run": True, "preview_id": None})
    ]
    assert world.agent.reloads == 0
    assert _bundle(world) == before
    records = world.worm.records("skill.improve.preview")
    assert [r["outcome"] for r in records] == ["preview"]


def test_improve_without_dry_run_param_is_a_preview(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("improve"), json={}, headers=OPERATOR)
    assert resp.status_code == 200
    assert world.agent.adapter.calls[0][1]["dry_run"] is True


def test_viewer_cannot_preview_or_apply_and_is_audited(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        preview = client.post(_url("improve?dry_run=1"), json={}, headers=VIEWER)
        apply = client.post(_url("improve?dry_run=0"), json={"confirm": True}, headers=VIEWER)
    assert preview.status_code == 403
    assert apply.status_code == 403
    assert world.agent.adapter.calls == []
    assert [r["outcome"] for r in world.worm.records("skill.improve.apply")] == ["denied"]


def test_apply_requires_explicit_confirmation(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("improve?dry_run=0"), json={}, headers=OPERATOR)
    assert resp.status_code == 400
    assert world.agent.adapter.calls == []


def test_operator_apply_of_a_preview_goes_to_the_running_agent_and_reloads(world: World) -> None:
    world.agent.adapter.improve_result = {
        "status": "applied",
        "skill_name": SKILL,
        "reason": "strict improvement",
        "candidate_id": "abc123def456",
        "generation": 2,
        "gate": dict(_PREVIEW["gate"]),
    }
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(
            _url("improve?dry_run=0"),
            json={"confirm": True, "preview_id": "p" * 32},
            headers=OPERATOR,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    assert world.agent.adapter.calls == [
        ("improve_now", {"skill_name": SKILL, "dry_run": False, "preview_id": "p" * 32})
    ]
    assert world.agent.reloads == 1
    records = world.worm.records("skill.improve.apply")
    assert [r["outcome"] for r in records] == ["applied"]
    assert "abc123def456" in records[0]["detail"]


def test_a_gate_rejection_is_a_result_not_an_error(world: World) -> None:
    world.agent.adapter.improve_result = {
        "status": "rejected",
        "skill_name": SKILL,
        "reason": "regression on 1 golden case(s)",
        "gate": {"accepted": False, "reason": "regression on 1 golden case(s)"},
    }
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("improve?dry_run=0"), json={"confirm": True}, headers=OPERATOR)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert world.agent.reloads == 0


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("unavailable", 503),
        ("not_found", 404),
        ("busy", 409),
        ("stale_preview", 409),
        ("preview_expired", 409),
        ("timeout", 504),
        # The operator-anchored revision writer refused the commit (stale head, bad path).
        ("refused", 409),
    ],
)
def test_control_statuses_map_to_http_codes(world: World, status: str, code: int) -> None:
    world.agent.adapter.improve_result = {"status": status, "skill_name": SKILL, "reason": "r"}
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("improve?dry_run=1"), json={}, headers=OPERATOR)
    assert resp.status_code == code
    assert resp.json()["status"] == status


def test_agent_not_running_here_is_503_and_audited(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(None)
        resp = client.post(_url("improve?dry_run=1"), json={}, headers=OPERATOR)
    assert resp.status_code == 503
    assert [r["outcome"] for r in world.worm.records("skill.improve.preview")] == ["failed"]


def test_a_crashing_pass_is_a_generic_500_and_audited(world: World) -> None:
    world.agent.adapter.error = RuntimeError("/secret/path leaked")
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("improve?dry_run=1"), json={}, headers=OPERATOR)
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert [r["outcome"] for r in world.worm.records("skill.improve.preview")] == ["failed"]


def test_unknown_skill_is_404_before_the_agent_is_asked(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(
            "/api/agents/tester/skills/ghost/improve?dry_run=1", json={}, headers=OPERATOR
        )
    assert resp.status_code == 404
    assert world.agent.adapter.calls == []


# ---------------------------------------------------------------------------
# POST evals/run and evals/regen
# ---------------------------------------------------------------------------


def test_operator_runs_the_golden_suite_and_sees_pass_fail_per_case(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("evals/run"), json={}, headers=OPERATOR)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["total"], body["passed"], body["failed"]) == (2, 1, 1)
    assert [c["passed"] for c in body["cases"]] == [True, False]
    assert [r["outcome"] for r in world.worm.records("skill.evals.run")] == ["completed"]


def test_viewer_cannot_run_the_suite(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        resp = client.post(_url("evals/run"), json={}, headers=VIEWER)
    assert resp.status_code == 403
    assert world.agent.adapter.calls == []
    assert [r["outcome"] for r in world.worm.records("skill.evals.run")] == ["denied"]


def test_regen_requires_confirmation_then_regenerates(world: World) -> None:
    with TestClient(world.app) as client:
        world.set_running(world.agent)
        refused = client.post(_url("evals/regen"), json={}, headers=OPERATOR)
        resp = client.post(_url("evals/regen"), json={"confirm": True}, headers=OPERATOR)
    assert refused.status_code == 400
    assert resp.status_code == 200, resp.text
    assert resp.json()["adopted"] == 3
    assert resp.json()["quarantined"][0]["reason"] == "flaky"
    assert world.agent.adapter.calls == [("regen_evals", {"skill_name": SKILL})]
    outcomes = [r["outcome"] for r in world.worm.records("skill.evals.regen")]
    assert outcomes == ["denied", "completed"]
