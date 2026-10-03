"""Journey: every audited act names who caused it, inside what, and the chain proves it.

Alpha-2 item 20 (P20-7) — the causal-audit CONTRACT, driven down the paths a user
actually takes. A person messages the agent on a channel; the agent calls a tool;
the operator's Security screen later asks "which run made that call, for whom?".
The answers live in the agent's operator-signed WORM chain and nowhere else, so
every assertion here reads that chain (or the arcui API over it) — never a mock
the test wired up to agree with itself.

Real: the gateway ``SessionRouter`` and ``AsyncioExecutor``, the started
``ArcAgent``, arcrun's loop, the tool registry, the arctrust policy pipeline,
the ``WormSink`` chain, ``StoreIngest`` verification, the arcui auth middleware
and audit routes, and the connection-health probe loop. Faked: the LLM wire
(:class:`ScriptedLLM`) and the provider's answer to a probe.

Concurrency claims force real interleaving with an ``asyncio.Barrier``: an
instant fake would run two "concurrent" turns one after the other and pass even
if attribution lived in a process global (see
``feedback_concurrency_tests_must_interleave``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcgateway.executor import AsyncioExecutor, InboundEvent
from arcgateway.session import SessionRouter
from arctrust import causal

from .conftest import Deployment, ScriptedLLM, ScriptedTurn

ALICE = "did:arc:user:alice"
BOB = "did:arc:user:bob"
PROBE_TOOL = "journey_probe"


# ---------------------------------------------------------------------------
# Helpers: the agent's WORM chain, a channel message, a recording tool
# ---------------------------------------------------------------------------


def agent_chain(deployment: Deployment) -> Path:
    """The agent's operator-signed policy chain — where ``policy.evaluate`` lands."""
    store = Path(str(deployment.home.parent / "store"))
    return store / "worm" / f"audit-chain-{deployment.agent_name}.jsonl"


def chain_events(chain: Path, action: str | None = None) -> list[dict[str, Any]]:
    """Every record's event on ``chain`` (optionally one action), in seq order."""
    if not chain.exists():
        return []
    events = [json.loads(line)["event"] for line in chain.read_text().splitlines() if line]
    return [e for e in events if action is None or e["action"] == action]


def tool_rows(deployment: Deployment, tool: str) -> list[dict[str, Any]]:
    """The ``policy.evaluate`` rows written for ``tool``, in order."""
    rows = chain_events(agent_chain(deployment), "policy.evaluate")
    return [r for r in rows if r.get("target") == tool]


async def send(router: SessionRouter, agent: Any, *messages: tuple[str, str, str]) -> None:
    """Hand each ``(user_did, chat_id, text)`` to the router and wait for every turn.

    ``SessionRouter.handle`` returns as soon as the turn is handed off, so the
    spawned tasks are awaited here — otherwise an assertion reads an empty chain.
    """
    for user, chat, text in messages:
        await router.handle(
            InboundEvent(
                platform="journey",
                chat_id=chat,
                user_did=user,
                agent_did=agent._identity.did,
                message=text,
            )
        )
    while router._pending_tasks:
        await asyncio.wait_for(
            asyncio.gather(*tuple(router._pending_tasks), return_exceptions=True), 20
        )


@pytest.fixture
def router(agent: Any) -> SessionRouter:
    async def _factory(_did: str) -> Any:
        return agent

    return SessionRouter(executor=AsyncioExecutor(agent_factory=_factory))


@dataclass
class ProbeTool:
    """A read-only tool whose body records the causal context it ran inside.

    Registered on the started agent's real registry, so dispatch, the policy
    pipeline and the WORM write are production code; only the body is the
    test's. ``before`` runs inside the body (a barrier, a background spawn).
    """

    seen: list[tuple[dict[str, Any], causal.CausalContext | None]] = field(default_factory=list)
    before: Callable[[dict[str, Any]], Any] | None = None

    def install(self, agent: Any) -> None:
        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        async def execute(**args: Any) -> str:
            if self.before is not None:
                await self.before(args)
            self.seen.append((args, causal.current()))
            return "probed"

        agent._tool_registry.register(
            RegisteredTool(
                name=PROBE_TOOL,
                description="Record who is asking.",
                input_schema={"type": "object", "properties": {"who": {"type": "string"}}},
                transport=ToolTransport.NATIVE,
                execute=execute,
                classification="read_only",
            )
        )


