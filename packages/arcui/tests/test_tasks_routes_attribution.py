"""SPEC-087 T-1282: task routes record the acting identity (REQ-568, REQ-562, REQ-571).

Every task mutation (create, edit, move, cancel, delete, approve, reject) must be
attributed to whoever is acting: a signed-in person's account DID, an app's own
DID, or (app acting for a person) the person's DID with the app named as
``via_app`` on the mutation audit row. The fixed ``did:arc:ui:operator``
attribution (``_CREATOR``) is deleted.

Apps reach only ``POST /api/team/tasks`` and ``POST /api/tasks/{id}/approve|reject``
(``APP_ROUTES``). Those rows carry no agent in the path, so the handler itself
must bind ``tasks:write`` to the task's owner agent (``require_scope``), and an
app acting for a person gets only what that person could do (REQ-562).

Observable surfaces only:

* the created task's ``creator_did`` (response body + store read);
* the task store's own audit events (``TaskStore(sink=...)`` is its public
  audit seam: every store write emits an ``AuditEvent`` naming ``actor_did``);
* the route's mutation audit row on ``app.state.audit_worm``.

Real ``create_app`` + real ``AuthMiddleware`` + real ``UserStore`` and
``PrincipalStore`` (``principal_fixtures``); the task store is the real
``TaskStore`` over the per-test in-memory backend from ``conftest``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arcstore.tasks import Task, TaskStore
from arctrust.audit import AuditEvent
from arctrust.principals import Delegation, Scope
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
    signed_headers,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

OLIVIA = agent_did()
SALES = agent_did()
SEED_CREATOR = "did:arc:test:human/seed"
FIXED_OPERATOR_DID = "did:arc:ui:operator"
OBO = "X-Arc-On-Behalf-Of"
CREATE_PATH = "/api/team/tasks"


# --- doubles at the audit boundary -------------------------------------------------


class StoreAudit:
    """The task store's audit sink: records every ``AuditEvent`` it emits."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actors_for(self, task_id: str) -> list[str]:
        return [e.actor_did for e in self.events if e.target.endswith(f"/{task_id}")]


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def team(tmp_path: Path) -> Path:
    return write_team(tmp_path / "team", {"olivia": OLIVIA, "sales": SALES})


class Harness:
    """One built app, its task store wired to a recording audit sink."""

    def __init__(self, built: PrincipalApp, backend: Any) -> None:
        self.built = built
        self.store_audit = StoreAudit()
        self.store = TaskStore(backend, sink=self.store_audit)
        built.app.state.task_store = self.store
        self.client = TestClient(built.app, raise_server_exceptions=False)

    def seed(self, **overrides: Any) -> Task:
        fields: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "title": "seeded task",
            "creator_did": SEED_CREATOR,
            "owner_did": OLIVIA,
        }
        fields.update(overrides)
        created = asyncio.run(self.store.create(Task(**fields)))
        self.store_audit.events.clear()
        return created

    def get(self, task_id: str) -> Task | None:
        return asyncio.run(self.store.get(task_id))

    def all_tasks(self) -> list[Task]:
        return asyncio.run(self.store.list())

    def rows(self, operation: str) -> list[Any]:
        return [row for row in self.built.worm.written if row.operation == operation]

    def denied(self) -> list[Any]:
        return self.built.worm.denied()


def _harness(
    tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, backend: Any, tier: str
) -> Harness:
    built = build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team, tier=tier
    )
    return Harness(built, backend)


@pytest.fixture
def h(tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, arcstore_backend: Any) -> Harness:
    return _harness(tmp_path, rig, users, team, arcstore_backend, "personal")


@pytest.fixture
def fed(
    tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, arcstore_backend: Any
) -> Harness:
    return _harness(tmp_path, rig, users, team, arcstore_backend, "federal")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tasks_app(
    rig: PrincipalRig, subjects: tuple[str, ...], agents: tuple[str, ...] = (OLIVIA,)
) -> IssuedPrincipal:
    return issue_principal(rig.open(), scopes={Scope.TASKS_WRITE: agents}, subjects=subjects)


def _store_actors_are(audit: StoreAudit, task_id: str, expected: str) -> None:
    actors = audit.actors_for(task_id)
    assert actors, "the task store emitted no audit event for this mutation"
    assert set(actors) == {expected}, actors


