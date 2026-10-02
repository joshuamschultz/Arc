"""The pending approvals list carries a reason and a diff for a workflow-sign ask (J3 F10)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arcteam.workflow import DefinitionStore, parse_definition, sign_definition
from arctrust import generate_keypair
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_AGENT = "did:arc:test:exec/agent1"


def _document(second_node: str) -> dict[str, Any]:
    return {
        "workflow": {"id": "brief", "owner": "@ops"},
        "node": [
            {"id": "gather", "kind": "agent", "agent": "@ops", "prompt": "prompts/gather.md"},
            {"id": second_node, "kind": "agent", "agent": "@ops", "needs": ["gather"]},
        ],
    }


def _save(store: DefinitionStore, second: str, prompt: bytes, expected: int | None) -> None:
    store.save_draft(
        parse_definition(_document(second)),
        actor_did=_AGENT,
        expected_version=expected,
        files={"prompts/gather.md": prompt},
    )


def _app(tmp_path: Path) -> tuple[Starlette, AuthConfig, DefinitionStore]:
    from arcui.routes.approvals import routes as approval_routes

    keys = generate_keypair()
    definitions = DefinitionStore(tmp_path / "wf", operator_public_key=keys.public_key)
    _save(definitions, "send", b"Gather.\nBe brief.\n", None)
    sign_definition(definitions, "brief", signer_did="did:arc:op", private_key=keys.private_key)
    _save(definitions, "review", b"Gather.\nBe thorough.\n", 1)

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=approval_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    backend = FakeBackend()
    asyncio.run(backend.start())
    store = ApprovalStore(backend)
    bundle = definitions.load("brief")
    asyncio.run(
        store.create(
            PendingApproval(
                id="wfsign1",
                agent_did=_AGENT,
                agent_label="brief",
                tool="workflow_sign",
                legs=[],
                call_hash=bundle.content_hash,
                arguments={
                    "workflow_id": "brief",
                    "version": str(bundle.definition.version),
                    "reason": "tighten the prompt",
                },
            )
        )
    )
    app.state.approval_store = store

    class _Plane:
        pass

    plane = _Plane()
    plane.definitions = definitions  # type: ignore[attr-defined]  # reason: minimal test double
    app.state.workflow_control_plane = plane
    return app, auth, definitions


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


def test_diff_vs_last_signed(tmp_path: Path) -> None:
    app, auth, _ = _app(tmp_path)

    resp = TestClient(app).get(
        "/api/approvals", headers={"Authorization": f"Bearer {auth.viewer_token}"}
    )

    [row] = resp.json()["approvals"]
    assert row["reason"] == "tighten the prompt"
    diff = row["diff"]
    assert diff["nodes"] == {"added": ["review"], "removed": ["send"], "changed": []}
    [file] = diff["files"]
    assert (file["path"], file["status"]) == ("prompts/gather.md", "changed")
    assert "+Be thorough." in file["diff"]


def test_a_non_workflow_approval_carries_no_diff(tmp_path: Path) -> None:
    app, auth, _ = _app(tmp_path)
    store: ApprovalStore = app.state.approval_store
    asyncio.run(
        store.create(
            PendingApproval(
                id="other",
                agent_did=_AGENT,
                agent_label="x",
                tool="send_message",
                legs=[],
                call_hash="h",
            )
        )
    )

    resp = TestClient(app).get(
        "/api/approvals", headers={"Authorization": f"Bearer {auth.viewer_token}"}
    )

    rows = {a["id"]: a for a in resp.json()["approvals"]}
    assert rows["other"].get("diff") is None
