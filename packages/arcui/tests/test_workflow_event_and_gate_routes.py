"""SPEC-088 T-1365: the workflow event route and the typed gate route (COMP-011).

``POST /api/workflows/{id}/events/{name}`` (REQ-605, REQ-606, REQ-608, REQ-631):

* the body (the event payload) is capped at 64 KiB BEFORE any JSON parse, by
  ``Content-Length`` and again while streaming, so a lying or absent
  ``Content-Length`` cannot smuggle a large body past the cap (413);
* ``Idempotency-Key`` is required (400);
* scope ``workflows:event`` bound to the bundle's owner agent (403);
* a per-(principal, workflow) limiter, default 60 per minute (429). The tests
  use the default limiter on the real clock: 61 calls finish well inside one
  minute. The limiter is expected at ``app.state.workflow_event_limiter``
  (built by ``create_app``); no test replaces it, so its constructor stays the
  implementer's choice;
* the control plane's result relayed: 201 new run, 200 replay with the same
  ``run_id``, 404 unknown workflow/event, 409 same key with another payload,
  422 listing every failing field (``{"errors": [{"field": ...}, ...]}``).

``POST /api/workflow-tasks/{id}/gate`` (REQ-611, REQ-613, REQ-614, REQ-616):
body ``{decision, notes?, payload?}``; ``gates:resolve`` required; a payload over
64 KiB is 413; an invalid payload is 422 with field issues and the row keeps
waiting; the stored output envelope (D-844) names the on-behalf-of person as
``decided_by``; a human_input row takes ``submit`` through the same route (D-845).

``GET /api/workflows/{id}`` lists the declared events as
``events: [{"name", "schema"}]`` (REQ-610).

Real collaborators, exactly as ``test_workflow_attribution.py`` composes them:
``create_app`` + real ``AuthMiddleware`` + real ``UserStore`` / ``PrincipalStore``,
a real ``WorkflowRunner`` from ``build_workflow_runner`` over the in-memory
arcstore backend, the real arcteam ``WorkflowControlPlane`` and the real
dashboard adapter, real bundles on disk SIGNED with the pinned operator key
(``sign_definition``), so event runs pass the every-tier signature gate. Only
the audit sinks are recorders. No LLM: every workflow starts at a gate node.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.tasks import TaskStore
from arcteam.workflow import parse_definition, sign_definition, validate_definition
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
from starlette.requests import Request

from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import DashboardWorkflowPlane

SALES = agent_did()
OLIVIA = agent_did()
FIXED_OPERATOR_DID = "did:arc:ui:operator"
SIGNER_DID = "did:arc:test:operator/bundle-signer"
OBO = "X-Arc-On-Behalf-Of"
IDEM = "Idempotency-Key"
ORDERS = "orders"
INVOICES = "invoices"
EVENT = "order_placed"
CAP = 64 * 1024
ORDER = {"order_id": "A-1", "qty": 2}

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}, "qty": {"type": "integer"}},
    "required": ["order_id", "qty"],
}
APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"qty": {"type": "integer", "minimum": 1}, "memo": {"type": "string"}},
    "required": ["qty"],
}
PROMISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"promised": {"type": "string", "minLength": 1}},
    "required": ["promised"],
}


# --- external-boundary doubles -----------------------------------------------------


class RecordingSink:
    """Audit sink for the runner and the control plane: keeps every event."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Entity:
    def __init__(self, did: str) -> None:
        self.did = did


class Registry:
    """The team registry the runner resolves ``@owner`` handles through."""

    def __init__(self, agents: dict[str, str]) -> None:
        self._agents = agents

    async def get(self, handle: str) -> Any:
        did = self._agents.get(handle)
        return None if did is None else _Entity(did)


# --- bundles -----------------------------------------------------------------------


