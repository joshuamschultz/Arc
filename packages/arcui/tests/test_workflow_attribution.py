"""SPEC-087 T-1283: workflow gate and run attribution (REQ-567, REQ-562, REQ-571).

A gate decision, a workflow run start and a workflow-run read are recorded under
whoever is acting, never the fixed ``did:arc:ui:operator``:

* a signed-in operator session decides as its account DID;
* an app acting for a person decides as that person, with the person's own
  roles (an app adds no authority), so the workflow control plane's approver
  list is what lets a viewer decide, and only when the viewer is listed;
* the federal static operator token may not record a decision at all.

The app-reachable rows of ``APP_ROUTES`` that carry no agent in the path
(``POST /api/workflows/{id}/run``, ``GET /api/workflow-runs/{id}``,
``GET /api/runs/{run_id}/timeline``, ``POST /api/workflow-tasks/{id}/gate``,
``POST /api/tasks/{id}/approve``) must bind the scope to the agent the handler
resolves (workflow owner, gate owner, run's own records). An app bound to one
agent never reaches another agent's workflow, run or trace; an unresolvable
target is 404 to an app.

Real collaborators: ``create_app`` with the real ``AuthMiddleware``, real
``UserStore`` and ``PrincipalStore`` (``principal_fixtures``), a real
``WorkflowRunner`` from ``build_workflow_runner`` over an in-memory arcstore
backend, the real arcteam ``WorkflowControlPlane`` and the real dashboard
adapter, real bundles on disk, the real Observe mirror reading a real spool.
Only the audit sinks are recorders. No LLM is involved: every workflow here
starts at a gate node.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record
from arcstore.tasks import TaskStore
from arcteam.workflow import parse_definition, validate_definition
from arcteam.workflow.control_plane import WorkflowControlPlane
from arcteam.workflow.runner import build_workflow_runner
from arctrust import OperatorKey
from arctrust.audit import AuditEvent
from arctrust.principals import Scope
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    IssuedPrincipal,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory

from arcui.routes import workflows as workflow_routes
from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import DashboardWorkflowPlane, build_dashboard_plane

SALES = agent_did()
OLIVIA = agent_did()
FIXED_OPERATOR_DID = "did:arc:ui:operator"
OBO = "X-Arc-On-Behalf-Of"
SALES_FLOW = "sales_flow"
OLIVIA_FLOW = "olivia_flow"
SALES_TRACE = "req-sales-trace"
OLIVIA_TRACE = "req-olivia-trace"


# --- external-boundary doubles -----------------------------------------------------


class RecordingSink:
    """Audit sink for the runner and the control plane: keeps every event."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def decisions(self, prefix: str, task_id: str) -> list[AuditEvent]:
        """The control plane's own events for one gate task.

        The control plane targets the gate's task id; the runner's later
        ``workflow.gate.resolved`` (actor: the runner) targets
        ``<workflow>/<node>`` and is not a decision record.
        """
        return [e for e in self.events if e.action.startswith(prefix) and e.target == task_id]


class _Entity:
    def __init__(self, did: str) -> None:
        self.did = did


class Registry:
    """The team registry the runner resolves ``@owner`` handles through.

    Mirrors the fleet roster written by ``write_team`` so the runner and the
    route layer agree on which DID owns which workflow.
    """

    def __init__(self, agents: dict[str, str]) -> None:
        self._agents = agents

    async def get(self, handle: str) -> Any:
        did = self._agents.get(handle)
        return None if did is None else _Entity(did)


# --- bundles -----------------------------------------------------------------------


def _gate_first(workflow_id: str, owner: str, approvers: tuple[str, ...]) -> str:
    listed = ", ".join(f'"{a}"' for a in approvers)
    return (
        f'[workflow]\nid = "{workflow_id}"\nversion = 1\ndescription = "{workflow_id}"\n'
        f'owner = "@{owner}"\n\n'
        f'[[node]]\nid = "review"\nkind = "gate"\ngate = "human:approve"\n'
        f"approvers = [{listed}]\n"
    )


