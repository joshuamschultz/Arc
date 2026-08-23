"""The runner actually starts and actually progresses a run (SPEC-061 COMP-009).

This is the anti-dead-wiring test. Everything else in the suite proves the
engine decides correctly *when driven*; this proves something drives it. A green
suite that never starts a runner is precisely the failure this file exists to
make loud: the host failed open, no runner ever started, and every gate the
engine enforces was bypassed by simply never running.

So: real ArcStore backend, real arcstore RunStore and TaskStore, real
DefinitionStore over a real signed-on-disk bundle, real RunnerIdentity from a
real operator key, and the real ``start_runner_host`` the gateway bootstrap
calls. No doubles anywhere in the construction path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import TaskStore
from arctrust import OperatorKey
from arctrust.paths import arc_state, default_operator_key_path, workflows_dir

from arcteam.workflow.runner import build_workflow_runner

# arcteam sits BELOW arcgateway and must never require it — the host half is
# proved when the full workspace is installed, and skipped when it is not, so
# arcteam's suite stays independently runnable.
_host = pytest.importorskip("arcgateway.workflow_runner_host")
RunnerHost = _host.RunnerHost
start_runner_host = _host.start_runner_host

WORKFLOW = """
[workflow]
id = "onboarding"
version = 1
description = "Two nodes, two owners."
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"

[[node]]
id = "verify"
kind = "agent"
agent = "@ops"
needs = ["collect"]
"""

SALES_DID = "did:arc:local:agent/1111aaaa"
OPS_DID = "did:arc:local:agent/2222bbbb"


class Registry:
    """The one method the runner asks of the entity registry."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    async def get(self, ref: str) -> Any:
        did = self._mapping.get(ref)
        return None if did is None else type("Entity", (), {"did": did})()


@pytest.fixture
async def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real workspace: operator key, workflow bundle, and store backend.

    ``ARC_CONFIG_DIR`` points at that workspace so the key the host resolves for
    itself — :func:`arctrust.paths.default_operator_key_path` — is written here.
    Left unset, the host reaches into the developer's real ``~/.arc`` and this
    file's verdict depends on whether that machine happens to have a key, and on
    whatever any earlier test in the process left the variable pointing at.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    key_path = default_operator_key_path(tmp_path)
    OperatorKey.generate().save(key_path)

    bundle = workflows_dir(tmp_path) / "onboarding"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(WORKFLOW)

    backend = FakeBackend()
    await backend.start()
    # The workspace root is the STATE root, matching both production callers:
    # `arc workflow` passes arc_state(arc_dir), and the gateway host derives it
    # from the operator key path. A test on a third root proves nothing.
    yield arc_state(tmp_path), key_path, backend
    await backend.stop()


@pytest.fixture(autouse=True)
async def _no_leftover_host() -> Any:
    yield
    host = RunnerHost.active()
    if host is not None:
        await host.stop()


async def test_start_runner_host_returns_a_live_host(deployment: Any) -> None:
    """The acceptance bar: not None, and actually running."""
    root, key_path, backend = deployment

    async def factory(*, tier: str, key_path: Path) -> Any:
        return build_workflow_runner(
            tier=tier,
            task_store_backend=backend,
            runner_key_path=key_path,
            workspace_root=root,
            registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
        )

    host = await start_runner_host(tier="personal", runner_factory=factory)

    assert host is not None, "the gateway started with NO runner — the feature is dead wiring"
    assert RunnerHost.active() is host
    await host.stop()


async def test_the_default_factory_finds_the_real_build_function() -> None:
    """The host resolves ``build_workflow_runner`` by name; prove that name exists.

    A plural-vs-singular module typo or a renamed factory is exactly how this
    feature was dead before, and the failure was silent.
    """
    import importlib

    module = importlib.import_module("arcteam.workflow.runner")
    assert hasattr(module, "build_workflow_runner")