def _flow(workflow_id: str, owner: str, approvers: tuple[str, ...]) -> str:
    """event -> approve (gate with a payload schema) -> ask (human_input)."""
    listed = ", ".join(f'"{a}"' for a in approvers)
    return (
        f'[workflow]\nid = "{workflow_id}"\nversion = 1\ndescription = "{workflow_id}"\n'
        f'owner = "@{owner}"\n\n'
        f'[[event]]\nname = "{EVENT}"\nschema = "schemas/order.json"\n\n'
        f'[[node]]\nid = "approve"\nkind = "gate"\ngate = "human:approve"\n'
        f'approvers = [{listed}]\noutput_schema = "schemas/approval.json"\n\n'
        f'[[node]]\nid = "ask"\nkind = "human_input"\nprompt = "Confirm the promised date"\n'
        f'needs = ["approve"]\napprovers = [{listed}]\noutput_schema = "schemas/promise.json"\n'
    )


def _write_bundle(root: Path, workflow_id: str, text: str) -> None:
    bundle = root / "workflows" / workflow_id
    (bundle / "schemas").mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(text, encoding="utf-8")
    for name, schema in (
        ("order", ORDER_SCHEMA),
        ("approval", APPROVAL_SCHEMA),
        ("promise", PROMISE_SCHEMA),
    ):
        (bundle / "schemas" / f"{name}.json").write_text(json.dumps(schema), encoding="utf-8")


def _compose_plane(runner: Any, sink: RecordingSink) -> DashboardWorkflowPlane:
    """The dashboard plane over the real control plane, with a readable audit sink."""
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
    boss: str  # operator account
    boss_token: str
    listed: str  # viewer account listed as an approver
    viewer: str  # viewer account on no approver list


@dataclass
class Harness:
    built: PrincipalApp
    client: httpx.AsyncClient
    runner: Any
    plane: DashboardWorkflowPlane
    sink: RecordingSink
    rig: PrincipalRig
    people: People

    def app(
        self, scopes: dict[Scope, tuple[str, ...]], subjects: tuple[str, ...] = ("*",)
    ) -> IssuedPrincipal:
        return issue_principal(self.rig.open(), scopes=scopes, subjects=subjects)

    @property
    def boss_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.people.boss_token}"}

    async def fire(
        self,
        payload: Any = None,
        *,
        key: str | None = "k-1",
        workflow: str = ORDERS,
        event: str = EVENT,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sent = dict(headers if headers is not None else self.boss_headers)
        if key is not None:
            sent[IDEM] = key
        return await self.client.post(
            f"/api/workflows/{workflow}/events/{event}",
            json=ORDER if payload is None else payload,
            headers=sent,
        )

    async def run_ids(self, workflow: str = ORDERS) -> set[str]:
        return {r.id for r in await self.runner.runs.list_for_workflow(workflow)}

    async def start(self) -> str:
        """Start a manual run through the real plane as the on-box operator."""
        actor = OperatorActor(did="did:arc:test:operator/seed", session_id="seed")
        started = await self.plane.run_workflow(ORDERS, {}, actor=actor)
        assert started.value is not None, started.errors
        self.sink.events.clear()
        return str(started.value["run_id"])

    async def waiting(self, run_id: str, kind: str) -> Any:
        rows = await self.runner.tasks.query_by_flow_run(run_id)
        found = [r for r in rows if r.metadata.get("node_kind") == kind]
        assert len(found) == 1, rows
        return found[0]

    def worm(self, prefix: str) -> list[Any]:
        return [r for r in self.built.worm.written if str(r.operation).startswith(prefix)]


async def _harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    users = user_store_factory(tmp_path / "accounts")
    rig = principal_rig(tmp_path / "authority")
    team = write_team(tmp_path / "team", {"sales": SALES, "olivia": OLIVIA})
    built = build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team
    )
    boss_token, boss = built.sign_in("boss@example.com", (OPERATOR,))
    _, listed = built.sign_in("listed@example.com", (VIEWER,))
    _, viewer = built.sign_in("viewer@example.com", (VIEWER,))

    work = tmp_path / "work"
    _write_bundle(work, ORDERS, _flow(ORDERS, "sales", (listed,)))
    _write_bundle(work, INVOICES, _flow(INVOICES, "sales", (listed,)))
    key = OperatorKey.generate()
    key.save(work / "operator" / "operator.key")

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
    # build_workflow_runner wires a real DefinitionStore pinned to this key;
    # the runner types it as the narrower protocol, hence Any here.
    definitions: Any = runner.definitions
    for workflow_id in (ORDERS, INVOICES):
        signed = sign_definition(
            definitions, workflow_id, signer_did=SIGNER_DID, private_key=key.seed
        )
        assert signed.status == "signed"
    plane = _compose_plane(runner, sink)
    built.app.state.workflow_control_plane = plane
    built.app.state.gate_control_plane = plane
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
    harness = await _harness(tmp_path, monkeypatch)
    yield harness
    await harness.client.aclose()


