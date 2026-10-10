"""SPEC-087 T-1290: app principal abuse (ASI03, ASI07) — adversarial battery scenario.

REQ-575 (replay, scope escalation, forged on-behalf-of, one app acting as another,
a revoked principal and an unscoped gate resolution are each refused AND audited),
REQ-556 (rollback of the principal authority fails closed), REQ-560 (signed
requests bind method, path, body and on-behalf-of), REQ-561 (on-behalf-of only for
live, enabled, allowlisted people), REQ-562 (an app adds no authority to the
person), REQ-572/578 (authority unreachable: 503, never a fallback), REQ-576
(rate limit 429).

Every attack goes through the REAL ``create_app`` build: the real
``AuthMiddleware``, the real principal pipeline (``arcui.principals``), the real
routes (workflows, workflow runs, run timeline, tasks, inbox, approvals, users,
principals, delegations, trust, standing grants), a real ``UserStore`` and a real
anchored ``PrincipalStore`` (``principal_fixtures``), a real ``WorkflowRunner`` and
control plane (``test_workflow_attribution._harness``). Only the audit sinks are
recorders. Credentials are minted and signed by ``arctrust.principal_auth``.

Each case asserts the refusal (status + fixed error code) AND the audit evidence:
exactly one refusal row on the dashboard audit WORM for that request. The
``leaks`` fixture then proves no bearer secret or request signature used in the
case reached a captured log record or an audit row.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record
from arctrust.principal_auth import HEADER_SIGNATURE, sign_request
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    WILDCARD,
    IssuedPrincipal,
    PrincipalApp,
    PrincipalRig,
    all_scopes,
    build_principal_app,
    issue_principal,
    principal_rig,
    signed_headers,
    write_team,
)
from packages.arcui.tests.test_workflow_attribution import (
    APPROVE,
    OBO,
    OLIVIA,
    OLIVIA_FLOW,
    SALES,
    SALES_FLOW,
    Harness,
    _gate_url,
    _harness,
)
from packages.arcui.tests.user_authority import user_store_factory

INVALID = {"error": "invalid_principal"}
SCOPE_DENIED = "scope_denied"
ROUTE_NOT_ALLOWED = {"error": "route_not_allowed"}
OBO_DENIED = {"error": "on_behalf_of_denied"}
UNAVAILABLE = {"error": "account_authority_unavailable"}
SALES_TRACE = "req-abuse-sales"
OLIVIA_TRACE = "req-abuse-olivia"


# --- leak guard: secrets and signatures never reach logs or audit rows -------------


@dataclass
class LeakGuard:
    """Collects every credential a case used; checks logs and audit rows at teardown."""

    secrets: set[str] = field(default_factory=set)
    apps: list[PrincipalApp] = field(default_factory=list)

    def watch(self, built: PrincipalApp) -> None:
        self.apps.append(built)

    def principal(self, issued: IssuedPrincipal) -> IssuedPrincipal:
        # The part after the dot is the secret; the id before it is a public handle.
        self.secrets.add(issued.bearer.split(".", 1)[1])
        self.secrets.add(issued.bearer)
        return issued

    def headers(self, headers: dict[str, str]) -> dict[str, str]:
        if HEADER_SIGNATURE in headers:
            self.secrets.add(headers[HEADER_SIGNATURE])
        return headers

    def audit_text(self) -> str:
        rows = [row for built in self.apps for row in built.worm.written]
        return "\n".join(json.dumps(row.model_dump(), default=str) for row in rows)


@pytest.fixture
def leaks(caplog: pytest.LogCaptureFixture) -> Iterator[LeakGuard]:
    caplog.set_level("DEBUG")
    guard = LeakGuard()
    yield guard
    # At teardown ``caplog.records`` is the teardown phase only; read the whole case.
    records = [r for phase in ("setup", "call") for r in caplog.get_records(phase)]
    logged = "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in records)
    audited = guard.audit_text()
    exposed = sorted(s for s in guard.secrets if s in logged or s in audited)
    assert exposed == [], "a principal secret or signature leaked into logs or audit rows"


# --- harnesses -----------------------------------------------------------------------


@pytest.fixture
async def h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leaks: LeakGuard
) -> AsyncIterator[Harness]:
    harness = await _harness(tmp_path, monkeypatch, "personal")
    leaks.watch(harness.built)
    yield harness
    await harness.client.aclose()


@pytest.fixture
async def fed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leaks: LeakGuard
) -> AsyncIterator[Harness]:
    harness = await _harness(tmp_path, monkeypatch, "federal")
    leaks.watch(harness.built)
    yield harness
    await harness.client.aclose()


def _issue(
    h: Harness,
    leaks: LeakGuard,
    scopes: dict[Scope, tuple[str, ...]],
    subjects: tuple[str, ...] = (WILDCARD,),
) -> IssuedPrincipal:
    return leaks.principal(h.app(scopes, subjects))


def _refusals(built: PrincipalApp) -> list[Any]:
    """Every refusal row on the dashboard audit WORM (denied or fail-closed error)."""
    return [row for row in built.worm.written if row.outcome in ("denied", "error")]


async def _one_refusal(
    h: Harness, send: Any, *, status: int, error: Any, actor: str | None = None
) -> Any:
    """Run ``send``; assert the refusal and EXACTLY one new refusal row; return the row."""
    before = len(_refusals(h.built))
    resp = await send()
    assert resp.status_code == status, resp.text
    if isinstance(error, dict):
        assert resp.json() == error
    else:
        assert resp.json()["error"] == error
    rows = _refusals(h.built)[before:]
    assert len(rows) == 1, rows
    if actor is not None:
        assert rows[0].actor_did == actor
    return rows[0]


def _signed(
    leaks: LeakGuard,
    issued: IssuedPrincipal,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    on_behalf_of: str | None = None,
) -> dict[str, str]:
    headers = signed_headers(
        issued, method=method, path=path, body=body, on_behalf_of=on_behalf_of
    )
    return leaks.headers(headers)


# --- ASI07: replayed, rebound and stale signed requests --------------------------------


class TestSignedRequestTampering:
    async def test_replayed_signed_request_refused_once_served(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflow-runs/{run_id}"
        headers = _signed(leaks, app, method="GET", path=path)

        first = await h.client.get(path, headers=headers)
        assert first.status_code == 200, first.text
        row = await _one_refusal(
            h, lambda: h.client.get(path, headers=headers), status=401, error=INVALID
        )
        assert row.actor_did != app.did  # refused before it was bound as the app
        assert "replay" in row.detail

    async def test_signature_over_a_swapped_body_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        app = _issue(h, leaks, {Scope.WORKFLOWS_RUN: (SALES,)})
        path = f"/api/workflows/{SALES_FLOW}/run"
        headers = _signed(leaks, app, method="POST", path=path, body=b'{"urgent":false}')

        await _one_refusal(
            h,
            lambda: h.client.post(path, content=b'{"urgent":true}', headers=headers),
            status=401,
            error=INVALID,
        )
        assert await h.runner.runs.list_for_workflow(SALES_FLOW) == []

    async def test_on_behalf_of_header_swapped_after_signing_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = _issue(h, leaks, {Scope.GATES_RESOLVE: (SALES,)})
        path = _gate_url(gate.id)
        body = json.dumps(APPROVE).encode()
        headers = _signed(
            leaks, app, method="POST", path=path, body=body, on_behalf_of=h.people.viewer
        )
        headers[OBO] = h.people.listed  # the listed approver the app was not signed for

        await _one_refusal(
            h,
            lambda: h.client.post(path, content=body, headers=headers),
            status=401,
            error=INVALID,
        )
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_duplicate_on_behalf_of_header_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        """An intermediary prepends a second X-Arc-On-Behalf-Of. The verifier must
        not sign-check one copy while the route acts on the other."""
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = _issue(h, leaks, {Scope.GATES_RESOLVE: (SALES,)})
        path = _gate_url(gate.id)
        body = json.dumps(APPROVE).encode()
        signed = _signed(
            leaks, app, method="POST", path=path, body=body, on_behalf_of=h.people.viewer
        )
        # The injected copy comes first (what a plain header lookup returns);
        # the signed copy comes last.
        headers = [(OBO, h.people.listed), *signed.items()]

        await _one_refusal(
            h,
            lambda: h.client.post(path, content=body, headers=headers),
            status=401,
            error=INVALID,
        )
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_signature_replayed_on_another_path_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        sales_run = await h.start(SALES_FLOW)
        other_run = await h.start(SALES_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        headers = _signed(leaks, app, method="GET", path=f"/api/workflow-runs/{sales_run}")

        await _one_refusal(
            h,
            lambda: h.client.get(f"/api/workflow-runs/{other_run}", headers=headers),
            status=401,
            error=INVALID,
        )

    async def test_signature_replayed_with_another_method_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        app = _issue(h, leaks, {Scope.WORKFLOWS_RUN: (SALES,), Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflows/{SALES_FLOW}/run"
        headers = _signed(leaks, app, method="GET", path=path)

        await _one_refusal(
            h, lambda: h.client.post(path, headers=headers), status=401, error=INVALID
        )
        assert await h.runner.runs.list_for_workflow(SALES_FLOW) == []

    @pytest.mark.parametrize(
        "offset", [timedelta(seconds=-301), timedelta(seconds=60)], ids=["stale", "future"]
    )
    async def test_timestamp_outside_the_window_refused(
        self, h: Harness, leaks: LeakGuard, offset: timedelta
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflow-runs/{run_id}"
        headers = leaks.headers(
            sign_request(
                app.signing_key,
                principal_did=app.did,
                method="GET",
                path=path,
                query="",
                body=b"",
                on_behalf_of=None,
                ts=(datetime.now(UTC) + offset).isoformat(),
            )
        )

        row = await _one_refusal(
            h, lambda: h.client.get(path, headers=headers), status=401, error=INVALID
        )
        assert "stale_timestamp" in row.detail


# --- ASI03: one app acting as another; revoked credentials -----------------------------


class TestIdentityAbuse:
    async def test_app_a_presenting_app_b_did_with_its_own_key_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        app_a = _issue(h, leaks, {Scope.RUNS_READ: (OLIVIA,)})
        app_b = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflow-runs/{run_id}"
        headers = leaks.headers(
            sign_request(
                app_a.signing_key,
                principal_did=app_b.did,
                method="GET",
                path=path,
                query="",
                body=b"",
                on_behalf_of=None,
            )
        )

        row = await _one_refusal(
            h, lambda: h.client.get(path, headers=headers), status=401, error=INVALID
        )
        assert row.actor_did not in (app_a.did, app_b.did)

    async def test_bearer_reused_after_revoke_refused(self, h: Harness, leaks: LeakGuard) -> None:
        run_id = await h.start(SALES_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflow-runs/{run_id}"
        assert (await h.client.get(path, headers=app.bearer_headers())).status_code == 200

        h.rig.open().revoke(app.record.id, h.people.boss)
        row = await _one_refusal(
            h,
            lambda: h.client.get(path, headers=app.bearer_headers()),
            status=401,
            error=INVALID,
        )
        assert "revoked" in row.detail

    async def test_signed_request_after_revoke_refused(self, h: Harness, leaks: LeakGuard) -> None:
        run_id = await h.start(SALES_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflow-runs/{run_id}"
        h.rig.open().revoke(app.record.id, h.people.boss)

        await _one_refusal(
            h,
            lambda: h.client.get(path, headers=_signed(leaks, app, method="GET", path=path)),
            status=401,
            error=INVALID,
        )


# --- scope escalation and agent binding ------------------------------------------------


class TestScopeEscalation:
    async def test_runs_read_app_calling_the_gate_route_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})

        row = await _one_refusal(
            h,
            lambda: h.client.post(
                _gate_url(gate.id),
                json=APPROVE,
                headers=app.bearer_headers(**{OBO: h.people.boss}),
            ),
            status=403,
            error=SCOPE_DENIED,
        )
        assert row.via_app == app.did
        assert (await h.runner.tasks.get(gate.id)).status == "review"
        assert h.sink.decisions("workflow.gate", gate.id) == []

    @pytest.mark.parametrize("route", ["gate", "task-approve"])
    async def test_app_without_gates_resolve_cannot_resolve_a_gate(
        self, h: Harness, leaks: LeakGuard, route: str
    ) -> None:
        run_id = await h.start(SALES_FLOW)
        gate = await h.gate_task(run_id)
        held = {s: (SALES,) for s in Scope if s is not Scope.GATES_RESOLVE}
        app = _issue(h, leaks, held)
        path = _gate_url(gate.id) if route == "gate" else f"/api/tasks/{gate.id}/approve"

        await _one_refusal(
            h,
            lambda: h.client.post(
                path, json=APPROVE, headers=app.bearer_headers(**{OBO: h.people.boss})
            ),
            status=403,
            error=SCOPE_DENIED,
        )
        assert (await h.runner.tasks.get(gate.id)).status == "review"

    async def test_app_for_a_viewer_cannot_write_tasks(self, h: Harness, leaks: LeakGuard) -> None:
        """REQ-562: tasks:write for the agent, but the person is a viewer — no authority added."""
        app = _issue(h, leaks, {Scope.TASKS_WRITE: (SALES,)})
        body = {"title": "abuse", "owner_did": SALES}

        await _one_refusal(
            h,
            lambda: h.client.post(
                "/api/team/tasks", json=body, headers=app.bearer_headers(**{OBO: h.people.viewer})
            ),
            status=403,
            error=SCOPE_DENIED,
            actor=h.people.viewer,
        )
        assert [t for t in await h.built.app.state.task_store.list() if t.title == "abuse"] == []
        # Control: the same app for an operator person is served.
        served = await h.client.post(
            "/api/team/tasks", json=body, headers=app.bearer_headers(**{OBO: h.people.boss})
        )
        assert served.status_code == 201, served.text


class TestAgentOutsideBinding:
    async def test_inbox_for_another_agent_refused(self, h: Harness, leaks: LeakGuard) -> None:
        app = _issue(h, leaks, {Scope.INBOX_SEND: (SALES,)})

        row = await _one_refusal(
            h,
            lambda: h.client.post(
                "/api/agents/olivia/inbox",
                json={"subject": "s", "body": "b"},
                headers=app.bearer_headers(),
            ),
            status=403,
            error=SCOPE_DENIED,
            actor=app.did,
        )
        assert "inbox:send" in row.detail

    async def test_workflow_run_for_another_agent_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        app = _issue(h, leaks, {Scope.WORKFLOWS_RUN: (SALES,)})

        await _one_refusal(
            h,
            lambda: h.client.post(
                f"/api/workflows/{OLIVIA_FLOW}/run", json={}, headers=app.bearer_headers()
            ),
            status=403,
            error=SCOPE_DENIED,
            actor=app.did,
        )
        assert await h.runner.runs.list_for_workflow(OLIVIA_FLOW) == []

    async def test_run_read_of_another_agents_run_refused(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await h.start(OLIVIA_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        before = len(_refusals(h.built))

        resp = await h.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer_headers())

        assert resp.status_code in (403, 404), resp.text
        assert resp.json()["error"] in (SCOPE_DENIED, "not found")
        assert run_id not in resp.text
        assert len(_refusals(h.built)[before:]) == 1

    async def test_timeline_of_another_agents_run_refused(
        self, h: Harness, leaks: LeakGuard, tmp_path: Path
    ) -> None:
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
        try:
            app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
            own = await h.client.get(
                f"/api/runs/{SALES_TRACE}/timeline", headers=app.bearer_headers()
            )
            assert own.status_code == 200, own.text  # control: the binding is what refuses
            before = len(_refusals(h.built))
            resp = await h.client.get(
                f"/api/runs/{OLIVIA_TRACE}/timeline", headers=app.bearer_headers()
            )
            assert resp.status_code in (403, 404), resp.text
            assert OLIVIA not in resp.text
            assert len(_refusals(h.built)[before:]) == 1
        finally:
            await observe.stop()


# --- forged on-behalf-of (ASI03, ASI09) ------------------------------------------------


class TestForgedOnBehalfOf:
    async def _attempt(self, h: Harness, app: IssuedPrincipal, person: str) -> httpx.Response:
        return await h.client.post(
            f"/api/workflows/{SALES_FLOW}/run",
            json={},
            headers=app.bearer_headers(**{OBO: person}),
        )

    async def test_unknown_disabled_and_unlisted_people_look_identical(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        _, disabled = h.built.sign_in("gone@example.com", (OPERATOR,))
        h.built.users().set_disabled("gone@example.com", True)
        app = _issue(
            h,
            leaks,
            {Scope.WORKFLOWS_RUN: (SALES,)},
            subjects=(h.people.listed, disabled),
        )
        attempts = {
            "unknown": "nobody@example.com",
            "disabled": "gone@example.com",
            "not_allowlisted": "boss@example.com",
        }
        seen: dict[str, tuple[int, str]] = {}
        for reason, person in attempts.items():
            row = await _one_refusal(
                h,
                lambda person=person: self._attempt(h, app, person),
                status=403,
                error=OBO_DENIED,
                actor=app.did,
            )
            assert row.on_behalf_of is None  # a refused person is never recorded as acting
            resp = await self._attempt(h, app, person)
            seen[reason] = (resp.status_code, resp.text)
        assert len(set(seen.values())) == 1, seen
        assert await h.runner.runs.list_for_workflow(SALES_FLOW) == []

    async def test_not_allowlisted_operator_cannot_be_named_even_by_did(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        """An operator outside the allowlist is refused; one inside it is served."""
        _, outsider = h.built.sign_in("ops2@example.com", (OPERATOR,))
        app = _issue(h, leaks, {Scope.WORKFLOWS_RUN: (SALES,)}, subjects=(h.people.boss,))
        await _one_refusal(
            h, lambda: self._attempt(h, app, outsider), status=403, error=OBO_DENIED
        )
        assert await h.runner.runs.list_for_workflow(SALES_FLOW) == []
        # Control: the allowlisted operator is served, so the allowlist is what refused.
        served = await self._attempt(h, app, h.people.boss)
        assert served.status_code == 201, served.text


# --- hard-denied routes: whatever scopes the app holds ----------------------------------


HARD_DENIED_CALLS = (
    ("GET", "/api/approvals"),
    ("POST", "/api/approvals/appr-1/approve"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("GET", "/api/principals"),
    ("POST", "/api/principals"),
    ("POST", "/api/principals/p-1/revoke"),
    ("GET", "/api/me/delegations"),
    ("POST", "/api/me/delegations"),
    ("GET", "/api/standing-grants"),
    ("GET", "/api/trust"),
    ("POST", "/api/workflows/sales_flow/request-signature"),
)


class TestHardDeniedRoutes:
    @pytest.mark.parametrize(("method", "path"), HARD_DENIED_CALLS)
    async def test_app_with_every_scope_refused(
        self, h: Harness, leaks: LeakGuard, method: str, path: str
    ) -> None:
        app = _issue(h, leaks, all_scopes((WILDCARD,)))
        await _one_refusal(
            h,
            lambda: h.client.request(
                method, path, json={}, headers=app.bearer_headers(**{OBO: h.people.boss})
            ),
            status=403,
            error=ROUTE_NOT_ALLOWED,
            actor=app.did,
        )

    async def test_app_cannot_approve_its_own_pending_issuance(
        self, fed: Harness, leaks: LeakGuard
    ) -> None:
        """Federal issuance waits for a second operator; the app may not be that operator."""
        app = leaks.principal(
            issue_principal(
                fed.rig.open(), scopes=all_scopes((SALES,)), subjects=(fed.people.boss,)
            )
        )
        path = "/api/approvals/any-approval/approve"
        await _one_refusal(
            fed,
            lambda: fed.client.post(path, headers=_signed(leaks, app, method="POST", path=path)),
            status=403,
            error=ROUTE_NOT_ALLOWED,
            actor=app.did,
        )


# --- federal: deciding needs the person's own delegation ---------------------------------


class TestFederalDelegation:
    async def test_federal_gate_decision_without_delegation_refused(
        self, fed: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await fed.start(SALES_FLOW)
        gate = await fed.gate_task(run_id)
        app = leaks.principal(
            issue_principal(
                fed.rig.open(),
                scopes={Scope.GATES_RESOLVE: (SALES,)},
                subjects=(fed.people.listed,),
            )
        )
        path = _gate_url(gate.id)
        body = json.dumps(APPROVE).encode()

        row = await _one_refusal(
            fed,
            lambda: fed.client.post(
                path,
                content=body,
                headers=_signed(
                    leaks, app, method="POST", path=path, body=body, on_behalf_of=fed.people.listed
                ),
            ),
            status=403,
            error="delegation_required",
        )
        assert (row.via_app, row.on_behalf_of) == (app.did, fed.people.listed)
        assert (await fed.runner.tasks.get(gate.id)).status == "review"
        assert fed.sink.decisions("workflow.gate.resolved", gate.id) == []

    async def test_federal_bearer_without_signature_refused(
        self, fed: Harness, leaks: LeakGuard
    ) -> None:
        run_id = await fed.start(SALES_FLOW)
        app = leaks.principal(
            issue_principal(
                fed.rig.open(), scopes={Scope.RUNS_READ: (SALES,)}, subjects=(fed.people.boss,)
            )
        )
        await _one_refusal(
            fed,
            lambda: fed.client.get(f"/api/workflow-runs/{run_id}", headers=app.bearer_headers()),
            status=401,
            error=INVALID,
        )


# --- rollback of the principal authority (REQ-556) ---------------------------------------


class TestRollback:
    async def test_previous_revision_restored_after_revoke_still_refuses(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        """One revision back is crash recovery: the store re-adopts the anchored head."""
        run_id = await h.start(SALES_FLOW)
        app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        path = f"/api/workflow-runs/{run_id}"
        before_revoke = h.rig.path.read_bytes()
        h.rig.open().revoke(app.record.id, h.people.boss)
        h.rig.path.write_bytes(before_revoke)

        row = await _one_refusal(
            h, lambda: h.client.get(path, headers=app.bearer_headers()), status=401, error=INVALID
        )
        assert "revoked" in row.detail

    async def test_older_file_restored_fails_closed_for_every_principal(
        self, h: Harness, leaks: LeakGuard
    ) -> None:
        """Two or more revisions back is an unexplained rollback: 503 for every app."""
        run_id = await h.start(SALES_FLOW)
        old_app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        snapshot = h.rig.path.read_bytes()
        new_app = _issue(h, leaks, {Scope.RUNS_READ: (SALES,)})
        h.rig.open().revoke(old_app.record.id, h.people.boss)
        h.rig.path.write_bytes(snapshot)
        path = f"/api/workflow-runs/{run_id}"

        for app in (old_app, new_app):
            await _one_refusal(
                h,
                lambda app=app: h.client.get(path, headers=app.bearer_headers()),
                status=503,
                error=UNAVAILABLE,
            )
            await _one_refusal(
                h,
                lambda app=app: h.client.get(
                    path, headers=_signed(leaks, app, method="GET", path=path)
                ),
                status=503,
                error=UNAVAILABLE,
            )
        # Human sessions are a separate authority and keep working.
        session = await h.client.get(
            path, headers={"Authorization": f"Bearer {h.people.boss_token}"}
        )
        assert session.status_code == 200, session.text


# --- unbounded consumption and authority outage -----------------------------------------


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, PrincipalRig, Path]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    users = user_store_factory(tmp_path / "accounts")
    rig = principal_rig(tmp_path / "authority")
    team = write_team(tmp_path / "team", {"sales": SALES, "olivia": OLIVIA})
    return users, rig, team


def _sync_one_refusal(built: PrincipalApp, send: Any, status: int, error: Any) -> None:
    before = len(_refusals(built))
    resp = send()
    assert resp.status_code == status, resp.text
    assert resp.json() == error
    assert len(_refusals(built)[before:]) == 1


class TestConsumptionAndOutage:
    def test_rate_limit_exhaustion_is_429_with_one_row(
        self, tmp_path: Path, isolated: tuple[Any, PrincipalRig, Path], leaks: LeakGuard
    ) -> None:
        from starlette.testclient import TestClient

        users, rig, team = isolated
        built = build_principal_app(
            tmp_path,
            principal_store_factory=rig.factory,
            users=users,
            team_root=team,
            tier="personal",
            rate_per_minute=2,
        )
        leaks.watch(built)
        app = leaks.principal(issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}))
        other = leaks.principal(issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}))
        client = TestClient(built.app, raise_server_exceptions=False)
        path = "/api/agents/olivia/inbox"
        for _ in range(2):
            served = client.post(path, json={}, headers=app.bearer_headers())
            assert served.status_code != 429, served.text

        _sync_one_refusal(
            built,
            lambda: client.post(path, json={}, headers=app.bearer_headers()),
            429,
            {"error": "rate_limited"},
        )
        # The window is per principal: another app is not starved by this one.
        assert client.post(path, json={}, headers=other.bearer_headers()).status_code != 429

    def test_authority_unreachable_is_503_never_a_static_token_fallback(
        self, tmp_path: Path, isolated: tuple[Any, PrincipalRig, Path], leaks: LeakGuard
    ) -> None:
        from starlette.testclient import TestClient

        users, rig, team = isolated
        app = leaks.principal(issue_principal(rig.open(), scopes=all_scopes((WILDCARD,))))

        def sealed_vault() -> Any:
            raise RuntimeError("vault sealed")

        built = build_principal_app(
            tmp_path,
            principal_store_factory=sealed_vault,
            users=users,
            team_root=team,
            tier="personal",
        )
        leaks.watch(built)
        client = TestClient(built.app, raise_server_exceptions=False)
        path = "/api/agents/olivia/inbox"
        _sync_one_refusal(
            built,
            lambda: client.post(path, json={}, headers=app.bearer_headers()),
            503,
            UNAVAILABLE,
        )
        signed = leaks.headers(signed_headers(app, method="POST", path=path, body=b"{}"))
        _sync_one_refusal(
            built, lambda: client.post(path, content=b"{}", headers=signed), 503, UNAVAILABLE
        )
        # Human sessions and the static token are a separate path and keep working.
        token, did = built.sign_in("boss@example.com", (OPERATOR,))
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["did"] == did
        operator = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}
        )
        assert operator.status_code == 200, operator.text