# ---------------------------------------------------------------------------
# Contract 1 — a tool call inside a real run carries its whole causal chain
# ---------------------------------------------------------------------------


async def test_a_tool_call_in_a_channel_turn_names_run_call_model_call_agent_and_user(
    agent: Any, router: SessionRouter, scripted_llm: ScriptedLLM, deployment: Deployment
) -> None:
    """Alice asks; the agent calls a tool; its signed policy row says exactly that."""
    probe = ProbeTool()
    probe.install(agent)
    scripted_llm.replies.extend([ScriptedTurn(tool=PROBE_TOOL, args={}), "done"])

    await send(router, agent, (ALICE, "chat-a", "check it"))

    ((_, ctx),) = probe.seen
    assert ctx is not None, "the tool body ran with no causal context bound"
    (row,) = tool_rows(deployment, PROBE_TOOL)
    chain = row["causal"]
    did = agent._identity.did
    assert row["actor_did"] == did, "the agent's tool call was signed as someone else"
    assert chain["initiator"] == "agent"
    assert chain["initiator_id"] == did
    assert chain["on_behalf_of"] == ALICE, "the requesting user is missing from the row"
    assert chain["run_id"] and chain["run_id"] == ctx.run_id
    assert chain["tool_call_id"] and chain["tool_call_id"] == ctx.tool_call_id
    assert chain["llm_call_id"], "the model call that asked for the tool is not named"
    assert chain["llm_call_id"] == ctx.llm_call_id


# ---------------------------------------------------------------------------
# Contract 4 — background isolation and concurrent callers
# ---------------------------------------------------------------------------