class ParseSpy:
    """Records every JSON parse an oversized body could reach.

    ``Request.json`` is Starlette's parse entry; ``json.loads`` is what it and
    any hand-rolled parse call. Wrapped (not replaced): behavior is unchanged.
    """

    def __init__(self) -> None:
        self.request_json_paths: list[str] = []
        self.loads_sizes: list[int] = []


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[ParseSpy]:
    spy = ParseSpy()
    real_request_json = Request.json
    real_loads = json.loads

    async def recording_request_json(self: Request) -> Any:
        spy.request_json_paths.append(self.url.path)
        return await real_request_json(self)

    def recording_loads(s: Any, *args: Any, **kwargs: Any) -> Any:
        spy.loads_sizes.append(len(s))
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(Request, "json", recording_request_json)
    monkeypatch.setattr(json, "loads", recording_loads)
    yield spy


def _event_path(workflow: str = ORDERS, event: str = EVENT) -> str:
    return f"/api/workflows/{workflow}/events/{event}"


def _oversized_payload() -> dict[str, str]:
    return {"order_id": "A-1", "blob": "x" * (CAP + 1024)}


def _gate_url(task_id: str) -> str:
    return f"/api/workflow-tasks/{task_id}/gate"


def _fields(resp: httpx.Response) -> set[str]:
    return {str(e.get("field", "")) for e in resp.json()["errors"]}


# --- POST /api/workflows/{id}/events/{name}: the happy path and replay ---------------


class TestEventRelay:
    async def test_first_event_is_201_with_a_run_id_and_starts_one_run(self, h: Harness) -> None:
        resp = await h.fire()
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["run_id"]
        assert run_id in await h.run_ids()
        assert len(await h.run_ids()) == 1

    async def test_replay_with_same_key_and_payload_is_200_with_the_same_run_id(
        self, h: Harness
    ) -> None:
        first = await h.fire()
        second = await h.fire()
        assert (first.status_code, second.status_code) == (201, 200), (first.text, second.text)
        assert first.json()["run_id"] == second.json()["run_id"]
        assert len(await h.run_ids()) == 1

    async def test_same_key_with_another_payload_is_409_and_starts_nothing(
        self, h: Harness
    ) -> None:
        first = await h.fire()
        assert first.status_code == 201, first.text
        clash = await h.fire({"order_id": "B-2", "qty": 9})
        assert clash.status_code == 409, clash.text
        assert await h.run_ids() == {first.json()["run_id"]}

    async def test_invalid_payload_is_422_naming_every_failing_field(self, h: Harness) -> None:
        resp = await h.fire({"qty": "two"})
        assert resp.status_code == 422, resp.text
        assert {"order_id", "qty"} <= _fields(resp)
        assert await h.run_ids() == set()

    async def test_unknown_event_is_404(self, h: Harness) -> None:
        resp = await h.fire(event="order_cancelled")
        assert resp.status_code == 404, resp.text
        assert await h.run_ids() == set()

    async def test_unknown_workflow_is_404(self, h: Harness) -> None:
        resp = await h.fire(workflow="no_such_flow")
        assert resp.status_code == 404, resp.text

    async def test_missing_idempotency_key_is_400_and_starts_nothing(self, h: Harness) -> None:
        resp = await h.fire(key=None)
        assert resp.status_code == 400, resp.text
        assert await h.run_ids() == set()

    async def test_malformed_json_is_400_and_starts_nothing(self, h: Harness) -> None:
        resp = await h.client.post(
            _event_path(), content=b"{not json", headers={**h.boss_headers, IDEM: "k-1"}
        )
        assert resp.status_code == 400, resp.text
        assert await h.run_ids() == set()

    async def test_event_run_lands_at_the_first_gate(self, h: Harness) -> None:
        resp = await h.fire()
        assert resp.status_code == 201, resp.text
        gate = await h.waiting(resp.json()["run_id"], "gate")
        assert gate.status == "review"


