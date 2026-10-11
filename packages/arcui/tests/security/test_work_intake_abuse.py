"""SPEC-088 T-1375: work intake abuse — the attacker's view of every new route (COMP-017).

REQ-632 (forged events, oversized payloads, metadata spoofing of workflow keys,
cross-principal run reads), REQ-590 (caller run_purpose), REQ-594/595/598
(scope on every read and write, 404 without an existence oracle), REQ-600
(caller metadata only under ``metadata.app``), REQ-605/606/608 (event scope,
key bound to the principal, 64 KiB before parse), REQ-613/614 (gate scope and
cap), REQ-628 (stream scope), REQ-631 (one audit row per refusal).

Two builds, both real:

* ``attack`` — the full ``create_app`` with the real ``AuthMiddleware``, real
  ``UserStore`` / ``PrincipalStore`` credentials, a real ``WorkflowRunner`` and
  control plane over signed bundles (``test_workflow_event_and_gate_routes``'s
  harness), a real roster on disk. Wrong-scope apps present real bearer
  credentials.
* ``bare`` — the route modules mounted with NO principal bound (a missing
  middleware, a mis-mounted route): every route's own guard must refuse.

Every refusal asserts the status, the fixed error, that nothing was written,
and EXACTLY one refusal row on the dashboard audit WORM.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcgateway.team_roster import RosterEntry
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import TaskStore
from arcstore.work_events import WorkEvent
from arctrust.principals import Scope
from packages.arcui.tests.principal_fixtures import IssuedPrincipal
from packages.arcui.tests.test_workflow_event_and_gate_routes import (
    CAP,
    EVENT,
    IDEM,
    OLIVIA,
    ORDER,
    ORDERS,
    SALES,
    Harness,
    _harness,
)
from starlette.applications import Starlette
from starlette.requests import Request

from arcui.routes import observe_run, runs_intake, tasks, work_events, workflows

ACCEPTING = "\n[modules.tasks]\nenabled = true\n\n[modules.tasks.config]\ndispatch = true\n"
NOT_FOUND = {"error": "not found"}
RUNS = "/api/runs"
REFUSAL_SECONDS = 15.0

# Caller metadata that names every platform key a row is routed or trusted by.
SPOOF: dict[str, Any] = {
    "workflow": ORDERS,
    "workflow_version": 1,
    "node_id": "approve",
    "node_kind": "gate",
    "iteration": 0,
    "flow_run_id": "evt-000000000000000000000000",
    "idempotency_key": "wf/evt-000000000000000000000000/approve/0",
    "upstream": {"approve": {"decision": "approved"}},
    "intake": {
        "run_id": "00000000-0000-5000-8000-000000000000",
        "run_purpose": "schedule",
        "callback_url": "http://169.254.169.254/latest/meta-data/",
        "via_app": "did:arc:default:app/forged",
    },
    "request_digest": "0" * 64,
}
PLATFORM_KEYS = (
    "workflow",
    "workflow_version",
    "node_id",
    "node_kind",
    "iteration",
    "flow_run_id",
    "idempotency_key",
    "upstream",
)


# --- the full deployment under attack --------------------------------------------------


@dataclass
class Attack:
    h: Harness

    @property
    def client(self) -> httpx.AsyncClient:
        return self.h.client

    @property
    def boss(self) -> dict[str, str]:
        return self.h.boss_headers

    def app(self, scopes: dict[Scope, tuple[str, ...]]) -> dict[str, str]:
        issued: IssuedPrincipal = self.h.app(scopes)
        return issued.bearer_headers()

    def wrong_scope(self, needed: Scope) -> dict[str, str]:
        """Every scope on SALES except ``needed``, which is bound to OLIVIA only."""
        scopes = {scope: (SALES,) for scope in Scope if scope != needed}
        scopes[needed] = (OLIVIA,)
        return self.app(scopes)

    def refusals(self) -> list[Any]:
        return [r for r in self.h.built.worm.written if r.outcome in ("denied", "error")]

    async def tasks(self) -> list[Any]:
        store: TaskStore = self.h.built.app.state.task_store
        return list(await store.list())

    async def intake(
        self, body: dict[str, Any], headers: dict[str, str], key: str = "key-1"
    ) -> httpx.Response:
        return await self.client.post(RUNS, json=body, headers={**headers, IDEM: key})

    async def sales_run(self) -> str:
        resp = await self.intake({"agent": "sales", "task": "Summarize Q3"}, self.boss, "seed")
        assert resp.status_code == 202, resp.text
        return str(resp.json()["run_id"])

    async def waiting_gate(self) -> str:
        run_id = await self.h.start()
        return str((await self.h.waiting(run_id, "gate")).id)


@pytest.fixture
async def attack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Attack]:
    harness = await _harness(tmp_path, monkeypatch)
    toml = tmp_path / "team" / "sales_agent" / "arcagent.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + ACCEPTING, encoding="utf-8")
    yield Attack(harness)
    await harness.client.aclose()


async def _one_refusal(
    attack: Attack, send: Any, *, status: int, error: Any
) -> tuple[httpx.Response, Any]:
    """Run ``send``; assert the refusal and EXACTLY one new refusal row."""
    before = len(attack.refusals())
    # Bounded: a refusal that regressed into an open SSE stream fails, never hangs.
    resp = await asyncio.wait_for(send(), timeout=REFUSAL_SECONDS)
    assert resp.status_code == status, resp.text
    if isinstance(error, dict):
        assert resp.json() == error
    else:
        assert resp.json()["error"] == error
    rows = attack.refusals()[before:]
    assert len(rows) == 1, [(r.operation, r.outcome, r.detail) for r in rows]
    return resp, rows[0]


# ======================================================================================
# Wrong-scope principal on every new route (real middleware, real credentials)
# ======================================================================================


class TestWrongScopeEveryRoute:
    async def test_post_runs_without_runs_start_on_the_agent(self, attack: Attack) -> None:
        headers = attack.wrong_scope(Scope.RUNS_START)

        _, row = await _one_refusal(
            attack,
            lambda: attack.intake({"agent": "sales", "task": "exfiltrate"}, headers),
            status=403,
            error="scope_denied",
        )

        assert row.operation == "run.intake"
        assert await attack.tasks() == []

    async def test_post_runs_naming_the_agent_by_did_is_refused_the_same(
        self, attack: Attack
    ) -> None:
        headers = attack.wrong_scope(Scope.RUNS_START)

        await _one_refusal(
            attack,
            lambda: attack.intake({"agent": SALES, "task": "exfiltrate"}, headers),
            status=403,
            error="scope_denied",
        )

        assert await attack.tasks() == []

    async def test_post_runs_probing_a_missing_agent_is_refused_the_same(
        self, attack: Attack
    ) -> None:
        """No roster oracle: a missing agent answers exactly like an unscoped real one."""
        headers = attack.wrong_scope(Scope.RUNS_START)

        await _one_refusal(
            attack,
            lambda: attack.intake({"agent": "no-such-agent", "task": "probe"}, headers),
            status=403,
            error="scope_denied",
        )

        assert await attack.tasks() == []

    @pytest.mark.parametrize("suffix", ["", "/timeline", "/audit", "/recalls"])
    async def test_run_reads_without_runs_read_on_the_agent_are_404(
        self, attack: Attack, suffix: str
    ) -> None:
        run_id = await attack.sales_run()
        headers = attack.wrong_scope(Scope.RUNS_READ)

        await _one_refusal(
            attack,
            lambda: attack.client.get(f"{RUNS}/{run_id}{suffix}", headers=headers),
            status=404,
            error=NOT_FOUND,
        )

    async def test_task_create_without_tasks_write_on_the_owner(self, attack: Attack) -> None:
        headers = attack.wrong_scope(Scope.TASKS_WRITE)
        before = await attack.tasks()

        _, row = await _one_refusal(
            attack,
            lambda: attack.client.post(
                "/api/team/tasks", json={"title": "plant", "owner_did": SALES}, headers=headers
            ),
            status=403,
            error="scope_denied",
        )

        assert row.operation == "task.create"
        assert await attack.tasks() == before

    async def test_task_create_with_no_owner_binds_no_agent_and_is_refused(
        self, attack: Attack
    ) -> None:
        headers = attack.app({Scope.TASKS_WRITE: (SALES,)})

        await _one_refusal(
            attack,
            lambda: attack.client.post(
                "/api/team/tasks", json={"title": "orphan"}, headers=headers
            ),
            status=403,
            error="scope_denied",
        )

    async def test_forged_event_without_workflows_event_on_the_owner(self, attack: Attack) -> None:
        headers = attack.wrong_scope(Scope.WORKFLOWS_EVENT)

        _, row = await _one_refusal(
            attack,
            lambda: attack.h.fire(headers=headers),
            status=403,
            error="scope_denied",
        )

        assert row.operation == "workflow.event.fire"
        assert await attack.h.run_ids() == set()

    async def test_event_holding_workflows_run_but_not_workflows_event(
        self, attack: Attack
    ) -> None:
        """A scope for the manual run route never admits the event route."""
        headers = attack.app({Scope.WORKFLOWS_RUN: (SALES,), Scope.RUNS_READ: (SALES,)})

        await _one_refusal(
            attack, lambda: attack.h.fire(headers=headers), status=403, error="scope_denied"
        )

        assert await attack.h.run_ids() == set()

    async def test_event_on_an_unknown_workflow_is_404_to_an_app(self, attack: Attack) -> None:
        headers = attack.app({Scope.WORKFLOWS_EVENT: (SALES,)})

        await _one_refusal(
            attack,
            lambda: attack.h.fire(workflow="no_such_flow", headers=headers),
            status=404,
            error="not found",
        )

    async def test_gate_without_gates_resolve_on_the_owner(self, attack: Attack) -> None:
        task_id = await attack.waiting_gate()
        headers = attack.wrong_scope(Scope.GATES_RESOLVE)

        _, row = await _one_refusal(
            attack,
            lambda: attack.client.post(
                f"/api/workflow-tasks/{task_id}/gate",
                json={"decision": "approve", "payload": {"qty": 1}},
                headers=headers,
            ),
            status=403,
            error="scope_denied",
        )

        assert row.operation == "gate.resolve"
        gate = await attack.h.runner.tasks.get(task_id)
        assert gate is not None and gate.status == "review"

    @pytest.mark.parametrize(
        "path", ["/api/work-events/stream", "/api/work-events", "/api/work-events/keys"]
    )
    async def test_work_events_without_events_subscribe_on_the_agent(
        self, attack: Attack, path: str
    ) -> None:
        headers = attack.wrong_scope(Scope.EVENTS_SUBSCRIBE)

        await _one_refusal(
            attack,
            lambda: attack.client.get(path, params={"agent": SALES}, headers=headers),
            status=403,
            error="scope_denied",
        )

    @pytest.mark.parametrize(
        "path", ["/api/work-events/stream", "/api/work-events", "/api/work-events/keys"]
    )
    async def test_work_events_from_an_app_that_names_no_agent(
        self, attack: Attack, path: str
    ) -> None:
        headers = attack.app({Scope.EVENTS_SUBSCRIBE: (SALES,)})

        await _one_refusal(
            attack,
            lambda: attack.client.get(path, headers=headers),
            status=403,
            error="scope_denied",
        )

    async def test_work_event_page_never_returns_another_agents_events(
        self, attack: Attack
    ) -> None:
        log = attack.h.built.app.state.work_event_log
        for agent in (SALES, OLIVIA):
            assert await log.append(
                WorkEvent(
                    event_id=f"evt-{uuid.uuid4().hex}",
                    kind="run.finished",
                    occurred_at="2026-10-10T12:00:00+00:00",
                    agent_did=agent,
                    run_id=str(uuid.uuid4()),
                    status="done",
                    classification="UNCLASSIFIED",
                )
            )
        headers = attack.app({Scope.EVENTS_SUBSCRIBE: (OLIVIA,)})

        resp = await attack.client.get(
            "/api/work-events", params={"agent": OLIVIA}, headers=headers
        )

        assert resp.status_code == 200, resp.text
        agents = {event["agent_did"] for event in resp.json()["events"]}
        assert agents == {OLIVIA}


# ======================================================================================
# Missing principal: each route's own guard refuses (no middleware bound one)
# ======================================================================================


@dataclass
class ObserveSpy:
    """Stands in for the arcstore run mirror; records every read a refusal must prevent."""

    reads: list[str] = field(default_factory=list)

    async def timeline(self, run_id: str) -> list[dict[str, Any]]:
        self.reads.append(f"timeline:{run_id}")
        return [{"agent_did": SALES, "kind": "llm", "run_id": run_id}]

    async def run_audit(self, run_id: str) -> list[dict[str, Any]]:
        self.reads.append(f"audit:{run_id}")
        return [{"run_id": run_id, "action": "secret"}]

    async def run_recalls(self, run_id: str) -> list[dict[str, Any]]:
        self.reads.append(f"recalls:{run_id}")
        return [{"run_id": run_id, "memory": "secret"}]


@dataclass
class Bare:
    client: httpx.AsyncClient
    worm: Any
    observe: ObserveSpy
    store: TaskStore

    def rows(self) -> list[Any]:
        return list(self.worm.written)


def _roster_row(team: Path) -> RosterEntry:
    folder = team / "sales_agent"
    (folder / "workspace").mkdir(parents=True, exist_ok=True)
    (folder / "arcagent.toml").write_text(
        f'[agent]\nname = "sales"\norg = "default"\ntype = "executor"\n\n'
        f'[identity]\ndid = "{SALES}"\n{ACCEPTING}',
        encoding="utf-8",
    )
    return RosterEntry(
        agent_id="sales",
        name="sales",
        did=SALES,
        org="default",
        type="executor",
        workspace_path=str(folder),
        model=None,
        provider=None,
        online=True,
        display_name="sales",
        color="#000000",
        role_label="",
        hidden=False,
    )


@pytest.fixture
async def bare(tmp_path: Path, principal_factory: Any) -> AsyncIterator[Bare]:
    app = Starlette(
        routes=[
            *runs_intake.routes,
            *observe_run.routes,
            *tasks.routes,
            *workflows.routes,
            *work_events.routes,
        ]
    )
    worm = principal_factory.install(app)  # binds whoever act_as names: here nobody
    backend = FakeBackend()
    await backend.start()
    store = TaskStore(backend)
    observe = ObserveSpy()
    entry = _roster_row(tmp_path / "team")
    app.state.task_store = store
    app.state.observe = observe
    app.state.roster_provider = lambda: [entry]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bare")
    yield Bare(client=client, worm=worm, observe=observe, store=store)
    await client.aclose()


def _one_bare_refusal(bare: Bare, before: int) -> Any:
    rows = bare.rows()[before:]
    assert len(rows) == 1, [(r.operation, r.outcome, r.detail) for r in rows]
    assert rows[0].outcome == "denied"
    return rows[0]


RUN_ID = "11111111-2222-5333-8444-555555555555"

BARE_WRITES = [
    ("POST", RUNS, {"agent": "sales", "task": "no principal"}),
    ("POST", "/api/team/tasks", {"title": "no principal", "owner_did": SALES}),
    ("POST", f"/api/workflows/{ORDERS}/events/{EVENT}", ORDER),
    ("POST", "/api/workflow-tasks/wf/run/approve/0/gate", {"decision": "approve"}),
]
BARE_READS = [
    f"{RUNS}/{RUN_ID}",
    f"{RUNS}/{RUN_ID}/timeline",
    f"{RUNS}/{RUN_ID}/audit",
    f"{RUNS}/{RUN_ID}/recalls",
    f"/api/work-events/stream?agent={SALES}",
    f"/api/work-events?agent={SALES}",
    f"/api/work-events/keys?agent={SALES}",
]


class TestMissingPrincipalEveryRoute:
    @pytest.mark.parametrize(("method", "path", "body"), BARE_WRITES)
    async def test_write_with_no_principal_is_refused_with_one_row(
        self, bare: Bare, method: str, path: str, body: dict[str, Any]
    ) -> None:
        before = len(bare.rows())

        resp = await asyncio.wait_for(
            bare.client.request(method, path, json=body, headers={IDEM: "k-1"}),
            timeout=REFUSAL_SECONDS,
        )

        assert resp.status_code == 403, resp.text
        _one_bare_refusal(bare, before)
        assert await bare.store.list() == []

    @pytest.mark.parametrize("path", BARE_READS)
    async def test_read_with_no_principal_is_refused_with_one_row(
        self, bare: Bare, path: str
    ) -> None:
        before = len(bare.rows())

        resp = await asyncio.wait_for(bare.client.get(path), timeout=REFUSAL_SECONDS)

        assert resp.status_code in (403, 404), resp.text
        assert resp.status_code != 200
        _one_bare_refusal(bare, before)
        # Nothing the refusal guards may be read (the timeline route reads its
        # trace to find the agent, so only that lookup is allowed).
        assert [r for r in bare.observe.reads if not r.startswith("timeline:")] == []


# ======================================================================================
# Cross-principal run reads: 404, byte-identical to a run that does not exist
# ======================================================================================


class TestCrossPrincipalReads:
    @pytest.mark.parametrize("suffix", ["", "/timeline", "/audit", "/recalls"])
    async def test_other_agents_run_reads_exactly_like_a_missing_run(
        self, attack: Attack, suffix: str
    ) -> None:
        run_id = await attack.sales_run()
        missing = str(uuid.uuid4())
        outsider = attack.app({Scope.RUNS_READ: (OLIVIA,), Scope.RUNS_START: (OLIVIA,)})

        exists, _ = await _one_refusal(
            attack,
            lambda: attack.client.get(f"{RUNS}/{run_id}{suffix}", headers=outsider),
            status=404,
            error=NOT_FOUND,
        )
        absent, _ = await _one_refusal(
            attack,
            lambda: attack.client.get(f"{RUNS}/{missing}{suffix}", headers=outsider),
            status=404,
            error=NOT_FOUND,
        )

        assert exists.content == absent.content
        assert exists.headers.get("content-type") == absent.headers.get("content-type")

    async def test_owner_scope_reads_the_run(self, attack: Attack) -> None:
        """Control: the 404s above are the scope check, not a broken run."""
        run_id = await attack.sales_run()
        reader = attack.app({Scope.RUNS_READ: (SALES,)})

        resp = await attack.client.get(f"{RUNS}/{run_id}", headers=reader)

        assert resp.status_code == 200, resp.text
        assert resp.json()["agent_did"] == SALES

    async def test_another_app_reusing_an_idempotency_key_gets_its_own_run(
        self, attack: Attack
    ) -> None:
        first = attack.app({Scope.RUNS_START: (SALES,), Scope.RUNS_READ: (SALES,)})
        second = attack.app({Scope.RUNS_START: (SALES,)})

        mine = await attack.intake({"agent": "sales", "task": "mine"}, first, key="shared")
        theirs = await attack.intake({"agent": "sales", "task": "theirs"}, second, key="shared")

        assert mine.status_code == 202, mine.text
        assert theirs.status_code == 202, theirs.text
        assert mine.json()["run_id"] != theirs.json()["run_id"]
        read = await attack.client.get(f"{RUNS}/{mine.json()['run_id']}", headers=first)
        assert read.json()["status"] == "queued"
        row = await attack.h.built.app.state.task_store.get(f"intake-{mine.json()['run_id']}")
        assert row is not None and row.description == "mine"


# ======================================================================================
# Metadata spoofing: workflow and intake keys land under metadata.app only
# ======================================================================================


class TestMetadataSpoofing:
    async def test_intake_metadata_spoof_lands_under_metadata_app_only(
        self, attack: Attack
    ) -> None:
        starter = attack.app({Scope.RUNS_START: (SALES,), Scope.RUNS_READ: (SALES,)})

        resp = await attack.intake({"agent": "sales", "task": "spoof", "metadata": SPOOF}, starter)

        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]
        row = await attack.h.built.app.state.task_store.get(f"intake-{run_id}")
        assert row is not None
        assert row.metadata["app"] == SPOOF
        for key in PLATFORM_KEYS:
            assert key not in row.metadata, key
        assert row.metadata["intake"]["run_id"] == run_id
        assert row.metadata["intake"]["run_purpose"] == "manual"
        assert row.metadata["intake"]["callback_url"] is None
        assert row.metadata["intake"]["via_app"] is None
        assert row.metadata["request_digest"] != SPOOF["request_digest"]
        read = await attack.client.get(f"{RUNS}/{run_id}", headers=starter)
        assert read.json()["run_purpose"] == "manual"
        assert read.json()["metadata"] == SPOOF

    async def test_spoofed_intake_row_is_never_a_workflow_node(self, attack: Attack) -> None:
        resp = await attack.intake(
            {"agent": "sales", "task": "spoof", "metadata": SPOOF}, attack.boss
        )
        run_id = resp.json()["run_id"]
        task_id = f"intake-{run_id}"

        nodes = await attack.h.runner.tasks.query_by_flow_run(SPOOF["flow_run_id"])
        gate = await attack.client.post(
            f"/api/workflow-tasks/{task_id}/gate",
            json={"decision": "approve", "payload": {"qty": 1}},
            headers=attack.boss,
        )

        assert task_id not in {n.id for n in nodes}
        assert 400 <= gate.status_code < 500, gate.text
        row = await attack.h.built.app.state.task_store.get(task_id)
        assert row is not None and row.status == "todo" and row.output is None

    async def test_task_create_metadata_spoof_lands_under_metadata_app_only(
        self, attack: Attack
    ) -> None:
        writer = attack.app({Scope.TASKS_WRITE: (SALES,)})
        forged_run = SPOOF["intake"]["run_id"]

        resp = await attack.client.post(
            "/api/team/tasks",
            json={"title": "spoof", "owner_did": SALES, "tags": ["intake"], "metadata": SPOOF},
            headers=writer,
        )

        assert resp.status_code == 201, resp.text
        created = await attack.h.built.app.state.task_store.get(resp.json()["id"])
        assert created is not None
        assert created.metadata == {"app": SPOOF}
        # Not readable as an intake run, not a node of any run, not a gate.
        run_read = await attack.client.get(f"{RUNS}/{forged_run}", headers=attack.boss)
        nodes = await attack.h.runner.tasks.query_by_flow_run(SPOOF["flow_run_id"])
        gate = await attack.client.post(
            f"/api/workflow-tasks/{created.id}/gate",
            json={"decision": "approve"},
            headers=attack.boss,
        )
        assert run_read.status_code == 404
        assert created.id not in {n.id for n in nodes}
        assert 400 <= gate.status_code < 500, gate.text
        after = await attack.h.built.app.state.task_store.get(created.id)
        assert after is not None and after.status == created.status

    async def test_intake_refuses_platform_fields_at_the_top_level(self, attack: Attack) -> None:
        """Only the documented fields are accepted; a top-level spoof is a 400, not a merge."""
        body = {"agent": "sales", "task": "spoof", "node_id": "approve", "flow_run_id": "x"}

        await _one_refusal(
            attack,
            lambda: attack.intake(body, attack.boss),
            status=400,
            error="invalid_request",
        )

        assert await attack.tasks() == []


# ======================================================================================
# Oversized bodies: 413 before any JSON parse
# ======================================================================================


@dataclass
class ParseSpy:
    sizes: list[int] = field(default_factory=list)
    request_json: list[str] = field(default_factory=list)


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> Iterator[ParseSpy]:
    """Wraps (never replaces) every JSON parse an oversized body could reach."""
    spy = ParseSpy()
    real_loads = json.loads
    real_request_json = Request.json

    def loads(s: Any, *args: Any, **kwargs: Any) -> Any:
        spy.sizes.append(len(s))
        return real_loads(s, *args, **kwargs)

    async def request_json(self: Request) -> Any:
        spy.request_json.append(self.url.path)
        return await real_request_json(self)

    monkeypatch.setattr(json, "loads", loads)
    monkeypatch.setattr(Request, "json", request_json)
    yield spy


def _big(size: int) -> bytes:
    """A syntactically valid JSON object of at least ``size`` bytes."""
    return json.dumps({"agent": "sales", "task": "x", "pad": "y" * size}).encode()


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    """The body as a stream with no Content-Length (a client that never declares one)."""
    for start in range(0, len(payload), 8192):
        yield payload[start : start + 8192]


class TestOversizedBodies:
    @pytest.mark.parametrize("declared", [True, False])
    async def test_intake_body_over_64_kib_is_413_before_parse(
        self, attack: Attack, parses: ParseSpy, declared: bool
    ) -> None:
        payload = _big(CAP + 1)
        content: Any = payload if declared else _chunks(payload)
        headers = {**attack.boss, IDEM: "big", "Content-Type": "application/json"}

        _, row = await _one_refusal(
            attack,
            lambda: attack.client.post(RUNS, content=content, headers=headers),
            status=413,
            error="body_too_large",
        )

        assert row.operation == "run.intake"
        assert not [n for n in parses.sizes if n > CAP], "an oversized body reached a parse"
        assert await attack.tasks() == []

    async def test_intake_metadata_over_16_kib_is_413(self, attack: Attack) -> None:
        body = {"agent": "sales", "task": "x", "metadata": {"pad": "z" * (16 * 1024)}}

        await _one_refusal(
            attack,
            lambda: attack.intake(body, attack.boss),
            status=413,
            error="metadata_too_large",
        )

        assert await attack.tasks() == []

    @pytest.mark.parametrize("declared", [True, False])
    async def test_event_body_over_64_kib_is_413_before_parse(
        self, attack: Attack, parses: ParseSpy, declared: bool
    ) -> None:
        payload = json.dumps({"order_id": "A-1", "qty": 1, "pad": "y" * CAP}).encode()
        content: Any = payload if declared else _chunks(payload)
        headers = {**attack.boss, IDEM: "big", "Content-Type": "application/json"}

        _, row = await _one_refusal(
            attack,
            lambda: attack.client.post(
                f"/api/workflows/{ORDERS}/events/{EVENT}", content=content, headers=headers
            ),
            status=413,
            error="payload_too_large",
        )

        assert row.operation == "workflow.event.fire"
        assert not [n for n in parses.sizes if n > CAP], "an oversized event reached a parse"
        assert parses.request_json == []
        assert await attack.h.run_ids() == set()

    async def test_gate_body_far_over_the_cap_is_413_before_parse(
        self, attack: Attack, parses: ParseSpy
    ) -> None:
        task_id = await attack.waiting_gate()
        payload = json.dumps(
            {"decision": "approve", "payload": {"qty": 1, "memo": "m" * (3 * CAP)}}
        ).encode()
        headers = {**attack.boss, "Content-Type": "application/json"}

        await _one_refusal(
            attack,
            lambda: attack.client.post(
                f"/api/workflow-tasks/{task_id}/gate", content=_chunks(payload), headers=headers
            ),
            status=413,
            error="payload_too_large",
        )

        assert not [n for n in parses.sizes if n > 2 * CAP]
        gate = await attack.h.runner.tasks.get(task_id)
        assert gate is not None and gate.status == "review"

    async def test_gate_payload_over_64_kib_is_413_and_the_gate_keeps_waiting(
        self, attack: Attack
    ) -> None:
        task_id = await attack.waiting_gate()

        await _one_refusal(
            attack,
            lambda: attack.client.post(
                f"/api/workflow-tasks/{task_id}/gate",
                json={"decision": "approve", "payload": {"qty": 1, "memo": "m" * CAP}},
                headers=attack.boss,
            ),
            status=413,
            error="payload_too_large",
        )

        gate = await attack.h.runner.tasks.get(task_id)
        assert gate is not None and gate.status == "review"


# ======================================================================================
# A caller may not claim a platform run purpose
# ======================================================================================


class TestRunPurpose:
    @pytest.mark.parametrize("purpose", ["schedule", "pulse", "workflow", "SCHEDULE", ""])
    async def test_platform_run_purpose_is_refused(self, attack: Attack, purpose: str) -> None:
        body = {"agent": "sales", "task": "pretend to be cron", "run_purpose": purpose}

        _, row = await _one_refusal(
            attack, lambda: attack.intake(body, attack.boss), status=400, error="invalid_request"
        )

        assert row.operation == "run.intake"
        assert await attack.tasks() == []