def _record_prompts(scripted_llm: ScriptedLLM, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Map each run id the model was called inside to the prompt it was sent.

    Read at the LLM wire, where the run's own context is bound: the one place a
    turn's run id and the user's words are both visible without trusting the
    code under test to report either.
    """
    prompts: dict[str, str] = {}
    invoke = scripted_llm.invoke

    async def recording(messages: list[Any], tools: list[Any] | None = None, **kw: Any) -> Any:
        ctx = causal.current()
        if ctx is not None and ctx.run_id:
            text = "\n".join(str(getattr(m, "content", m)) for m in messages)
            prompts[ctx.run_id] = prompts.get(ctx.run_id, "") + text
        return await invoke(messages, tools, **kw)

    monkeypatch.setattr(scripted_llm, "invoke", recording)
    return prompts


async def test_two_concurrent_turns_from_different_users_never_cross_attribute(
    agent: Any,
    router: SessionRouter,
    scripted_llm: ScriptedLLM,
    deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alice and Bob talk to one agent at once; each tool row names its own caller.

    Both tool bodies rendezvous on a barrier, so both turns are provably inside
    tool dispatch at the same moment. A shared (non task-local) binding would
    then hand one turn's principal to the other.
    """
    prompts = _record_prompts(scripted_llm, monkeypatch)
    both_in_flight = asyncio.Barrier(2)

    async def rendezvous(_args: dict[str, Any]) -> None:
        await asyncio.wait_for(both_in_flight.wait(), 10)

    probe = ProbeTool(before=rendezvous)
    probe.install(agent)
    scripted_llm.replies.extend(
        [ScriptedTurn(tool=PROBE_TOOL), ScriptedTurn(tool=PROBE_TOOL), "done", "done"]
    )

    await send(router, agent, (ALICE, "chat-a", "from alice"), (BOB, "chat-b", "from bob"))

    rows = tool_rows(deployment, PROBE_TOOL)
    assert len(rows) == 2 and len(probe.seen) == 2
    expected = {"from alice": ALICE, "from bob": BOB}
    by_call = {ctx.tool_call_id: ctx for _, ctx in probe.seen if ctx is not None}
    for row in rows:
        chain = row["causal"]
        prompt = prompts[chain["run_id"]]
        users = [who for said, who in expected.items() if said in prompt]
        assert len(users) == 1, f"run {chain['run_id']} carried both callers' words: {users}"
        (user,) = users
        assert chain["on_behalf_of"] == user, f"{user}'s tool call was attributed elsewhere"
        assert chain["initiator_id"] == agent._identity.did
        body = by_call[chain["tool_call_id"]]
        assert (body.run_id, body.on_behalf_of) == (chain["run_id"], user)
    assert len({r["causal"]["run_id"] for r in rows}) == 2
    assert len({r["causal"]["request_id"] for r in rows}) == 2


async def test_background_work_spawned_mid_run_never_inherits_the_runs_ids(
    agent: Any, router: SessionRouter, scripted_llm: ScriptedLLM
) -> None:
    """A job a tool starts in the background is its own root, not part of Alice's run.

    The tool body and the background job meet on a barrier, so the job reads its
    context while the run is still inside the tool call — the moment a leaked
    binding would be visible.
    """
    from arcagent.core.config import EvalConfig
    from arcagent.utils.model_helpers import spawn_background

    background: list[causal.CausalContext | None] = []
    tasks: set[asyncio.Task[None]] = set()
    meet = asyncio.Barrier(2)

    async def job() -> None:
        await asyncio.wait_for(meet.wait(), 10)
        background.append(causal.current())

    async def start_job(_args: dict[str, Any]) -> None:
        spawn_background(
            job(),
            background_tasks=tasks,
            semaphore=asyncio.Semaphore(1),
            eval_config=EvalConfig(),
        )
        await asyncio.wait_for(meet.wait(), 10)

    probe = ProbeTool(before=start_job)
    probe.install(agent)
    scripted_llm.replies.extend([ScriptedTurn(tool=PROBE_TOOL), "done"])

    await send(router, agent, (ALICE, "chat-a", "start the job"))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 10)

    ((_, run_ctx),) = probe.seen
    (job_ctx,) = background
    assert run_ctx is not None and run_ctx.run_id and run_ctx.on_behalf_of == ALICE
    assert job_ctx is not None, "the background job ran with nothing bound"
    leaked = {
        name: getattr(job_ctx, name)
        for name in ("run_id", "tool_call_id", "llm_call_id", "on_behalf_of")
        if getattr(job_ctx, name) is not None
    }
    assert not leaked, f"the background job inherited the run's {leaked}"
    assert job_ctx.request_id != run_ctx.request_id
    assert job_ctx.initiator == "system" and job_ctx.initiator_id != agent._identity.did


# ---------------------------------------------------------------------------
# Contract 2 — a timed connector probe is caused by the probe loop, not a person
# ---------------------------------------------------------------------------


