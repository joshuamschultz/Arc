"""SPEC-088 T-1361: run reads are scoped to ``runs:read`` on the run's agent (REQ-598).

``GET /api/runs/{run_id}/timeline``, ``/audit`` and ``/recalls`` (``arcui.routes.
observe_run``) must answer 404 to an app that does not hold ``runs:read`` for
the run's agent, and 200 to one that does. The agent is resolved from the
intake row ``intake-<run_id>`` (its ``owner_did``) when one exists, else from
the trace's single agent DID; an unresolvable run is 404 to an app (SDD
COMP-001). Before this task ``/audit`` and ``/recalls`` never checked scope at
all (any app reaching the row read every run), and ``/timeline`` ignored the
intake row.

Operator and viewer sessions and the operator token keep every read (SPEC-087
``require_scope``: operators pass every scope, viewers pass READ_SCOPES).

Every app refusal writes exactly one ``denied`` mutation audit row and the 404
body carries no events.

Doubles: the principal (``principal_factory``), the audit WORM spy, and the
``app.state.observe`` read mirror (a stub returning fixed rows per run id; the
route's job under test is the scope gate, not the arcstore query). The intake
row lives in the real ``TaskStore`` over the per-test in-memory backend.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from arcstore.tasks import Task, TaskStore
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import agent_did
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.principals import Scope
from arcui.routes import observe_run

OLIVIA = agent_did()
BOB = agent_did()
READS = ("timeline", "audit", "recalls")


class StubObserve:
    """``app.state.observe`` stand-in: per-run timeline, audit and recall rows."""

    def __init__(self) -> None:
        self.traces: dict[str, list[dict[str, Any]]] = {}

    def add_trace(self, run_id: str, *actors: str) -> None:
        self.traces[run_id] = [
            {"kind": "tool_call", "request_id": run_id, "actor_did": actor, "seq": i}
            for i, actor in enumerate(actors)
        ]

    async def timeline(self, *, run_id: str) -> list[dict[str, Any]]:
        return list(self.traces.get(run_id, []))

    async def run_audit(self, run_id: str) -> list[dict[str, Any]]:
        return [{"action": "tool.call", "run_id": run_id, "secret": f"audit-{run_id}"}]

    async def run_recalls(self, run_id: str) -> list[dict[str, Any]]:
        return [{"card": "recall", "run_id": run_id, "secret": f"recall-{run_id}"}]


class Rig:
    def __init__(self, principal_factory: Any, backend: Any) -> None:
        self.factory = principal_factory
        self.app = Starlette(routes=list(observe_run.routes))
        self.worm = principal_factory.install(self.app)
        self.observe = StubObserve()
        self.store = TaskStore(backend)
        self.app.state.observe = self.observe
        self.app.state.task_store = self.store
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def intake_run(self, owner: str, *trace_actors: str) -> str:
        """A run started through intake on ``owner``; the trace names ``trace_actors``."""
        run_id = str(uuid.uuid4())
        asyncio.run(
            self.store.create(
                Task(
                    id=f"intake-{run_id}",
                    title="t",
                    creator_did="did:arc:default:app/caller",
                    owner_did=owner,
                    status="in_progress",
                    max_attempts=1,
                    tags=["intake"],
                    metadata={
                        "intake": {
                            "run_id": run_id,
                            "run_purpose": "manual",
                            "correlation_id": None,
                            "callback_url": None,
                            "on_behalf_of": None,
                            "via_app": None,
                        },
                        "app": {},
                    },
                )
            )
        )
        self.observe.add_trace(run_id, *trace_actors)
        return run_id

    def trace_run(self, *actors: str) -> str:
        """A run with no intake row (chat, schedule); only its trace names agents."""
        run_id = str(uuid.uuid4())
        self.observe.add_trace(run_id, *actors)
        return run_id

    def as_app(self, scopes: dict[Scope, tuple[str, ...]]) -> Any:
        principal = self.factory.app(scopes)
        self.factory.act_as(self.app, principal)
        return principal

    def act_as(self, principal: Any) -> None:
        self.factory.act_as(self.app, principal)

    def read(self, run_id: str, what: str) -> Any:
        return self.client.get(f"/api/runs/{run_id}/{what}")


@pytest.fixture
def rig(principal_factory: Any, arcstore_backend: Any, tmp_path: Path) -> Rig:
    return Rig(principal_factory, arcstore_backend)


def _assert_hidden(rig: Rig, run_id: str, what: str) -> None:
    """404, nothing about the run in the body, exactly one denied audit row."""
    mark = len(rig.worm.written)
    resp = rig.read(run_id, what)
    assert resp.status_code == 404, f"{what}: {resp.status_code} {resp.text}"
    assert "secret" not in resp.text
    assert "tool_call" not in resp.text
    new_rows = rig.worm.written[mark:]
    assert [row.outcome for row in new_rows] == ["denied"], [
        (r.operation, r.outcome, r.detail) for r in new_rows
    ]


def _assert_served(rig: Rig, run_id: str, what: str) -> None:
    resp = rig.read(run_id, what)
    assert resp.status_code == 200, f"{what}: {resp.status_code} {resp.text}"
    if what == "timeline":
        assert resp.json()["timeline"] == rig.observe.traces[run_id]
    else:
        assert resp.json()["events"][0]["run_id"] == run_id


# ===============================================================================
# Apps: runs:read on the run's agent, or 404
# ===============================================================================


class TestAppOutsideScope:
    @pytest.mark.parametrize("what", READS)
    def test_intake_run_on_agent_outside_scope_is_404(self, rig: Rig, what: str) -> None:
        run_id = rig.intake_run(OLIVIA, OLIVIA)
        rig.as_app({Scope.RUNS_READ: (BOB,)})

        _assert_hidden(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_trace_run_on_agent_outside_scope_is_404(self, rig: Rig, what: str) -> None:
        run_id = rig.trace_run(OLIVIA)
        rig.as_app({Scope.RUNS_READ: (BOB,)})

        _assert_hidden(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_app_with_no_scopes_is_404(self, rig: Rig, what: str) -> None:
        run_id = rig.intake_run(OLIVIA, OLIVIA)
        rig.as_app({})

        _assert_hidden(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_app_with_other_scopes_for_agent_but_not_runs_read_is_404(
        self, rig: Rig, what: str
    ) -> None:
        run_id = rig.intake_run(OLIVIA, OLIVIA)
        rig.as_app({Scope.RUNS_START: (OLIVIA,), Scope.TASKS_WRITE: (OLIVIA,)})

        _assert_hidden(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_unknown_run_is_404_to_an_app(self, rig: Rig, what: str) -> None:
        rig.as_app({Scope.RUNS_READ: (OLIVIA, BOB)})

        _assert_hidden(rig, str(uuid.uuid4()), what)

    @pytest.mark.parametrize("what", READS)
    def test_trace_naming_several_agents_without_intake_row_is_404(
        self, rig: Rig, what: str
    ) -> None:
        run_id = rig.trace_run(OLIVIA, BOB)
        rig.as_app({Scope.RUNS_READ: (OLIVIA,)})

        _assert_hidden(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_intake_row_owner_wins_over_trace_agent(self, rig: Rig, what: str) -> None:
        """The run belongs to the intake row's agent, even if its trace names another."""
        run_id = rig.intake_run(OLIVIA, BOB)
        rig.as_app({Scope.RUNS_READ: (BOB,)})

        _assert_hidden(rig, run_id, what)

    def test_404_bodies_do_not_reveal_whether_a_run_exists(self, rig: Rig) -> None:
        run_id = rig.intake_run(OLIVIA, OLIVIA)
        rig.as_app({Scope.RUNS_READ: (BOB,)})

        for what in READS:
            hidden = rig.read(run_id, what)
            missing = rig.read(str(uuid.uuid4()), what)
            assert hidden.status_code == missing.status_code == 404, what
            assert hidden.json() == missing.json(), what