async def test_a_live_runner_progresses_a_real_run_end_to_end(deployment: Any) -> None:
    """Start a run, let the TICK drive it, and watch both nodes materialize."""
    root, key_path, backend = deployment
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
    )
    tasks = TaskStore(backend)

    run = await runner.start_run("onboarding", input={}, initiator_did="did:arc:local:user/9999")
    assert run.status == "running"

    # The first node materialized as a real task row owned by the named agent.
    first = await tasks.get(f"wf/{run.run_id}/collect/0")
    assert first is not None and first.owner_did == SALES_DID

    # The owning agent does its work; the TICK — not a direct advance call — is
    # what moves the run forward, because that is what the host actually runs.
    await tasks.start_task(first.id, SALES_DID)
    await tasks.finish(
        first.id, status="done", resolution="done", actor_did=SALES_DID, output={"ok": True}
    )
    assert await runner.tick() == 1

    second = await tasks.get(f"wf/{run.run_id}/verify/0")
    assert second is not None, "the frontier did not advance"
    assert second.owner_did == OPS_DID, "the handoff must name the next node's owner"

    await tasks.start_task(second.id, OPS_DID)
    await tasks.finish(
        second.id, status="done", resolution="done", actor_did=OPS_DID, output={"ok": True}
    )
    await runner.tick()

    final = await runner._runs.get(run.run_id)
    assert final is not None and final.status == "done"


async def test_the_run_gets_a_shared_workspace(deployment: Any) -> None:
    """D-539: work product is files on a shared desk, not payloads in a bus."""
    root, key_path, backend = deployment
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
    )

    run = await runner.start_run("onboarding", input={}, initiator_did="did:arc:local:user/9")

    assert (root / "shared" / "runs" / run.run_id).is_dir()


async def test_run_forever_ticks_until_cancelled(deployment: Any) -> None:
    """The host runs this forever; a tick failure must not end the loop."""
    root, key_path, backend = deployment
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
    )
    ticks = 0
    original = runner.tick

    async def counting_tick() -> int:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise RuntimeError("a poisoned run")
        return await original()

    runner.tick = counting_tick  # type: ignore[method-assign]
    task = asyncio.create_task(runner.run_forever(interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ticks > 1, "a raising tick must not stop the engine"


async def test_a_runner_without_an_identity_refuses_to_build(tmp_path: Path) -> None:
    """No operator key means no attributable rows — do not start (REQ-232)."""
    from arcteam.workflow.identity import RunnerIdentityUnavailableError

    backend = FakeBackend()
    await backend.start()
    try:
        with pytest.raises(RunnerIdentityUnavailableError):
            build_workflow_runner(
                tier="personal",
                task_store_backend=backend,
                runner_key_path=tmp_path / "missing" / "operator.key",
            )
    finally:
        await backend.stop()


# ---------------------------------------------------------------------------
# The two dead wires: owner resolution and narration (D-511, D-536)
# ---------------------------------------------------------------------------

CHANNEL_WORKFLOW = """
[workflow]
id = "narrated"
version = 1
description = "One node, bound to a channel."
owner = "@sales"
channel = "channel://onboarding"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"
"""


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: Any) -> Any:
    """A deployment wired the way the gateway wires one, on tmp paths.

    Only the path resolvers and the bus factory are redirected. Everything else
    — the default factory, the team bindings, the registry, the messenger — is
    the real production path, because the bug being guarded lives exactly there.
    """
    from arctrust import OperatorKey

    from arcteam.audit import AuditLogger
    from arcteam.registry import EntityRegistry
    from arcteam.storage import MemoryBackend
    from arcteam.types import Entity, EntityType

    key_path = default_operator_key_path(tmp_path)
    key = OperatorKey.generate()
    key.save(key_path)

    bundle = workflows_dir(tmp_path) / "narrated"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(CHANNEL_WORKFLOW)

    team_backend = MemoryBackend()
    audit = AuditLogger(team_backend, key.into_signer())
    await audit.initialize()
    registry = EntityRegistry(team_backend, audit)
    await registry.register(
        Entity(
            did=SALES_DID,
            handle="sales",
            id="agent://sales",
            name="Sales",
            type=EntityType.AGENT,
            public_key="00" * 32,
        )
    )

    import arcgateway.workflow_runner_host as host_mod

    arcstore_backend = FakeBackend()
    await arcstore_backend.start()

    async def _shared_backend(url: str) -> Any:
        return team_backend

    monkeypatch.setattr(host_mod, "_resolve_runner_key_path", lambda: key_path)
    monkeypatch.setattr(host_mod, "_nats_url", lambda: "")
    monkeypatch.setattr("arcstore.backends.open_backend", lambda: arcstore_backend)
    # Patch the name the host actually calls. The host reaches ArcAgent through
    monkeypatch.setattr("arcteam.composition.make_backend", _shared_backend)

    yield tmp_path, key_path, team_backend, registry


async def test_the_default_factory_resolves_a_node_owner(wired: Any) -> None:
    """A runner from the REAL default path must own its rows to a real agent."""
    host = await start_runner_host(tier="personal")
    assert host is not None

    owners = host._runner._owners
    assert await owners.resolve_owner("@sales") == SALES_DID, (
        "the default factory wired no registry — every run fails at node 1"
    )
    await host.stop()


async def test_the_default_factory_narrates_to_the_bound_channel(wired: Any) -> None:
    """A run through the real path must actually post to its channel."""
    host = await start_runner_host(tier="personal")
    assert host is not None
    runner = host._runner

    run = await runner.start_run("narrated", input={}, initiator_did="did:arc:local:user/9999")

    messenger = runner._narrator._sender
    posted = await messenger.list_channel_messages("onboarding")
    bodies = [m.body for m in posted]
    assert bodies, "narration was wired but nothing reached the channel"
    assert any("Run started" in b for b in bodies)
    assert any("collect" in b for b in bodies), "the handoff was not narrated"
    assert all(m.action_required is False and m.mentions == [] for m in posted)
    assert run.status == "running"
    await host.stop()


async def test_two_runs_of_one_workflow_take_the_identical_path(deployment: Any) -> None:
    """T-864 — the whole point of the feature: the same process, the same way, twice.

    A workflow that improvises is a workflow you cannot audit or hold a process
    to. Both runs must materialize the same nodes, to the same owners, in the
    same order, against the same definition version and content hash.

    Deliberately drives the real machinery: real store, real bundle, real
    operator key, real runner, advanced through the same ``tick`` the gateway
    host runs.
    """
    root, key_path, backend = deployment
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
    )
    tasks = TaskStore(backend)

    async def drive_one_run() -> tuple[list[tuple[str, str | None]], int, str]:
        run = await runner.start_run(
            "onboarding", input={}, initiator_did="did:arc:local:user/9999"
        )
        # Node rows are keyed wf/<run>/<node>/<iteration>; Task.run_id is the
        # arcrun correlation id, a different thing from the flow run id.
        prefix = f"wf/{run.run_id}/"
        for _ in range(6):
            rows = [t for t in await tasks.list() if t.id.startswith(prefix)]
            pending = [t for t in rows if t.status not in {"done", "failed"}]
            if not pending:
                break
            for row in pending:
                await tasks.start_task(row.id, row.owner_did or "")
                await tasks.finish(
                    row.id,
                    status="done",
                    resolution="ok",
                    actor_did=row.owner_did or "",
                    output={"ok": True},
                )
            await runner.tick()
        final = await runner._runs.get(run.run_id)
        rows = sorted(
            (t for t in await tasks.list() if t.id.startswith(prefix)),
            key=lambda t: t.created_at or "",
        )
        order = [(t.metadata.get("node_id", "?"), t.owner_did) for t in rows]
        return order, final.version, final.content_hash

    first_order, first_version, first_hash = await drive_one_run()
    second_order, second_version, second_hash = await drive_one_run()

    assert first_order, "the first run materialized no nodes at all"
    assert first_order == second_order, (
        f"the same workflow took different paths: {first_order} then {second_order}"
    )
    assert [n for n, _ in first_order] == ["collect", "verify"], (
        f"nodes ran out of declared order: {first_order}"
    )
    assert (first_version, first_hash) == (second_version, second_hash), (
        "both runs must pin the same definition version and content hash"
    )