def _write_bundle(root: Path, workflow_id: str, text: str) -> None:
    bundle = root / "workflows" / workflow_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(text, encoding="utf-8")


def _compose_plane(runner: Any, sink: RecordingSink) -> DashboardWorkflowPlane:
    """The dashboard plane over the real control plane, with an audit sink we can read.

    Same composition as ``build_dashboard_plane`` (one runner, its own stores and
    tier) plus the sink, so the control plane's ``workflow.gate.*`` and
    ``workflow.run.*`` events are observable here.
    """
    root = runner.definitions.root

    def _validate(definition: Any, *, pending_files: frozenset[str] = frozenset()) -> Any:
        return validate_definition(
            definition, bundle_root=root / definition.id, pending_files=pending_files
        )

    plane = WorkflowControlPlane(
        definitions=runner.definitions,
        parse=lambda document: parse_definition(dict(document)),
        validate=_validate,
        runner=runner,
        runs=runner.runs,
        tier=runner.tier,
        audit_sink=sink,
    )
    return DashboardWorkflowPlane(
        plane=plane, definitions=runner.definitions, runs=runner.runs, tasks=runner.tasks
    )


# --- harness -----------------------------------------------------------------------


@dataclass
class People:
    boss: str  # operator account, NOT on any approver list
    boss_token: str
    listed: str  # viewer account, listed as an approver on sales_flow
    viewer: str  # viewer account, on no approver list


@dataclass
class Harness:
    built: PrincipalApp
    client: httpx.AsyncClient
    runner: Any
    plane: DashboardWorkflowPlane
    sink: RecordingSink
    rig: PrincipalRig
    people: People

    async def start(self, workflow_id: str) -> str:
        """Start a run as the on-box operator through the real plane; return its id."""
        actor = OperatorActor(did="did:arc:test:operator/seed", session_id="seed")
        started = await self.plane.run_workflow(workflow_id, {}, actor=actor)
        assert started.value is not None, started.errors
        run_id = str(started.value["run_id"])
        self.sink.events.clear()
        return run_id

    async def gate_task(self, run_id: str) -> Any:
        rows = await self.runner.tasks.query_by_flow_run(run_id)
        gates = [r for r in rows if r.metadata.get("node_kind") == "gate"]
        assert len(gates) == 1, rows
        return gates[0]

    def app(
        self,
        scopes: dict[Scope, tuple[str, ...]],
        subjects: tuple[str, ...] = ("*",),
    ) -> IssuedPrincipal:
        return issue_principal(self.rig.open(), scopes=scopes, subjects=subjects)

    def worm_rows(self, operation: str) -> list[Any]:
        return [row for row in self.built.worm.written if row.operation == operation]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: str) -> Harness:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    users = user_store_factory(tmp_path / "accounts")
    rig = principal_rig(tmp_path / "authority")
    team = write_team(tmp_path / "team", {"sales": SALES, "olivia": OLIVIA})
    built = build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team, tier=tier
    )

    boss_token, boss = built.sign_in("boss@example.com", (OPERATOR,))
    _, listed = built.sign_in("listed@example.com", (VIEWER,))
    _, viewer = built.sign_in("viewer@example.com", (VIEWER,))

    # The approver lists are pinned at run start from the signed definition, so
    # the people exist before the bundles are written.
    work = tmp_path / "work"
    _write_bundle(work, SALES_FLOW, _gate_first(SALES_FLOW, "sales", (listed,)))
    _write_bundle(work, OLIVIA_FLOW, _gate_first(OLIVIA_FLOW, "olivia", (listed,)))
    OperatorKey.generate().save(work / "operator" / "operator.key")

    backend = built.app.state.arcstore_backend
    await backend.start()
    sink = RecordingSink()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=work / "operator" / "operator.key",
        workspace_root=work,
        registry=Registry({"sales": SALES, "olivia": OLIVIA}),
        audit_sink=sink,
    )
    plane = _compose_plane(runner, sink)
    built.app.state.workflow_control_plane = plane
    built.app.state.gate_control_plane = plane
    # The generic task routes read the same rows the runner writes.
    built.app.state.task_store = TaskStore(backend)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built.app), base_url="http://testserver"
    )
    return Harness(
        built=built,
        client=client,
        runner=runner,
        plane=plane,
        sink=sink,
        rig=rig,
        people=People(boss=boss, boss_token=boss_token, listed=listed, viewer=viewer),
    )


