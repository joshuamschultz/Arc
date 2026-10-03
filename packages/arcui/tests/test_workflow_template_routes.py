"""Template, from-template and test-run routes for the workflow screens (J3 F6, G5, G8).

The UI codes against three exact contracts, so the route layer is tested against a
fake plane (operator gate, shapes, audit) and the real adapter is driven over a
real runner and store (a template really lands as an unsigned draft, and a test
run really starts above personal tier).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner, is_test_run
from arctrust import OperatorKey
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.workflows import ControlPlaneResult, OperatorActor, WorkflowFieldError
from arcui.routes.workflows import routes as workflow_routes
from arcui.workflow_plane import build_dashboard_plane

_ACTOR = OperatorActor(did="did:arc:ui:operator", session_id="s1")


class _FakePlane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.test_result = ControlPlaneResult(value={"run_id": "test-1", "mode": "test"})
        self.template_result = ControlPlaneResult(value={"workflow_id": "mine"})

    async def test_run_workflow(self, workflow_id: str, *, actor: OperatorActor) -> Any:
        self.calls.append(("test_run_workflow", (workflow_id, actor)))
        return self.test_result

    async def list_templates(self) -> list[dict[str, Any]]:
        self.calls.append(("list_templates", ()))
        return [{"id": "maker_checker", "title": "Maker and checker", "description": "d"}]

    async def create_from_template(
        self, template: str, workflow_id: str, *, owner: str, actor: OperatorActor
    ) -> Any:
        self.calls.append(("create_from_template", (template, workflow_id, owner, actor)))
        return self.template_result


def _app() -> tuple[TestClient, AuthConfig, _FakePlane]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=workflow_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    plane = _FakePlane()
    app.state.workflow_control_plane = plane
    return TestClient(app), auth, plane


def _op(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.operator_token}"}


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def test_the_template_list_has_the_contract_shape() -> None:
    client, auth, _ = _app()

    resp = client.get("/api/workflow-templates", headers=_op(auth))

    assert resp.status_code == 200
    assert resp.json() == {
        "templates": [{"id": "maker_checker", "title": "Maker and checker", "description": "d"}]
    }


def test_from_template_returns_the_new_id() -> None:
    client, auth, plane = _app()

    resp = client.post(
        "/api/workflows/from-template",
        json={"template": "maker_checker", "workflow_id": "mine", "owner": "@writer"},
        headers=_op(auth),
    )

    assert resp.status_code == 201
    assert resp.json() == {"workflow_id": "mine"}
    assert plane.calls[-1][0] == "create_from_template"
    assert plane.calls[-1][1][:3] == ("maker_checker", "mine", "@writer")


@pytest.mark.parametrize("owner", [None, "", 7])
def test_from_template_refuses_a_missing_or_blank_owner(owner: Any) -> None:
    client, auth, plane = _app()
    body: dict[str, Any] = {"template": "maker_checker", "workflow_id": "mine"}
    if owner is not None:
        body["owner"] = owner

    resp = client.post("/api/workflows/from-template", json=body, headers=_op(auth))

    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == "owner"
    assert plane.calls == []


@pytest.mark.parametrize("body", [{}, {"template": "x"}, {"workflow_id": "y"}, {"template": 1}])
def test_from_template_needs_both_strings(body: dict[str, Any]) -> None:
    client, auth, plane = _app()

    resp = client.post("/api/workflows/from-template", json=body, headers=_op(auth))

    assert resp.status_code == 400
    assert plane.calls == []


def test_a_template_refusal_is_a_typed_error() -> None:
    client, auth, plane = _app()
    plane.template_result = ControlPlaneResult(
        errors=[WorkflowFieldError(node_id="", field="template", error="no template 'x'")]
    )

    resp = client.post(
        "/api/workflows/from-template",
        json={"template": "x", "workflow_id": "mine", "owner": "@writer"},
        headers=_op(auth),
    )

    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == "template"


def test_test_run_returns_the_flagged_run() -> None:
    client, auth, plane = _app()

    resp = client.post("/api/workflows/brief/test-run", headers=_op(auth))

    assert resp.status_code == 201
    assert resp.json() == {"run_id": "test-1", "mode": "test"}
    assert plane.calls[-1][1][0] == "brief"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/workflow-templates", None),
        (
            "post",
            "/api/workflows/from-template",
            {"template": "a", "workflow_id": "b", "owner": "@w"},
        ),
        ("post", "/api/workflows/brief/test-run", None),
    ],
)
def test_a_viewer_is_refused_and_nothing_runs(method: str, path: str, body: Any) -> None:
    client, auth, plane = _app()

    resp = getattr(client, method)(path, headers=_viewer(auth), **({"json": body} if body else {}))

    assert resp.status_code == 403
    assert plane.calls == []


# --- the real adapter over a real runner and store -----------------------------------


@pytest.fixture
async def enterprise_plane(tmp_path: Path) -> Any:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    backend = FakeBackend()
    await backend.start()
    runner = build_workflow_runner(
        tier="enterprise",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
    )
    yield build_dashboard_plane(runner=runner)
    await backend.stop()


async def test_the_real_adapter_lists_the_four_templates(enterprise_plane: Any) -> None:
    templates = await enterprise_plane.list_templates()

    assert {t["id"] for t in templates} == {
        "intake_specialist",
        "fanout_synthesize",
        "maker_checker",
        "scheduled_watcher",
    }
    assert all(set(t) == {"id", "title", "description"} for t in templates)


async def test_a_template_lands_as_an_unsigned_draft_with_its_files(
    enterprise_plane: Any, tmp_path: Path
) -> None:
    result = await enterprise_plane.create_from_template(
        "maker_checker", "release-notes", owner="@writer", actor=_ACTOR
    )

    assert result.errors is None, result.errors
    assert result.value == {"workflow_id": "release-notes"}
    detail = await enterprise_plane.get_workflow("release-notes", actor=_ACTOR)
    assert detail is not None and detail["status"] == "draft"
    assert (tmp_path / "workflows" / "release-notes" / "prompts" / "maker.md").is_file()
    assert not (tmp_path / "workflows" / "release-notes" / "workflow.toml.arcsig").exists()


async def test_creating_over_an_existing_workflow_conflicts(enterprise_plane: Any) -> None:
    await enterprise_plane.create_from_template(
        "maker_checker", "dup", owner="@writer", actor=_ACTOR
    )

    again = await enterprise_plane.create_from_template(
        "maker_checker", "dup", owner="@writer", actor=_ACTOR
    )

    assert again.errors is not None


async def test_an_unknown_template_is_refused(enterprise_plane: Any) -> None:
    result = await enterprise_plane.create_from_template(
        "nope", "mine", owner="@writer", actor=_ACTOR
    )

    assert result.errors is not None and result.errors[0].field == "template"


async def test_a_draft_test_run_starts_above_personal_tier(enterprise_plane: Any) -> None:
    await enterprise_plane.create_from_template(
        "scheduled_watcher", "watch", owner="@writer", actor=_ACTOR
    )

    # The same draft is refused as a live run at enterprise tier ...
    live = await enterprise_plane.run_workflow("watch", {}, actor=_ACTOR)
    assert live.errors is not None

    # ... and starts as a test run, in the reserved namespace.
    tested = await enterprise_plane.test_run_workflow("watch", actor=_ACTOR)

    assert tested.errors is None, tested.errors
    assert tested.value is not None
    assert tested.value["mode"] == "test"
    assert is_test_run(tested.value["run_id"])


async def test_the_plane_refuses_the_template_placeholder_everywhere(
    enterprise_plane: Any,
) -> None:
    """The dashboard can never author a workflow that cannot sign."""
    from arcteam.workflow.ownership import PLACEHOLDER_OWNER

    tpl = await enterprise_plane.create_from_template(
        "maker_checker", "ph", owner=PLACEHOLDER_OWNER, actor=_ACTOR
    )
    made = await enterprise_plane.create_workflow(
        {"name": "ph2", "owner": PLACEHOLDER_OWNER}, actor=_ACTOR
    )
    await enterprise_plane.create_workflow({"name": "ok", "owner": "@writer"}, actor=_ACTOR)
    patched = await enterprise_plane.patch_workflow(
        "ok", {"owner": PLACEHOLDER_OWNER}, expected_version=1, actor=_ACTOR
    )

    for result in (tpl, made, patched):
        assert result.errors is not None and result.errors[0].field == "owner"
