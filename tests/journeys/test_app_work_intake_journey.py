"""SPEC-088 T-1378: journey — an app uses only the new routes, end to end, for a person.

PRD Success Metric 1: an app built only on these routes starts a run, files a dependent
task, fires an event, resolves a gate with a payload and receives a signed callback,
with zero ``did:arc:ui:operator`` attributions in its audit trail.
Requirements: REQ-588, REQ-605, REQ-611, REQ-623, REQ-631 (COMP-001, 004, 011, 013).

What a user does, only through public HTTP routes:

0. an operator signs in (``POST /api/auth/login``), adds the app's callback origin
   (``POST /api/work-events/callbacks``, allow_private at personal tier) and issues an
   app principal bound to the agent (``POST /api/principals``) for one person;
1. the app starts a run for that person (``POST /api/runs`` -> 202); the agent's real
   tasks dispatch loop runs the intake row under that ``run_id`` through
   ``ArcAgent.run_collected``; ``GET /api/runs/{id}`` shows it done with the result; a
   signed ``run.finished`` callback reaches a real local HTTP receiver and verifies
   against ``GET /api/work-events/keys`` (``Arc-Key-Id`` matches);
2. the app files a dependent task (``parent_id`` + ``blocked_by``) via
   ``POST /api/team/tasks``;
3. the app fires a declared event (``POST /api/workflows/{id}/events/{name}``) that
   starts the signed workflow;
4. the app approves the gate for the person with ``payload = {"qty": 3}``
   (``POST /api/workflow-tasks/{id}/gate``); the downstream tool node reads
   ``$nodes.approve.output.payload.qty`` and the agent's real ``read`` tool uses it;
5. every audit sink the deployment writes (ui mutation WORM, workflow audit,
   work-event callback audit, the principal and allowlist authorities, and every
   file under the deployment) holds zero ``did:arc:ui:operator`` and names the app
   DID and the person.

Real: ``create_app`` with its lifespan (so the production ``CallbackDispatcher``
starts), ``AuthMiddleware``, the real ``UserStore``, anchored ``PrincipalStore`` and
``CallbackAllowlistStore``, the work-events key provisioned in the local custody
notary, a started ``ArcAgent`` with the installed tasks module and dispatch on, the
production ``WorkflowRunner`` (``build_workflow_runner``) with the production
dashboard plane, a bundle signed with the deployment operator key, and a real HTTP
receiver on 127.0.0.1. One arcstore ``FakeBackend`` is shared by arcui and the agent,
standing in for the one PostgreSQL both use in production.

Doubles, each at an edge a unit box cannot run: the LLM wire (``scripted_llm``); the
NATS broker start (answered "already running"); the team registry the runner resolves
``@journey`` through (NATS-backed in production); the at-rest cipher of the two sealed
authorities (Vault in production); audit sinks record instead of appending to files.

Driving: the agent's dispatch loop ticks every 15 s and the runner's ``run_forever``
every 5 s. The journey calls the same tick functions those loops call
(``capabilities._dispatch_tick`` and ``WorkflowRunner.tick``) instead of sleeping, and
then waits on the durable row state, so a tick the background loop wins is also fine.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import FileJournalAnchor, InProcessSigner, OperatorKey, verify_signature
from arctrust.audit import AuditEvent
from arctrust.keypair import generate_keypair
from arctrust.paths import default_operator_key_path
from arctrust.principals import Scope
from arctrust.users import OPERATOR

from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    PASSWORD,
    VIEWER_TOKEN,
    DurableSink,
    RecordingSink,
    SecretBoxCipher,
    principal_rig,
)
from packages.arcui.tests.user_authority import user_store_factory

from .conftest import Deployment, ScriptedLLM, ScriptedTurn

FIXED_OPERATOR_DID = "did:arc:ui:operator"
OBO = "X-Arc-On-Behalf-Of"
IDEM = "Idempotency-Key"
BOSS_EMAIL = "boss@example.com"
PERSON_EMAIL = "buyer@example.com"
FLOW = "replenish"
EVENT = "order_placed"
GATE_NODE = "approve"
TOOL_NODE = "pick"
STOCK_FILE = "stock.txt"
STOCK_LINES = ("bolt", "nut", "washer", "screw", "rivet")
TASK_TEXT = "Summarize the open purchase orders for the north plant."
REPLY = "Three purchase orders are open for the north plant."
CORRELATION = "po-7731"
APP_SECRET_METADATA = "customer-ref-0d1f"
SIGNING_DOMAIN = b"arc.work-event.v1"
WAIT_S = 30.0

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
}
APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"qty": {"type": "integer", "minimum": 1}},
    "required": ["qty"],
    "additionalProperties": False,
}


# --- edges -------------------------------------------------------------------------------


class AuditRecorder(RecordingSink):
    """The WORM file edge: keeps every row; ``close`` is a no-op like a flushed file."""

    def close(self) -> None:
        return None


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


class CallbackReceiver:
    """The app's real HTTP endpoint on 127.0.0.1: records every POST, answers 204."""

    def __init__(self) -> None:
        self.received: list[Received] = []
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                receiver.received.append(
                    Received(self.path, {k: v for k, v in self.headers.items()}, body)
                )
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Entity:
    def __init__(self, did: str) -> None:
        self.did = did