@pytest.fixture
async def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    harness = await _harness(tmp_path, monkeypatch, "personal")
    yield harness
    await harness.client.aclose()


@pytest.fixture
async def fed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    harness = await _harness(tmp_path, monkeypatch, "federal")
    yield harness
    await harness.client.aclose()


def _gate_url(task_id: str) -> str:
    return f"/api/workflow-tasks/{task_id}/gate"


APPROVE = {"decision": "approve"}


# --- the actor type and the deleted constant ------------------------------------------


class TestOperatorActorShape:
    def test_operator_actor_carries_on_behalf_of_and_via_app(self) -> None:
        names = {f.name for f in fields(OperatorActor)}
        assert {"did", "session_id", "on_behalf_of", "via_app"} <= names

    def test_fixed_operator_did_constant_is_deleted_from_workflow_routes(self) -> None:
        assert not hasattr(workflow_routes, "_OPERATOR_DID")
        assert FIXED_OPERATOR_DID not in inspect.getsource(workflow_routes)


# --- gate decisions by a signed-in operator session -----------------------------------


class TestSessionGateAttribution:
    async def test_session_gate_decision_records_account_did_on_gate_row(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)

        resp = await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=_bearer(h.people.boss_token)
        )

        assert resp.status_code == 200, resp.text
        settled = await h.runner.tasks.get(gate.id)
        assert settled.output["decided_by"] == h.people.boss
        assert settled.output["decided_by"] != FIXED_OPERATOR_DID

    async def test_session_gate_decision_names_account_in_control_plane_audit(
        self, h: Harness
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)

        await h.client.post(_gate_url(gate.id), json=APPROVE, headers=_bearer(h.people.boss_token))

        resolved = h.sink.decisions("workflow.gate.resolved", gate.id)
        assert [e.actor_did for e in resolved] == [h.people.boss]

    async def test_session_gate_decision_via_task_approve_records_account_did(
        self, h: Harness
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)

        resp = await h.client.post(
            f"/api/tasks/{gate.id}/approve", headers=_bearer(h.people.boss_token)
        )

        assert resp.status_code == 200, resp.text
        settled = await h.runner.tasks.get(gate.id)
        assert settled.output["decided_by"] == h.people.boss


# --- gate decisions by an app ----------------------------------------------------------


