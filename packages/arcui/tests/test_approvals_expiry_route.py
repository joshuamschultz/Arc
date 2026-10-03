"""An expired approval request can never run the call (D3, sweep 2026-10-03).

The agent's gate stops waiting after its timeout and the call is already denied.
An operator who approves afterwards must be told so, and nothing may be granted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arcstore.standing_grants import StandingGrantStore
from arctrust import OperatorKey, default_operator_key_path
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_AGENT = "did:arc:local:executor/c0bef560"
_GONE = "This request expired; the agent has moved on"


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)


def _iso(delta_seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_seconds)).isoformat()


class _Box:
    def __init__(self) -> None:
        from arcui.routes.approvals import routes as approval_routes

        backend = FakeBackend()
        asyncio.run(backend.start())
        self.approvals = ApprovalStore(backend)
        self.standing = StandingGrantStore(backend)
        for approval_id, expires_at in (("stale", _iso(-60)), ("live", _iso(+300))):
            asyncio.run(
                self.approvals.create(
                    PendingApproval(
                        id=approval_id,
                        agent_did=_AGENT,
                        tool="dropbox_upload",
                        legs=["external_comms", "private_data", "untrusted_input"],
                        call_hash="abc",
                        destination="personal_dropbox",
                        grant_tool="dropbox_upload",
                        standing_eligible=True,
                        expires_at=expires_at,
                    )
                )
            )
        self.auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = Starlette(routes=approval_routes)
        app.add_middleware(AuthMiddleware, auth_config=self.auth)
        app.state.auth_config = self.auth
        app.state.audit = UIAuditLogger(enabled=False)
        app.state.approval_store = self.approvals
        app.state.standing_grant_store = self.standing
        self.client = TestClient(app)

    def operator(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.operator_token}"}

    def row(self, approval_id: str) -> PendingApproval:
        row = asyncio.run(self.approvals.get(approval_id))
        assert row is not None
        return row


def test_approving_after_expiry_is_410_and_grants_nothing() -> None:
    box = _Box()

    resp = box.client.post("/api/approvals/stale/approve", headers=box.operator())

    assert resp.status_code == 410
    assert resp.json() == {"error": _GONE}
    row = box.row("stale")
    assert row.status == "expired"
    assert row.grant is None


def test_always_allow_after_expiry_is_410_and_stores_no_standing_grant() -> None:
    box = _Box()

    resp = box.client.post("/api/approvals/stale/always", headers=box.operator())

    assert resp.status_code == 410
    assert resp.json() == {"error": _GONE}
    assert box.row("stale").grant is None
    assert asyncio.run(box.standing.active_for(_AGENT)) == []


def test_approving_an_already_expired_row_is_410() -> None:
    box = _Box()
    asyncio.run(
        box.approvals.resolve("live", status="expired", actor_did=_AGENT, resolved_by=_AGENT)
    )

    resp = box.client.post("/api/approvals/live/approve", headers=box.operator())

    assert resp.status_code == 410
    assert box.row("live").grant is None


def test_list_hides_expired_rows_and_closes_the_stale_one() -> None:
    box = _Box()

    resp = box.client.get("/api/approvals", headers=box.operator())

    assert [a["id"] for a in resp.json()["approvals"]] == ["live"]
    assert box.row("stale").status == "expired"


def test_a_live_request_still_approves() -> None:
    box = _Box()

    resp = box.client.post("/api/approvals/live/approve", headers=box.operator())

    assert resp.status_code == 200
    assert box.row("live").status == "approved"
