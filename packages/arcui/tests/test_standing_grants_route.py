"""arcui "Always allow" + Standing approvals (SPEC-035 OQ-3, ruled 2026-10-03).

``POST /api/approvals/{id}/always`` (operator only) resolves the request approved
AND stores an operator-signed interactive standing grant for its scope.
``GET /api/standing-grants`` lists them (any role, signature bytes never shown);
``POST /api/standing-grants/{id}/revoke`` (operator only) revokes one at once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arcstore.standing_grants import StandingGrantStore
from arctrust import OperatorKey, default_operator_key_path
from arctrust.policy import (
    INTERACTIVE_ORIGIN,
    OperatorApprovalAuthority,
    scenario_grant_from_wire,
    verify_interactive_grant,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_AGENT = "did:arc:local:executor/c0bef560"
_TRIFECTA = ["external_comms", "private_data", "untrusted_input"]


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)


def _operator_did(tmp_path: Path) -> str:
    key = OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=False)
    return OperatorApprovalAuthority(key.into_signer()).did


class _Box:
    def __init__(self, *, eligible: bool = True) -> None:
        from arcui.routes.approvals import routes as approval_routes
        from arcui.routes.standing_grants import routes as standing_routes

        backend = FakeBackend()
        asyncio.run(backend.start())
        self.approvals = ApprovalStore(backend)
        self.standing = StandingGrantStore(backend)
        asyncio.run(
            self.approvals.create(
                PendingApproval(
                    id="req1",
                    agent_did=_AGENT,
                    agent_label="Olivia",
                    tool="dropbox_upload",
                    legs=_TRIFECTA,
                    call_hash="abc",
                    session_id="82dca58cd015a1f1",
                    destination="personal_dropbox",
                    grant_tool="dropbox_upload",
                    standing_eligible=eligible,
                )
            )
        )
        self.auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = Starlette(routes=[*approval_routes, *standing_routes])
        app.add_middleware(AuthMiddleware, auth_config=self.auth)
        app.state.auth_config = self.auth
        app.state.audit = UIAuditLogger(enabled=False)
        app.state.approval_store = self.approvals
        app.state.standing_grant_store = self.standing
        self.client = TestClient(app)

    def as_operator(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.operator_token}"}

    def as_viewer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.viewer_token}"}


def test_always_allow_approves_the_call_and_stores_a_signed_standing_grant(
    tmp_path: Path,
) -> None:
    box = _Box()

    resp = box.client.post("/api/approvals/req1/always", headers=box.as_operator())

    assert resp.status_code == 200, resp.text
    assert resp.json()["approval"]["status"] == "approved"
    [row] = asyncio.run(box.standing.active_for(_AGENT))
    assert row.destination == "personal_dropbox"
    assert row.granted_by == _operator_did(tmp_path)
    grant = scenario_grant_from_wire(row.grant)
    assert grant.origin == INTERACTIVE_ORIGIN
    assert verify_interactive_grant(
        grant,
        agent_did=_AGENT,
        tool_name="dropbox_upload",
        legs=frozenset(_TRIFECTA),
        destination="personal_dropbox",
        tier="personal",
    )


def test_viewer_cannot_always_allow() -> None:
    box = _Box()

    resp = box.client.post("/api/approvals/req1/always", headers=box.as_viewer())

    assert resp.status_code == 403
    assert asyncio.run(box.standing.list()) == []
    assert asyncio.run(box.approvals.get("req1")).status == "pending"  # type: ignore[union-attr]


def test_ineligible_request_cannot_be_made_to_stand() -> None:
    # The agent marks a federal request ineligible; arcui refuses and changes nothing.
    box = _Box(eligible=False)

    resp = box.client.post("/api/approvals/req1/always", headers=box.as_operator())

    assert resp.status_code == 409
    assert asyncio.run(box.standing.list()) == []
    assert asyncio.run(box.approvals.get("req1")).status == "pending"  # type: ignore[union-attr]


def test_list_shows_scope_grantor_and_use_count_never_the_signature() -> None:
    box = _Box()
    box.client.post("/api/approvals/req1/always", headers=box.as_operator())

    resp = box.client.get(f"/api/standing-grants?agent_did={_AGENT}", headers=box.as_viewer())

    assert resp.status_code == 200
    [row] = resp.json()["grants"]
    assert row["composition"] == _TRIFECTA
    assert row["destination"] == "personal_dropbox"
    assert row["tool"] == "dropbox_upload"
    assert row["use_count"] == 0
    assert row["granted_by"]
    assert row["granted_at"]
    assert "grant" not in row


def test_operator_revoke_takes_effect_at_once() -> None:
    box = _Box()
    box.client.post("/api/approvals/req1/always", headers=box.as_operator())
    [row] = asyncio.run(box.standing.active_for(_AGENT))

    denied = box.client.post(f"/api/standing-grants/{row.id}/revoke", headers=box.as_viewer())
    assert denied.status_code == 403

    resp = box.client.post(f"/api/standing-grants/{row.id}/revoke", headers=box.as_operator())

    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"
    assert asyncio.run(box.standing.active_for(_AGENT)) == []
    again = box.client.post(f"/api/standing-grants/{row.id}/revoke", headers=box.as_operator())
    assert again.status_code == 404


def test_list_filters_revoked_rows_out_by_default() -> None:
    box = _Box()
    box.client.post("/api/approvals/req1/always", headers=box.as_operator())
    [row] = asyncio.run(box.standing.active_for(_AGENT))
    box.client.post(f"/api/standing-grants/{row.id}/revoke", headers=box.as_operator())

    active: Any = box.client.get("/api/standing-grants", headers=box.as_viewer()).json()
    every: Any = box.client.get("/api/standing-grants?status=all", headers=box.as_viewer()).json()

    assert active["grants"] == []
    assert [g["status"] for g in every["grants"]] == ["revoked"]