class TestAppInsideScope:
    @pytest.mark.parametrize("what", READS)
    def test_intake_run_with_runs_read_for_agent_is_200(self, rig: Rig, what: str) -> None:
        run_id = rig.intake_run(OLIVIA, OLIVIA)
        rig.as_app({Scope.RUNS_READ: (OLIVIA,)})

        _assert_served(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_trace_run_with_runs_read_for_agent_is_200(self, rig: Rig, what: str) -> None:
        run_id = rig.trace_run(OLIVIA, OLIVIA)
        rig.as_app({Scope.RUNS_READ: (OLIVIA,)})

        _assert_served(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_queued_intake_run_with_empty_trace_is_200(self, rig: Rig, what: str) -> None:
        """Before the first event the trace names nobody; the intake row decides."""
        run_id = rig.intake_run(OLIVIA)
        rig.as_app({Scope.RUNS_READ: (OLIVIA,)})

        _assert_served(rig, run_id, what)

    @pytest.mark.parametrize("what", READS)
    def test_intake_row_owner_grants_read_when_trace_names_another(
        self, rig: Rig, what: str
    ) -> None:
        run_id = rig.intake_run(OLIVIA, BOB)
        rig.as_app({Scope.RUNS_READ: (OLIVIA,)})

        _assert_served(rig, run_id, what)


# ===============================================================================
# Sessions keep their reads (T-1362 acceptance)
# ===============================================================================


class TestSessionsKeepReads:
    @pytest.mark.parametrize("what", READS)
    @pytest.mark.parametrize("roles", [(VIEWER,), (OPERATOR,)], ids=["viewer", "operator"])
    def test_session_reads_any_run(self, rig: Rig, roles: tuple[str, ...], what: str) -> None:
        runs = [rig.intake_run(OLIVIA, OLIVIA), rig.trace_run(OLIVIA, BOB)]
        rig.act_as(rig.factory.user(roles))
        mark = len(rig.worm.written)

        for run_id in runs:
            _assert_served(rig, run_id, what)
        assert rig.worm.written[mark:] == []

    @pytest.mark.parametrize("what", READS)
    def test_operator_token_reads_any_run(self, rig: Rig, what: str) -> None:
        run_id = rig.trace_run(OLIVIA, BOB)
        rig.act_as(rig.factory.operator_token())

        _assert_served(rig, run_id, what)