async def test_a_lost_journal_row_raises_instead_of_mis_accounting(
    deployment: Any,
) -> None:
    """The companion row carries settle/skip/route bookkeeping, not just a trace.

    Dropping an append silently would double-count spend and re-decide branches
    on the next tick, so the store raises and the tick fails THIS run loudly.
    """
    from arcteam.workflow.stores import RunStateMissingError, WorkflowRunStore

    root, key_path, backend = deployment
    runs = WorkflowRunStore(backend)
    await runs.create_run(
        run_id="run-orphan",
        workflow_id="onboarding",
        version=1,
        content_hash="sha256:x",
        initiator_did="did:arc:local:user/9",
        channel=None,
        input={},
        budget_tokens=None,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )
    await backend.mutable_delete(
        "workflow_run_state", "run-orphan", actor_did="did:arc:local:user/9"
    )

    with pytest.raises(RunStateMissingError):
        await runs.append_path(
            "run-orphan",
            {"kind": "settled", "node_id": "a", "iteration": 0, "tokens": 10},
            actor_did="did:arc:local:user/9",
        )


async def test_the_definition_stores_events_reach_the_audit_chain(
    deployment: Any,
) -> None:
    """An unsigned run at personal tier must leave a record that it happened.

    The store emits `workflow.unsigned_run_permitted` on the dispatch path this
    runner drives every tick, and it emits nothing at all unless an audit hook
    is wired at construction. The composition root is the only place that can
    wire it, and nothing did — so a deployment ran unsigned definitions and no
    record of that fact existed anywhere.

    Asserting the event LANDS, not that the hook is present: presence is what
    let every other dead wire in this feature hide.
    """
    root, key_path, backend = deployment
    events: list[Any] = []

    class Sink:
        def write(self, event: Any) -> None:
            events.append(event)

    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
        audit_sink=Sink(),
    )

    await runner.start_run("onboarding", input={}, initiator_did="did:arc:local:user/9")

    actions = [e.action for e in events]
    assert "workflow.unsigned_run_permitted" in actions, (
        "an unsigned run left no trace — the store's audit hook is unwired"
    )
    recorded = next(e for e in events if e.action == "workflow.unsigned_run_permitted")
    assert recorded.tier == "personal"
    assert recorded.target == "onboarding"


