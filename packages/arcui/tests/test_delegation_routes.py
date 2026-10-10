"""SPEC-087 T-1280: a signed-in person's own delegations (COMP-011).

Routes under test (new module ``arcui.routes.principals``, mounted by ``create_app``):

- ``POST /api/me/delegations`` body ``{principal_did, scopes, ttl_seconds}`` -> 201
  with the delegation (``id, principal_did, scopes, created_at, expires_at``);
- ``GET /api/me/delegations`` -> ``{"delegations": [...]}``, only the caller's own;
- ``POST /api/me/delegations/{id}/revoke`` -> 200 with the revoked delegation.

Requirements: REQ-564 (a person creates, lists and revokes only their own
delegations; every change is stored in the account authority and audited) and
REQ-563 (at federal, an app deciding for a person needs a live delegation the
person made from their own session, for at most 8 hours).

Contract choices pinned here (orchestrator brief):

- another person's delegation answers 404, never 403, so ids do not leak;
- consent is personal, not an operator power: a viewer session manages its own;
- static operator token, static viewer token and app principals are refused 403;
- ttl > 8 h (28800 s), scopes outside ``DECISION_SCOPES`` and unknown or
  non-active principals are refused 400 (404 accepted for the principal) and
  write nothing to the authority.

Everything runs through the REAL ``create_app`` + ``AuthMiddleware`` with a real
``UserStore`` and a real anchored ``PrincipalStore`` (``principal_fixtures``).
Only the audit sinks and the at-rest cipher are doubles.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arctrust.keypair import generate_keypair
from arctrust.principals import Delegation, Scope, derive_app_did
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    VIEWER_TOKEN,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    all_scopes,
    build_principal_app,
    build_session_only_app,
    issue_principal,
    principal_rig,
    signed_headers,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

DELEGATIONS = "/api/me/delegations"
MAX_TTL = 28800
OLIVIA = agent_did()
GATE_PATTERN = "/api/workflow-tasks/{id:path}/gate"
GATE_PATH = "/api/workflow-tasks/task-1/gate"
ADD_EVENT = "principal.delegation.add"
REVOKE_EVENT = "principal.delegation.revoke"
OBO = "X-Arc-On-Behalf-Of"


# --- fixtures --------------------------------------------------------------------


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def built(tmp_path: Path, rig: PrincipalRig, users: Any) -> PrincipalApp:
    return build_principal_app(tmp_path, principal_store_factory=rig.factory, users=users)


@pytest.fixture
def client(built: PrincipalApp) -> TestClient:
    return TestClient(built.app, raise_server_exceptions=False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _app_principal(rig: PrincipalRig, **kw: Any) -> Any:
    kw.setdefault("scopes", all_scopes())
    return issue_principal(rig.open(), **kw)


def _body(principal_did: str, scopes: list[str] | None = None, ttl: Any = 3600) -> dict[str, Any]:
    return {
        "principal_did": principal_did,
        "scopes": ["gates:resolve"] if scopes is None else scopes,
        "ttl_seconds": ttl,
    }


def _create(client: TestClient, token: str, body: dict[str, Any]) -> Any:
    return client.post(DELEGATIONS, json=body, headers=_bearer(token))


def _revoke(client: TestClient, token: str, delegation_id: str) -> Any:
    return client.post(f"{DELEGATIONS}/{delegation_id}/revoke", headers=_bearer(token))


def _listed_ids(client: TestClient, token: str) -> set[str]:
    resp = client.get(DELEGATIONS, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()["delegations"]}


def _store_events(rig: PrincipalRig, action: str) -> list[Any]:
    return [event for event in rig.strict.events if event.action == action]


def _applied(built: PrincipalApp) -> list[Any]:
    return [row for row in built.worm.written if row.outcome == "applied"]


def _stored(rig: PrincipalRig, person_did: str) -> list[Delegation]:
    return rig.factory().delegations_for(person_did)


# --- fixture smoke: proves the harness, not the feature ----------------------------


class TestFixtureSmoke:
    """These pass today; they prove the RED below is the missing routes, not the rig."""

    def test_store_rig_accepts_a_delegation_directly(self, rig: PrincipalRig) -> None:
        app = _app_principal(rig)
        now = datetime.now(UTC)
        rig.open().add_delegation(
            Delegation(
                id="d-smoke",
                person_did="did:arc:test:user/smoke",
                principal_did=app.did,
                scopes=(Scope.GATES_RESOLVE,),
                created_at=now,
                expires_at=now + timedelta(hours=1),
                revoked=False,
            )
        )
        assert rig.factory().live_delegation(
            "did:arc:test:user/smoke", app.did, Scope.GATES_RESOLVE, now
        )
        assert len(_store_events(rig, ADD_EVENT)) == 1

    def test_session_person_is_authenticated(
        self, built: PrincipalApp, client: TestClient
    ) -> None:
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        resp = client.get("/api/auth/me", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["did"] == did

    def test_federal_app_builds_and_gate_probe_is_reached(
        self, tmp_path: Path, rig: PrincipalRig, users: Any
    ) -> None:
        fed = build_principal_app(
            tmp_path, principal_store_factory=rig.factory, users=users, tier="federal"
        )
        assert fed.app.state.principals_config.tier == "federal"
        fed.add_probe(GATE_PATTERN)
        token, _ = fed.sign_in("boss@example.com", (OPERATOR,))
        resp = TestClient(fed.app).post(GATE_PATH, content=b"{}", headers=_bearer(token))
        assert resp.status_code == 200, resp.text

    def test_session_only_build_still_works(self, tmp_path: Path, users: Any) -> None:
        plain = build_session_only_app(tmp_path, users=users)
        token, _ = plain.sign_in("boss@example.com", (OPERATOR,))
        resp = TestClient(plain.app).get("/api/auth/me", headers=_bearer(token))
        assert resp.status_code == 200, resp.text


# --- the route module exists and is mounted ------------------------------------------


def test_routes_module_exposes_routes() -> None:
    from arcui.routes import principals as principal_routes

    paths = {getattr(route, "path", None) for route in principal_routes.routes}
    assert DELEGATIONS in paths
    assert any(
        str(path).startswith(DELEGATIONS + "/{") and str(path).endswith("}/revoke")
        for path in paths
    )


# --- create / list / revoke own (REQ-564) ---------------------------------------------


class TestOwnDelegations:
    @pytest.mark.parametrize("roles", [(OPERATOR,), (VIEWER,)], ids=["operator", "viewer"])
    def test_person_creates_lists_and_revokes_their_own(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig, roles: tuple[str, ...]
    ) -> None:
        token, did = built.sign_in("ada@example.com", roles)
        app = _app_principal(rig)

        resp = _create(client, token, _body(app.did, ["gates:resolve", "tasks:write"], 3600))
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert set(created) >= {"id", "principal_did", "scopes", "created_at", "expires_at"}
        assert created["principal_did"] == app.did
        assert sorted(created["scopes"]) == ["gates:resolve", "tasks:write"]
        lifetime = datetime.fromisoformat(created["expires_at"]) - datetime.fromisoformat(
            created["created_at"]
        )
        assert lifetime == timedelta(seconds=3600)

        stored = _stored(rig, did)
        assert [d.id for d in stored] == [created["id"]]
        assert stored[0].person_did == did and not stored[0].revoked

        assert _listed_ids(client, token) == {created["id"]}

        resp = _revoke(client, token, created["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == created["id"]
        assert _stored(rig, did)[0].revoked is True

    def test_each_change_writes_one_audit_row_and_one_store_event(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        app = _app_principal(rig)
        before = len(_applied(built))

        created = _create(client, token, _body(app.did)).json()
        rows = _applied(built)[before:]
        assert len(rows) == 1
        assert rows[0].actor_did == did
        assert created["id"] in rows[0].target or created["id"] in rows[0].detail
        assert len(_store_events(rig, ADD_EVENT)) == 1

        _listed_ids(client, token)
        assert len(_applied(built)) == before + 1  # a read is not a change

        assert _revoke(client, token, created["id"]).status_code == 200
        rows = _applied(built)[before:]
        assert len(rows) == 2
        assert rows[1].actor_did == did
        assert len(_store_events(rig, REVOKE_EVENT)) == 1

    def test_ttl_exactly_eight_hours_is_accepted(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token, _ = built.sign_in("ada@example.com", (VIEWER,))
        resp = _create(client, token, _body(_app_principal(rig).did, ttl=MAX_TTL))
        assert resp.status_code == 201, resp.text

    def test_person_did_in_body_is_ignored_or_refused(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        """The person is always the session; a body cannot name someone else."""
        token_a, did_a = built.sign_in("ada@example.com", (VIEWER,))
        _, did_b = built.sign_in("bob@example.com", (OPERATOR,))
        body = {**_body(_app_principal(rig).did), "person_did": did_b}
        resp = _create(client, token_a, body)
        assert resp.status_code in (201, 400), resp.text
        assert _stored(rig, did_b) == []
        if resp.status_code == 201:
            assert [d.person_did for d in _stored(rig, did_a)] == [did_a]


# --- isolation between people (REQ-564) ------------------------------------------------


class TestOtherPeople:
    def test_person_b_cannot_see_or_revoke_person_a(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token_a, did_a = built.sign_in("ada@example.com", (VIEWER,))
        token_b, _ = built.sign_in("bob@example.com", (OPERATOR,))
        created = _create(client, token_a, _body(_app_principal(rig).did)).json()

        assert created["id"] not in _listed_ids(client, token_b)

        resp = _revoke(client, token_b, created["id"])
        assert resp.status_code == 404, resp.text
        assert _stored(rig, did_a)[0].revoked is False
        assert _store_events(rig, REVOKE_EVENT) == []

    def test_unknown_id_is_the_same_404(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token, _ = built.sign_in("ada@example.com", (VIEWER,))
        assert _revoke(client, token, "no-such-delegation").status_code == 404

    def test_second_revoke_is_refused_and_writes_nothing(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token, _ = built.sign_in("ada@example.com", (VIEWER,))
        created = _create(client, token, _body(_app_principal(rig).did)).json()
        assert _revoke(client, token, created["id"]).status_code == 200
        assert _revoke(client, token, created["id"]).status_code in (400, 404, 409)
        assert len(_store_events(rig, REVOKE_EVENT)) == 1


# --- input refusals ---------------------------------------------------------------------


class TestRefusedInput:
    @pytest.mark.parametrize("ttl", [MAX_TTL + 1, 86400, 0, -1, "3600", 1.5, None])
    def test_bad_ttl_is_refused_400(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig, ttl: Any
    ) -> None:
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        resp = _create(client, token, _body(_app_principal(rig).did, ttl=ttl))
        assert resp.status_code == 400, resp.text
        assert _stored(rig, did) == []

    @pytest.mark.parametrize(
        "scopes",
        [["runs:read"], ["gates:resolve", "inbox:send"], ["admin"], [], "gates:resolve"],
        ids=["read-scope", "mixed", "unknown", "empty", "not-a-list"],
    )
    def test_scopes_outside_decision_scopes_are_refused_400(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig, scopes: Any
    ) -> None:
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        resp = _create(client, token, _body(_app_principal(rig).did, scopes=scopes))
        assert resp.status_code == 400, resp.text
        assert _stored(rig, did) == []

    def test_unknown_principal_is_refused(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        resp = _create(client, token, _body("did:arc:default:app/nobody"))
        assert resp.status_code in (400, 404), resp.text
        assert _stored(rig, did) == []

    def test_revoked_principal_is_refused(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        app = _app_principal(rig)
        rig.open().revoke(app.record.id, "did:arc:test:operator/x")
        resp = _create(client, token, _body(app.did))
        assert resp.status_code in (400, 404), resp.text
        assert _stored(rig, did) == []

    def test_pending_principal_is_refused(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        """The store accepts a delegation to a pending app; the route must not."""
        token, did = built.sign_in("ada@example.com", (VIEWER,))
        template = _app_principal(rig).record
        keys = generate_keypair()
        pending = replace(
            template,
            id="pending-app",
            did=derive_app_did(keys.public_key, org="default"),
            public_key=keys.public_key.hex(),
            status="pending",
            approved_by=None,
        )
        rig.open().request(pending)
        resp = _create(client, token, _body(pending.did))
        assert resp.status_code in (400, 404), resp.text
        assert _stored(rig, did) == []

    def test_malformed_json_is_refused_400(self, built: PrincipalApp, client: TestClient) -> None:
        token, _ = built.sign_in("ada@example.com", (VIEWER,))
        resp = client.post(
            DELEGATIONS,
            content=b"{not json",
            headers={**_bearer(token), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400, resp.text


# --- who may call at all ----------------------------------------------------------------


class TestRefusedCallers:
    @pytest.mark.parametrize("token", [OPERATOR_TOKEN, VIEWER_TOKEN], ids=["operator", "viewer"])
    def test_static_token_is_refused_403(
        self, client: TestClient, rig: PrincipalRig, token: str
    ) -> None:
        app = _app_principal(rig)
        assert _create(client, token, _body(app.did)).status_code == 403
        assert client.get(DELEGATIONS, headers=_bearer(token)).status_code == 403
        assert _revoke(client, token, "anything").status_code == 403
        assert _store_events(rig, ADD_EVENT) == []

    def test_static_token_refusal_is_audited(
        self, built: PrincipalApp, client: TestClient, rig: PrincipalRig
    ) -> None:
        _create(client, OPERATOR_TOKEN, _body(_app_principal(rig).did))
        assert any(row.target.startswith(DELEGATIONS) for row in built.worm.denied())

    def test_app_principal_is_refused_403(self, client: TestClient, rig: PrincipalRig) -> None:
        app = _app_principal(rig)
        body = json.dumps(_body(app.did)).encode()
        resp = client.post(DELEGATIONS, content=body, headers=app.bearer_headers())
        assert resp.status_code == 403, resp.text
        assert client.get(DELEGATIONS, headers=app.bearer_headers()).status_code == 403
        assert _store_events(rig, ADD_EVENT) == []

    def test_authority_unreachable_fails_closed_503(self, tmp_path: Path, users: Any) -> None:
        down = build_principal_app(tmp_path, principal_store_factory=None, users=users)
        token, _ = down.sign_in("ada@example.com", (VIEWER,))
        resp = TestClient(down.app, raise_server_exceptions=False).post(
            DELEGATIONS, json=_body("did:arc:default:app/x"), headers=_bearer(token)
        )
        assert resp.status_code == 503, resp.text


# --- federal: a gate decision needs a live delegation (REQ-563) ---------------------------


async def _gate_decider(request: Request) -> JSONResponse:
    """Test-only stand-in for a gate route: it asks ``decider`` and reports the answer."""
    from arcui.acting import Refusal, decider

    await request.body()
    result = await run_in_threadpool(decider, request, Scope.GATES_RESOLVE)
    if isinstance(result, Refusal):
        return JSONResponse({"error": result.code}, status_code=result.status)
    return JSONResponse({"actor_did": result.actor_did, "via_app": result.via_app})


class TestFederalGateNeedsDelegation:
    def _setup(self, tmp_path: Path, rig: PrincipalRig, users: Any) -> tuple[Any, ...]:
        fed = build_principal_app(
            tmp_path, principal_store_factory=rig.factory, users=users, tier="federal"
        )
        fed.add_probe(GATE_PATTERN, handler=_gate_decider)
        token, person = fed.sign_in("pat@example.com", (OPERATOR,))
        app = issue_principal(
            rig.open(),
            scopes={Scope.GATES_RESOLVE: (OLIVIA,), Scope.TASKS_WRITE: (OLIVIA,)},
            subjects=(person,),
        )
        return fed, TestClient(fed.app, raise_server_exceptions=False), token, person, app

    @staticmethod
    def _gate(client: TestClient, app: Any, person: str) -> Any:
        headers = signed_headers(
            app, method="POST", path=GATE_PATH, body=b"{}", on_behalf_of=person
        )
        return client.post(GATE_PATH, content=b"{}", headers=headers)

    def test_gate_is_allowed_only_while_the_delegation_is_live(
        self, tmp_path: Path, rig: PrincipalRig, users: Any
    ) -> None:
        _, client, token, person, app = self._setup(tmp_path, rig, users)

        before = self._gate(client, app, person)
        assert before.status_code == 403, before.text
        assert before.json()["error"] == "delegation_required"

        created = _create(client, token, _body(app.did, ["gates:resolve"], 3600))
        assert created.status_code == 201, created.text

        during = self._gate(client, app, person)
        assert during.status_code == 200, during.text
        assert during.json() == {"actor_did": person, "via_app": app.did}

        assert _revoke(client, token, created.json()["id"]).status_code == 200

        after = self._gate(client, app, person)
        assert after.status_code == 403, after.text
        assert after.json()["error"] == "delegation_required"

    def test_delegation_for_another_scope_does_not_open_the_gate(
        self, tmp_path: Path, rig: PrincipalRig, users: Any
    ) -> None:
        _, client, token, person, app = self._setup(tmp_path, rig, users)
        created = _create(client, token, _body(app.did, ["tasks:write"], 3600))
        assert created.status_code == 201, created.text
        resp = self._gate(client, app, person)
        assert resp.status_code == 403
        assert resp.json()["error"] == "delegation_required"

    def test_delegation_to_another_app_does_not_open_the_gate(
        self, tmp_path: Path, rig: PrincipalRig, users: Any
    ) -> None:
        _, client, token, person, app = self._setup(tmp_path, rig, users)
        other = issue_principal(
            rig.open(), scopes={Scope.GATES_RESOLVE: (OLIVIA,)}, subjects=(person,)
        )
        assert _create(client, token, _body(other.did)).status_code == 201
        resp = self._gate(client, app, person)
        assert resp.status_code == 403
        assert resp.json()["error"] == "delegation_required"

    def test_one_persons_delegation_does_not_cover_another(
        self, tmp_path: Path, rig: PrincipalRig, users: Any
    ) -> None:
        fed, client, token, person, _ = self._setup(tmp_path, rig, users)
        _, other_person = fed.sign_in("quinn@example.com", (OPERATOR,))
        app = issue_principal(
            rig.open(),
            scopes={Scope.GATES_RESOLVE: (OLIVIA,)},
            subjects=(person, other_person),
        )
        assert _create(client, token, _body(app.did)).status_code == 201
        resp = self._gate(client, app, other_person)
        assert resp.status_code == 403
        assert resp.json()["error"] == "delegation_required"