# --- size cap: 413 before any JSON parse --------------------------------------------


class TestEventSizeCap:
    async def test_body_over_64_kib_is_413_without_json_parse(
        self, h: Harness, parse_spy: ParseSpy
    ) -> None:
        resp = await h.fire(_oversized_payload())
        assert resp.status_code == 413, resp.text
        assert _event_path() not in parse_spy.request_json_paths
        assert not [n for n in parse_spy.loads_sizes if n > CAP], parse_spy.loads_sizes
        assert await h.run_ids() == set()

    async def test_lying_content_length_with_streamed_oversized_body_is_413(
        self, h: Harness, parse_spy: ParseSpy
    ) -> None:
        body = json.dumps(_oversized_payload()).encode()

        async def stream() -> AsyncIterator[bytes]:
            for start in range(0, len(body), 8192):
                yield body[start : start + 8192]

        resp = await h.client.post(
            _event_path(),
            content=stream(),
            headers={
                **h.boss_headers,
                IDEM: "k-lie",
                "Content-Type": "application/json",
                "Content-Length": "32",
            },
        )
        assert resp.status_code == 413, resp.text
        assert not [n for n in parse_spy.loads_sizes if n > CAP], parse_spy.loads_sizes
        assert await h.run_ids() == set()

    async def test_chunked_oversized_body_without_content_length_is_413(
        self, h: Harness, parse_spy: ParseSpy
    ) -> None:
        body = json.dumps(_oversized_payload()).encode()

        async def stream() -> AsyncIterator[bytes]:
            for start in range(0, len(body), 8192):
                yield body[start : start + 8192]

        resp = await h.client.post(
            _event_path(),
            content=stream(),
            headers={**h.boss_headers, IDEM: "k-chunked", "Content-Type": "application/json"},
        )
        assert resp.status_code == 413, resp.text
        assert not [n for n in parse_spy.loads_sizes if n > CAP], parse_spy.loads_sizes
        assert await h.run_ids() == set()

    async def test_oversized_refusal_is_audited(self, h: Harness) -> None:
        resp = await h.fire(_oversized_payload())
        assert resp.status_code == 413, resp.text
        assert [r.outcome for r in h.worm("workflow.event")] == ["denied"]

    async def test_body_just_under_the_cap_is_parsed_and_validated(self, h: Harness) -> None:
        # The positive control: under the cap the route parses and the plane decides.
        pad = "x" * (CAP - 200)
        resp = await h.fire({"order_id": pad, "qty": 1})
        assert resp.status_code == 201, resp.text


# --- authorization and attribution ---------------------------------------------------


