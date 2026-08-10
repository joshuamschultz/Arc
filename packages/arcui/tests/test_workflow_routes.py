"""Workflow routes (COMP-023) — thin delegating adapter, operator-gated, audited.

SPEC-061 ArcFlow, T-872. Every route in ``arcui.routes.workflows`` is tested
against a hand-rolled fake satisfying ``WorkflowControlPlane``/
``GateControlPlane`` — the real ``arcteam`` implementation (COMP-021/COMP-018)
is a concurrent, not-yet-merged workstream. These tests prove the route layer
does its ONE job: gate on operator role, translate HTTP <-> one control-plane
call, relay the result, and audit every outcome — never anything more.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from arcteam.workflow.runner import node_task_id
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes import workflows as workflows_module
from arcui.routes.workflows import ControlPlaneResult, OperatorActor, WorkflowFieldError
from arcui.routes.workflows import routes as workflow_routes


class FakeControlPlane:
    """Records every call it receives; returns whatever the test configured."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.list_workflows_result: list[dict[str, Any]] = []
        self.get_workflow_result: dict[str, Any] | None = None
        self.create_result = ControlPlaneResult(value={"id": "wf-1", "version": 1})
        self.patch_result = ControlPlaneResult(value={"id": "wf-1", "version": 2})
        self.archive_result = ControlPlaneResult(value={"id": "wf-1", "status": "archived"})
        self.unarchive_result = ControlPlaneResult(value={"id": "wf-1", "status": "draft"})
        self.run_result = ControlPlaneResult(value={"run_id": "run-1"})
        self.cancel_result = ControlPlaneResult(value={"run_id": "run-1", "status": "cancelling"})
        self.list_runs_result: list[dict[str, Any]] = []
        self.get_run_result: dict[str, Any] | None = None

    async def list_workflows(self, *, actor: OperatorActor) -> list[dict[str, Any]]:
        self.calls.append(("list_workflows", (), {"actor": actor}))
        return self.list_workflows_result

    async def get_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> dict[str, Any] | None:
        self.calls.append(("get_workflow", (workflow_id,), {"actor": actor}))
        return self.get_workflow_result

    async def create_workflow(
        self, definition: dict[str, Any], *, actor: OperatorActor
    ) -> ControlPlaneResult:
        self.calls.append(("create_workflow", (definition,), {"actor": actor}))
        return self.create_result

    async def patch_workflow(
        self,
        workflow_id: str,
        patch: dict[str, Any],
        *,
        expected_version: int,
        actor: OperatorActor,
    ) -> ControlPlaneResult:
        self.calls.append(
            (
                "patch_workflow",
                (workflow_id, patch),
                {"expected_version": expected_version, "actor": actor},
            )
        )
        return self.patch_result

    async def archive_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        self.calls.append(("archive_workflow", (workflow_id,), {"actor": actor}))
        return self.archive_result

    async def unarchive_workflow(
        self, workflow_id: str, *, actor: OperatorActor
    ) -> ControlPlaneResult:
        self.calls.append(("unarchive_workflow", (workflow_id,), {"actor": actor}))
        return self.unarchive_result

    async def run_workflow(
        self, workflow_id: str, run_input: dict[str, Any], *, actor: OperatorActor
    ) -> ControlPlaneResult:
        self.calls.append(("run_workflow", (workflow_id, run_input), {"actor": actor}))
        return self.run_result

    async def cancel_run(self, run_id: str, *, actor: OperatorActor) -> ControlPlaneResult:
        self.calls.append(("cancel_run", (run_id,), {"actor": actor}))
        return self.cancel_result

    async def list_runs(self, workflow_id: str, *, actor: OperatorActor) -> list[dict[str, Any]]:
        self.calls.append(("list_runs", (workflow_id,), {"actor": actor}))
        return self.list_runs_result

    async def get_run(self, run_id: str, *, actor: OperatorActor) -> dict[str, Any] | None:
        self.calls.append(("get_run", (run_id,), {"actor": actor}))
        return self.get_run_result


