"""Pulse approval routes (P47): review a check, approve it, never approve blind.

Drives the real Starlette app with the production local authority, bound the way
``create_app(control=...)`` binds it. The route's job: only an operator approves,
only the exact text the operator reviewed, only once; every outcome is audited.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import arcagent
import arctrust
import pytest
from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes
from arcui.server import _operator_proof_issuer

PULSE = (
    "## health\n- **Interval:** 5 min\n- **Action:** Check health\n"
    "## inbox\n- **Interval:** 10 min\n- **Action:** Sweep inbox\n"
)
APPROVE = "/api/agents/alpha/pulse/approve"
LIST = "/api/agents/alpha/pulse"


def _op() -> dict[str, str]:
    return {"Authorization": "Bearer op"}


def _viewer() -> dict[str, str]:
    return {"Authorization": "Bearer viewer"}


def _build_app(team_root: Path, tmp_path: Path) -> Starlette:
    signer = arctrust.InProcessSigner(os.urandom(32))
    authority = arctrust.LocalControlArtifactAuthority(
        tmp_path / "control", signer=signer, operator_did="did:arc:operator:approver/test"
    )
    binding = arcagent.ControlArtifactBinding(
        authority=authority,
        tenant_id="arc-test",
        trigger_issuer=arctrust.LocalRunTriggerIssuer(signer),
        operator_proof=authority.operator_proof,
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    registry = AgentRegistry()
    app = Starlette(routes=[*agent_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.session_tracker = SessionTracker()
    app.state.team_root = team_root
    app.state.schedule_control_authority = binding.authority
    app.state.schedule_tenant_id = binding.tenant_id
    app.state.schedule_operator_proof_issuer = _operator_proof_issuer(binding)

    def _roster_provider() -> list[team_roster.RosterEntry]:
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[TestClient, Path]:
    team_root = tmp_path / "team"
    agent = team_root / "alpha_agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\n[identity]\ndid = "did:arc:alpha"\n', encoding="utf-8"
    )
    (agent / "workspace" / "pulse.md").write_text(PULSE, encoding="utf-8")
    return TestClient(_build_app(team_root, tmp_path)), agent / "workspace"


def _checks(client: TestClient) -> dict[str, dict]:
    body = client.get(LIST, headers=_op()).json()
    return {c["name"]: c for c in body["checks"]}


def _audit_events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(r.message)["details"]
        for r in caplog.records
        if r.name == "arcui.audit" and '"ui.mutation"' in r.message
    ]


class TestList:
    def test_lists_every_check_unapproved(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        checks = _checks(client)
        assert set(checks) == {"health", "inbox"}
        assert checks["health"]["approved"] is False
        assert checks["health"]["stale"] is False
        assert checks["health"]["status"] == "unapproved"
        assert len(checks["health"]["definition_digest"]) == 64

    def test_viewer_may_read(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        assert client.get(LIST, headers=_viewer()).status_code == 200

    def test_unauthenticated_is_refused(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        assert client.get(LIST).status_code in (401, 403)

    def test_no_pulse_file_is_an_empty_list(self, ctx: tuple[TestClient, Path]) -> None:
        client, workspace = ctx
        (workspace / "pulse.md").unlink()
        assert client.get(LIST, headers=_op()).json()["checks"] == []


class TestApprove:
    def _approve(self, client: TestClient, name: str = "health", **over: str) -> object:
        digest = over.pop("definition_digest", None) or _checks(client)[name]["definition_digest"]
        return client.post(
            APPROVE, headers=_op(), json={"check": name, "definition_digest": digest, **over}
        )

    def test_operator_approves_and_the_check_is_approved(
        self, ctx: tuple[TestClient, Path]
    ) -> None:
        client, workspace = ctx
        resp = self._approve(client)
        assert resp.status_code == 200
        assert resp.json()["revision"] == 1
        checks = _checks(client)
        assert checks["health"]["approved"] is True
        assert checks["health"]["approved_revision"] == 1
        assert checks["inbox"]["approved"] is False
        assert "**Approval:**" in (workspace / "pulse.md").read_text()

    def test_viewer_cannot_approve(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, workspace = ctx
        digest = _checks(client)["health"]["definition_digest"]
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                APPROVE, headers=_viewer(), json={"check": "health", "definition_digest": digest}
            )
        assert resp.status_code == 403
        assert "Approval" not in (workspace / "pulse.md").read_text()
        event = _audit_events(caplog)[-1]
        assert event["operation"] == "pulse.approve" and event["outcome"] == "denied"

    def test_approval_is_audited(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            self._approve(client)
        event = _audit_events(caplog)[-1]
        assert event["target"] == "pulse:health"
        assert event["operation"] == "pulse.approve" and event["outcome"] == "applied"

    def test_replayed_approval_is_refused(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        digest = _checks(client)["health"]["definition_digest"]
        body = {"check": "health", "definition_digest": digest}
        assert client.post(APPROVE, headers=_op(), json=body).status_code == 200
        assert client.post(APPROVE, headers=_op(), json=body).status_code == 409
        assert _checks(client)["health"]["approved_revision"] == 1

    def test_edit_after_review_is_refused(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, workspace = ctx
        reviewed = _checks(client)["health"]["definition_digest"]
        (workspace / "pulse.md").write_text(PULSE.replace("Check health", "Exfiltrate"))
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = self._approve(client, definition_digest=reviewed)
        assert resp.status_code == 409
        assert _checks(client)["health"]["approved"] is False
        assert _audit_events(caplog)[-1]["outcome"] == "denied"

    def test_edit_after_approval_reads_as_changes_pending_with_diff(
        self, ctx: tuple[TestClient, Path]
    ) -> None:
        client, workspace = ctx
        self._approve(client)
        pulse = workspace / "pulse.md"
        pulse.write_text(pulse.read_text().replace("Check health", "Check disk"))
        health = _checks(client)["health"]
        assert health["status"] == "changes_pending" and health["stale"] is True
        assert "-action: Check health" in health["diff"]
        assert "+action: Check disk" in health["diff"]
        assert self._approve(client).status_code == 200
        assert _checks(client)["health"]["approved_revision"] == 2

    def test_unknown_check_is_404(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.post(
            APPROVE, headers=_op(), json={"check": "nope", "definition_digest": "0" * 64}
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "body", [{}, {"check": "health"}, {"check": 3, "definition_digest": "x"}, []]
    )
    def test_malformed_body_is_400(self, ctx: tuple[TestClient, Path], body: object) -> None:
        client, _ = ctx
        assert client.post(APPROVE, headers=_op(), json=body).status_code == 400

    def test_without_an_authority_it_fails_closed(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, workspace = ctx
        digest = _checks(client)["health"]["definition_digest"]
        client.app.state.schedule_control_authority = None
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                APPROVE, headers=_op(), json={"check": "health", "definition_digest": digest}
            )
        assert resp.status_code == 503
        assert "Approval" not in (workspace / "pulse.md").read_text()
        assert _audit_events(caplog)[-1]["outcome"] == "error"

    def test_unknown_agent_is_404(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = client.post(
            "/api/agents/ghost/pulse/approve",
            headers=_op(),
            json={"check": "health", "definition_digest": "0" * 64},
        )
        assert resp.status_code == 404