class _HealthyAcme:
    """What the provider answers a probe: reachable. The one double on this path."""

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> Any:
        from arcagent.extension.attachment import ProbeResult

        return ProbeResult(reachable=True, tools=await self.describe_tools(), detail="")

    async def describe_tools(self) -> list[Any]:
        from arcagent.extension.attachment import ToolSpec

        return [ToolSpec(name="ping", description="Ping Acme.", classification="read_only")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        from arcagent.extension.attachment import ToolResult

        return ToolResult(tool=tool, content="pong")


async def test_a_timed_connector_probe_is_attributed_to_the_probe_loop_and_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe loop runs while an operator page view is in flight; it never borrows it.

    The loop is started from inside a bound UI-session root — exactly where
    arcui's lifespan starts it while requests are already being served — and
    every row the probe writes must still name the probe loop and the connection.
    """
    import arcagent
    from arctrust.audit import WormSink
    from arctrust.keypair import generate_keypair
    from arctrust.signer import InProcessSigner
    from arcui.connection_health import ConnectionHealthMonitor

    from packages.arcui.tests.connection_fleet import INSTANCE, Fleet

    fleet = Fleet(tmp_path, monkeypatch)
    await asyncio.to_thread(fleet.install)
    chain = tmp_path / "data" / "worm" / "audit-chain-arcui.jsonl"
    sink = WormSink(chain, InProcessSigner(generate_keypair().private_key))

    async def opener() -> Any:
        return fleet.backend

    monitor = ConnectionHealthMonitor(
        lambda: arcagent.Connections.for_deployment(
            audit=arcagent.AuditChain.held(sink),
            state_opener=opener,
            attachment_factory=lambda *_a, **_kw: _HealthyAcme(),
        ),
        store_opener=opener,
        agents_resolver=lambda _instance: [],
        sink=sink,
        rng=lambda: 0.0,
        initial_delay_seconds=0,
    )
    with causal.bind(causal.root("ui_session", "did:arc:ui:session:page-view")):
        monitor.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not chain_events(chain, "connection.health.checked"):
            assert loop.time() < deadline, "the probe loop never checked the connection"
            await asyncio.sleep(0.01)
    finally:
        await monitor.stop()
        sink.close()

    (checked,) = chain_events(chain, "connection.health.checked")
    assert checked["target"] == f"connection:{INSTANCE}"
    rows = chain_events(chain)
    for row in rows:
        ctx = row["causal"]
        assert ctx["initiator"] == "connector_probe", f"{row['action']} borrowed {ctx}"
        assert ctx["initiator_id"] == arcagent.PROBE_DID
        assert ctx["on_behalf_of"] is None
        assert ctx["connection_id"] == INSTANCE, f"{row['action']} lost its connection"


# ---------------------------------------------------------------------------
# Contract 3 — a viewer's read leaves no chain row; a viewer's refused change does
# ---------------------------------------------------------------------------


@dataclass
class OperatorConsole:
    """arcui's real auth middleware and routes over a real operator-signed UI chain."""

    client: Any
    chain: Path

    def rows(self) -> list[dict[str, Any]]:
        return chain_events(self.chain)


@pytest.fixture
def console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from arcgateway import team_roster
    from arcstore.approvals import ApprovalStore
    from arcstore.backends.memory import FakeBackend
    from arcstore.ingest import UI_WORM_FILENAME
    from arctrust.audit import WormSink
    from arctrust.keypair import generate_keypair
    from arctrust.signer import InProcessSigner
    from arcui.audit import MutationWormWriter
    from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
    from arcui.routes.approvals import routes as approval_routes
    from arcui.routes.connectors import routes as connector_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from packages.arcui.tests.connection_fleet import Fleet

    fleet = Fleet(tmp_path, monkeypatch)
    # A connected account, so the pages below read a real secret and a real record.
    fleet.install()
    chain = tmp_path / "data" / "worm" / UI_WORM_FILENAME
    sink = WormSink(chain, InProcessSigner(generate_keypair().private_key))
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*connector_routes, *approval_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.session_tracker = SessionTracker()
    backend = FakeBackend()
    app.state.arcstore_backend = backend
    app.state.approval_store = ApprovalStore(backend)
    app.state.audit_worm = MutationWormWriter(sink=sink, operator_did="did:arc:operator:journey")
    team_root = fleet._team_root
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    with TestClient(app) as client:
        yield OperatorConsole(client=client, chain=chain)
    sink.close()


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/connections",
        "/api/connectors/catalog",
        "/api/connections/work/auth-status",
        "/api/connections/work/doctor",
        "/api/approvals",
    ],
)
def test_a_viewer_page_view_writes_no_chain_row(console: OperatorConsole, path: str) -> None:
    """Reading a page changes nothing, so the tamper-evident chain records nothing."""
    response = console.client.get(path, headers=_as("viewer"))
    assert response.status_code == 200, response.text
    assert console.rows() == [], f"GET {path} appended {[r['action'] for r in console.rows()]}"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/approvals/appr-1/approve", None),
        ("/api/connections", {"extension": "acme", "instance": "work", "agents": []}),
    ],
)
def test_a_viewers_refused_change_writes_one_denial_row(
    console: OperatorConsole, path: str, body: dict[str, Any] | None
) -> None:
    """A viewer trying an operator change is refused AND that attempt is on the chain.

    A refusal that leaves no record is invisible to the operator reviewing who
    tried what (AU-2, AC-7): the Security screen's "Denied" filter would be empty
    for exactly the event it exists to show.
    """
    response = console.client.post(path, json=body, headers=_as("viewer"))
    assert response.status_code == 403, response.text
    rows = console.rows()
    denials = [r for r in rows if r["outcome"] in {"deny", "denied"}]
    assert len(denials) == 1, f"POST {path} by a viewer wrote {[r['action'] for r in rows]}"
    (denial,) = denials
    assert denial["causal"]["initiator"] == "ui_session"
    assert denial["actor_did"] == denial["causal"]["initiator_id"]
    assert denial["actor_did"] != "did:arc:operator:journey", "the viewer was recorded as the key"