class Registry:
    """The team registry the runner resolves ``@journey`` through (NATS in production)."""

    def __init__(self, handle: str, did: str) -> None:
        self._handle = handle
        self._did = did

    async def get(self, handle: str) -> Any:
        return _Entity(self._did) if handle == self._handle else None


@dataclass
class CallbackRig:
    """A real anchored ``CallbackAllowlistStore``; only the cipher is a Vault stand-in."""

    path: Path
    anchor: FileJournalAnchor
    cipher: SecretBoxCipher = field(default_factory=SecretBoxCipher)
    audit: RecordingSink = field(default_factory=RecordingSink)
    strict: DurableSink = field(default_factory=DurableSink)

    def open(self) -> Any:
        from arctrust.callback_allowlist import CallbackAllowlistStore

        return CallbackAllowlistStore(
            self.path,
            anchor=self.anchor,
            cipher=self.cipher,
            audit_sink=self.audit,
            strict_audit_sink=self.strict,
            actor_did="did:arc:test:operator/callback-store-actor",
            tier="personal",
        )


def _callback_rig(root: Path) -> CallbackRig:
    for sub in ("callbacks", "callbacks-anchor"):
        (root / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / sub).chmod(0o700)
    return CallbackRig(
        path=root / "callbacks" / "callbacks.json",
        anchor=FileJournalAnchor(
            root / "callbacks-anchor",
            scope="test/callbacks",
            signer=InProcessSigner(bytes(range(1, 33))),
        ),
    )