class FakeGatePlane:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = ControlPlaneResult(value={"task_id": "t-1", "status": "done"})

    async def resolve_gate(
        self, task_id: str, *, decision: str, notes: str, actor: OperatorActor
    ) -> ControlPlaneResult:
        self.calls.append((task_id, decision, notes, actor))
        return self.result


def _make_app() -> tuple[Starlette, AuthConfig, FakeControlPlane, FakeGatePlane]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=workflow_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    plane = FakeControlPlane()
    gate = FakeGatePlane()
    app.state.workflow_control_plane = plane
    app.state.gate_control_plane = gate
    return app, auth, plane, gate


def _operator(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.operator_token}"}


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def _mutations(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    out = []
    for record in caplog.records:
        if record.name != "arcui.audit":
            continue
        payload = json.loads(record.message)
        if payload["event_type"] == "ui.mutation":
            out.append(payload["details"])
    return out


class TestOperatorGate:
    """Every route refuses a viewer token before touching the control plane."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/workflows"),
            ("POST", "/api/workflows"),
            ("GET", "/api/workflows/wf-1"),
            ("PATCH", "/api/workflows/wf-1"),
            ("POST", "/api/workflows/wf-1/archive"),
            ("POST", "/api/workflows/wf-1/unarchive"),
            ("POST", "/api/workflows/wf-1/run"),
            ("GET", "/api/workflows/wf-1/runs"),
            ("GET", "/api/workflow-runs/run-1"),
            ("POST", "/api/workflow-runs/run-1/cancel"),
            ("POST", "/api/workflow-tasks/t-1/gate"),
        ],
    )
    def test_viewer_forbidden(self, method: str, path: str) -> None:
        app, auth, plane, gate = _make_app()
        client = TestClient(app)
        resp = client.request(method, path, headers=_viewer(auth), json={"expected_version": 1})
        assert resp.status_code == 403
        assert resp.json() == {"error": "operator_role_required"}
        assert plane.calls == []
        assert gate.calls == []


class TestListAndGet:
    def test_list_workflows(self, caplog: pytest.LogCaptureFixture) -> None:
        app, auth, plane, _ = _make_app()
        app.state.audit = UIAuditLogger()
        plane.list_workflows_result = [{"id": "wf-1", "name": "onboarding", "status": "signed"}]
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.get("/api/workflows", headers=_operator(auth))

        assert resp.status_code == 200
        assert resp.json() == {
            "workflows": [{"id": "wf-1", "name": "onboarding", "status": "signed"}]
        }
        assert _mutations(caplog)[0]["operation"] == "workflow.list"
        assert _mutations(caplog)[0]["outcome"] == "applied"

    def test_get_workflow_not_found(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.get_workflow_result = None
        client = TestClient(app)
        resp = client.get("/api/workflows/missing", headers=_operator(auth))
        assert resp.status_code == 404

    def test_get_workflow_found(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.get_workflow_result = {"id": "wf-1", "nodes": []}
        client = TestClient(app)
        resp = client.get("/api/workflows/wf-1", headers=_operator(auth))
        assert resp.status_code == 200
        assert resp.json() == {"id": "wf-1", "nodes": []}
        assert plane.calls[0][0] == "get_workflow"


class TestCreateWorkflow:
    def test_create_success(self, caplog: pytest.LogCaptureFixture) -> None:
        app, auth, plane, _ = _make_app()
        app.state.audit = UIAuditLogger()
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                "/api/workflows", headers=_operator(auth), json={"name": "onboarding"}
            )

        assert resp.status_code == 201
        assert resp.json() == {"id": "wf-1", "version": 1}
        assert plane.calls[0] == ("create_workflow", ({"name": "onboarding"},), plane.calls[0][2])
        assert _mutations(caplog)[0]["operation"] == "workflow.create"
        assert _mutations(caplog)[0]["target"] == "workflow:wf-1"

    def test_create_validation_errors_relayed_verbatim(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.create_result = ControlPlaneResult(
            errors=[
                WorkflowFieldError(
                    node_id="fetch_data",
                    field="needs",
                    error="dangling reference",
                    observed="missing_node",
                    admissible=["ingest", "validate"],
                )
            ]
        )
        client = TestClient(app)
        resp = client.post("/api/workflows", headers=_operator(auth), json={"name": "bad"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["errors"] == [
            {
                "node_id": "fetch_data",
                "field": "needs",
                "error": "dangling reference",
                "observed": "missing_node",
                "admissible": ["ingest", "validate"],
            }
        ]

    def test_create_non_json_body_is_400(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/workflows",
            headers={**_operator(auth), "Content-Type": "application/json"},
            content=b"not json",
        )
        assert resp.status_code == 400
        assert plane.calls == []

    def test_control_plane_unavailable_is_503(self) -> None:
        app, auth, _, _ = _make_app()
        app.state.workflow_control_plane = None
        client = TestClient(app)
        resp = client.post("/api/workflows", headers=_operator(auth), json={"name": "x"})
        assert resp.status_code == 503


class TestPatchWorkflow:
    def test_patch_requires_expected_version(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.patch("/api/workflows/wf-1", headers=_operator(auth), json={"name": "new"})
        assert resp.status_code == 400
        assert plane.calls == []

    def test_patch_expected_version_must_be_int(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.patch(
            "/api/workflows/wf-1",
            headers=_operator(auth),
            json={"expected_version": "1", "name": "new"},
        )
        assert resp.status_code == 400
        assert plane.calls == []

    def test_patch_success_strips_expected_version_from_patch(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.patch(
            "/api/workflows/wf-1",
            headers=_operator(auth),
            json={"expected_version": 1, "name": "renamed"},
        )
        assert resp.status_code == 200
        call = plane.calls[0]
        assert call[0] == "patch_workflow"
        assert call[1] == ("wf-1", {"name": "renamed"})
        assert call[2]["expected_version"] == 1

    def test_patch_version_conflict_is_409(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.patch_result = ControlPlaneResult(conflict=True)
        client = TestClient(app)
        resp = client.patch(
            "/api/workflows/wf-1", headers=_operator(auth), json={"expected_version": 1}
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "version_conflict"}

    def test_patch_not_found_is_404(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.patch_result = ControlPlaneResult(not_found=True)
        client = TestClient(app)
        resp = client.patch(
            "/api/workflows/missing", headers=_operator(auth), json={"expected_version": 1}
        )
        assert resp.status_code == 404


class TestArchiveUnarchive:
    def test_archive(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.post("/api/workflows/wf-1/archive", headers=_operator(auth))
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"
        assert plane.calls[0][0] == "archive_workflow"

    def test_unarchive(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.post("/api/workflows/wf-1/unarchive", headers=_operator(auth))
        assert resp.status_code == 200
        assert resp.json()["status"] == "draft"
        assert plane.calls[0][0] == "unarchive_workflow"


class TestRunAndCancel:
    def test_run_workflow(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/workflows/wf-1/run", headers=_operator(auth), json={"input": {"x": 1}}
        )
        assert resp.status_code == 201
        assert resp.json() == {"run_id": "run-1"}
        assert plane.calls[0] == ("run_workflow", ("wf-1", {"input": {"x": 1}}), plane.calls[0][2])

    def test_run_workflow_no_body(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.post("/api/workflows/wf-1/run", headers=_operator(auth))
        assert resp.status_code == 201
        assert plane.calls[0][1] == ("wf-1", {})

    def test_list_runs(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.list_runs_result = [{"run_id": "run-1", "status": "done"}]
        client = TestClient(app)
        resp = client.get("/api/workflows/wf-1/runs", headers=_operator(auth))
        assert resp.status_code == 200
        assert resp.json() == {"runs": [{"run_id": "run-1", "status": "done"}]}

    def test_get_run(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.get_run_result = {"run_id": "run-1", "path_taken": ["a", "b"]}
        client = TestClient(app)
        resp = client.get("/api/workflow-runs/run-1", headers=_operator(auth))
        assert resp.status_code == 200
        assert resp.json()["path_taken"] == ["a", "b"]

    def test_get_run_not_found(self) -> None:
        app, auth, plane, _ = _make_app()
        plane.get_run_result = None
        client = TestClient(app)
        resp = client.get("/api/workflow-runs/missing", headers=_operator(auth))
        assert resp.status_code == 404

    def test_cancel_run(self) -> None:
        app, auth, plane, _ = _make_app()
        client = TestClient(app)
        resp = client.post("/api/workflow-runs/run-1/cancel", headers=_operator(auth))
        assert resp.status_code == 200
        assert plane.calls[0][0] == "cancel_run"


class TestGateResolution:
    def test_resolve_gate_approve(self, caplog: pytest.LogCaptureFixture) -> None:
        app, auth, _, gate = _make_app()
        app.state.audit = UIAuditLogger()
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                "/api/workflow-tasks/t-1/gate",
                headers=_operator(auth),
                json={"decision": "approve"},
            )

        assert resp.status_code == 200
        task_id, decision, notes, actor = gate.calls[0]
        assert (task_id, decision, notes) == ("t-1", "approve", "")
        assert isinstance(actor, OperatorActor)
        assert _mutations(caplog)[0]["operation"] == "gate.resolve"
        assert _mutations(caplog)[0]["outcome"] == "applied"

    def test_resolve_gate_return_for_revision_with_notes(self) -> None:
        app, auth, _, gate = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/workflow-tasks/t-1/gate",
            headers=_operator(auth),
            json={"decision": "return_for_revision", "notes": "please add error handling"},
        )
        assert resp.status_code == 200
        assert gate.calls[0][1:3] == ("return_for_revision", "please add error handling")

    def test_resolve_gate_fail_run(self) -> None:
        app, auth, _, gate = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/workflow-tasks/t-1/gate",
            headers=_operator(auth),
            json={"decision": "fail_run", "notes": "unrecoverable"},
        )
        assert resp.status_code == 200
        assert gate.calls[0][1] == "fail_run"

    def test_resolve_gate_accepts_a_real_workflow_task_id(self) -> None:
        """A real id is ``wf/{run_id}/{node_id}/{iteration}`` — it has slashes.

        Every test above uses ``t-1``, a shape the runner never mints, so a
        single-segment route matched them all while answering 404 to every id
        a human could actually be asked to approve.
        """
        app, auth, _, gate = _make_app()
        client = TestClient(app)
        task_id = node_task_id("run-9", "review", 0)

        resp = client.post(
            f"/api/workflow-tasks/{task_id}/gate",
            headers=_operator(auth),
            json={"decision": "approve"},
        )

        assert resp.status_code == 200
        assert gate.calls[0][0] == task_id

    def test_resolve_gate_percent_encoded_task_id_reaches_the_same_route(self) -> None:
        """uvicorn decodes ``%2F`` back to ``/`` before Starlette routes, so
        encoding the id is not a way around a single-segment route."""
        app, auth, _, gate = _make_app()
        client = TestClient(app)
        task_id = node_task_id("run-9", "review", 0)

        resp = client.post(
            f"/api/workflow-tasks/{task_id.replace('/', '%2F')}/gate",
            headers=_operator(auth),
            json={"decision": "approve"},
        )

        assert resp.status_code == 200
        assert gate.calls[0][0] == task_id

    def test_resolve_gate_missing_decision_is_400(self) -> None:
        app, auth, _, gate = _make_app()
        client = TestClient(app)
        resp = client.post("/api/workflow-tasks/t-1/gate", headers=_operator(auth), json={})
        assert resp.status_code == 400
        assert gate.calls == []

    def test_resolve_gate_control_plane_unavailable(self) -> None:
        app, auth, _, _ = _make_app()
        app.state.gate_control_plane = None
        client = TestClient(app)
        resp = client.post(
            "/api/workflow-tasks/t-1/gate", headers=_operator(auth), json={"decision": "approve"}
        )
        assert resp.status_code == 503

    def test_resolve_gate_never_reachable_without_operator_role(self) -> None:
        """REQ-246: no agent-callable (or unauthenticated) path resolves a gate."""
        app, auth, _, gate = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/workflow-tasks/t-1/gate", headers=_viewer(auth), json={"decision": "approve"}
        )
        assert resp.status_code == 403
        assert gate.calls == []


# ---------------------------------------------------------------------------
# The rule-preserving test: the route module holds no operational logic.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_PREFIXES = ("arcteam", "arcstore", "arctrust")

# Substrings that would indicate validation, versioning, signing, or
# execution logic leaking into what must stay a thin delegating adapter.
_FORBIDDEN_SUBSTRINGS = (
    "def _validate",
    "def validate",
    "topological",
    "hashlib",
    "Ed25519",
    "canonicalize",
    "content_hash",
    "materialize",
    "dispatch_node",
    " SCC",
    "cycle_detect",
    "sign(",
    "GraphValidator",
    "PredicateEvaluator",
)


def test_route_module_imports_no_operational_packages() -> None:
    """COMP-023: the route layer never reaches directly into arcteam/arcstore/arctrust.

    Everything it needs from those layers arrives through the injected
    ``WorkflowControlPlane``/``GateControlPlane`` — importing their real
    implementations here would let operational logic creep back into arcui.
    """
    source = Path(workflows_module.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(_FORBIDDEN_IMPORT_PREFIXES), (
                    f"workflows.py imports {alias.name!r} directly — operational logic "
                    "must live in the control plane, never the route layer"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith(_FORBIDDEN_IMPORT_PREFIXES), (
                f"workflows.py imports from {module!r} — operational logic must live "
                "in the control plane, never the route layer"
            )


def test_route_module_has_no_operational_logic_markers() -> None:
    """No validation/versioning/signing/execution vocabulary in the route file.

    This is the test the task calls for explicitly: it is what keeps "arcui
    holds no operational work" true as this file evolves, rather than relying
    on review discipline alone.
    """
    source = Path(workflows_module.__file__).read_text()
    for needle in _FORBIDDEN_SUBSTRINGS:
        assert needle not in source, f"found operational-logic marker {needle!r} in workflows.py"


def test_every_route_handler_delegates_to_a_control_plane_call() -> None:
    """Every handler function calls ``plane.<something>`` — never computes the answer itself.

    Walks the AST of each route handler and asserts it contains an ``await
    plane.<method>(...)``/``await gate.<method>(...)`` call reaching
    ``_control_plane``/``_gate_plane`` — i.e. the handler's job is dispatch,
    not computation.
    """
    source = Path(workflows_module.__file__).read_text()
    tree = ast.parse(source)
    handler_names = {
        "list_workflows",
        "get_workflow",
        "create_workflow",
        "patch_workflow",
        "archive_workflow",
        "unarchive_workflow",
        "run_workflow",
        "list_runs",
        "get_run",
        "cancel_run",
        "resolve_gate",
    }
    seen = set()
    # Module-level functions only (`iter_child_nodes`, not `walk`) — the
    # Protocol classes above declare same-named abstract methods (``...``
    # bodies) that a full-tree walk would wrongly catch as "handlers with no
    # plane call".
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in handler_names:
            seen.add(node.name)
            calls_plane = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr
                in {
                    "list_workflows",
                    "get_workflow",
                    "create_workflow",
                    "patch_workflow",
                    "archive_workflow",
                    "unarchive_workflow",
                    "run_workflow",
                    "list_runs",
                    "get_run",
                    "cancel_run",
                    "resolve_gate",
                }
                for n in ast.walk(node)
            )
            assert calls_plane, f"{node.name} never calls a control-plane operation"
    assert seen == handler_names