# ---------------------------------------------------------------------------
# Contracts 5 and 6 — the Security screen's ledger: verified, filterable, honest
# ---------------------------------------------------------------------------


def chain_records(worm_dir: Path) -> list[dict[str, Any]]:
    """Every raw record of every chain (and rotated segment) under ``worm_dir``."""
    return [
        json.loads(line)
        for path in sorted(worm_dir.glob("audit-chain*.jsonl"))
        for line in path.read_text().splitlines()
        if line
    ]


def operator_signer() -> Any:
    """The deployment's operator key — the authority that signs every chain."""
    from arctrust import OperatorKey, default_operator_key_path

    return OperatorKey.load(default_operator_key_path()).into_signer()


@dataclass
class Ledger:
    """arcui's audit API over a real ``Observe`` ingest of the WORM directory."""

    client: Any
    observe: Any
    worm_dir: Path

    def audit(self, query: str = "") -> dict[str, Any]:
        response = self.client.get(f"/api/team/audit?limit=1000&{query}", headers=_as("viewer"))
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def hashes(self, query: str) -> set[str]:
        return {e["event_hash"] for e in self.audit(query)["events"]}


async def open_ledger(
    data_dir: Path, public_key: bytes, writer: Any = None, backend: Any = None
) -> Ledger:
    """The production read path: StoreIngest verifies; the routes serve what it stored."""
    from arcstore.backends.memory import FakeBackend
    from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
    from arcui.observe import Observe
    from arcui.routes.observe_run import routes as run_routes
    from arcui.routes.team_pages import routes as team_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    store = backend if backend is not None else FakeBackend()
    observe = Observe(data_dir=data_dir, backend=store, worm_public_key=public_key)
    await observe.refresh()
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*team_routes, *run_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.session_tracker = SessionTracker()
    app.state.observe = observe
    app.state.audit_worm = writer
    app.state.roster_provider = lambda: []
    return Ledger(client=TestClient(app), observe=observe, worm_dir=data_dir / "worm")


