"""SPEC-087 T-1289: journey — an app runs a workflow and resolves its gate for a person.

REQ-558 (an app acts through public routes only, scope-bound to one agent),
REQ-562 (an app adds no authority: the person must be able to decide), REQ-565
(audit names app AND person), REQ-567 (decisions recorded under the person, never
a fixed operator DID).

What a user does, end to end, only through public HTTP routes:

1. an operator signs in (``POST /api/auth/login``) and issues an app principal
   (``POST /api/principals``) bound to the workflow owner agent, for two people;
   the bearer secret comes back once;
2. the app starts the workflow (``POST /api/workflows/{id}/run``); the run waits
   at its first node, a human gate;
3. the app reads the run (``GET /api/workflow-runs/{id}``) and finds the gate;
4. the app tries the gate for a person who is allowlisted but NOT an approver:
   403, audited, and the run keeps waiting;
5. the app resolves the gate for the listed approver
   (``POST /api/workflow-tasks/{id}/gate`` + ``X-Arc-On-Behalf-Of: <email>``);
   the runner continues through a deterministic rules router to completion;
6. the operator revokes the app (``POST /api/principals/{id}/revoke``); the app's
   next call is 401.

Federal variant: the app must sign every request; deciding for a person needs a
delegation the person made in their own session (``POST /api/me/delegations``).

Real collaborators: ``create_app`` with the real ``AuthMiddleware``, the real
``UserStore`` and anchored ``PrincipalStore`` (``principal_fixtures``), the real
login, principal, approval, delegation and workflow routes, a real
``WorkflowRunner`` from ``build_workflow_runner`` over the app's own arcstore
backend, the production ``build_dashboard_plane`` over it, and the production
``MutationWormWriter``. Doubles sit only at the edges: the audit sinks record
events instead of writing files, and the team registry resolves ``@sales``.
No LLM is involved: the workflow is a gate followed by a rules router, both of
which the runner settles itself.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcteam.workflow.runner import build_workflow_runner
from arctrust import OperatorKey, default_operator_key_path
from arctrust.audit import AuditEvent
from arctrust.keypair import KeyPair, generate_keypair
from arctrust.principals import Scope
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import (
    PASSWORD,
    IssuedPrincipal,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    build_principal_app,
    principal_rig,
    signed_headers,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory

from arcui.audit import MutationWormWriter
from arcui.workflow_plane import build_dashboard_plane

SALES = agent_did()
FLOW = "lead_review"
OBO = "X-Arc-On-Behalf-Of"
GATE_NODE = "review"
ROUTER_NODE = "triage"
ESCALATE_NODE = "escalate"
FIXED_OPERATOR_DID = "did:arc:ui:operator"

BOSS_EMAIL = "boss@example.com"  # operator who issues and revokes
SECOND_EMAIL = "second@example.com"  # second operator (federal two-person rule)
APPROVER_EMAIL = "approver@example.com"  # viewer, listed as the gate approver
BYSTANDER_EMAIL = "bystander@example.com"  # viewer, allowlisted, NOT an approver


# --- external-boundary doubles ------------------------------------------------------


@dataclass
class RecordingSink:
    """Stands in for an audit file: keeps every event the producer emitted."""

    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Entity:
    def __init__(self, did: str) -> None:
        self.did = did


class Registry:
    """The team registry the runner resolves ``@owner`` through (mirrors the roster)."""

    async def get(self, handle: str) -> Any:
        return _Entity(SALES) if handle == "sales" else None


# --- the workflow -------------------------------------------------------------------


def _bundle(approver_did: str) -> str:
    """A human gate, then a deterministic rules router.

    After approval the router routes to ``escalate``, a second gate that applies
    only to urgent leads; the journey's lead is not urgent, so it is skipped and
    the run completes without anyone else deciding.
    """
    return (
        f'[workflow]\nid = "{FLOW}"\nversion = 1\ndescription = "Review a lead"\n'
        'owner = "@sales"\n\n'
        f'[[node]]\nid = "{GATE_NODE}"\nkind = "gate"\ngate = "human:approve"\n'
        f'approvers = ["{approver_did}"]\n\n'
        f'[[node]]\nid = "{ROUTER_NODE}"\nkind = "router"\nmode = "rules"\n'
        f'needs = ["{GATE_NODE}"]\n'
        f'routes = [{{ to = "{ESCALATE_NODE}", default = true }}]\n\n'
        f'[[node]]\nid = "{ESCALATE_NODE}"\nkind = "gate"\ngate = "human:escalate"\n'
        f'needs = ["{ROUTER_NODE}"]\nwhen = "$input.urgent == true"\n'
        f'approvers = ["{approver_did}"]\n'
    )


# --- harness ------------------------------------------------------------------------


@dataclass
class Journey:
    built: PrincipalApp
    client: httpx.AsyncClient
    runner: Any
    rig: PrincipalRig
    workflow_audit: RecordingSink
    mutation_audit: RecordingSink
    people: dict[str, str]  # email -> account DID

    async def login(self, email: str) -> dict[str, str]:
        """Sign in through the real login route; the session's bearer headers."""
        resp = await self.client.post(
            "/api/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["did"] == self.people[email]
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    def did(self, email: str) -> str:
        return self.people[email]

    def mutation_rows(self, operation: str) -> list[AuditEvent]:
        return [e for e in self.mutation_audit.events if e.action == operation]

    def gate_events(self, prefix: str, task_id: str) -> list[AuditEvent]:
        """The control plane's own records for one gate task (targets the task id)."""
        return [
            e
            for e in self.workflow_audit.events
            if e.action.startswith(prefix) and e.target == task_id
        ]


async def _journey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: str) -> Journey:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    # The approvals approve route (federal issuance) signs with the on-box operator key.
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)

    users = user_store_factory(tmp_path / "accounts")
    rig = principal_rig(tmp_path / "authority")
    team = write_team(tmp_path / "team", {"sales": SALES})
    built = build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team, tier=tier
    )
    # The production mutation writer, recording instead of appending to the chain file.
    mutation_audit = RecordingSink()
    built.app.state.audit_worm = MutationWormWriter(
        sink=mutation_audit,  # type: ignore[arg-type]  # reason: WORM file is the faked edge
        operator_did="did:arc:test:operator/worm",
    )

    # Accounts exist before the journey (an operator created them earlier).
    store = users()
    people = {
        BOSS_EMAIL: store.add(BOSS_EMAIL, PASSWORD, roles=(OPERATOR,)).did,
        SECOND_EMAIL: store.add(SECOND_EMAIL, PASSWORD, roles=(OPERATOR,)).did,
        APPROVER_EMAIL: store.add(APPROVER_EMAIL, PASSWORD, roles=(VIEWER,)).did,
        BYSTANDER_EMAIL: store.add(BYSTANDER_EMAIL, PASSWORD, roles=(VIEWER,)).did,
    }

    work = tmp_path / "work"
    bundle = work / "workflows" / FLOW
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_bundle(people[APPROVER_EMAIL]), encoding="utf-8")
    OperatorKey.generate().save(work / "operator" / "operator.key")

    backend = built.app.state.arcstore_backend
    await backend.start()
    workflow_audit = RecordingSink()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=work / "operator" / "operator.key",
        workspace_root=work,
        registry=Registry(),
        audit_sink=workflow_audit,
    )
    plane = build_dashboard_plane(runner=runner)
    built.app.state.workflow_control_plane = plane
    built.app.state.gate_control_plane = plane

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built.app), base_url="http://testserver"
    )
    return Journey(
        built=built,
        client=client,
        runner=runner,
        rig=rig,
        workflow_audit=workflow_audit,
        mutation_audit=mutation_audit,
        people=people,
    )


