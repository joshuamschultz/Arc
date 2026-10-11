"""SPEC-088 T-1363: the richer task create and patch fields (REQ-599..602, REQ-631).

``POST /api/team/tasks`` additionally accepts ``metadata``, ``parent_id``,
``blocked_by``, ``due_at`` and ``classification`` (SDD COMP-004, D-850):

* caller metadata is stored ONLY under ``metadata.app``; a caller can never set
  the platform keys (``workflow``, ``flow_run_id``, ``node_id``, ``intake``)
  that turn an ordinary row into a workflow node row, so arcagent's
  ``node_from_task`` returns ``None`` for whatever the caller sent;
* ``parent_id`` / ``blocked_by`` go through ``TaskStore.validate_links``: a
  missing parent, a missing blocker, more than 50 blockers or a cycle is a 400
  and nothing is created;
* ``due_at`` must be timezone-aware;
* ``classification`` is validated through the ``Task`` model with the
  deployment tier (``app.state.principals_config.tier``) and clearance
  (``app.state.inbox_clearance``) as context: above clearance is refused at
  every tier, an unknown label is refused at federal;
* ``creator_did`` is SPEC-087's acting identity, and the mutation audit row
  names the app (``via_app``) and the person.

``PATCH /api/tasks/{id}`` accepts ``due_at`` and ``classification`` under the
same validation.

Real collaborators: ``create_app`` with the real ``AuthMiddleware``, real
``UserStore`` / ``PrincipalStore`` (``principal_fixtures``), the real arcstore
``TaskStore`` over the per-test in-memory backend. Only the audit WORM is a spy.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import uuid
from pathlib import Path
from typing import Any

import pytest
from arcagent.modules.tasks.node_execution import node_from_task
from arcstore.tasks import Task, TaskStore
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
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
from starlette.testclient import TestClient

OLIVIA = agent_did()
SEED_CREATOR = "did:arc:test:human/seed"
FIXED_OPERATOR_DID = "did:arc:ui:operator"
OBO = "X-Arc-On-Behalf-Of"
CREATE_PATH = "/api/team/tasks"
PLATFORM_KEYS = ("workflow", "flow_run_id", "node_id", "intake")
AWARE_DUE = "2026-11-01T17:00:00+00:00"
NAIVE_DUE = "2026-11-01T17:00:00"


# --- harness -------------------------------------------------------------------------


class Harness:
    """One built app at one tier, a real task store, and a signed-in operator."""

    def __init__(self, built: PrincipalApp, backend: Any, rig: PrincipalRig) -> None:
        self.built = built
        self.rig = rig
        self.store = TaskStore(backend)
        built.app.state.task_store = self.store
        self.client = TestClient(built.app, raise_server_exceptions=False)
        self.token, self.operator_did = built.sign_in("boss@example.com", (OPERATOR,))

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def create(self, **body: Any) -> Any:
        return self.client.post(CREATE_PATH, json={"title": "t", **body}, headers=self.headers)

    def patch(self, task_id: str, **body: Any) -> Any:
        return self.client.patch(f"/api/tasks/{task_id}", json=body, headers=self.headers)

    def seed(self, **overrides: Any) -> Task:
        fields: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "title": "seeded task",
            "creator_did": SEED_CREATOR,
            "status": "backlog",
        }
        fields.update(overrides)
        return asyncio.run(self.store.create(Task(**fields)))

    def get(self, task_id: str) -> Task | None:
        return asyncio.run(self.store.get(task_id))

    def all_tasks(self) -> list[Task]:
        return asyncio.run(self.store.list())

    def rows(self, operation: str) -> list[Any]:
        return [row for row in self.built.worm.written if row.operation == operation]

    def app(self, subjects: tuple[str, ...] = ("*",)) -> IssuedPrincipal:
        return issue_principal(
            self.rig.open(), scopes={Scope.TASKS_WRITE: (OLIVIA,)}, subjects=subjects
        )


def _build(tmp_path: Path, backend: Any, monkeypatch: pytest.MonkeyPatch, tier: str) -> Harness:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    rig = principal_rig(tmp_path / "authority")
    built = build_principal_app(
        tmp_path,
        principal_store_factory=rig.factory,
        users=user_store_factory(tmp_path / "accounts"),
        team_root=write_team(tmp_path / "team", {"olivia": OLIVIA}),
        tier=tier,
    )
    # The deployment clearance the classification ceiling is read from (COMP-004).
    built.app.state.inbox_clearance = "CUI"
    return Harness(built, backend, rig)


@pytest.fixture
def h(tmp_path: Path, arcstore_backend: Any, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return _build(tmp_path, arcstore_backend, monkeypatch, "personal")


@pytest.fixture
def fed(tmp_path: Path, arcstore_backend: Any, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return _build(tmp_path, arcstore_backend, monkeypatch, "federal")


@pytest.fixture(params=["personal", "enterprise", "federal"])
def any_tier(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    arcstore_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    return _build(tmp_path, arcstore_backend, monkeypatch, str(request.param))


def _refused_with_nothing_created(h: Harness, resp: Any, before: int) -> None:
    assert resp.status_code == 400, resp.text
    assert "error" in resp.json()
    assert len(h.all_tasks()) == before, "a refused create must write no task"


# --- REQ-600 / D-850: caller metadata lives under metadata.app ------------------------


class TestMetadataNamespace:
    def test_create_with_metadata_stores_it_only_under_metadata_app(self, h: Harness) -> None:
        caller = {"customer": "acme", "ticket": 42}
        resp = h.create(metadata=caller)
        assert resp.status_code == 201, resp.text
        assert resp.json()["metadata"] == {"app": caller}
        stored = h.get(resp.json()["id"])
        assert stored is not None
        assert stored.metadata == {"app": caller}

    def test_create_metadata_with_platform_keys_never_reaches_top_level(self, h: Harness) -> None:
        spoof = {
            "workflow": "orders",
            "flow_run_id": "run-forged",
            "node_id": "approve",
            "intake": {"run_id": "forged"},
            "node_kind": "gate",
        }
        resp = h.create(metadata=spoof)
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None
        assert stored.metadata == {"app": spoof}
        assert not set(PLATFORM_KEYS) & set(stored.metadata)
        # The arcagent dispatch path sees an ordinary task, not a forged node row.
        assert node_from_task(stored) is None

    def test_create_with_platform_keys_at_body_top_level_never_sets_them(self, h: Harness) -> None:
        resp = h.create(
            workflow="orders", flow_run_id="run-forged", node_id="approve", intake={"x": 1}
        )
        assert resp.status_code in (201, 400), resp.text
        for task in h.all_tasks():
            assert not set(PLATFORM_KEYS) & set(task.metadata), task.metadata
            assert node_from_task(task) is None

    def test_create_with_non_object_metadata_is_400_and_creates_nothing(self, h: Harness) -> None:
        resp = h.create(metadata=["not", "an", "object"])
        _refused_with_nothing_created(h, resp, 0)


# --- body cap: 64 KiB, counted as it streams --------------------------------------------

OVER_CAP = 65 * 1024


class TestBodyCap:
    def _assert_refused(self, h: Harness, resp: Any) -> None:
        assert resp.status_code == 413, resp.text[:200]
        assert h.all_tasks() == [], "an oversized create must write no task"
        [row] = h.rows("task.create")
        assert row.outcome == "denied"

    def test_a_65_kib_body_is_413_and_creates_nothing(self, h: Harness) -> None:
        resp = h.create(metadata={"pad": "x" * OVER_CAP})

        self._assert_refused(h, resp)

    def test_a_streamed_body_without_content_length_is_capped(self, h: Harness) -> None:
        def chunks() -> Any:
            yield b'{"title": "t", "metadata": {"pad": "'
            for _ in range(OVER_CAP // 1024):
                yield b"x" * 1024
            yield b'"}}'

        resp = h.client.post(
            CREATE_PATH,
            content=chunks(),
            headers={**h.headers, "Content-Type": "application/json"},
        )

        self._assert_refused(h, resp)


# --- REQ-601: links are validated, nothing is created on refusal ----------------------


class TestLinks:
    def test_create_with_existing_parent_and_blockers_stores_links(self, h: Harness) -> None:
        parent = h.seed()
        blocker_a, blocker_b = h.seed(), h.seed()
        resp = h.create(parent_id=parent.id, blocked_by=[blocker_a.id, blocker_b.id])
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None
        assert stored.parent_id == parent.id
        assert list(stored.blocked_by) == [blocker_a.id, blocker_b.id]

    def test_create_with_unknown_parent_is_400_and_creates_nothing(self, h: Harness) -> None:
        resp = h.create(parent_id="no-such-parent")
        _refused_with_nothing_created(h, resp, 0)

    def test_create_with_one_missing_blocker_is_400_and_creates_nothing(self, h: Harness) -> None:
        real = h.seed()
        resp = h.create(blocked_by=[real.id, "no-such-blocker"])
        _refused_with_nothing_created(h, resp, 1)

    def test_create_with_51_existing_blockers_is_400_and_creates_nothing(self, h: Harness) -> None:
        blockers = [h.seed().id for _ in range(51)]
        resp = h.create(blocked_by=blockers)
        _refused_with_nothing_created(h, resp, 51)

    def test_create_with_exactly_50_blockers_is_accepted(self, h: Harness) -> None:
        blockers = [h.seed().id for _ in range(50)]
        resp = h.create(blocked_by=blockers)
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None and list(stored.blocked_by) == blockers

    def test_create_blocked_by_a_blocker_cycle_is_400_and_creates_nothing(
        self, h: Harness
    ) -> None:
        a_id, b_id = uuid.uuid4().hex, uuid.uuid4().hex
        h.seed(id=a_id, blocked_by=[b_id])
        h.seed(id=b_id, blocked_by=[a_id])
        resp = h.create(blocked_by=[a_id])
        _refused_with_nothing_created(h, resp, 2)

    def test_create_under_a_parent_cycle_is_400_and_creates_nothing(self, h: Harness) -> None:
        p_id, q_id = uuid.uuid4().hex, uuid.uuid4().hex
        h.seed(id=p_id, parent_id=q_id)
        h.seed(id=q_id, parent_id=p_id)
        resp = h.create(parent_id=p_id)
        _refused_with_nothing_created(h, resp, 2)

    def test_create_with_non_list_blocked_by_is_400(self, h: Harness) -> None:
        resp = h.create(blocked_by="not-a-list")
        _refused_with_nothing_created(h, resp, 0)


# --- REQ-602 / REQ-603: due_at and classification ------------------------------------


class TestDueAt:
    def test_create_with_aware_due_at_stores_it(self, h: Harness) -> None:
        resp = h.create(due_at=AWARE_DUE)
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None and stored.due_at == AWARE_DUE

    def test_create_with_naive_due_at_is_400_and_creates_nothing(self, h: Harness) -> None:
        resp = h.create(due_at=NAIVE_DUE)
        _refused_with_nothing_created(h, resp, 0)
        assert "due_at" in resp.json()["error"]

    def test_create_with_garbage_due_at_is_400(self, h: Harness) -> None:
        resp = h.create(due_at="next tuesday")
        _refused_with_nothing_created(h, resp, 0)


class TestClassification:
    def test_create_at_clearance_stores_the_label(self, any_tier: Harness) -> None:
        resp = any_tier.create(classification="CUI")
        assert resp.status_code == 201, resp.text
        stored = any_tier.get(resp.json()["id"])
        assert stored is not None and stored.classification == "CUI"

    def test_create_above_clearance_is_400_at_every_tier(self, any_tier: Harness) -> None:
        resp = any_tier.create(classification="SECRET")
        _refused_with_nothing_created(any_tier, resp, 0)
        assert "classification" in resp.json()["error"]

    def test_create_unknown_label_at_federal_is_400(self, fed: Harness) -> None:
        resp = fed.create(classification="BANANA")
        _refused_with_nothing_created(fed, resp, 0)
        assert "classification" in resp.json()["error"]

    def test_create_label_is_parsed_to_the_ladder_name(self, h: Harness) -> None:
        # The label goes through the tier-gated validator, which stores the
        # ladder name, not the caller's spelling.
        resp = h.create(classification=" cui ")
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None and stored.classification == "CUI"

    def test_clearance_ceiling_follows_app_state_inbox_clearance(self, h: Harness) -> None:
        h.built.app.state.inbox_clearance = "SECRET"
        resp = h.create(classification="SECRET")
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None and stored.classification == "SECRET"


# --- PATCH accepts due_at and classification under the same validation ---------------


class TestPatch:
    def test_patch_aware_due_at_is_stored(self, h: Harness) -> None:
        task = h.seed()
        resp = h.patch(task.id, due_at=AWARE_DUE)
        assert resp.status_code == 200, resp.text
        stored = h.get(task.id)
        assert stored is not None and stored.due_at == AWARE_DUE

    def test_patch_naive_due_at_is_400_and_leaves_row_unchanged(self, h: Harness) -> None:
        # A valid title rides along, so the 400 can only come from due_at.
        task = h.seed(due_at=AWARE_DUE)
        resp = h.patch(task.id, title="renamed", due_at=NAIVE_DUE)
        assert resp.status_code == 400, resp.text
        assert "due_at" in resp.json()["error"]
        stored = h.get(task.id)
        assert stored is not None and stored.due_at == AWARE_DUE
        assert stored.title == "seeded task"

    def test_patch_classification_within_clearance_is_stored(self, any_tier: Harness) -> None:
        task = any_tier.seed()
        resp = any_tier.patch(task.id, classification="CUI")
        assert resp.status_code == 200, resp.text
        stored = any_tier.get(task.id)
        assert stored is not None and stored.classification == "CUI"

    def test_patch_classification_above_clearance_is_400_at_every_tier(
        self, any_tier: Harness
    ) -> None:
        task = any_tier.seed()
        resp = any_tier.patch(task.id, title="renamed", classification="SECRET")
        assert resp.status_code == 400, resp.text
        assert "classification" in resp.json()["error"]
        stored = any_tier.get(task.id)
        assert stored is not None and stored.classification == "UNCLASSIFIED"
        assert stored.title == "seeded task"

    def test_patch_unknown_label_at_federal_is_400(self, fed: Harness) -> None:
        task = fed.seed()
        resp = fed.patch(task.id, title="renamed", classification="BANANA")
        assert resp.status_code == 400, resp.text
        assert "classification" in resp.json()["error"]

    def test_patch_cannot_write_metadata_or_links(self, h: Harness) -> None:
        # The patch allowlist grows by due_at and classification only:
        # metadata and links stay create-time / store-managed (SEC-F4).
        other = h.seed()
        task = h.seed()
        resp = h.patch(
            task.id,
            title="renamed",
            metadata={"workflow": "orders"},
            blocked_by=[other.id],
            parent_id=other.id,
        )
        assert resp.status_code == 200, resp.text
        stored = h.get(task.id)
        assert stored is not None
        assert stored.metadata == {}
        assert list(stored.blocked_by) == []
        assert stored.parent_id is None


# --- REQ-599 / REQ-631: creator and audit name the acting identity -------------------


class TestAttribution:
    def test_app_alone_creates_with_metadata_as_itself(self, h: Harness) -> None:
        app = h.app()
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "digest", "owner_did": OLIVIA, "metadata": {"src": "erp"}},
            headers=app.bearer_headers(),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["creator_did"] == app.did
        stored = h.get(resp.json()["id"])
        assert stored is not None
        assert stored.creator_did == app.did
        assert stored.metadata == {"app": {"src": "erp"}}
        (row,) = h.rows("task.create")
        assert (row.outcome, row.actor_did, row.via_app, row.on_behalf_of) == (
            "applied",
            app.did,
            None,
            None,
        )

    def test_app_for_person_creates_as_person_and_audit_names_app(self, h: Harness) -> None:
        _, person = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = h.app(subjects=(person,))
        resp = h.client.post(
            CREATE_PATH,
            json={
                "title": "follow up",
                "owner_did": OLIVIA,
                "metadata": {"src": "crm"},
                "due_at": AWARE_DUE,
            },
            headers=app.bearer_headers(**{OBO: person}),
        )
        assert resp.status_code == 201, resp.text
        stored = h.get(resp.json()["id"])
        assert stored is not None
        assert stored.creator_did == person
        assert stored.metadata == {"app": {"src": "crm"}}
        assert stored.due_at == AWARE_DUE
        (row,) = h.rows("task.create")
        assert (row.outcome, row.actor_did, row.via_app, row.on_behalf_of) == (
            "applied",
            person,
            app.did,
            person,
        )

    def test_app_creating_an_ownerless_task_is_refused(self, h: Harness) -> None:
        app = h.app()
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "orphan", "metadata": {"src": "erp"}},
            headers=app.bearer_headers(),
        )
        assert resp.status_code == 403, resp.text
        assert h.all_tasks() == []
        assert [r.outcome for r in h.rows("task.create")] == ["denied"]

    def test_link_refusal_by_an_app_is_audited_with_app_and_person(self, h: Harness) -> None:
        _, person = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = h.app(subjects=(person,))
        resp = h.client.post(
            CREATE_PATH,
            json={"title": "x", "owner_did": OLIVIA, "parent_id": "no-such-parent"},
            headers=app.bearer_headers(**{OBO: person}),
        )
        assert resp.status_code == 400, resp.text
        assert h.all_tasks() == []
        (row,) = h.rows("task.create")
        assert (row.outcome, row.actor_did, row.via_app, row.on_behalf_of) == (
            "denied",
            person,
            app.did,
            person,
        )

    def test_session_create_with_new_fields_is_attributed_to_the_account(self, h: Harness) -> None:
        resp = h.create(metadata={"k": "v"}, classification="CUI", due_at=AWARE_DUE)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["creator_did"] == h.operator_did != FIXED_OPERATOR_DID
        assert (body["metadata"], body["classification"], body["due_at"]) == (
            {"app": {"k": "v"}},
            "CUI",
            AWARE_DUE,
        )


def test_tasks_route_module_carries_no_fixed_actor_literal() -> None:
    module = importlib.import_module("arcui.routes.tasks")
    assert FIXED_OPERATOR_DID not in inspect.getsource(module)
    assert not hasattr(module, "_CREATOR")