async def test_the_audit_api_returns_exactly_the_rows_each_causal_filter_names(
    agent: Any, router: SessionRouter, scripted_llm: ScriptedLLM, deployment: Deployment
) -> None:
    """Real turns plus a probe row, ingested and verified; every filter is exact.

    "Exact" means the API's row set equals the set computed from the raw signed
    chain — not merely non-empty, so a filter that ignored its parameter (and
    returned the page) or matched the wrong column fails.
    """
    import arcagent
    from arctrust.audit import AuditEvent, WormSink

    probe = ProbeTool()
    probe.install(agent)
    scripted_llm.replies.extend(
        [ScriptedTurn(tool=PROBE_TOOL), ScriptedTurn(tool=PROBE_TOOL), "done"]
    )
    await send(router, agent, (ALICE, "chat-a", "twice please"))
    scripted_llm.replies.extend([ScriptedTurn(tool=PROBE_TOOL), "done"])
    await send(router, agent, (BOB, "chat-b", "once please"))

    signer = operator_signer()
    worm_dir = agent_chain(deployment).parent
    probe_sink = WormSink(worm_dir / "audit-chain-probe.jsonl", signer)
    with causal.bind(causal.root("connector_probe", arcagent.PROBE_DID, connection_id="work")):
        probe_sink.write(
            AuditEvent(
                actor_did=arcagent.PROBE_DID,
                action="connection.health.checked",
                target="connection:work",
                outcome="allow",
            )
        )
    probe_sink.close()

    ledger = await open_ledger(worm_dir.parent, signer.public_key)
    records = chain_records(worm_dir)
    every = {r["event_hash"] for r in records}
    assert ledger.audit()["totals"]["verified"] == len(records), "a real record failed to verify"

    def where(column: str, value: str) -> set[str]:
        return {
            r["event_hash"]
            for r in records
            if (r["event"].get("causal") or {}).get(column) == value
        }

    alice_run, bob_run = (ctx.run_id for _, ctx in probe.seen[::2] if ctx is not None)
    first_call = probe.seen[0][1].tool_call_id if probe.seen[0][1] is not None else ""
    cases = {
        f"run_id={alice_run}": where("run_id", alice_run),
        f"run_id={bob_run}": where("run_id", bob_run),
        f"tool_call_id={first_call}": where("tool_call_id", first_call),
        "initiator=agent": where("initiator", "agent"),
        "initiator=connector_probe": where("initiator", "connector_probe"),
        "connection_id=work": where("connection_id", "work"),
    }
    for query, expected in cases.items():
        assert expected and expected != every, f"{query}: the seed cannot tell a filter from none"
        assert ledger.hashes(query) == expected, f"{query} returned the wrong rows"
    assert len(where("tool_call_id", first_call)) == 1
    assert ledger.hashes("initiator=scheduler") == set()

    response = ledger.client.get(f"/api/runs/{alice_run}/audit", headers=_as("viewer"))
    assert response.status_code == 200
    assert {e["event_hash"] for e in response.json()["events"]} == where("run_id", alice_run)


def _signed_chain(worm_dir: Path, count: int, **sink_kwargs: Any) -> tuple[Path, bytes]:
    """``count`` records an agent would write, signed by a fresh operator key."""
    from arctrust.audit import AuditEvent, WormSink
    from arctrust.keypair import generate_keypair
    from arctrust.signer import InProcessSigner

    key = generate_keypair()
    chain = worm_dir / "audit-chain-olivia.jsonl"
    sink = WormSink(chain, InProcessSigner(key.private_key), **sink_kwargs)
    for index in range(count):
        with causal.bind(causal.root("agent", "did:arc:t:olivia/1", run_id=f"run-{index}")):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:t:olivia/1",
                    action="policy.evaluate",
                    target=f"tool-{index}",
                    outcome="allow",
                )
            )
    sink.close()
    return chain, key.public_key


def _flip_one_byte(chain: Path, seq: int) -> bytes:
    """Change one character of record ``seq``'s target; return the original bytes."""
    original = chain.read_bytes()
    lines = original.split(b"\n")
    assert lines[seq].count(f"tool-{seq}".encode()) == 1
    lines[seq] = lines[seq].replace(f"tool-{seq}".encode(), f"tool-{seq}".encode()[:-1] + b"X")
    chain.write_bytes(b"\n".join(lines))
    return original


def _verdicts(ledger: Ledger) -> dict[int, bool]:
    events = ledger.audit()["events"]
    return {e["seq"]: e["verified"] for e in events if e["action"] == "policy.evaluate"}


