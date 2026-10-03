"""Workflow repair buttons: preview, migrate, migrate-and-re-sign (J1-5).

The dashboard's unreadable / stale-signature card used to print a shell command.
These tests drive the real operator-only route over the real dashboard plane and
a real bundle store: a viewer cannot repair, a preview writes nothing, an apply
rewrites only the named workflow, and a re-sign is made only by the pinned
operator key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner
from arctrust import OperatorKey
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.workflows import OperatorActor
from arcui.routes.workflows import routes as workflow_routes
from arcui.workflow_plane import build_dashboard_plane

_LEGACY = """
[workflow]
id = "morning"
version = 1
owner = "@olivia"

[[node]]
id = "a"
kind = "agent"
agent = "@olivia"
join = "all"
"""


class _Box:
    def __init__(self, tmp_path: Path, *, signing_key: OperatorKey | None = None) -> None:
        self.key = OperatorKey.generate()
        self.key.save(tmp_path / "operator" / "operator.key")
        backend = FakeBackend()
        asyncio.run(backend.start())
        runner = build_workflow_runner(
            tier="personal",
            task_store_backend=backend,
            runner_key_path=tmp_path / "operator" / "operator.key",
            workspace_root=tmp_path,
        )
        self.plane = build_dashboard_plane(runner=runner)
        self.root = tmp_path / "workflows"
        signing = signing_key or self.key
        self.auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = Starlette(routes=workflow_routes)
        app.add_middleware(AuthMiddleware, auth_config=self.auth)
        app.state.auth_config = self.auth
        app.state.audit = UIAuditLogger(enabled=False)
        app.state.workflow_control_plane = self.plane
        app.state.operator_signer_factory = signing.into_signer
        self.client = TestClient(app)

    def legacy(self, wid: str = "morning") -> Path:
        bundle = self.root / wid
        bundle.mkdir(parents=True)
        (bundle / "workflow.toml").write_text(_LEGACY.replace('id = "morning"', f'id = "{wid}"'))
        return bundle

    def operator(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.operator_token}"}

    def viewer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.viewer_token}"}

    def post(self, wid: str, body: dict[str, Any], *, headers: dict[str, str]) -> Any:
        return self.client.post(f"/api/workflows/{wid}/migrate", json=body, headers=headers)


@pytest.fixture
def box(tmp_path: Path) -> _Box:
    return _Box(tmp_path)


def test_a_viewer_cannot_migrate_a_workflow(box: _Box) -> None:
    bundle = box.legacy()
    before = (bundle / "workflow.toml").read_text()

    response = box.post("morning", {"apply": True, "resign": True}, headers=box.viewer())

    assert response.status_code == 403
    assert (bundle / "workflow.toml").read_text() == before


def test_preview_reports_the_change_and_writes_nothing(box: _Box) -> None:
    bundle = box.legacy()
    before = (bundle / "workflow.toml").read_text()

    response = box.post("morning", {"apply": False}, headers=box.operator())

    assert response.status_code == 200
    assert response.json()["action"] == "would_rewrite"
    assert response.json()["nodes"] == ["a"]
    assert (bundle / "workflow.toml").read_text() == before


def test_apply_rewrites_the_named_workflow_only(box: _Box) -> None:
    bundle = box.legacy()
    other = box.legacy("other")
    untouched = (other / "workflow.toml").read_text()

    response = box.post("morning", {"apply": True}, headers=box.operator())

    assert response.json()["action"] == "rewritten"
    assert "join" not in (bundle / "workflow.toml").read_text()
    assert (other / "workflow.toml").read_text() == untouched


def test_apply_with_resign_signs_through_the_pinned_operator_key(box: _Box) -> None:
    box.legacy()
    # Only a bundle that carried a signature is re-signed; a draft stays a draft.
    from arctrust import sign_artifact_with_signer

    stale = sign_artifact_with_signer(
        b"old-canonical-form", signer_did="operator:x", signer=box.key.into_signer()
    )
    (box.root / "morning" / "workflow.toml.arcsig").write_text(stale.to_json())

    response = box.post("morning", {"apply": True, "resign": True}, headers=box.operator())

    assert response.json()["action"] == "rewritten"
    rows = asyncio.run(box.plane.list_workflows(actor=OperatorActor("did:arc:ui:operator", "s")))
    assert rows[0]["health"] == "ok"
    assert rows[0]["status"] == "signed"


def test_unreadable_row_offers_the_migrate_button_not_a_command(box: _Box) -> None:
    box.legacy()

    rows = asyncio.run(box.plane.list_workflows(actor=OperatorActor("did:arc:ui:operator", "s")))

    assert rows[0]["status"] == "unreadable"
    assert rows[0]["health_fix_action"] == "migrate"
    assert "arc workflow" not in str(rows[0])


def test_an_unknown_workflow_is_not_found(box: _Box) -> None:
    assert box.post("nope", {"apply": False}, headers=box.operator()).status_code == 404


def test_a_malformed_body_is_a_client_error(box: _Box) -> None:
    box.legacy()
    response = box.client.post(
        "/api/workflows/morning/migrate", content="nope", headers=box.operator()
    )
    assert response.status_code == 400


def test_resign_by_a_key_that_is_not_the_pinned_operator_key_is_refused(
    tmp_path: Path,
) -> None:
    stranger = _Box(tmp_path, signing_key=OperatorKey.generate())
    bundle = stranger.legacy()
    before = (bundle / "workflow.toml").read_text()

    response = stranger.post(
        "morning", {"apply": True, "resign": True}, headers=stranger.operator()
    )

    assert response.json()["action"] == "refused"
    assert "pinned" in response.json()["reason"]
    assert (bundle / "workflow.toml").read_text() == before
