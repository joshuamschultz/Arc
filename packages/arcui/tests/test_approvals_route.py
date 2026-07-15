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
from arcstore.backends.sqlite import SqliteBackend
from arctrust import OperatorKey
from arctrust.policy import (
    OperatorApprovalAuthority,
    ToolCall,
    _hash_call,
    grant_from_wire,
    verify_approval,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_AGENT = "did:arc:test:exec/agent1"


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


def _call() -> ToolCall:
    return ToolCall(
        tool_name="send_message", arguments={"to": "coder_agent"}, agent_did=_AGENT,
        session_id="", classification="unclassified",
    )


async def _seed_store(
    data_dir: Path, call_hash: str, *, enriched: bool = False
) -> ApprovalStore:
    backend = SqliteBackend(data_dir / "store" / "arcui.db")
    await backend.start()
    store = ApprovalStore(backend)
    extra: dict[str, Any] = {}
    if enriched:
        extra = {
            "session_id": "sess-1",
            "arguments": {"to": "coder_agent", "body": "hi"},
            "provenance": [{"legs": ["private_data"], "tool": "file_read", "args": "p", "at": "t"}],
        }
    await store.create(
        PendingApproval(
            id="req1", agent_did=_AGENT, agent_label="josh_agent",
            tool="send_message", legs=["external_comms", "private_data"], call_hash=call_hash,
            **extra,
        )
    )
    return store


def _make_app(
    tmp_path: Path, call_hash: str, *, enriched: bool = False
) -> tuple[Starlette, AuthConfig]:
    from arcui.routes.approvals import routes as approval_routes

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=approval_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.approval_store = asyncio.run(_seed_store(tmp_path, call_hash, enriched=enriched))
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def _operator(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.operator_token}"}


def _read(tmp_path: Path) -> Any:
    async def _run() -> Any:
        backend = SqliteBackend(tmp_path / "store" / "arcui.db")
        await backend.start()
        try:
            return await ApprovalStore(backend).get("req1")
        finally:
            await backend.stop()

    return asyncio.run(_run())


def test_list_pending_visible_to_viewer(tmp_path: Path) -> None:
    app, auth = _make_app(tmp_path, _hash_call(_call()))
    client = TestClient(app)
    resp = client.get("/api/approvals", headers=_viewer(auth))
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["approvals"]]
    assert ids == ["req1"]


def test_list_surfaces_enrichment_fields(tmp_path: Path) -> None:
    # SPEC-035 approval enrichment — GET exposes session_id, redacted arguments,
    # and leg provenance so the panel can render triage context.
    app, auth = _make_app(tmp_path, _hash_call(_call()), enriched=True)
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
    app, auth = _make_app(tmp_path, _hash_call(_call()))
    client = TestClient(app)
    resp = client.post("/api/approvals/req1/approve", headers=_viewer(auth))
    assert resp.status_code == 403
    assert _read(tmp_path).status == "pending"


def test_operator_approve_mints_verifiable_pinned_grant(tmp_path: Path) -> None:
    call = _call()
    # Pre-create the on-box operator key the route will sign with.
    OperatorKey.load(tmp_path / "operator" / "operator.key", generate_if_absent=True)
    app, auth = _make_app(tmp_path, _hash_call(call))
    client = TestClient(app)

    resp = client.post("/api/approvals/req1/approve", headers=_operator(auth))
    assert resp.status_code == 200, resp.text

    row = _read(tmp_path)
    assert row.status == "approved"
    grant = grant_from_wire(row.grant)
    assert verify_approval(call, grant) is True
    key = OperatorKey.load(tmp_path / "operator" / "operator.key", generate_if_absent=False)
    assert grant.approver_did == OperatorApprovalAuthority(key.into_signer()).did


def test_operator_deny(tmp_path: Path) -> None:
    app, auth = _make_app(tmp_path, _hash_call(_call()))
    client = TestClient(app)
    resp = client.post("/api/approvals/req1/deny", headers=_operator(auth))
    assert resp.status_code == 200
    row = _read(tmp_path)
    assert row.status == "denied"
    assert row.grant is None