class TestEventAuthorization:
    async def test_app_with_workflows_event_on_owner_agent_fires(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_EVENT: (SALES,)})
        resp = await h.fire(headers=app.bearer_headers())
        assert resp.status_code == 201, resp.text

    async def test_app_without_workflows_event_scope_is_403(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_RUN: (SALES,), Scope.GATES_RESOLVE: (SALES,)})
        resp = await h.fire(headers=app.bearer_headers())
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "scope_denied"
        assert await h.run_ids() == set()
        assert [r.outcome for r in h.worm("workflow.event")] == ["denied"]

    async def test_app_scoped_to_another_agent_is_403(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_EVENT: (OLIVIA,)})
        resp = await h.fire(headers=app.bearer_headers())
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "scope_denied"
        assert await h.run_ids() == set()

    async def test_viewer_session_cannot_fire(self, h: Harness) -> None:
        token, _ = h.built.sign_in("watcher@example.com", (VIEWER,))
        resp = await h.fire(headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403, resp.text
        assert await h.run_ids() == set()

    async def test_app_for_person_audit_names_app_and_person(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_EVENT: (SALES,)}, subjects=(h.people.boss,))
        resp = await h.fire(headers=app.bearer_headers(**{OBO: h.people.boss}))
        assert resp.status_code == 201, resp.text
        applied = [r for r in h.worm("workflow.event") if r.outcome == "applied"]
        assert len(applied) == 1, h.built.worm.written
        assert (applied[0].actor_did, applied[0].via_app, applied[0].on_behalf_of) == (
            h.people.boss,
            app.did,
            h.people.boss,
        )

    async def test_two_apps_with_the_same_key_never_share_a_run(self, h: Harness) -> None:
        # REQ-606: the run id is derived from the PRINCIPAL DID, so one caller
        # can neither replay nor block another's event, even for one person.
        first = h.app({Scope.WORKFLOWS_EVENT: (SALES,)}, subjects=(h.people.boss,))
        second = h.app({Scope.WORKFLOWS_EVENT: (SALES,)}, subjects=(h.people.boss,))
        a = await h.fire(key="shared", headers=first.bearer_headers(**{OBO: h.people.boss}))
        b = await h.fire(key="shared", headers=second.bearer_headers(**{OBO: h.people.boss}))
        assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
        assert a.json()["run_id"] != b.json()["run_id"]


# --- rate limit: 60 per minute per (principal, workflow) -----------------------------


class TestEventRateLimit:
    async def test_61st_event_in_a_minute_from_one_principal_is_429(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_EVENT: (SALES,)})
        statuses = [
            (await h.fire(key=f"k-{i}", headers=app.bearer_headers())).status_code
            for i in range(60)
        ]
        assert statuses == [201] * 60, statuses
        over = await h.fire(key="k-60", headers=app.bearer_headers())
        assert over.status_code == 429, over.text
        assert len(await h.run_ids()) == 60
        assert [r.outcome for r in h.worm("workflow.event") if r.outcome == "denied"] == ["denied"]

    async def test_limit_is_per_workflow_and_per_principal(self, h: Harness) -> None:
        app = h.app({Scope.WORKFLOWS_EVENT: (SALES,)})
        for i in range(60):
            await h.fire(key=f"k-{i}", headers=app.bearer_headers())
        assert (await h.fire(key="k-60", headers=app.bearer_headers())).status_code == 429
        other_flow = await h.fire(key="k-x", workflow=INVOICES, headers=app.bearer_headers())
        other_caller = await h.fire(key="k-y")
        assert (other_flow.status_code, other_caller.status_code) == (201, 201), (
            other_flow.text,
            other_caller.text,
        )


# --- GET /api/workflows/{id} lists declared events -----------------------------------