def _no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer as an already-running NATS broker would (the lifespan's external process)."""
    import arcteam.nats_server as nats_server

    async def _already_running(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nats_server, "ensure_nats_server", _already_running)
    consumer = importlib.import_module("arcgateway.broker_bootstrap")
    monkeypatch.setattr(consumer, "ensure_nats_server", _already_running)


# --- the workflow ------------------------------------------------------------------------


def _bundle(owner: str, approver_did: str) -> str:
    """event -> human gate with a typed payload -> a tool node that reads the payload."""
    return (
        f'[workflow]\nid = "{FLOW}"\nversion = 1\ndescription = "Replenish stock"\n'
        f'owner = "@{owner}"\n\n'
        f'[[event]]\nname = "{EVENT}"\nschema = "schemas/order.json"\n\n'
        f'[[node]]\nid = "{GATE_NODE}"\nkind = "gate"\ngate = "human:approve"\n'
        f'approvers = ["{approver_did}"]\noutput_schema = "schemas/approval.json"\n\n'
        f'[[node]]\nid = "{TOOL_NODE}"\nkind = "tool"\ntool = "read"\n'
        f'needs = ["{GATE_NODE}"]\nmax_attempts = 1\n'
        f'args = {{ file_path = "{STOCK_FILE}", '
        f'limit = "$nodes.{GATE_NODE}.output.payload.qty" }}\n'
    )


def _write_signed_bundle(home: Path, runner: Any, owner: str, approver_did: str) -> None:
    from arcteam.workflow import sign_definition

    bundle = home / "workflows" / FLOW
    (bundle / "schemas").mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(_bundle(owner, approver_did), encoding="utf-8")
    (bundle / "schemas" / "order.json").write_text(json.dumps(ORDER_SCHEMA), encoding="utf-8")
    (bundle / "schemas" / "approval.json").write_text(
        json.dumps(APPROVAL_SCHEMA), encoding="utf-8"
    )
    key = OperatorKey.load(default_operator_key_path(home))
    signed = sign_definition(
        runner.definitions,
        FLOW,
        signer_did="did:arc:test:operator/bundle-signer",
        private_key=key.seed,
    )
    assert signed.status == "signed", signed


# --- harness -----------------------------------------------------------------------------


@dataclass
class Journey:
    deployment: Deployment
    client: httpx.AsyncClient
    app: Any
    agent: Any
    agent_did: str
    runner: Any
    backend: FakeBackend
    llm: ScriptedLLM
    receiver: CallbackReceiver
    mutation_audit: RecordingSink
    workflow_audit: RecordingSink
    principal_rig: Any
    callback_rig: CallbackRig
    people: dict[str, str]

    async def login(self, email: str) -> dict[str, str]:
        resp = await self.client.post(
            "/api/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    def all_audit_events(self) -> dict[str, list[AuditEvent]]:
        return {
            "ui_mutation_worm": list(self.mutation_audit.events),
            "workflow_audit": list(self.workflow_audit.events),
            "principal_authority": [
                *self.principal_rig.audit.events,
                *self.principal_rig.strict.events,
            ],
            "callback_allowlist_authority": [
                *self.callback_rig.audit.events,
                *self.callback_rig.strict.events,
            ],
        }


@dataclass(frozen=True)
class App:
    did: str
    secret: str

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", OBO: PERSON_EMAIL, **extra}


def _denials(j: Journey) -> list[tuple[str, str, str]]:
    """Every refused mutation row: what a failed step was told and why."""
    return [
        (e.action, e.target, str((e.extra or {}).get("detail", "")))
        for e in j.mutation_audit.events
        if e.outcome != "applied"
    ]


async def _until(check: Callable[[], Awaitable[bool]], *, seconds: float = WAIT_S) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.1)
    return False


async def _dispatch_until_settled(j: Journey, task_id: str) -> Any:
    """One real dispatch tick, then wait for the agent to leave the row terminal."""
    from arcagent.modules.tasks.capabilities import _dispatch_tick

    await _dispatch_tick()

    async def settled() -> bool:
        row = await j.runner.tasks.get(task_id)
        return row is not None and row.status in ("done", "failed")

    if not await _until(settled):
        from arcagent.modules.tasks import _runtime

        st = _runtime.state()
        busy = await st.store.list(status="in_progress", owner_did=st.identity.did)
        todo = await st.store.list(status="todo", owner_did=st.identity.did)
        deps = [(t.id, await st.store.deps_met(t)) for t in todo]
        blockers = [await st.store.get(b) for t in todo for b in t.blocked_by]
        raise AssertionError(
            f"agent never settled {task_id}: busy={[(b.id, b.status) for b in busy]} "
            f"deps={deps} blockers={[(b.id, b.status) if b else None for b in blockers]} "
            f"dispatch={st.config.dispatch} runfn={st.agent_run_fn is not None}"
        )
    return await j.runner.tasks.get(task_id)


@pytest.fixture
async def journey(
    deployment: Deployment,
    enable_modules: Any,
    install_modules: Any,
    scripted_llm: ScriptedLLM,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Journey]:
    import arcagent
    from arcagent.scaffold import mint_agent_identity
    from arcteam.workflow.runner import build_workflow_runner
    from arctrust.work_event_signer import provision_work_event_key, work_event_signer_for
    from arcui.audit import MutationWormWriter
    from arcui.auth import AuthConfig
    from arcui.principals import PrincipalsConfig
    from arcui.server import create_app
    from arcui.workflow_plane import build_dashboard_plane

    _no_broker(monkeypatch)
    home = deployment.home
    # The agent accepts runs: tasks module on, dispatch loop on (operator TOML only).
    enable_modules("tasks", config={"tasks": {"dispatch": True}})
    agent_did = mint_agent_identity(deployment.agent_dir).did  # rewritten toml: restore DID
    install_modules()
    lines = "\n".join(STOCK_LINES) + "\n"
    (deployment.agent_dir / "workspace" / STOCK_FILE).write_text(lines, encoding="utf-8")

    # `arc init` provisions the work-events key in the local custody notary.
    provision_work_event_key()
    signer = work_event_signer_for()
    assert signer is not None, "work-events key was not provisioned"

    backend = FakeBackend()

    def opener(self: Any) -> Callable[[], Awaitable[FakeBackend]]:
        async def open_backend() -> FakeBackend:
            return backend

        return open_backend

    monkeypatch.setattr(arcagent.ArcAgent, "_make_arcstore_opener", opener)

    users = user_store_factory(tmp_path / "accounts")
    store = users()
    people = {
        BOSS_EMAIL: store.add(BOSS_EMAIL, PASSWORD, roles=(OPERATOR,)).did,
        # REQ-562: an app adds no authority. The person it acts for holds the operator
        # role (so may start runs, file tasks and fire events) and is the gate approver.
        PERSON_EMAIL: store.add(PERSON_EMAIL, PASSWORD, roles=(OPERATOR,)).did,
    }
    p_rig = principal_rig(tmp_path / "authority")
    c_rig = _callback_rig(tmp_path / "authority")
    app = create_app(
        auth_config=AuthConfig({"operator_token": OPERATOR_TOKEN, "viewer_token": VIEWER_TOKEN}),
        user_store_factory=users,
        principal_store_factory=p_rig.factory,
        principals_config=PrincipalsConfig(tier="personal"),
        callback_allowlist_factory=c_rig.open,
        work_event_signer=signer,
        team_root=deployment.team_root,
        data_dir=tmp_path / "arcui-data",
        arcstore_backend=backend,
        allow_external_task_refs=True,  # what `arc ui start` passes at personal tier
    )
    mutation_audit = AuditRecorder()
    app.state.audit_worm = MutationWormWriter(
        sink=mutation_audit,  # type: ignore[arg-type]  # reason: WORM file is the faked edge
        operator_did="did:arc:test:operator/worm",
    )

    receiver = CallbackReceiver()
    receiver.start()
    try:
        async with app.router.lifespan_context(app):
            assert app.state.callback_dispatcher is not None, (
                "create_app's lifespan started no CallbackDispatcher despite a work-events key "
                "and a callback allowlist"
            )
            workflow_audit = RecordingSink()
            runner = build_workflow_runner(
                tier="personal",
                task_store_backend=backend,
                runner_key_path=default_operator_key_path(home),
                workspace_root=home,
                registry=Registry(deployment.agent_name, agent_did),
                audit_sink=workflow_audit,
            )
            _write_signed_bundle(home, runner, deployment.agent_name, people[PERSON_EMAIL])
            plane = build_dashboard_plane(runner=runner)
            app.state.workflow_control_plane = plane
            app.state.gate_control_plane = plane

            config_path = deployment.agent_dir / "arcagent.toml"
            agent = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
            await agent.startup()
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            )
            try:
                yield Journey(
                    deployment=deployment,
                    client=client,
                    app=app,
                    agent=agent,
                    agent_did=agent_did,
                    runner=runner,
                    backend=backend,
                    llm=scripted_llm,
                    receiver=receiver,
                    mutation_audit=mutation_audit,
                    workflow_audit=workflow_audit,
                    principal_rig=p_rig,
                    callback_rig=c_rig,
                    people=people,
                )
            finally:
                await client.aclose()
                await agent.shutdown()
                await runner.aclose()
    finally:
        receiver.stop()