# --- signed-in operator session: every mutation names the account DID ----------------


class TestSessionOperatorAttribution:
    def test_create_by_session_stores_account_did_as_creator(self, h: Harness) -> None:
        token, did = h.built.sign_in("boss@example.com", (OPERATOR,))
        resp = h.client.post(CREATE_PATH, json={"title": "write report"}, headers=_bearer(token))
        assert resp.status_code == 201, resp.text
        assert resp.json()["creator_did"] == did
        stored = h.get(resp.json()["id"])
        assert stored is not None and stored.creator_did == did
        _store_actors_are(h.store_audit, stored.id, did)

    def test_edit_by_session_records_account_did_as_actor(self, h: Harness) -> None:
        token, did = h.built.sign_in("boss@example.com", (OPERATOR,))
        task = h.seed(status="backlog")
        resp = h.client.patch(
            f"/api/tasks/{task.id}", json={"title": "renamed"}, headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        _store_actors_are(h.store_audit, task.id, did)

    def test_move_by_session_records_account_did_as_actor(self, h: Harness) -> None:
        token, did = h.built.sign_in("boss@example.com", (OPERATOR,))
        task = h.seed(status="backlog")
        resp = h.client.post(
            f"/api/tasks/{task.id}/move", json={"status": "todo"}, headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        _store_actors_are(h.store_audit, task.id, did)

    def test_cancel_by_session_records_account_did_as_actor(self, h: Harness) -> None:
        token, did = h.built.sign_in("boss@example.com", (OPERATOR,))
        task = h.seed(status="in_progress")
        resp = h.client.post(f"/api/tasks/{task.id}/cancel", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        _store_actors_are(h.store_audit, task.id, did)

    def test_delete_by_session_records_account_did_as_actor(self, h: Harness) -> None:
        token, did = h.built.sign_in("boss@example.com", (OPERATOR,))
        task = h.seed(status="backlog")
        resp = h.client.delete(f"/api/tasks/{task.id}", headers=_bearer(token))
        assert resp.status_code == 204, resp.text
        _store_actors_are(h.store_audit, task.id, did)

    @pytest.mark.parametrize(("verb", "after"), [("approve", "done"), ("reject", "todo")])
    def test_review_decision_by_session_records_account_did_as_actor(
        self, h: Harness, verb: str, after: str
    ) -> None:
        token, did = h.built.sign_in("boss@example.com", (OPERATOR,))
        task = h.seed(status="review")
        resp = h.client.post(f"/api/tasks/{task.id}/{verb}", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == after
        _store_actors_are(h.store_audit, task.id, did)

    def test_two_operators_are_not_collapsed_into_one_actor(self, h: Harness) -> None:
        token_a, did_a = h.built.sign_in("ada@example.com", (OPERATOR,))
        token_b, did_b = h.built.sign_in("bob@example.com", (OPERATOR,))
        a = h.client.post(CREATE_PATH, json={"title": "from ada"}, headers=_bearer(token_a))
        b = h.client.post(CREATE_PATH, json={"title": "from bob"}, headers=_bearer(token_b))
        assert a.status_code == b.status_code == 201
        assert (a.json()["creator_did"], b.json()["creator_did"]) == (did_a, did_b)

    def test_no_task_mutation_is_ever_attributed_to_the_fixed_operator_did(
        self, h: Harness
    ) -> None:
        token, _ = h.built.sign_in("boss@example.com", (OPERATOR,))
        task = h.seed(status="backlog")
        h.client.patch(f"/api/tasks/{task.id}", json={"title": "x"}, headers=_bearer(token))
        h.client.post(
            f"/api/tasks/{task.id}/move", json={"status": "todo"}, headers=_bearer(token)
        )
        h.client.delete(f"/api/tasks/{task.id}", headers=_bearer(token))
        assert FIXED_OPERATOR_DID not in {e.actor_did for e in h.store_audit.events}


# --- app principals on POST /api/team/tasks -----------------------------------------


class TestAppCreate:
    def test_app_alone_creates_task_for_its_bound_agent_as_itself(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        app = _tasks_app(rig, subjects=("*",))
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "nightly digest", "owner_did": OLIVIA},
            headers=app.bearer_headers(),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["creator_did"] == app.did
        stored = h.get(resp.json()["id"])
        assert stored is not None and stored.creator_did == app.did
        (row,) = h.rows("task.create")
        assert (row.outcome, row.actor_did, row.via_app, row.on_behalf_of) == (
            "applied",
            app.did,
            None,
            None,
        )

    def test_app_for_operator_person_stores_person_as_creator_and_names_app(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        _, person = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, subjects=(person,))
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "follow up", "owner_did": OLIVIA},
            headers=app.bearer_headers(**{OBO: person}),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["creator_did"] == person
        _store_actors_are(h.store_audit, resp.json()["id"], person)
        (row,) = h.rows("task.create")
        assert (row.outcome, row.actor_did, row.via_app, row.on_behalf_of) == (
            "applied",
            person,
            app.did,
            person,
        )

    def test_app_cannot_create_task_owned_by_an_agent_it_is_not_bound_to(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        app = _tasks_app(rig, subjects=("*",), agents=(OLIVIA,))
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "sneak", "owner_did": SALES},
            headers=app.bearer_headers(),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "scope_denied"
        assert h.all_tasks() == []
        assert len(h.denied()) == 1
        assert h.rows("task.create") == h.denied()

    def test_app_cannot_create_an_unowned_task(self, h: Harness, rig: PrincipalRig) -> None:
        # Every app scope is agent-bound: no owner means no agent to bind to.
        app = _tasks_app(rig, subjects=("*",))
        resp = h.client.post(CREATE_PATH, json={"title": "orphan"}, headers=app.bearer_headers())
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "scope_denied"
        assert h.all_tasks() == []
        assert len(h.denied()) == 1

    def test_app_for_viewer_person_is_refused_even_with_tasks_write(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        # REQ-562: the app gets only what the person could do; a viewer cannot
        # create tasks, so the app acting for them cannot either.
        _, viewer = h.built.sign_in("watcher@example.com", (VIEWER,))
        app = _tasks_app(rig, subjects=(viewer,))
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "escalate", "owner_did": OLIVIA},
            headers=app.bearer_headers(**{OBO: viewer}),
        )
        assert resp.status_code == 403, resp.text
        assert h.all_tasks() == []
        (row,) = h.denied()
        assert (row.actor_did, row.via_app, row.on_behalf_of) == (viewer, app.did, viewer)

    def test_same_app_same_scope_succeeds_for_operator_and_fails_for_viewer(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        # The positive control for the intersection: only the person differs.
        _, boss = h.built.sign_in("boss@example.com", (OPERATOR,))
        _, viewer = h.built.sign_in("watcher@example.com", (VIEWER,))
        app = _tasks_app(rig, subjects=(boss, viewer))
        body = {"title": "t", "owner_did": OLIVIA}
        ok = h.client.post(CREATE_PATH, json=body, headers=app.bearer_headers(**{OBO: boss}))
        no = h.client.post(CREATE_PATH, json=body, headers=app.bearer_headers(**{OBO: viewer}))
        assert (ok.status_code, no.status_code) == (201, 403), (ok.text, no.text)


# --- app principals on review approve/reject ----------------------------------------


class TestAppReviewDecision:
    @pytest.mark.parametrize(("verb", "after"), [("approve", "done"), ("reject", "todo")])
    def test_app_for_operator_person_records_person_and_names_app(
        self, h: Harness, rig: PrincipalRig, verb: str, after: str
    ) -> None:
        _, person = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, subjects=(person,))
        task = h.seed(status="review")
        resp = h.client.post(
            f"/api/tasks/{task.id}/{verb}", headers=app.bearer_headers(**{OBO: person})
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == after
        _store_actors_are(h.store_audit, task.id, person)
        (row,) = h.rows(f"task.{verb}")
        assert (row.outcome, row.actor_did, row.via_app, row.on_behalf_of) == (
            "applied",
            person,
            app.did,
            person,
        )

    def test_app_cannot_decide_a_review_task_owned_by_an_unbound_agent(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        _, person = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, subjects=(person,), agents=(OLIVIA,))
        task = h.seed(status="review", owner_did=SALES)
        resp = h.client.post(
            f"/api/tasks/{task.id}/approve", headers=app.bearer_headers(**{OBO: person})
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "scope_denied"
        stored = h.get(task.id)
        assert stored is not None and stored.status == "review"
        assert h.store_audit.events == []

    def test_app_deciding_an_unknown_task_gets_404(self, h: Harness, rig: PrincipalRig) -> None:
        # An unresolvable agent is 404 for an app: existence is not leaked.
        _, person = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, subjects=(person,))
        resp = h.client.post(
            "/api/tasks/no-such-task/approve", headers=app.bearer_headers(**{OBO: person})
        )
        assert resp.status_code == 404, resp.text

    def test_app_for_viewer_person_cannot_decide_a_review_task(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        _, viewer = h.built.sign_in("watcher@example.com", (VIEWER,))
        app = _tasks_app(rig, subjects=(viewer,))
        task = h.seed(status="review")
        resp = h.client.post(
            f"/api/tasks/{task.id}/approve", headers=app.bearer_headers(**{OBO: viewer})
        )
        assert resp.status_code == 403, resp.text
        stored = h.get(task.id)
        assert stored is not None and stored.status == "review"


# --- federal decisions go through decider() (REQ-571, REQ-563) -----------------------


def _signed_post(fed: Harness, app: IssuedPrincipal, path: str, *, on_behalf_of: str) -> Any:
    body = b""
    headers = signed_headers(app, method="POST", path=path, body=body, on_behalf_of=on_behalf_of)
    return fed.client.post(path, content=body, headers=headers)


class TestFederalReviewDecision:
    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_federal_static_operator_token_is_refused_operator_account_required(
        self, fed: Harness, verb: str
    ) -> None:
        task = fed.seed(status="review")
        resp = fed.client.post(f"/api/tasks/{task.id}/{verb}", headers=_bearer(OPERATOR_TOKEN))
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "operator_account_required"
        stored = fed.get(task.id)
        assert stored is not None and stored.status == "review"
        assert len(fed.denied()) == 1

    def test_federal_session_operator_may_decide_and_is_recorded(self, fed: Harness) -> None:
        token, did = fed.built.sign_in("boss@example.com", (OPERATOR,))
        task = fed.seed(status="review")
        resp = fed.client.post(f"/api/tasks/{task.id}/approve", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        _store_actors_are(fed.store_audit, task.id, did)

    def test_federal_app_for_person_without_delegation_is_refused(
        self, fed: Harness, rig: PrincipalRig
    ) -> None:
        _, person = fed.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, subjects=(person,))
        task = fed.seed(status="review")
        resp = _signed_post(fed, app, f"/api/tasks/{task.id}/approve", on_behalf_of=person)
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "delegation_required"
        stored = fed.get(task.id)
        assert stored is not None and stored.status == "review"

    def test_federal_app_for_person_with_live_delegation_records_person(
        self, fed: Harness, rig: PrincipalRig
    ) -> None:
        _, person = fed.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, subjects=(person,))
        now = datetime.now(UTC)
        rig.open().add_delegation(
            Delegation(
                id=uuid.uuid4().hex,
                person_did=person,
                principal_did=app.did,
                scopes=(Scope.TASKS_WRITE,),
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=1),
                revoked=False,
            )
        )
        task = fed.seed(status="review")
        resp = _signed_post(fed, app, f"/api/tasks/{task.id}/approve", on_behalf_of=person)
        assert resp.status_code == 200, resp.text
        _store_actors_are(fed.store_audit, task.id, person)
        (row,) = fed.rows("task.approve")
        assert (row.actor_did, row.via_app, row.on_behalf_of) == (person, app.did, person)


# --- architecture: the fixed attribution is gone --------------------------------------


def test_tasks_route_module_has_no_fixed_creator_constant() -> None:
    module = importlib.import_module("arcui.routes.tasks")
    assert not hasattr(module, "_CREATOR")
    assert FIXED_OPERATOR_DID not in inspect.getsource(module)


def test_seeded_tasks_are_listable_through_the_store(h: Harness) -> None:
    # Harness smoke: proves seeding and the store audit seam work, so a red
    # attribution test above is about attribution, not the rig.
    task = h.seed(status="backlog")
    assert [t.id for t in h.all_tasks()] == [task.id]
    asyncio.run(h.store.edit(task.id, {"title": "y"}, actor_did="did:arc:test:x"))
    assert h.store_audit.actors_for(task.id) == ["did:arc:test:x"]