async def test_the_runner_records_the_tier_it_actually_enforces(
    deployment: Any,
) -> None:
    """Posture must be a matter of record, not something you read code to learn.

    The runner's tier arrives from the gateway's config while every agent's
    stringency comes from a different setting, and nothing reconciles them. That
    may be legitimate, but a fleet hardened to federal whose dispatch component
    is still personal would otherwise be invisible: the store permits unsigned
    definitions with a warning and everything looks healthy.
    """
    root, key_path, backend = deployment
    events: list[Any] = []

    class Sink:
        def write(self, event: Any) -> None:
            events.append(event)

    build_workflow_runner(
        tier="federal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
        audit_sink=Sink(),
    )

    recorded = next(e for e in events if e.action == "workflow.runner.constructed")
    assert recorded.tier == "federal"
    assert recorded.extra["signed_definitions_required"] is True

    events.clear()
    build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
        audit_sink=Sink(),
    )
    weaker = next(e for e in events if e.action == "workflow.runner.constructed")
    assert weaker.tier == "personal"
    assert weaker.extra["signed_definitions_required"] is False


# ---------------------------------------------------------------------------
# The composition root supplies the real security parameter (not just honours it)
# ---------------------------------------------------------------------------


def _sign_with(key: Any, root: Path, tier: str, workflow_id: str) -> Any:
    """Sign the bundle with an arbitrary key, the way the operator CLI would."""
    from arcteam.workflow.store import DefinitionStore, sign_definition

    store = DefinitionStore(root / "workflows", tier=tier)
    return sign_definition(
        store,
        workflow_id,
        signer_did="did:arc:local:operator/test",
        private_key=key.seed,
    )


async def test_the_root_pins_the_operator_key_so_signed_means_operator_signed(
    deployment: Any,
) -> None:
    """A bundle signed by the DEPLOYMENT key verifies through the real factory.

    The component correctly honours whatever key it is given; nothing proved the
    right key arrives. That gap is structural to construction-time injection —
    the failure moves out of the component and into whoever composes it, where
    the component's own tests can never see it. This is that missing test.
    """
    from arctrust import OperatorKey

    root, key_path, backend = deployment
    _sign_with(
        OperatorKey.load(key_path, generate_if_absent=False), root, "personal", "onboarding"
    )

    runner = build_workflow_runner(
        tier="federal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
    )
    bundle = runner._definitions.load("onboarding")

    assert bundle.is_verified, "the deployment key's own signature must verify"
    assert bundle.status == "signed"


async def test_a_foreign_signature_is_not_trusted_through_the_real_factory(
    deployment: Any,
) -> None:
    """An agent self-signing its own workflow must not produce a trusted bundle.

    With no key pinned this passed as verified — trust-on-first-use at the
    composition root. The pin is what makes 'signed' mean 'signed BY THE
    OPERATOR' rather than 'carries some signature'.
    """
    from arctrust import OperatorKey

    root, key_path, backend = deployment
    _sign_with(OperatorKey.generate(), root, "personal", "onboarding")

    runner = build_workflow_runner(
        tier="federal",
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
    )
    bundle = runner._definitions.load("onboarding")

    assert not bundle.is_verified, "a foreign key must never read as verified"

    # Fail-closed at the store's own gate, before the runner's tier check even
    # runs — two independent refusals, and the foreign key clears neither.
    with pytest.raises(Exception, match="not signed by the deployment operator key"):
        await runner.start_run("onboarding", input={}, initiator_did="did:arc:local:user/9")