# --- operator setup through the UI routes ------------------------------------------------


async def _operator_setup(j: Journey) -> App:
    boss = await j.login(BOSS_EMAIL)
    added = await j.client.post(
        "/api/work-events/callbacks",
        json={"origin": j.receiver.origin, "allow_private": True},
        headers=boss,
    )
    assert added.status_code == 201, added.text
    assert added.json()["status"] == "active", added.json()

    keys = generate_keypair()
    agent = [j.agent_did]
    issued = await j.client.post(
        "/api/principals",
        json={
            "name": "purchasing-portal",
            "public_key": keys.public_key.hex(),
            "scopes": {
                str(Scope.RUNS_START): agent,
                str(Scope.RUNS_READ): agent,
                str(Scope.TASKS_WRITE): agent,
                str(Scope.WORKFLOWS_EVENT): agent,
                str(Scope.GATES_RESOLVE): agent,
                str(Scope.EVENTS_SUBSCRIBE): agent,
            },
            "subjects": [j.people[PERSON_EMAIL]],
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        },
        headers=boss,
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["status"] == "active", body
    return App(did=body["principal"]["did"], secret=body["secret"])


def _verify_callback(received: Received, key: dict[str, Any]) -> dict[str, Any]:
    """Verify one callback exactly as a receiver would, against the published key."""
    headers = {k.lower(): v for k, v in received.headers.items()}
    assert headers["arc-key-id"] == key["key_id"], (headers, key)
    algorithm, _, encoded = headers["arc-signature"].partition("=")
    assert algorithm == key["algorithm"]
    message = b"\n".join(
        (
            SIGNING_DOMAIN,
            headers["arc-timestamp"].encode("ascii"),
            headers["arc-event-id"].encode("ascii"),
            hashlib.sha256(received.body).hexdigest().encode(),
        )
    )
    assert verify_signature(
        algorithm, message, base64.b64decode(encoded), base64.b64decode(key["public_key"])
    ), "callback signature does not verify against GET /api/work-events/keys"
    envelope: dict[str, Any] = json.loads(received.body)
    assert envelope["event_id"] == headers["arc-event-id"]
    return envelope


# --- the journey -------------------------------------------------------------------------


async def test_app_runs_files_fires_resolves_and_is_called_back_without_operator_attribution(
    journey: Journey,
) -> None:
    j = journey
    person = j.people[PERSON_EMAIL]
    app = await _operator_setup(j)
    j.llm.replies.append(REPLY)

    # 1. Start a run for the person; the agent's dispatch loop runs it under that id.
    started = await j.client.post(
        "/api/runs",
        json={
            "agent": j.agent_did,
            "task": TASK_TEXT,
            "metadata": {"customer_ref": APP_SECRET_METADATA},
            "correlation_id": CORRELATION,
            "callback_url": f"{j.receiver.origin}/arc/callback",
        },
        headers=app.headers(**{IDEM: "po-7731-summary"}),
    )
    assert started.status_code == 202, (started.text, _denials(j))
    run_id = started.json()["run_id"]
    assert started.json()["status"] == "queued"
    assert "notifications" not in started.json()["links"]["events"], started.json()

    intake = await _dispatch_until_settled(j, f"intake-{run_id}")
    assert intake.status == "done", intake
    assert intake.run_id == run_id, "the agent ran the intake row under a different run id"
    assert intake.creator_did == person, intake
    assert intake.metadata["intake"]["on_behalf_of"] == person
    assert intake.metadata["intake"]["via_app"] == app.did
    assert any(TASK_TEXT in str(m) for call in j.llm.calls for m in call), (
        "task never reached the model"
    )
    prompts = "\n".join(str(m) for call in j.llm.calls for m in call)
    assert APP_SECRET_METADATA not in prompts and CORRELATION not in prompts, (
        "caller metadata or correlation id leaked into the model prompt"
    )

    read = await j.client.get(f"/api/runs/{run_id}", headers=app.headers())
    assert read.status_code == 200, read.text
    projection = read.json()
    assert projection["status"] == "done", projection
    assert projection["result"] == {"text": REPLY}, projection
    assert projection["correlation_id"] == CORRELATION
    assert projection["agent_did"] == j.agent_did

    async def called_back() -> bool:
        return any(b'"run.finished"' in r.body for r in j.receiver.received)

    callback_audit = [e for e in j.mutation_audit.events if e.action == "work_event.callback"]
    assert await _until(called_back), (
        f"no signed run.finished callback reached the receiver; callback audit={callback_audit}"
    )
    key = await j.client.get(
        "/api/work-events/keys", params={"agent": j.agent_did}, headers=app.headers()
    )
    assert key.status_code == 200, key.text
    finished = [r for r in j.receiver.received if b'"run.finished"' in r.body]
    assert len(finished) == 1, [r.body for r in j.receiver.received]
    envelope = _verify_callback(finished[0], key.json())
    assert finished[0].path == "/arc/callback"
    assert (envelope["kind"], envelope["run_id"]) == ("run.finished", run_id)
    assert envelope["correlation_id"] == CORRELATION
    assert REPLY not in finished[0].body.decode(), "a callback must carry pointers, not results"

    # 2. File a dependent task: a parent, and a child blocked by the finished run.
    parent = await j.client.post(
        "/api/team/tasks",
        json={"title": "Restock the north plant", "owner_did": j.agent_did},
        headers=app.headers(),
    )
    assert parent.status_code == 201, parent.text
    child = await j.client.post(
        "/api/team/tasks",
        json={
            "title": "Order fasteners",
            "owner_did": j.agent_did,
            "parent_id": parent.json()["id"],
            "blocked_by": [f"intake-{run_id}"],
            "metadata": {"po": CORRELATION},
        },
        headers=app.headers(),
    )
    assert child.status_code == 201, child.text
    stored_child = await j.runner.tasks.get(child.json()["id"])
    assert stored_child.parent_id == parent.json()["id"]
    assert stored_child.blocked_by == [f"intake-{run_id}"]
    assert stored_child.creator_did == person
    assert stored_child.metadata == {"app": {"po": CORRELATION}}
    # Filed for the agent, the dependent task is real work: its blocker (the finished
    # run) is done, so the agent's dispatch loop runs it, and the model closes it.
    j.llm.replies.extend(
        [
            ScriptedTurn(
                tool="complete_task",
                args={"id": stored_child.id, "resolution": "fasteners ordered"},
            ),
            "Fasteners ordered.",
        ]
    )
    worked = await _dispatch_until_settled(j, stored_child.id)
    assert (worked.status, worked.resolution) == ("done", "fasteners ordered"), worked
    assert (await j.runner.tasks.get(parent.json()["id"])).status != "in_progress"

    # 3. Fire the declared event: it starts the signed workflow.
    fired = await j.client.post(
        f"/api/workflows/{FLOW}/events/{EVENT}",
        json={"order_id": "A-1"},
        headers=app.headers(**{IDEM: "order-A-1"}),
    )
    assert fired.status_code == 201, fired.text
    wf_run = fired.json()["run_id"]
    waiting = await j.client.get(f"/api/workflow-runs/{wf_run}", headers=app.headers())
    assert waiting.status_code == 200, waiting.text
    gates = [n for n in waiting.json()["nodes"] if n["node_id"] == GATE_NODE]
    assert len(gates) == 1 and gates[0]["status"] == "waiting_gate", waiting.json()
    gate_task = gates[0]["task_id"]

    # 4. Approve the gate for the person with a typed payload.
    approved = await j.client.post(
        f"/api/workflow-tasks/{gate_task}/gate",
        json={"decision": "approve", "payload": {"qty": 3}},
        headers=app.headers(),
    )
    assert approved.status_code == 200, approved.text
    gate_row = await j.runner.tasks.get(gate_task)
    assert gate_row.output["payload"] == {"qty": 3}, gate_row.output
    assert gate_row.output["decided_by"] == person
    assert gate_row.output["decided_by"] != FIXED_OPERATOR_DID

    tool_rows = [
        r
        for r in await j.runner.tasks.query_by_flow_run(wf_run)
        if r.metadata.get("node_id") == TOOL_NODE
    ]
    assert len(tool_rows) == 1, await j.runner.tasks.query_by_flow_run(wf_run)
    assert tool_rows[0].metadata["args"]["limit"] == 3, tool_rows[0].metadata
    tool_row = await _dispatch_until_settled(j, tool_rows[0].id)
    assert tool_row.status == "done", (tool_row.status, tool_row.last_error)
    read_lines = tool_row.output["result"].splitlines()
    assert [line.split("\t", 1)[1] for line in read_lines] == list(STOCK_LINES[:3]), (
        tool_row.output
    )

    await j.runner.tick()
    done = await j.client.get(f"/api/workflow-runs/{wf_run}", headers=app.headers())
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done", done.json()

    # 5. The whole audit trail: zero fixed operator DIDs, and it names the app and person.
    sinks = j.all_audit_events()
    for name, events in sinks.items():
        dumped = [e.model_dump_json() for e in events]
        offenders = [d for d in dumped if FIXED_OPERATOR_DID in d]
        assert not offenders, f"{name} holds {FIXED_OPERATOR_DID}: {offenders}"
    callback_rows = [e for e in j.mutation_audit.events if e.action == "work_event.callback"]
    assert any(e.outcome == "delivered" for e in callback_rows), callback_rows
    assert all(FIXED_OPERATOR_DID not in e.actor_did for e in callback_rows)

    for operation in ("run.intake", "task.create", "workflow.event.fire", "gate.resolve"):
        applied = [
            e for e in j.mutation_audit.events if e.action == operation and e.outcome == "applied"
        ]
        assert applied, (
            f"no applied {operation} row: {[e.action for e in j.mutation_audit.events]}"
        )
        for row in applied:
            assert row.extra["via_app"] == app.did, (operation, row)
            assert row.extra["on_behalf_of"] == person, (operation, row)

    run_events = [
        e
        for e in j.workflow_audit.events
        if wf_run in e.target or (e.extra or {}).get("run_id") == wf_run
    ]
    named = {e.actor_did for e in run_events} | {
        v
        for e in run_events
        if e.causal is not None
        for v in (e.causal.initiator_id, e.causal.on_behalf_of)
    }
    assert {app.did, person} <= named, run_events

    # The durable store is part of the trail: rows, work events and deliveries.
    # (The in-memory backend stands in for PostgreSQL; its tables are read directly.)
    stored = repr(vars(j.backend))
    assert FIXED_OPERATOR_DID not in stored, "the store records the fixed operator DID"
    assert app.did in stored and person in stored

    leaked = [
        str(path)
        for path in j.deployment.home.parent.rglob("*")
        if path.is_file() and FIXED_OPERATOR_DID.encode() in path.read_bytes()
    ]
    assert not leaked, f"files under the deployment record {FIXED_OPERATOR_DID}: {leaked}"
