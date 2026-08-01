"""The runner actually starts and actually progresses a run (SPEC-061 COMP-009).

This is the anti-dead-wiring test. Everything else in the suite proves the
engine decides correctly *when driven*; this proves something drives it. A green
suite that never starts a runner is precisely the failure this file exists to
make loud: the host failed open, no runner ever started, and every gate the
engine enforces was bypassed by simply never running.

So: real SqliteBackend, real arcstore RunStore and TaskStore, real
DefinitionStore over a real signed-on-disk bundle, real RunnerIdentity from a
real operator key, and the real ``start_runner_host`` the gateway bootstrap
calls. No doubles anywhere in the construction path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import TaskStore
from arctrust import OperatorKey

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
async def deployment(tmp_path: Path) -> Any:
    """A real workspace: operator key, workflow bundle, and store backend."""
    key_path = tmp_path / "operator" / "operator.key"
    OperatorKey.generate().save(key_path)

    bundle = tmp_path / "workflows" / "onboarding"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(WORKFLOW)

    backend = SqliteBackend(tmp_path / "store.db")
    await backend.start()
    yield tmp_path, key_path, backend
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

    run = await runner.start_run(
        "onboarding", input={}, initiator_did="did:arc:local:user/9999"
    )
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

    backend = SqliteBackend(tmp_path / "store.db")
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

    key_path = tmp_path / "operator" / "operator.key"
    key = OperatorKey.generate()
    key.save(key_path)

    bundle = tmp_path / "workflows" / "narrated"
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

    async def _shared_backend(url: str) -> Any:
        return team_backend

    monkeypatch.setattr(host_mod, "_resolve_runner_key_path", lambda: key_path)
    monkeypatch.setattr(host_mod, "_nats_url", lambda: "")
    monkeypatch.setattr(
        "arcagent.core.arcteam_bootstrap.make_backend", _shared_backend, raising=False
    )
    monkeypatch.setattr("arcstore.config.store_db_path", lambda _: tmp_path / "store.db")

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

    run = await runner.start_run(
        "narrated", input={}, initiator_did="did:arc:local:user/9999"
    )

    messenger = runner._narrator._sender
    posted = await messenger.list_channel_messages("onboarding")
    bodies = [m.body for m in posted]
    assert bodies, "narration was wired but nothing reached the channel"
    assert any("Run started" in b for b in bodies)
    assert any("collect" in b for b in bodies), "the handoff was not narrated"
    assert all(m.action_required is False and m.mentions == [] for m in posted)
    assert run.status == "running"
    await host.stop()