def _broken_markers(ledger: Ledger) -> list[dict[str, Any]]:
    return [e for e in ledger.audit()["events"] if e["action"] == "audit.chain.broken"]


async def test_n_signed_records_are_n_verified_rows(tmp_path: Path) -> None:
    worm_dir = tmp_path / "data" / "worm"
    _, key = _signed_chain(worm_dir, 6)
    ledger = await open_ledger(tmp_path / "data", key)

    assert _verdicts(ledger) == dict.fromkeys(range(6), True)
    totals = ledger.audit()["totals"]
    assert (totals["total"], totals["verified"], totals["broken"]) == (6, 6, 0)


async def test_one_flipped_byte_breaks_that_row_and_every_later_row_with_one_marker(
    tmp_path: Path,
) -> None:
    """Tampering is not local: every record after the edit is unproven, and said once.

    "Once" holds across a re-scan AND across an arcui restart, whose fresh
    verifier walks the chain from genesis and finds the same break again.
    """
    from arcstore.backends.memory import FakeBackend

    worm_dir = tmp_path / "data" / "worm"
    chain, key = _signed_chain(worm_dir, 6)
    _flip_one_byte(chain, 3)
    backend = FakeBackend()
    ledger = await open_ledger(tmp_path / "data", key, backend=backend)

    assert _verdicts(ledger) == {0: True, 1: True, 2: True, 3: False, 4: False, 5: False}
    (marker,) = _broken_markers(ledger)
    assert marker["seq"] == 3
    await ledger.observe.refresh()
    assert len(_broken_markers(ledger)) == 1, "a re-scan reported the same break again"
    restarted = await open_ledger(tmp_path / "data", key, backend=backend)
    assert len(_broken_markers(restarted)) == 1, "a restart reported the same break again"
    totals = ledger.audit()["totals"]
    assert (totals["verified"], totals["broken"]) == (3, 1)


async def test_a_rotated_chain_verifies_as_one_chain_with_no_duplicates(tmp_path: Path) -> None:
    worm_dir = tmp_path / "data" / "worm"
    _, key = _signed_chain(worm_dir, 7, max_records=3)
    assert len(list(worm_dir.glob("audit-chain-olivia*.jsonl"))) == 3, "the chain never rotated"
    ledger = await open_ledger(tmp_path / "data", key)
    await ledger.observe.refresh()

    events = [e for e in ledger.audit()["events"] if e["action"] == "policy.evaluate"]
    assert sorted(e["seq"] for e in events) == list(range(7)), "a segment row was lost or doubled"
    assert all(e["verified"] for e in events), "a rotated segment did not verify"
    assert _broken_markers(ledger) == []


async def test_reverify_after_repair_restores_every_verdict(tmp_path: Path) -> None:
    """The operator repairs the file from a good copy, presses Re-verify, and is green."""
    from arcstore.ingest import UI_WORM_FILENAME
    from arctrust.audit import WormSink
    from arctrust.keypair import generate_keypair
    from arctrust.signer import InProcessSigner
    from arcui.audit import MutationWormWriter

    worm_dir = tmp_path / "data" / "worm"
    chain, key = _signed_chain(worm_dir, 5)
    original = _flip_one_byte(chain, 2)
    ui_sink = WormSink(
        worm_dir.parent / "ui" / UI_WORM_FILENAME, InProcessSigner(generate_keypair().private_key)
    )
    writer = MutationWormWriter(sink=ui_sink, operator_did="did:arc:operator:journey")
    ledger = await open_ledger(tmp_path / "data", key, writer)
    assert _verdicts(ledger) == {0: True, 1: True, 2: False, 3: False, 4: False}

    chain.write_bytes(original)
    response = ledger.client.post("/api/team/audit/reverify", headers=_as("operator"))
    assert response.status_code == 200, response.text
    assert response.json()["verified"] == 5 and response.json()["broken"] == 0
    assert _verdicts(ledger) == dict.fromkeys(range(5), True)
    ui_sink.close()
