"""``/api/approvals`` — operator-gated mechanical HITL surface (SPEC-035).

GET lists pending (any role); POST approve/deny is operator-only. Approve mints
an operator-signed grant that verifies against the matching call and is pinned to
the on-box operator key — a viewer is refused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arctrust import OperatorKey, default_operator_key_path
from arctrust.policy import (
    OperatorApprovalAuthority,
    ToolCall,
    _hash_call,
    grant_from_wire,
    verify_approval,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.approval_notifications import ApprovalNotificationHub
from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_AGENT = "did:arc:test:exec/agent1"


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


def _call() -> ToolCall:
    return ToolCall(
        tool_name="send_message",
        arguments={"to": "coder_agent"},
        agent_did=_AGENT,
        session_id="",
        classification="unclassified",
    )


async def _seed_store(data_dir: Path, call_hash: str, *, enriched: bool = False) -> ApprovalStore:
    backend = FakeBackend()
    await backend.start()
    store = ApprovalStore(backend)
    extra: dict[str, Any] = {}
    if enriched:
        extra = {
            "session_id": "sess-1",
            "arguments": {"to": "coder_agent", "body": "hi"},
            "provenance": [
                {"legs": ["private_data"], "tool": "file_read", "args": "p", "at": "t"}
            ],
        }
    await store.create(
        PendingApproval(
            id="req1",
            agent_did=_AGENT,
            agent_label="josh_agent",
            tool="send_message",
            legs=["external_comms", "private_data"],
            call_hash=call_hash,
            **extra,
        )
    )
    return store


def _make_app(
    tmp_path: Path, call_hash: str, *, enriched: bool = False
) -> tuple[Starlette, AuthConfig, ApprovalStore]:
    from arcui.routes.approvals import routes as approval_routes

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=approval_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.approval_store = asyncio.run(_seed_store(tmp_path, call_hash, enriched=enriched))
    return app, auth, app.state.approval_store


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def _operator(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.operator_token}"}


def _read(store: ApprovalStore) -> Any:
    return asyncio.run(store.get("req1"))


def test_list_pending_visible_to_viewer(tmp_path: Path) -> None:
    app, auth, _ = _make_app(tmp_path, _hash_call(_call()))
    client = TestClient(app)
    resp = client.get("/api/approvals", headers=_viewer(auth))
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["approvals"]]
    assert ids == ["req1"]


def test_list_surfaces_enrichment_fields(tmp_path: Path) -> None:
    # SPEC-035 approval enrichment — GET exposes session_id, redacted arguments,
    # and leg provenance so the panel can render triage context.
    app, auth, _ = _make_app(tmp_path, _hash_call(_call()), enriched=True)
    client = TestClient(app)
    resp = client.get("/api/approvals", headers=_viewer(auth))
    assert resp.status_code == 200
    row = resp.json()["approvals"][0]
    assert row["session_id"] == "sess-1"
    assert row["arguments"] == {"to": "coder_agent", "body": "hi"}
    assert row["provenance"] == [
        {"legs": ["private_data"], "tool": "file_read", "args": "p", "at": "t"}
    ]


def test_viewer_cannot_approve(tmp_path: Path) -> None:
    app, auth, store = _make_app(tmp_path, _hash_call(_call()))
    client = TestClient(app)
    resp = client.post("/api/approvals/req1/approve", headers=_viewer(auth))
    assert resp.status_code == 403
    assert _read(store).status == "pending"


def test_operator_approve_mints_verifiable_pinned_grant(tmp_path: Path) -> None:
    call = _call()
    # Pre-create the on-box operator key the route will sign with.
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)
    app, auth, store = _make_app(tmp_path, _hash_call(call))
    client = TestClient(app)

    resp = client.post("/api/approvals/req1/approve", headers=_operator(auth))
    assert resp.status_code == 200, resp.text

    row = _read(store)
    assert row.status == "approved"
    grant = grant_from_wire(row.grant)
    assert verify_approval(call, grant) is True
    key = OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=False)
    assert grant.approver_did == OperatorApprovalAuthority(key.into_signer()).did


def test_operator_deny(tmp_path: Path) -> None:
    app, auth, store = _make_app(tmp_path, _hash_call(_call()))
    client = TestClient(app)
    resp = client.post("/api/approvals/req1/deny", headers=_operator(auth))
    assert resp.status_code == 200
    row = _read(store)
    assert row.status == "denied"
    assert row.grant is None


def test_notification_feed_is_authenticated_operator_only_and_sanitized(tmp_path: Path) -> None:
    app, auth, _ = _make_app(tmp_path, _hash_call(_call()))
    hub = ApprovalNotificationHub()
    from arcstore.approval_dispatcher import ApprovalNotification

    asyncio.run(
        hub(
            ApprovalNotification(
                event_id="event-1",
                approval_id="req1",
                status="pending",
                agent_did=_AGENT,
                tool="send_message",
                classification="UNCLASSIFIED",
                attempts=1,
            )
        )
    )
    app.state.approval_notification_hub = hub
    client = TestClient(app)

    assert client.get("/api/approvals/notifications").status_code == 401
    assert client.get("/api/approvals/notifications", headers=_viewer(auth)).status_code == 403
    response = client.get("/api/approvals/notifications", headers=_operator(auth))
    assert response.status_code == 200
    assert response.json() == {
        "events": [
            {
                "type": "approval_notification",
                "event_id": "event-1",
                "approval_id": "req1",
                "status": "pending",
                "agent_did": _AGENT,
                "tool": "send_message",
                "classification": "UNCLASSIFIED",
            }
        ]
    }
    assert client.post(
        "/api/approvals/notifications/event-1/ack", headers=_viewer(auth)
    ).status_code == 403
    acknowledged = client.post(
        "/api/approvals/notifications/event-1/ack", headers=_operator(auth)
    )
    assert acknowledged.status_code == 200
    assert client.get("/api/approvals/notifications", headers=_operator(auth)).json() == {
        "events": []
    }