@pytest.fixture
async def personal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Journey]:
    journey = await _journey(tmp_path, monkeypatch, "personal")
    yield journey
    await journey.client.aclose()


@pytest.fixture
async def federal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Journey]:
    journey = await _journey(tmp_path, monkeypatch, "federal")
    yield journey
    await journey.client.aclose()


# --- what the app holds -----------------------------------------------------------


@dataclass(frozen=True)
class App:
    """What the app developer walks away with: its key pair and the one-time secret."""

    keys: KeyPair
    principal_id: str
    did: str
    secret: str
    approval_id: str | None

    def bearer(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", **extra}


def _issue_body(keys: KeyPair, subjects: list[str], *, days: int) -> dict[str, Any]:
    return {
        "name": "lead-review-app",
        "public_key": keys.public_key.hex(),
        "scopes": {
            str(Scope.WORKFLOWS_RUN): [SALES],
            str(Scope.RUNS_READ): [SALES],
            str(Scope.GATES_RESOLVE): [SALES],
        },
        "subjects": subjects,
        "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
    }


async def _issue(j: Journey, operator: dict[str, str], *, days: int = 7) -> tuple[App, Any]:
    keys = generate_keypair()
    subjects = [j.did(APPROVER_EMAIL), j.did(BYSTANDER_EMAIL)]
    resp = await j.client.post(
        "/api/principals", json=_issue_body(keys, subjects, days=days), headers=operator
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    app = App(
        keys=keys,
        principal_id=body["principal"]["id"],
        did=body["principal"]["did"],
        secret=body["secret"],
        approval_id=body.get("approval_id"),
    )
    return app, body


def _gate_node(run: dict[str, Any]) -> dict[str, Any]:
    gates = [n for n in run["nodes"] if n["node_id"] == GATE_NODE]
    assert len(gates) == 1, run
    return gates[0]


def _gate_url(task_id: str) -> str:
    return f"/api/workflow-tasks/{task_id}/gate"


APPROVE = {"decision": "approve"}


# --- personal journey -----------------------------------------------------------------


class TestPersonalJourney:
    async def test_app_runs_workflow_and_resolves_gate_for_person_then_is_revoked(
        self, personal: Journey
    ) -> None:
        j = personal
        approver = j.did(APPROVER_EMAIL)
        bystander = j.did(BYSTANDER_EMAIL)

        # 1. The operator signs in and issues the app; the secret is shown once.
        boss = await j.login(BOSS_EMAIL)
        app, issued = await _issue(j, boss)
        assert issued["status"] == "active"
        assert app.secret.startswith("arcp_")
        listed = await j.client.get("/api/principals", headers=boss)
        assert listed.status_code == 200, listed.text
        assert app.secret not in listed.text

        # 2. The app starts the run; the run waits at the gate.
        started = await j.client.post(
            f"/api/workflows/{FLOW}/run", json={"urgent": False}, headers=app.bearer()
        )
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]

        # 3. The app reads the run and finds the waiting gate.
        waiting = await j.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer())
        assert waiting.status_code == 200, waiting.text
        gate = _gate_node(waiting.json())
        assert gate["status"] == "waiting_gate", waiting.json()
        assert waiting.json()["status"] not in ("done", "completed", "failed")
        task_id = gate["task_id"]

        # 4. Allowlisted, but not an approver: refused, audited, run keeps waiting.
        refused = await j.client.post(
            _gate_url(task_id), json=APPROVE, headers=app.bearer(**{OBO: BYSTANDER_EMAIL})
        )
        assert refused.status_code == 403, refused.text
        denied = j.gate_events("workflow.gate.denied", task_id)
        assert [e.actor_did for e in denied] == [bystander]
        denied_rows = [r for r in j.mutation_rows("gate.resolve") if r.outcome == "denied"]
        assert len(denied_rows) == 1, j.mutation_audit.events
        assert denied_rows[0].extra["on_behalf_of"] == bystander
        assert denied_rows[0].extra["via_app"] == app.did
        still = await j.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer())
        assert _gate_node(still.json())["status"] == "waiting_gate", still.json()

        # 5. The listed approver decides through the app; the run completes.
        approved = await j.client.post(
            _gate_url(task_id), json=APPROVE, headers=app.bearer(**{OBO: APPROVER_EMAIL})
        )
        assert approved.status_code == 200, approved.text
        finished = await j.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer())
        assert finished.status_code == 200, finished.text
        detail = finished.json()
        assert detail["status"] == "done", detail
        statuses = {n["node_id"]: n["status"] for n in detail["nodes"]}
        assert statuses[GATE_NODE] == "done", detail
        assert statuses[ROUTER_NODE] == "done", detail
        assert statuses[ESCALATE_NODE] == "skipped", detail

        # The gate row records the person as the decider, never a fixed operator.
        row = await j.runner.tasks.get(task_id)
        assert row.metadata["gate_actor_did"] == approver
        assert row.metadata["gate_actor_did"] != FIXED_OPERATOR_DID

        # The control plane's decision names the person, initiated by the app.
        resolved = j.gate_events("workflow.gate.resolved", task_id)
        assert len(resolved) == 1, j.workflow_audit.events
        assert resolved[0].actor_did == approver
        assert resolved[0].causal is not None
        assert resolved[0].causal.initiator == "app"
        assert resolved[0].causal.initiator_id == app.did
        assert resolved[0].causal.on_behalf_of == approver

        # The mutation row names the person, the app, and the app as initiator.
        applied = [r for r in j.mutation_rows("gate.resolve") if r.outcome == "applied"]
        assert len(applied) == 1, j.mutation_audit.events
        assert applied[0].extra["acting_did"] == approver
        assert applied[0].extra["on_behalf_of"] == approver
        assert applied[0].extra["via_app"] == app.did
        assert applied[0].causal is not None
        assert applied[0].causal.initiator == "app"
        assert applied[0].causal.initiator_id == app.did
        assert applied[0].causal.on_behalf_of == approver

        # The run's own audit names both the app (it started the run) and the person.
        run_events = [
            e
            for e in j.workflow_audit.events
            if run_id in e.target or (e.extra or {}).get("run_id") == run_id
        ]
        started_events = [e for e in run_events if e.action == "workflow.run.started"]
        assert started_events, j.workflow_audit.events
        assert {e.actor_did for e in started_events} == {app.did}
        named = {e.actor_did for e in run_events} | {
            e.causal.on_behalf_of for e in run_events if e.causal is not None
        }
        assert {app.did, approver} <= named, run_events

        # 6. The operator revokes the app; its very next call is refused.
        revoked = await j.client.post(f"/api/principals/{app.principal_id}/revoke", headers=boss)
        assert revoked.status_code == 200, revoked.text
        after = await j.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer())
        assert after.status_code == 401, after.text