class TestWorkflowDetailEvents:
    async def test_detail_lists_declared_events(self, h: Harness) -> None:
        resp = await h.client.get(f"/api/workflows/{ORDERS}", headers=h.boss_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["events"] == [{"name": EVENT, "schema": "schemas/order.json"}]


# --- POST /api/workflow-tasks/{id}/gate: typed payloads -------------------------------


class TestGateRoute:
    async def test_app_without_gates_resolve_is_403(self, h: Harness) -> None:
        gate = await h.waiting(await h.start(), "gate")
        app = h.app({Scope.WORKFLOWS_EVENT: (SALES,)}, subjects=(h.people.listed,))
        resp = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "payload": {"qty": 2}},
            headers=app.bearer_headers(**{OBO: h.people.listed}),
        )
        assert resp.status_code == 403, resp.text
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_approve_with_payload_records_person_as_decided_by(self, h: Harness) -> None:
        gate = await h.waiting(await h.start(), "gate")
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))
        resp = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "notes": "ok to ship", "payload": {"qty": 3}},
            headers=app.bearer_headers(**{OBO: h.people.listed}),
        )
        assert resp.status_code == 200, resp.text
        output = (await h.runner.tasks.get(gate.id)).output
        assert output["decision"] == "approved"
        assert output["payload"] == {"qty": 3}
        assert output["notes"] == "ok to ship"
        assert output["decided_by"] == h.people.listed
        assert output["on_behalf_of"] == h.people.listed

    async def test_invalid_payload_is_422_with_field_issues_and_row_keeps_waiting(
        self, h: Harness
    ) -> None:
        gate = await h.waiting(await h.start(), "gate")
        resp = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "payload": {"qty": 0}},
            headers=h.boss_headers,
        )
        assert resp.status_code == 422, resp.text
        assert "payload.qty" in _fields(resp)
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_payload_over_64_kib_is_413_and_row_keeps_waiting(self, h: Harness) -> None:
        gate = await h.waiting(await h.start(), "gate")
        resp = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "payload": {"qty": 1, "memo": "x" * (CAP + 1024)}},
            headers=h.boss_headers,
        )
        assert resp.status_code == 413, resp.text
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_payload_must_be_an_object(self, h: Harness) -> None:
        gate = await h.waiting(await h.start(), "gate")
        resp = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "payload": ["qty", 1]},
            headers=h.boss_headers,
        )
        assert resp.status_code == 422, resp.text
        assert "payload" in _fields(resp)
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_human_input_submit_through_the_gate_route(self, h: Harness) -> None:
        run_id = await h.start()
        gate = await h.waiting(run_id, "gate")
        approved = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "payload": {"qty": 1}},
            headers=h.boss_headers,
        )
        assert approved.status_code == 200, approved.text
        ask = await h.waiting(run_id, "human_input")
        assert ask.status == "review"
        app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))

        bad = await h.client.post(
            _gate_url(ask.id),
            json={"decision": "submit", "payload": {"promised": ""}},
            headers=app.bearer_headers(**{OBO: h.people.listed}),
        )
        assert bad.status_code == 422, bad.text
        assert (await h.runner.tasks.get(ask.id)).status == "review"

        good = await h.client.post(
            _gate_url(ask.id),
            json={"decision": "submit", "payload": {"promised": "2026-11-01"}},
            headers=app.bearer_headers(**{OBO: h.people.listed}),
        )
        assert good.status_code == 200, good.text
        output = (await h.runner.tasks.get(ask.id)).output
        assert output["decision"] == "submitted"
        assert output["payload"] == {"promised": "2026-11-01"}
        assert output["decided_by"] == h.people.listed

    @pytest.mark.parametrize("caller", ["session", "static_token", "app_for_person"])
    async def test_no_response_or_audit_names_the_fixed_operator_did(
        self, h: Harness, caller: str
    ) -> None:
        gate = await h.waiting(await h.start(), "gate")
        headers = {
            "session": h.boss_headers,
            "static_token": {"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        }.get(caller)
        if headers is None:
            app = h.app({Scope.GATES_RESOLVE: (SALES,)}, subjects=(h.people.listed,))
            headers = app.bearer_headers(**{OBO: h.people.listed})
        resp = await h.client.post(
            _gate_url(gate.id),
            json={"decision": "approve", "payload": {"qty": 1}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert FIXED_OPERATOR_DID not in resp.text
        assert FIXED_OPERATOR_DID not in repr(h.built.worm.written)
        assert FIXED_OPERATOR_DID not in {e.actor_did for e in h.sink.events}
        output = (await h.runner.tasks.get(gate.id)).output
        assert output["decided_by"] != FIXED_OPERATOR_DID
        assert FIXED_OPERATOR_DID not in json.dumps(output)

    async def test_event_fire_responses_and_audit_never_name_the_fixed_operator_did(
        self, h: Harness
    ) -> None:
        resp = await h.fire()
        assert resp.status_code == 201, resp.text
        assert FIXED_OPERATOR_DID not in resp.text
        assert FIXED_OPERATOR_DID not in repr(h.built.worm.written)
        assert FIXED_OPERATOR_DID not in {e.actor_did for e in h.sink.events}