class TestAppGateDecision:
    async def test_app_for_unlisted_viewer_is_denied_by_control_plane_even_with_scope(
        self, h: Harness
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.viewer,))

        resp = await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.viewer})
        )

        assert resp.status_code == 403, resp.text
        # Refused by the approver list, as the person, not by a role check before it.
        denied = h.sink.decisions("workflow.gate.denied", gate.id)
        assert [e.actor_did for e in denied] == [h.people.viewer]
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_for_listed_viewer_resolves_gate_as_that_person(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))

        resp = await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.listed})
        )

        assert resp.status_code == 200, resp.text
        settled = await h.runner.tasks.get(gate.id)
        assert settled.output["decided_by"] == h.people.listed

    async def test_app_gate_audit_names_app_and_person(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))

        await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.listed})
        )

        resolved = h.sink.decisions("workflow.gate.resolved", gate.id)
        assert len(resolved) == 1, h.sink.events
        event = resolved[0]
        assert event.actor_did == h.people.listed
        assert event.causal is not None
        assert event.causal.initiator == "app"
        assert event.causal.initiator_id == app.did
        assert event.causal.on_behalf_of == h.people.listed
        rows = [r for r in h.worm_rows("gate.resolve") if r.outcome == "applied"]
        assert len(rows) == 1
        assert (rows[0].actor_did, rows[0].via_app, rows[0].on_behalf_of) == (
            h.people.listed,
            app.did,
            h.people.listed,
        )

    async def test_app_for_unlisted_operator_person_resolves_with_persons_role(
        self, h: Harness
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.boss,))

        resp = await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.boss})
        )

        assert resp.status_code == 200, resp.text
        assert (await h.runner.tasks.get(gate.id)).output["decided_by"] == h.people.boss

    async def test_app_alone_is_no_approver_and_is_denied(self, h: Harness) -> None:
        """An app acting for nobody has no roles and is on no list: it adds no authority."""
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)})

        resp = await h.client.post(_gate_url(gate.id), json=APPROVE, headers=app.bearer_headers())

        assert resp.status_code == 403, resp.text
        assert [e.actor_did for e in h.sink.decisions("workflow.gate.denied", gate.id)] == [
            app.did
        ]
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_without_gates_resolve_is_scope_denied(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.TASKS_WRITE: (SALES,)}, subjects=(h.people.listed,))

        resp = await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.listed})
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert h.sink.decisions("workflow.gate", gate.id) == []
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_bound_to_another_agent_is_scope_denied(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (OLIVIA,)}, subjects=(h.people.listed,))

        resp = await h.client.post(
            _gate_url(gate.id), json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.listed})
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert h.sink.decisions("workflow.gate", gate.id) == []
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_on_unknown_gate_is_404(self, h: Harness) -> None:
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))

        resp = await h.client.post(
            _gate_url("wf/run-nope/review/0"),
            json=APPROVE,
            headers=app.bearer_headers(**{OBO: h.people.listed}),
        )

        assert resp.status_code == 404, resp.text


