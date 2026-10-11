"""SPEC-088 T-1330: the ``principal_factory`` fixture drives the SPEC-087 scope seam.

A tiny route guarded by :func:`arcui.principals.require_scope` proves the fixture
puts a real :class:`~arcui.principals.RequestPrincipal` on each request: an app
without the scope (or holding it for another agent) gets an audited 403 with
exactly one denial row; an app holding the scope for that agent passes with no
row. People and the static operator token follow SPEC-087's role rules.
"""

from __future__ import annotations

from typing import Any

import pytest
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import agent_did
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.principals import Scope, require_scope

OLIVIA = agent_did()
BOB = agent_did()
PERSON = "did:arc:default:user/alice"


async def _start_run(request: Request) -> Response:
    """Stand-in for a SPEC-088 route: resolve the agent, then require the scope."""
    agent = request.path_params["agent"]
    agent_did_value = {"olivia": OLIVIA, "bob": BOB}.get(agent)
    denial = require_scope(
        request,
        Scope.RUNS_START,
        agent_did=agent_did_value,
        target=f"agent:{agent}",
        operation="probe.run.start",
    )
    if denial is not None:
        return denial
    return JSONResponse({"started": agent})


async def _read_run(request: Request) -> Response:
    denial = require_scope(
        request, Scope.RUNS_READ, agent_did=OLIVIA, target="run:r1", operation="probe.run.read"
    )
    return denial or JSONResponse({"run": "r1"})


@pytest.fixture
def guarded() -> Starlette:
    return Starlette(
        routes=[
            Route("/probe/{agent}/runs", _start_run, methods=["POST"]),
            Route("/probe/run", _read_run, methods=["GET"]),
        ]
    )


def _denied(worm: Any) -> list[Any]:
    return [row for row in worm.written if row.outcome == "denied"]


class TestAppPrincipal:
    def test_app_without_scope_gets_403_and_one_denial_row(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        app = principal_factory.app({Scope.RUNS_READ: (OLIVIA,)})
        principal_factory.act_as(guarded, app)

        resp = TestClient(guarded).post("/probe/olivia/runs")

        assert resp.status_code == 403
        assert resp.json() == {"error": "scope_denied", "scope": "runs:start", "agent": OLIVIA}
        rows = _denied(worm)
        assert len(rows) == 1
        assert len(worm.written) == 1
        row = rows[0]
        assert row.actor_did == app.did
        assert row.actor_role == "app"
        assert row.operation == "probe.run.start"
        assert row.target == "agent:olivia"
        assert "scope=runs:start" in row.detail

    def test_app_holding_scope_for_that_agent_passes_without_audit(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        principal_factory.act_as(guarded, principal_factory.app({Scope.RUNS_START: (OLIVIA,)}))

        resp = TestClient(guarded).post("/probe/olivia/runs")

        assert resp.status_code == 200
        assert resp.json() == {"started": "olivia"}
        assert worm.written == []

    def test_scope_for_another_agent_is_refused(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        principal_factory.act_as(guarded, principal_factory.app({Scope.RUNS_START: (OLIVIA,)}))

        resp = TestClient(guarded).post("/probe/bob/runs")

        assert resp.status_code == 403
        assert len(_denied(worm)) == 1

    def test_unknown_agent_fails_closed_for_an_app(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        principal_factory.act_as(guarded, principal_factory.app({Scope.RUNS_START: (OLIVIA,)}))

        resp = TestClient(guarded).post("/probe/nobody/runs")

        assert resp.status_code == 403
        assert resp.json()["agent"] is None
        assert len(_denied(worm)) == 1

    def test_wildcard_binding_honoured_at_personal_only(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        principal_factory.install(guarded, tier="enterprise")
        principal_factory.act_as(guarded, principal_factory.app({Scope.RUNS_START: ("*",)}))

        assert TestClient(guarded).post("/probe/olivia/runs").status_code == 403

    def test_denial_on_behalf_of_names_person_and_app(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        app = principal_factory.app({}, on_behalf_of=PERSON, person_roles=(VIEWER,))
        principal_factory.act_as(guarded, app)

        resp = TestClient(guarded).post("/probe/olivia/runs")

        assert resp.status_code == 403
        (row,) = _denied(worm)
        assert row.actor_did == PERSON
        assert row.on_behalf_of == PERSON
        assert row.via_app == app.did

    def test_principal_can_be_swapped_between_requests(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        principal_factory.install(guarded)
        client = TestClient(guarded)
        principal_factory.act_as(guarded, principal_factory.app({}))
        assert client.post("/probe/olivia/runs").status_code == 403
        principal_factory.act_as(guarded, principal_factory.app({Scope.RUNS_START: (OLIVIA,)}))
        assert client.post("/probe/olivia/runs").status_code == 200


class TestPeopleAndOperatorToken:
    def test_operator_session_passes_every_scope(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        principal_factory.act_as(guarded, principal_factory.user((OPERATOR,)))

        assert TestClient(guarded).post("/probe/bob/runs").status_code == 200
        assert worm.written == []

    def test_viewer_session_refused_a_write_scope_but_reads(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)
        viewer = principal_factory.user((VIEWER,))
        principal_factory.act_as(guarded, viewer)
        client = TestClient(guarded)

        assert client.post("/probe/olivia/runs").status_code == 403
        (row,) = _denied(worm)
        assert row.actor_did == viewer.did
        assert row.actor_role == "viewer"
        assert client.get("/probe/run").status_code == 200

    def test_static_operator_token_passes_and_is_a_session_pseudo_did(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        principal_factory.install(guarded)
        token = principal_factory.operator_token()
        principal_factory.act_as(guarded, token)

        assert token.kind == "operator"
        assert token.did.startswith("did:arc:ui:session:")
        assert TestClient(guarded).post("/probe/olivia/runs").status_code == 200

    def test_no_principal_bound_fails_closed(
        self, principal_factory: Any, guarded: Starlette
    ) -> None:
        worm = principal_factory.install(guarded)

        assert TestClient(guarded).post("/probe/olivia/runs").status_code == 403
        assert len(_denied(worm)) == 1