# --- federal journey -------------------------------------------------------------------


def _as_signer(j: Journey, app: App) -> IssuedPrincipal:
    record = j.rig.open().get(app.principal_id)
    assert record is not None
    return IssuedPrincipal(record=record, bearer=app.secret, keys=app.keys)


async def _signed_post(
    j: Journey, app: App, path: str, payload: dict[str, Any], on_behalf_of: str | None = None
) -> httpx.Response:
    body = json.dumps(payload).encode("utf-8")
    headers = signed_headers(
        _as_signer(j, app), method="POST", path=path, body=body, on_behalf_of=on_behalf_of
    )
    headers["Content-Type"] = "application/json"
    return await j.client.post(path, content=body, headers=headers)


async def _signed_get(j: Journey, app: App, path: str) -> httpx.Response:
    headers = signed_headers(_as_signer(j, app), method="GET", path=path)
    return await j.client.get(path, headers=headers)


class TestFederalJourney:
    async def test_federal_app_needs_signing_and_a_delegation_to_decide(
        self, federal: Journey
    ) -> None:
        j = federal
        approver = j.did(APPROVER_EMAIL)

        # Issue (pending at federal), a second operator approves (two-person rule).
        boss = await j.login(BOSS_EMAIL)
        app, issued = await _issue(j, boss, days=14)
        assert issued["status"] == "pending"
        assert app.approval_id is not None
        second = await j.login(SECOND_EMAIL)
        approved_issue = await j.client.post(
            f"/api/approvals/{app.approval_id}/approve", headers=second
        )
        assert approved_issue.status_code == 200, approved_issue.text

        # Federal refuses an unsigned bearer call outright.
        unsigned = await j.client.post(
            f"/api/workflows/{FLOW}/run", json={"urgent": False}, headers=app.bearer()
        )
        assert unsigned.status_code == 401, unsigned.text

        started = await _signed_post(j, app, f"/api/workflows/{FLOW}/run", {"urgent": False})
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]
        waiting = await _signed_get(j, app, f"/api/workflow-runs/{run_id}")
        assert waiting.status_code == 200, waiting.text
        task_id = _gate_node(waiting.json())["task_id"]

        # Without a delegation from the person, the app may not decide for them.
        no_delegation = await _signed_post(
            j, app, _gate_url(task_id), APPROVE, on_behalf_of=APPROVER_EMAIL
        )
        assert no_delegation.status_code == 403, no_delegation.text
        assert no_delegation.json()["error"] == "delegation_required"
        assert (await j.runner.tasks.get(task_id)).status == "review"
        refusals = [
            r
            for r in j.mutation_audit.events
            if r.outcome == "denied" and (r.extra or {}).get("via_app") == app.did
        ]
        assert refusals, j.mutation_audit.events

        # The person signs in and delegates gate decisions to this app.
        person = await j.login(APPROVER_EMAIL)
        delegated = await j.client.post(
            "/api/me/delegations",
            json={
                "principal_did": app.did,
                "scopes": [str(Scope.GATES_RESOLVE)],
                "ttl_seconds": 3600,
            },
            headers=person,
        )
        assert delegated.status_code == 201, delegated.text

        decided = await _signed_post(
            j, app, _gate_url(task_id), APPROVE, on_behalf_of=APPROVER_EMAIL
        )
        assert decided.status_code == 200, decided.text
        row = await j.runner.tasks.get(task_id)
        assert row.metadata["gate_actor_did"] == approver
        resolved = j.gate_events("workflow.gate.resolved", task_id)
        assert len(resolved) == 1, j.workflow_audit.events
        assert resolved[0].causal is not None
        assert (resolved[0].causal.initiator, resolved[0].causal.initiator_id) == ("app", app.did)
        assert resolved[0].causal.on_behalf_of == approver
        finished = await _signed_get(j, app, f"/api/workflow-runs/{run_id}")
        assert finished.json()["status"] == "done", finished.json()