class TestAppGateViaTaskApprove:
    """``POST /api/tasks/{id}/approve`` on a gate task follows the same rules."""

    async def test_app_for_listed_viewer_approves_gate_task_as_that_person(
        self, h: Harness
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))

        resp = await h.client.post(
            f"/api/tasks/{gate.id}/approve", headers=app.bearer_headers(**{OBO: h.people.listed})
        )

        assert resp.status_code == 200, resp.text
        assert (await h.runner.tasks.get(gate.id)).output["decided_by"] == h.people.listed

    async def test_app_for_unlisted_viewer_is_denied_by_control_plane(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.viewer,))

        resp = await h.client.post(
            f"/api/tasks/{gate.id}/approve", headers=app.bearer_headers(**{OBO: h.people.viewer})
        )

        assert resp.status_code == 403, resp.text
        assert [e.actor_did for e in h.sink.decisions("workflow.gate.denied", gate.id)] == [
            h.people.viewer
        ]
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_with_only_tasks_write_cannot_approve_a_gate_task(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.TASKS_WRITE: (SALES,)}, subjects=(h.people.boss,))

        resp = await h.client.post(
            f"/api/tasks/{gate.id}/approve", headers=app.bearer_headers(**{OBO: h.people.boss})
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_bound_to_another_agent_cannot_approve_a_gate_task(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = h.app({Scope.GATES_RESOLVE: (OLIVIA,)}, subjects=(h.people.listed,))

        resp = await h.client.post(
            f"/api/tasks/{gate.id}/approve", headers=app.bearer_headers(**{OBO: h.people.listed})
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert (await h.runner.tasks.get(gate.id)).status == "review"


# --- federal: the static operator token may not decide ---------------------------------


class TestFederalStaticToken:
    async def test_federal_static_token_gate_decision_refused(self, fed: Harness) -> None:
        run_id = await fed.start(SALES_FLOW)
        gate = await fed.gate_task(run_id)

        resp = await fed.client.post(
            _gate_url(gate.id), json=APPROVE, headers=_bearer(OPERATOR_TOKEN)
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "operator_account_required"
        assert (await fed.runner.tasks.get(gate.id)).status == "review"
        assert fed.sink.decisions("workflow.gate", gate.id) == []

    async def test_federal_static_token_gate_task_approve_refused(self, fed: Harness) -> None:
        run_id = await fed.start(SALES_FLOW)
        gate = await fed.gate_task(run_id)

        resp = await fed.client.post(
            f"/api/tasks/{gate.id}/approve", headers=_bearer(OPERATOR_TOKEN)
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "operator_account_required"
        assert (await fed.runner.tasks.get(gate.id)).status == "review"

    async def test_federal_named_operator_session_still_decides(self, fed: Harness) -> None:
        run_id = await fed.start(SALES_FLOW)
        gate = await fed.gate_task(run_id)

        resp = await fed.client.post(
            _gate_url(gate.id), json=APPROVE, headers=_bearer(fed.people.boss_token)
        )

        assert resp.status_code == 200, resp.text
        assert (await fed.runner.tasks.get(gate.id)).output["decided_by"] == fed.people.boss


# --- running a workflow ----------------------------------------------------------------


class TestAppRunWorkflow:
    async def test_app_runs_its_own_agents_workflow_as_the_person(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_RUN: (SALES,)}, subjects=(h.people.boss,))

        resp = await h.client.post(
            f"/api/workflows/{SALES_FLOW}/run",
            json={},
            headers=app.bearer_headers(**{OBO: h.people.boss}),
        )

        assert resp.status_code == 201, resp.text
        run = await h.runner.runs.record(resp.json()["run_id"])
        assert run.initiator_did == h.people.boss

    async def test_session_run_records_account_did_as_initiator(self, h: Harness) -> None:
        resp = await h.client.post(
            f"/api/workflows/{SALES_FLOW}/run", json={}, headers=_bearer(h.people.boss_token)
        )

        assert resp.status_code == 201, resp.text
        run = await h.runner.runs.record(resp.json()["run_id"])
        assert run.initiator_did == h.people.boss

    async def test_app_cannot_run_another_agents_workflow(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_RUN: (SALES,)})

        resp = await h.client.post(
            f"/api/workflows/{OLIVIA_FLOW}/run", json={}, headers=app.bearer_headers()
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert await h.runner.runs.list_for_workflow(OLIVIA_FLOW) == []

    async def test_app_with_only_runs_read_cannot_run_a_workflow(self, h: Harness) -> None:
        app = h.app({Scope.RUNS_READ: (SALES,)})

        resp = await h.client.post(
            f"/api/workflows/{SALES_FLOW}/run", json={}, headers=app.bearer_headers()
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert await h.runner.runs.list_for_workflow(SALES_FLOW) == []

    async def test_app_on_unknown_workflow_is_404(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_RUN: (SALES,)})

        resp = await h.client.post(
            "/api/workflows/no_such_flow/run", json={}, headers=app.bearer_headers()
        )

        assert resp.status_code == 404, resp.text


# --- reading a workflow run --------------------------------------------------------------


_NOT_SERVED = {"scope_denied", "not found"}


class TestAppReadWorkflowRun:
    async def test_app_reads_its_own_agents_run(self, h: Harness) -> None:
        run_id = await h.start(SALES_FLOW)
        app = h.app({Scope.RUNS_READ: (SALES,)})

        resp = await h.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer_headers())

        assert resp.status_code == 200, resp.text
        assert resp.json()["run_id"] == run_id

    async def test_app_cannot_read_another_agents_run(self, h: Harness) -> None:
        run_id = await h.start(OLIVIA_FLOW)
        app = h.app({Scope.RUNS_READ: (SALES,)})

        resp = await h.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer_headers())

        assert resp.status_code in (403, 404)
        assert resp.json()["error"] in _NOT_SERVED
        assert run_id not in resp.text

    async def test_app_reading_unknown_run_is_404(self, h: Harness) -> None:
        app = h.app({Scope.RUNS_READ: (SALES,)})

        resp = await h.client.get("/api/workflow-runs/run-unknown", headers=app.bearer_headers())

        assert resp.status_code == 404

    async def test_operator_session_still_reads_any_run(self, h: Harness) -> None:
        run_id = await h.start(OLIVIA_FLOW)

        resp = await h.client.get(
            f"/api/workflow-runs/{run_id}", headers=_bearer(h.people.boss_token)
        )

        assert resp.status_code == 200, resp.text


# --- reading a run's timeline ------------------------------------------------------------


@pytest.fixture
async def traces(h: Harness, tmp_path: Path) -> AsyncIterator[Harness]:
    """One trace per agent in the real spool, mirrored by the app's real Observe plane."""
    # ``build_principal_app`` hands ``create_app`` this data dir; Observe tails its spool.
    spool = tmp_path / "arcui-data" / "spool" / "operational-2026-10-10.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    for request_id, did in ((SALES_TRACE, SALES), (OLIVIA_TRACE, OLIVIA)):
        spool_record(
            SpoolRecord(
                kind="run_event",
                actor_did=did,
                request_id=request_id,
                ts="2026-10-10T00:00:01+00:00",
                name="turn.start",
            ),
            path=spool,
        )
    observe = h.built.app.state.observe
    await observe.start()
    yield h
    await observe.stop()


class TestAppReadTimeline:
    async def test_app_reads_its_own_agents_timeline(self, traces: Harness) -> None:
        app = traces.app({Scope.RUNS_READ: (SALES,)})

        resp = await traces.client.get(
            f"/api/runs/{SALES_TRACE}/timeline", headers=app.bearer_headers()
        )

        assert resp.status_code == 200, resp.text
        assert [e["actor_did"] for e in resp.json()["timeline"]] == [SALES]

    async def test_app_cannot_read_another_agents_timeline(self, traces: Harness) -> None:
        app = traces.app({Scope.RUNS_READ: (SALES,)})

        resp = await traces.client.get(
            f"/api/runs/{OLIVIA_TRACE}/timeline", headers=app.bearer_headers()
        )

        assert resp.status_code in (403, 404), resp.text
        assert OLIVIA not in resp.text

    async def test_app_reading_unknown_runs_timeline_is_404(self, traces: Harness) -> None:
        app = traces.app({Scope.RUNS_READ: (SALES,)})

        resp = await traces.client.get(
            "/api/runs/req-unknown/timeline", headers=app.bearer_headers()
        )

        assert resp.status_code == 404, resp.text

    async def test_operator_session_still_reads_any_timeline(self, traces: Harness) -> None:
        resp = await traces.client.get(
            f"/api/runs/{OLIVIA_TRACE}/timeline", headers=_bearer(traces.people.boss_token)
        )

        assert resp.status_code == 200, resp.text
        assert [e["actor_did"] for e in resp.json()["timeline"]] == [OLIVIA]


# --- production composition: the gate audit is not dropped -------------------------------


async def test_production_dashboard_plane_records_gate_audit_in_runner_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_dashboard_plane`` must not give the control plane a NullSink.

    Built the way the server builds it (``_attach_workflow_plane``): a gate
    denial and a resolution through the dashboard must reach the runner's audit
    sink, or the ``workflow.gate.*`` record that names app and person is lost.
    """
    harness = await _harness(tmp_path, monkeypatch, "personal")
    try:
        plane = build_dashboard_plane(runner=harness.runner)
        harness.built.app.state.workflow_control_plane = plane
        harness.built.app.state.gate_control_plane = plane
        run_id = await harness.start(SALES_FLOW)
        gate = await harness.gate_task(run_id)
        app = harness.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(harness.people.listed,))

        resp = await harness.client.post(
            _gate_url(gate.id),
            json=APPROVE,
            headers=app.bearer_headers(**{OBO: harness.people.listed}),
        )

        assert resp.status_code == 200, resp.text
        resolved = harness.sink.decisions("workflow.gate.resolved", gate.id)
        assert [e.actor_did for e in resolved] == [harness.people.listed]
    finally:
        await harness.client.aclose()


def test_no_fixed_operator_attribution_left_in_workflow_plane() -> None:
    import arcui.workflow_plane as module

    source = inspect.getsource(module)
    assert FIXED_OPERATOR_DID not in source
    # The roles passed to the control plane come from the acting identity.
    assert not re.search(r"actor_roles\s*=\s*frozenset\(\{OPERATOR_ROLE\}\)", source)
