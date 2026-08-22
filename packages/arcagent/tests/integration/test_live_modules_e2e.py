"""The modules a live Arc fleet runs, exercised end to end after SPEC-066.

SPEC-066 takes every module out of the ``arc-agent`` wheel and delivers it as a
separately signed bundle, verified in full, materialized read-only at
the deployment module root, with each module's capability surface
copied per agent into ``<agent_dir>/capabilities/<name>/``. That is a change to
how an agent *acquires its abilities*, so "the unit tests pass" is not evidence
that a deployed agent still has them.

The blueprints an operating fleet runs enable **memory, tasks, scheduler,
skills** and **messaging**. Every case that needs a module installs it the way
an operator does — :func:`arcbundle.build_bundle` →
:func:`arcbundle.verify_bundle` → :func:`arcbundle.materialize` →
:func:`arcbundle.copy_capabilities`, the exact four calls ``arc module install``
makes — and then boots a **real** :class:`ArcAgent` over the result. The
upgrade-path cases at the end install nothing on purpose: an empty module root
under a config that still enables five modules is the state every existing box
wakes up in after this ships.

**Only the LLM is stubbed.** Real identity, real operator key, real signed
bundles, real capability scan, real module runtimes, real tool registry, real
bus, real turn dispatch. Nothing reaches the network: ``ARC_CONFIG_DIR``,
``ARCSTORE_DATA_DIR`` (autouse in ``tests/conftest.py``), the identity key dir
and the operator key dir all point inside ``tmp_path``, and the memory backend
runs with its embedder switched off so no model is fetched.

Isolating the arc home matters more here than in most suites: the operator key
dir defaults to a literal ``~/.arc/operator`` — *not* ``ARC_CONFIG_DIR`` — and
is generated on first use, so a test that leaves it at the default writes the
developer's real arc home. :func:`_config` overrides it explicitly.

Three questions are asked per module, because passing two of them is not the
same as working: is the runtime *configured* (not merely discovered), are the
module's tools *registered in the live agent's registry by name*, and does a
turn *complete* with the module active. The behaviour cases that follow ask the
question each module would actually be reported broken for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcbundle
import pytest
from arcrun import StreamEvent, ToolContext, TurnEndEvent
from arcstore.backends.memory import FakeBackend
from arctrust import ValidatorsConfig, generate_keypair
from arctrust.paths import identity_dir, module_root, operator_dir

from arcagent.core import turn_context
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    SecurityConfig,
    TelemetryConfig,
)

# The repository source tree modules are bundled FROM. Resolved from the repo
# layout, never from ``arcagent.__file__``: SPEC-066 removes ``modules/`` from
# the built wheel, and this suite has to keep packaging real modules at exactly
# the moment the installed package stops carrying them.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"

#: The modules the shipping blueprints enable, plus messaging (how the fleet is
#: actually talked to). These are the names an upgrade is allowed to break only
#: over this file's dead body.
_LIVE_MODULES = ("memory", "tasks", "scheduler", "skills", "messaging")

#: The tool names each module must contribute to the live agent's ToolRegistry.
#: ``skills`` is deliberately empty — it registers hooks and a background task
#: and no tools at all, so demanding a tool from it would be a fiction. Its
#: surface is asserted separately in :func:`test_skills_module_registers_its_hooks`.
_EXPECTED_TOOLS: dict[str, frozenset[str]] = {
    "memory": frozenset({"memory_search"}),
    "tasks": frozenset(
        {
            "create_task",
            "update_task",
            "start_task",
            "complete_task",
            "fail_task",
            "assign_task",
            "claim_task",
            "list_tasks",
            "decompose_task",
            "set_task_output",
        }
    ),
    "scheduler": frozenset(
        {"schedule_create", "schedule_list", "schedule_update", "schedule_cancel"}
    ),
    "skills": frozenset(),
    "messaging": frozenset(
        {
            "notify_user",
            "messaging_send",
            "messaging_check_inbox",
            "messaging_read_thread",
            "messaging_list_entities",
            "messaging_list_channels",
            "store_team_file",
            "list_team_files",
        }
    ),
}

#: Module config overrides. Everything here is either an operator setting a real
#: deployment sets too (``dispatch``, ``check_interval_seconds``) or a switch
#: that keeps the test off the network (``embed_backend``). None of it changes
#: which code path runs.
_MODULE_CONFIG: dict[str, dict[str, Any]] = {
    # A real Brain, so a capture/recall round trip is a real round trip.
    # ``embed_backend = "none"`` drops the vector channel (BM25 + graph remain),
    # which is what keeps this offline — the embedder would otherwise fetch a
    # sentence-transformers model from the HF hub on first use.
    "memory": {"brain": "arcmemory", "embed_backend": "none"},
    # ``dispatch`` is the operator opt-in that starts the autonomous loop; the
    # loop exists but does nothing without it, and "nothing" would pass a test
    # that only checked the task was spawned.
    "tasks": {"enabled": True, "dispatch": True},
    # Whole seconds by design in production; one second here so a due schedule
    # is observed firing inside a test rather than inside half a minute.
    "scheduler": {"enabled": True, "check_interval_seconds": 1},
    "skills": {},
    "messaging": {},
}

_ISSUER = "did:arc:test-operator"

#: The issuer key every bundle in this file is signed with, minted once so the
#: agent config can pin it the way ``arc module install`` does. Pinning matters:
#: since T-972 a ``module:*`` root is VERIFIED, so each module's
#: ``capabilities.py`` must carry a sidecar that verifies under a key this agent
#: trusts — otherwise the module materializes and then registers nothing.
_ISSUER_KEYPAIR = generate_keypair()


# --------------------------------------------------------------------------
# Deployment construction — the real SPEC-066 install path
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Deployment:
    """The on-disk shape a booted agent reads: arc home, agent dir, workspace."""

    arc_home: Path
    agent_dir: Path
    workspace: Path

    @property
    def modules_root(self) -> Path:
        # Through the resolver: a bundle materialized anywhere else is a bundle
        # discovery never sees, which is the whole failure this suite watches for.
        return module_root(self.arc_home)

    @property
    def config_path(self) -> Path:
        return self.agent_dir / "arcagent.toml"

    def capability_copy(self, module: str) -> Path:
        # Derived from the shipping path rule rather than restated, so a change
        # to where copies land shows up as a real failure here instead of this
        # helper quietly disagreeing with the code under test.
        from arcbundle import capability_dir

        return capability_dir(self.agent_dir, module)


def _deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Deployment:
    """Lay out an empty deployment and point ``ARC_CONFIG_DIR`` at it.

    ``ARC_CONFIG_DIR`` is the only lever that moves the module root, so setting
    it is what makes every case below reach the deployment root the way a real
    install does rather than through a patched discovery seam.
    """
    arc_home = tmp_path / "arc"
    agent_dir = tmp_path / "agent"
    workspace = tmp_path / "ws"
    for path in (arc_home, agent_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    return Deployment(arc_home=arc_home, agent_dir=agent_dir, workspace=workspace)


def _install(deployment: Deployment, names: tuple[str, ...], tmp_path: Path) -> None:
    """Install ``names`` exactly as ``arc module install`` does.

    Build a signed bundle from the source catalog, verify it in full against a
    trusted issuer, materialize it read-only at the deployment module root, and
    copy its capability surface into the agent's own capabilities root. Any gate
    that would refuse an operator refuses here, because these are the same four
    calls behind the same four failures.
    """
    staging = tmp_path / "bundles"
    for name in names:
        bundle = arcbundle.build_bundle(
            _SOURCE_CATALOG / name,
            module=name,
            version="1.0.0",
            private_key=_ISSUER_KEYPAIR.private_key,
            issuer=_ISSUER,
            out=staging / name,
        )
        verified = arcbundle.verify_bundle(
            bundle,
            tier="personal",
            trusted_issuers={_ISSUER: _ISSUER_KEYPAIR.public_key},
        )
        installed = arcbundle.materialize(verified, deployment.modules_root)
        arcbundle.copy_capabilities(installed, deployment.agent_dir, module=name)


def _config(
    deployment: Deployment,
    names: tuple[str, ...],
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> ArcAgentConfig:
    """An agent config enabling exactly ``names``.

    Every key/state directory is pinned inside the deployment. ``operator_key_dir``
    especially: it defaults to a literal ``~/.arc/operator`` that ignores
    ``ARC_CONFIG_DIR`` and is generated on first use, so leaving it alone writes
    into the developer's real home.
    """
    module_config = dict(_MODULE_CONFIG)
    module_config.update(overrides or {})
    return ArcAgentConfig(
        agent=AgentConfig(
            name="live-modules-agent",
            org="testorg",
            type="executor",
            workspace=str(deployment.workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(identity_dir(deployment.arc_home))),
        security=SecurityConfig(
            operator_key_dir=str(operator_dir(deployment.arc_home)),
            # The bundle issuer's key, pinned exactly as ``arc module install``
            # pins it. Without it the loader cannot verify any module capability
            # signature and every module goes dark at load.
            validators=ValidatorsConfig(trusted_keys=(_ISSUER_KEYPAIR.public_key.hex(),)),
        ),
        # Telemetry ON. It is on in every real deployment, the audit trail is one
        # of the four pillars, and a suite that switched it off would be unable to
        # tell "this operation emits no audit event" from "auditing was disabled".
        telemetry=TelemetryConfig(enabled=True, export_traces=False),
        modules={
            name: ModuleEntry(enabled=True, config=dict(module_config.get(name, {})))
            for name in names
        },
    )


async def _one_token_turn(*args: Any, **kwargs: Any) -> Any:
    """Stand in for ``arcrun.run_stream`` — the only stub in this file."""

    async def _events() -> AsyncIterator[StreamEvent]:
        yield TurnEndEvent(final_text="live-modules-ok", tool_calls_made=0)

    return _events()


@asynccontextmanager
async def _booted(
    deployment: Deployment,
    config: ArcAgentConfig,
    *,
    audit_spy: list[tuple[str, dict[str, Any]]] | None = None,
) -> AsyncIterator[ArcAgent]:
    """Start a real agent over ``deployment`` and shut it down afterwards.

    The ``arcrun.run_stream`` patch stays active for the whole body, not only
    around an explicit ``agent.run(...)``: the tasks dispatch loop and the
    scheduler engine drive turns of their own from background tasks, and those
    turns must reach the stub rather than a provider.

    ``audit_spy``, when given, collects every ``AgentTelemetry.audit_event``
    call made from construction onwards — including the ones emitted during
    startup, which is the only window in which the enabled-but-absent module
    decision is made.
    """
    model = MagicMock()
    model.close = AsyncMock()
    stack: list[Any] = [
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_one_token_turn),
    ]
    if audit_spy is not None:
        stack.append(_recording_audit(audit_spy))
        stack.append(_recording_arctrust_audit(audit_spy))

    for entered in stack:
        entered.__enter__()
    # The production opener is intentionally PostgreSQL-only.  This suite is
    # offline and exercises module wiring, so inject one explicit backend for
    # the agent's lifetime instead of relying on a removed SQLite fallback.
    backend = FakeBackend()
    await backend.start()

    async def open_test_backend() -> FakeBackend:
        return backend

    agent = ArcAgent(config=config, config_path=deployment.config_path)
    try:
        with patch.object(agent, "_make_arcstore_opener", return_value=open_test_backend):
            await agent.startup()
        yield agent
    finally:
        try:
            await agent.shutdown()
        finally:
            await backend.stop()
            for entered in reversed(stack):
                entered.__exit__(None, None, None)


def _recording_audit(sink: list[tuple[str, dict[str, Any]]]) -> Any:
    """Patch that records every audit event without changing what it does."""
    from arcagent.core.telemetry import AgentTelemetry

    original = AgentTelemetry.audit_event

    def _spy(self: AgentTelemetry, action: str, detail: dict[str, Any] | None = None) -> Any:
        recorded = dict(detail or {})
        sink.append((action, recorded))
        return original(self, action, recorded)

    return patch.object(AgentTelemetry, "audit_event", _spy)


def _recording_arctrust_audit(sink: list[tuple[str, dict[str, Any]]]) -> Any:
    """Patch that records the arctrust single-emission point, unchanged.

    The other half of "was this audited anywhere": ``AgentTelemetry.audit_event``
    is arcagent's route, ``arctrust.audit.emit`` is the pillar's declared single
    emission point that every sink fans out from. Watching only one of the two
    would leave "it went out the other way" as an untested excuse.
    """
    import arctrust.audit as arctrust_audit

    original = arctrust_audit.emit

    def _spy(event: Any, audit_sink: Any) -> Any:
        sink.append((str(getattr(event, "action", event)), {}))
        return original(event, audit_sink)

    return patch.object(arctrust_audit, "emit", _spy)


# --------------------------------------------------------------------------
# Observation helpers — read the live agent, never recompute what it should be
# --------------------------------------------------------------------------


def _runtime_of(name: str) -> ModuleType:
    """The runtime object the agent actually loaded for ``name``.

    Read out of ``sys.modules`` under the canonical dotted name because that is
    where ``load_module_runtime`` registers it. A second, separately loaded copy
    would own its own ``ContextVar`` and answer about state ``configure`` never
    touched, which is precisely the confusion this lookup avoids.
    """
    dotted = f"arcagent.modules.{name}._runtime"
    runtime = sys.modules.get(dotted)
    assert runtime is not None, f"{dotted} was never loaded — the module runtime did not load"
    return runtime


def _module_scan_roots(agent: ArcAgent) -> set[str]:
    """Module names the capability loader actually took as a scan root."""
    assert agent._capability_loader is not None
    return {
        name.removeprefix("module:")
        for name, _ in agent._capability_loader._scan_roots
        if name.startswith("module:")
    }


def _registered_tools(agent: ArcAgent) -> set[str]:
    assert agent._tool_registry is not None
    return set(agent._tool_registry.tools)


def _tool_context() -> ToolContext:
    return ToolContext(
        run_id=str(uuid.uuid4()),
        tool_call_id=str(uuid.uuid4()),
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )


async def _call_tool(agent: ArcAgent, name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool through the same conversion the loop is handed.

    ``to_arcrun_tools`` is what the agent gives arcrun, so argument validation,
    the bus events, the timeout and the audit emission all run exactly as they
    do in production. Reaching for the underlying function instead would prove
    the function works and say nothing about the wire.
    """
    assert agent._tool_registry is not None
    tools = {tool.name: tool for tool in agent._tool_registry.to_arcrun_tools()}
    assert name in tools, f"{name} is not registered; available: {sorted(tools)}"
    return await tools[name].execute(args, _tool_context())


async def _run_a_turn(agent: ArcAgent, key: str = "live-modules") -> list[StreamEvent]:
    session = await agent.session(key)
    return [event async for event in agent.run("say hello", session=session)]


async def _until(
    predicate: Callable[[], bool | Any], *, timeout: float, poll: float = 0.05
) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses.

    Wall-clock rather than a fixed number of sleeps: what is being waited on is
    a background loop's own cadence, and a count of sleeps would silently pass
    on a machine that never got round to running it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return bool(predicate())


# --------------------------------------------------------------------------
# 1. Per module: runtime configured, tools registered, a turn completes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module", _LIVE_MODULES)
async def test_module_installed_from_a_signed_bundle_is_live_in_the_agent(
    module: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One module, installed alone, is configured + registered + survives a turn.

    Installed alone on purpose. SPEC-066 makes each module a separately signed,
    separately installed unit, so "works when all five are present" would not
    tell an operator whether the one they installed works. A failure here names
    exactly one module.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, (module,), tmp_path)
    config = _config(deployment, (module,))

    async with _booted(deployment, config) as agent:
        # The runtime is configured, not merely discovered: state() raises if
        # configure() never ran for this agent.
        state = _runtime_of(module).state()
        assert state is not None

        assert _module_scan_roots(agent) == {module}
        missing = _EXPECTED_TOOLS[module] - _registered_tools(agent)
        assert not missing, f"{module} did not register {sorted(missing)}"

        events = await _run_a_turn(agent)

    assert isinstance(events[-1], TurnEndEvent)
    assert events[-1].final_text == "live-modules-ok"


async def test_the_whole_blueprint_module_set_boots_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All five at once: the shape a fleet agent actually boots in.

    Modules are independent by design, and independence is exactly the property
    that stops being checked when each is only ever tested alone.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, _LIVE_MODULES, tmp_path)
    config = _config(deployment, _LIVE_MODULES)

    async with _booted(deployment, config) as agent:
        assert _module_scan_roots(agent) == set(_LIVE_MODULES)
        registered = _registered_tools(agent)
        for module in _LIVE_MODULES:
            missing = _EXPECTED_TOOLS[module] - registered
            assert not missing, f"{module} did not register {sorted(missing)}"
            assert _runtime_of(module).state() is not None

        events = await _run_a_turn(agent)

    assert isinstance(events[-1], TurnEndEvent)


async def test_the_capability_surface_is_copied_into_the_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-agent copy lands, carries the tools, and leaves the runtime behind.

    ``_runtime.py`` staying at the deployment root is the property that keeps an
    agent from being handed its own execution path (ASI05/ASI06), so it is
    asserted as an absence, not assumed from the copy's docstring.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, _LIVE_MODULES, tmp_path)

    for module in _LIVE_MODULES:
        copied = deployment.capability_copy(module)
        assert (copied / "capabilities.py").is_file(), f"{module} capability surface missing"
        assert not (copied / "_runtime.py").exists(), f"{module} runtime leaked into the agent dir"
        assert (deployment.modules_root / module / "_runtime.py").is_file()


# --------------------------------------------------------------------------
# 2. Memory — a write and a recall round-trip on the agent's own DID
# --------------------------------------------------------------------------


async def test_memory_captures_and_recalls_for_the_agents_own_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture through the live bus, recall through the live tool.

    Both halves go through the agent: the capture hook is reached by emitting
    the real ``agent:post_respond`` event on the real module bus, and the recall
    is the registered ``memory_search`` tool dispatched through
    ``to_arcrun_tools``. Nothing here calls the Brain directly, so a break
    anywhere in the wiring — hook bridging, ACL gate, DID binding, tool
    registration — shows up as a failed round trip.

    A ``MemoryIsolationError`` ("no agent DID bound") from this test does not
    mean the isolation guard is broken; it means the runtime was never
    configured for this agent, which is the SPEC-066 failure this file exists
    to catch.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)
    config = _config(deployment, ("memory",))
    fact = "The Kestrel deployment root for Arc modules is /srv/kestrel/modules."

    async with _booted(deployment, config) as agent:
        state = _runtime_of("memory").state()
        assert state.active, "memory selected a NullBrain — the round trip below would be a lie"
        assert agent._identity is not None
        assert state.agent_did == agent._identity.did

        # Before the capture, so a pass cannot come from a brain that answers
        # every query with the same text.
        before = await _call_tool(agent, "memory_search", {"query": "Kestrel deployment root"})
        assert "Kestrel" not in before

        assert agent._bus is not None
        await agent._bus.emit("agent:post_respond", {"messages": [{"content": fact}]})

        recalled = await _call_tool(agent, "memory_search", {"query": "Kestrel deployment root"})

    assert "Kestrel" in recalled, f"memory did not recall what it captured: {recalled!r}"


async def test_existing_memory_backfills_the_routing_digest_over_the_real_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Knowledge filed before the digest existed still makes the agent findable.

    A holding already in memory but never announced leaves the agent invisible to
    channel routing — the reason one holding the answer never even tried. Seed a
    semantic holding, re-fire ``agent:ready`` on the real module bus, and the
    messaging digest gains a pointer naming it: the memory-side backfill and the
    cross-module publish are actually wired, not merely each correct in isolation.
    """
    from arcmemory.index.graph import WeightedGraph
    from arcmemory.stores.semantic import SemanticStore

    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory", "messaging"), tmp_path)
    config = _config(deployment, ("memory", "messaging"))

    async with _booted(deployment, config) as agent:
        mem = _runtime_of("memory").state()
        msg = _runtime_of("messaging").state()
        assert mem.active, "memory selected a NullBrain — nothing would backfill"
        did = msg.identity.did

        # A fresh agent has no NNL pointer: startup backfill found nothing to publish.
        before = await msg.digests.get(did)
        assert before is None or all("NNL" not in e.as_document() for e in before.entries)

        # Seed durable knowledge the way a prior day's work would have left it,
        # then re-fire the real startup event over the real module bus.
        store = SemanticStore(mem.brain._workspace, WeightedGraph(mem.brain._db), scope=did)
        store.write_fact("nnl", "requirements", "Rust toolchain and a signed SBOM", name="NNL")
        mem.digest_backfilled = False
        assert agent._bus is not None
        await agent._bus.emit("agent:ready", {})

        published = await msg.digests.get(did)

    assert published is not None, "backfill published no digest at all"
    assert any("NNL" in e.as_document() for e in published.entries), (
        "existing memory did not backfill a routing pointer"
    )


async def test_a_memory_recall_is_recorded_as_a_tool_event_in_the_run_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory lookup the operator could never see now shows in the run trace.

    A recall runs while the prompt is assembled, before the loop's tool dispatch,
    so it never spooled a tool_event and was invisible. The dispatcher now binds
    one run id across assembly and loop, and the recall records itself as an
    implicit tool_event under it — the same record shape a real tool writes, so it
    renders inline with the reads. Drive a real turn and read it back from the
    spool, correlated to a run.
    """
    import arcstore.spool as spool

    spool_file = tmp_path / "trace-spool.jsonl"
    monkeypatch.setattr(spool, "spool_path", lambda **_: spool_file)

    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)
    config = _config(deployment, ("memory",))

    async with _booted(deployment, config) as agent:
        assert _runtime_of("memory").state().active, "memory off — no recall would run"
        await _run_a_turn(agent)

    rows = [json.loads(line) for line in spool_file.read_text().splitlines()]
    recalls = [
        r for r in rows if r.get("tool_name") == "memory_search" and r.get("extra", {}).get("implicit")
    ]
    assert recalls, "a memory recall was not recorded as a tool_event in the run trace"
    assert all(r.get("request_id") for r in recalls), "recall tool_event not correlated to a run"
    assert {"start", "end"} <= {r.get("phase") for r in recalls}


# --------------------------------------------------------------------------
# 3. Tasks — the dispatch loop picks work up, and keeps picking it up
# --------------------------------------------------------------------------


async def _seed_task(agent: ArcAgent, title: str) -> str:
    """Create a task through the live ``create_task`` tool and return its id.

    ``owner`` is omitted deliberately: omitted means "self", which is the only
    ownership the dispatch loop will act on and the only one resolvable without
    a live arcteam registry.
    """
    raw = await _call_tool(agent, "create_task", {"title": title})
    payload = json.loads(raw)
    task_id = payload.get("id")
    assert task_id, f"create_task returned no id: {raw!r}"
    return str(task_id)


async def _task_status(agent: ArcAgent, task_id: str) -> str:
    del agent  # state is resolved off the module runtime, not the agent object
    store = _runtime_of("tasks").state().store
    task = await store.get(task_id)
    return "" if task is None else str(task.status)


async def _await_status_change(agent: ArcAgent, task_id: str, *, timeout: float) -> str:
    """Poll the durable task store until the task leaves ``todo``, or time out."""
    deadline = time.monotonic() + timeout
    status = await _task_status(agent, task_id)
    while status == "todo" and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        status = await _task_status(agent, task_id)
    return status


async def test_tasks_dispatch_loop_picks_up_a_task_created_at_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task already waiting when the loop starts is claimed and run.

    This is the cheap half of the dispatch contract. It proves the wire — store,
    owner resolution, the ``agent:ready`` run-callback binding, the claim — but
    on its own it would also pass for a loop that ran exactly once and died,
    which is a bug this codebase has actually shipped. The next test is the one
    that rules that out.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("tasks",), tmp_path)
    config = _config(deployment, ("tasks",))

    async with _booted(deployment, config) as agent:
        task_id = await _seed_task(agent, "answer the boot-time question")
        # Poll the real store until dispatch moves the task off ``todo``.
        status = await _await_status_change(agent, task_id, timeout=45.0)

    assert status != "todo", "the dispatch loop never claimed a ready task"


@pytest.mark.slow
async def test_tasks_dispatch_loop_iterates_rather_than_running_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop's SECOND tick picks work up — the ``while True`` regression gate.

    ``register_task`` calls the decorated function exactly once, so a
    ``@background_task`` whose body lacks ``while True`` runs one tick at
    startup and dies silently. Every symptom of that bug is invisible to a test
    that seeds work before boot: the single startup tick handles it and the
    suite goes green while the agent has, in fact, stopped dispatching forever.

    So the task here is created only *after* the first tick has demonstrably
    already returned empty. Nothing but a further iteration can claim it. The
    wait is the module's real 15-second cadence, which is why this is marked
    slow rather than sped up — the cadence is not a test seam.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("tasks",), tmp_path)
    config = _config(deployment, ("tasks",))

    async with _booted(deployment, config) as agent:
        # Let the startup tick run and find nothing.
        await asyncio.sleep(1.0)
        task_id = await _seed_task(agent, "answer the question raised after the first tick")
        status = await _await_status_change(agent, task_id, timeout=60.0)

    assert status != "todo", (
        "a task created after the first dispatch tick was never picked up — "
        "the dispatch loop ran once at startup and stopped iterating"
    )


# --------------------------------------------------------------------------
# 4. Scheduler — a due schedule fires
# --------------------------------------------------------------------------


async def test_scheduler_fires_a_due_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create a schedule through the live tool; the engine runs it.

    ``metadata.last_run`` is read back off the schedule store on disk — the same
    file ``arc`` and arcui read — so a pass means the timer loop resolved the
    agent callback, executed the entry, and persisted the outcome. Asserting on
    the engine's in-memory counters instead would survive a store that never
    got written, which is the state an operator sees as "it never fired".
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("scheduler",), tmp_path)
    config = _config(deployment, ("scheduler",))

    async with _booted(deployment, config) as agent:
        state = _runtime_of("scheduler").state()
        assert state.engine is not None, "the scheduler capability never built its engine"
        assert state.engine.running

        def _fired() -> bool:
            return any(entry.metadata.last_run for entry in state.store.load())

        # Nothing has fired yet, so a pass below is a transition rather than a
        # property the store already had.
        assert not _fired()

        await _call_tool(
            agent,
            "schedule_create",
            {"type": "interval", "prompt": "heartbeat", "every_seconds": 3600},
        )
        fired = await _until(_fired, timeout=20.0, poll=0.2)

    assert fired, "a due schedule never fired"


# --------------------------------------------------------------------------
# 5. Skills — a skill in the agent's capability root is discovered and injected
# --------------------------------------------------------------------------


_SKILL_MD = """---
name: kestrel-handover
version: 1.0.0
description: Hand a Kestrel deployment over to the on-call operator without downtime.
triggers: [hand over kestrel, kestrel handover, transfer the deployment]
tools: [read, write]
---

## Resources

(auto-filled by the loader)

## Contract

Inputs you must have:
- The name of the operator taking over.

Outputs the agent must produce:
- A written handover note in the workspace.

## Knowledge

A Kestrel handover is a write, not a restart: the incoming operator needs the
current deployment state in one place before they hold the pager.

## Steps

1. Read the current deployment state.
2. Write the handover note naming the incoming operator.

## Red Flags & Rationalizations

- "They already know the state" — write it down anyway.
- No operator named: stop and ask.

## Validation

The note exists and names the incoming operator.

## Examples

"hand over kestrel to Dana"
"""


def _write_signed_skill(agent: ArcAgent, deployment: Deployment, name: str) -> Path:
    """Write ``SKILL.md`` into the agent's skills root and self-sign it.

    The same two steps ``create_skill`` performs: write the bytes, then write the
    ``.arcsig`` sidecar over exactly those bytes under the agent's own DID.
    """
    from arcagent.capabilities import artifact_signing

    identity = agent._identity
    assert identity is not None and identity.can_sign
    folder = deployment.agent_dir / "capabilities" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    skill_md = folder / "SKILL.md"
    content = _SKILL_MD.encode("utf-8")
    skill_md.write_bytes(content)
    artifact_signing.write_signature(
        skill_md, content, signer_did=identity.did, private_key=identity.signing_seed
    )
    return skill_md


async def test_a_skill_in_the_agent_capability_root_is_discovered_and_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skill loads into the live registry AND reaches the assembled prompt.

    Written and signed after boot with the agent's own DID key, then picked up
    by ``agent.reload()`` — the SPEC-033 self-modification path, which is how a
    skill actually arrives in an agent's own capability root. The agent's
    capabilities root is an UNTRUSTED root, so an unsigned SKILL.md is denied by
    the TOFU gate at every tier; writing one unsigned here would test the gate,
    not the feature.

    Discovery alone is not the feature: a skill the model is never told about
    does nothing. The second half drives the real ``agent:assemble_prompt``
    event over the real bus and reads the section the capability-manifest
    subscriber wrote — the text that actually goes to the provider.

    The ``skills`` MODULE is deliberately not installed here, so this measures
    the skill surface itself. Installing it breaks this exact flow, which is a
    separate finding with its own case below.

    The skills directory is created before boot because ``setup_capabilities``
    only adds ``<caps_dir>/skills`` as a scan root when it already exists —
    a directory that appears afterwards is invisible until the next startup, and
    a scaffolded agent ships with it.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)
    (deployment.agent_dir / "capabilities" / "skills").mkdir(parents=True)
    config = _config(deployment, ("memory",))

    async with _booted(deployment, config) as agent:
        _write_signed_skill(agent, deployment, "kestrel-handover")
        await agent.reload()

        assert agent._capability_registry is not None
        names = {entry.name for entry in agent._capability_registry.skill_entries()}
        assert "kestrel-handover" in names, f"skill not discovered; registry holds {sorted(names)}"

        assert agent._bus is not None
        sections: dict[str, str] = {}
        await agent._bus.emit("agent:assemble_prompt", {"sections": sections, "query": "handover"})

    assert "kestrel-handover" in sections.get("capabilities", ""), (
        "the discovered skill never reached the assembled prompt"
    )


async def test_the_skills_module_copy_does_not_poison_the_agent_skills_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the module named ``skills`` must not land on the skills root.

    ``copy_capabilities`` used to write a module's surface to
    ``<agent_dir>/capabilities/<module>/``. For every module but one that is a
    fresh subdirectory the loader ignores. For the module literally named
    ``skills`` it was ``<agent_dir>/capabilities/skills/`` — already the agent's
    SKILLS scan root (``append_capability_scan_roots`` adds ``<caps_dir>/skills``
    as ``agent-skills``). The module's ``capabilities.py`` landed as a loose
    ``.py`` inside an UNTRUSTED root, unsigned, and was denied by the TOFU gate
    on every scan, forever.

    The damage was not the denied file. ``ArcAgent.reload()`` is transactional —
    "an invalid candidate is useful diagnostic output, never permission to
    damage the working set" — so it returned the diff and **refused to commit
    while any error was present**. One permanently invalid file meant every
    later reload was a no-op: an agent with the ``skills`` module installed
    could never register a skill or tool it authored, and was told nothing.

    Fixed by a path rule, not a trust change: copies are namespaced under
    ``capabilities/modules/``, which no conventional root claims.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("skills",), tmp_path)
    config = _config(deployment, ("skills",))

    skills_root = deployment.agent_dir / "capabilities" / "skills"
    # A scaffolded agent ships with this directory, and a root that appears after
    # startup is invisible until the next one. Creating it explicitly is what a
    # real agent has — before the fix, the colliding module copy created it as a
    # side effect, which quietly satisfied this precondition.
    skills_root.mkdir(parents=True, exist_ok=True)
    assert not (skills_root / "capabilities.py").exists(), (
        "the skills module's capability surface is back on the agent skills root"
    )

    async with _booted(deployment, config) as agent:
        _write_signed_skill(agent, deployment, "kestrel-handover")
        rendered = await agent.reload()
        assert agent._capability_registry is not None
        names = {entry.name for entry in agent._capability_registry.skill_entries()}

    assert "kestrel-handover" in names, (
        "reload could not commit — an agent-authored skill never registered. If the "
        "skills module's copied capabilities.py is back on the agent skills root, it "
        f"fails the trust gate on every scan and blocks every commit. reload said: "
        f"{rendered}"
    )


async def test_skills_module_registers_its_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skills module contributes hooks and a background task, not tools.

    Stated as its own case so the empty entry in :data:`_EXPECTED_TOOLS` reads
    as a measured fact about this module rather than an omission.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("skills",), tmp_path)
    config = _config(deployment, ("skills",))

    async with _booted(deployment, config) as agent:
        assert agent._capability_registry is not None
        hooks = agent._capability_registry.hook_entries()
        hook_names = {entry.meta.name for entries in hooks.values() for entry in entries}
        assert {"skills_post_tool", "skills_post_plan", "skills_ready"} <= hook_names
        assert (
            await agent._capability_registry.get_task("skills_review_lifecycle_loop") is not None
        )


# --------------------------------------------------------------------------
# 6. Messaging — configured and registered, with nothing on the wire
# --------------------------------------------------------------------------


async def test_messaging_configures_and_registers_without_touching_a_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The messaging runtime builds its services locally and its tools register.

    ``nats_url`` is left empty, so the live backend upgrade never runs and no
    socket is opened. What is asserted is the part an operator depends on at
    boot: the runtime holds a signed messenger keyed to this agent's handle, and
    the tools the fleet is driven through exist by name.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("messaging",), tmp_path)
    config = _config(deployment, ("messaging",))

    async with _booted(deployment, config) as agent:
        state = _runtime_of("messaging").state()
        assert state.agent_name == "live-modules-agent"
        assert state.svc is not None, "messaging built no messenger"
        assert state.registry is not None
        assert not state.live_backend_ready, "messaging opened a live backend in a test"
        assert state.config.nats_url == ""

        missing = _EXPECTED_TOOLS["messaging"] - _registered_tools(agent)
        assert not missing, f"messaging did not register {sorted(missing)}"

        result = await _call_tool(agent, "messaging_list_entities", {})

    assert isinstance(result, str)


async def test_a_channel_turns_answer_is_posted_back_to_that_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A woken channel turn's final text lands back in the channel it came from.

    The reported bug: an operator's group post in the arcui dashboard reaches a
    member, the run produces a full answer, and the channel shows silence —
    because nothing streamed the run's final text back onto the team bus. The
    fix is an ``agent:post_respond`` hook; this proves it is bridged to the real
    module bus, reads the turn's origin channel, and drives the real messenger.

    Emitted on the real bus rather than through a stubbed loop: a hook that was
    written but never subscribed (the producers-unwired trap) fails here.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("messaging",), tmp_path)
    config = _config(deployment, ("messaging",))

    async with _booted(deployment, config) as agent:
        st = _runtime_of("messaging").state()
        st.svc.send = AsyncMock()
        assert agent._bus is not None
        turn_context.set_inbound_channel("channel://work")
        try:
            await agent._bus.emit(
                "agent:post_respond",
                {
                    "messages": [
                        {"role": "user", "content": "what is the tech stack for NNL?"},
                        {"role": "assistant", "content": "Haystack + Qdrant + vLLM."},
                    ],
                },
            )
        finally:
            turn_context.set_inbound_channel(None)

    st.svc.send.assert_awaited_once()
    sent = st.svc.send.await_args.args[0]
    assert sent.to == ["channel://work"]
    assert sent.body == "Haystack + Qdrant + vLLM."


# --------------------------------------------------------------------------
# 7. The upgrade path — config enables a module the box does not have
# --------------------------------------------------------------------------


async def test_enabled_but_absent_module_leaves_the_agent_without_its_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """What actually happens today, asserted rather than assumed.

    This is the upgrade every fleet agent takes: the wheel stops carrying module
    source, the deployment module root is empty until an operator installs, and
    the agent's existing ``arcagent.toml`` still says ``[modules.scheduler]
    enabled = true``. The agent boots, completes turns, and has none of the
    tools its config claims.

    Nothing here is an endorsement of that behaviour — see the next test.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    config = _config(deployment, _LIVE_MODULES)  # nothing installed

    with caplog.at_level(logging.WARNING, logger="arcagent.core.agent_lifecycle"):
        async with _booted(deployment, config) as agent:
            assert not _module_scan_roots(agent)
            registered = _registered_tools(agent)
            for module in _LIVE_MODULES:
                assert not (_EXPECTED_TOOLS[module] & registered), (
                    f"{module} registered tools despite not being installed"
                )
            events = await _run_a_turn(agent)

    assert isinstance(events[-1], TurnEndEvent), "the agent did not boot healthy"
    skips = [
        r.getMessage() for r in caplog.records if "no module folder is present" in r.getMessage()
    ]
    unlogged = [name for name in _LIVE_MODULES if not any(repr(name) in msg for msg in skips)]
    assert not unlogged, f"enabled-but-absent modules with no log line at all: {unlogged}"


async def test_the_audit_spies_capture_a_real_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the finding below: the instrument records what is emitted.

    Without this, "no audit event was recorded" is indistinguishable from
    "the recorder was never wired", and the finding would prove nothing.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)
    audited: list[tuple[str, dict[str, Any]]] = []

    async with _booted(deployment, _config(deployment, ("memory",)), audit_spy=audited) as agent:
        await _call_tool(agent, "memory_search", {"query": "anything"})

    assert any("tool" in action for action, _ in audited), (
        f"the audit spies recorded nothing for a tool dispatch: {sorted(a for a, _ in audited)}"
    )


async def test_enabled_but_absent_module_is_audited_not_only_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXPECTED TO FAIL — recorded here because the gap is the finding.

    Contract: an agent that silently drops an ability its own config claims is a
    security-relevant deployment event, and this repository's fourth pillar says
    every operation emits an ``AuditEvent`` through a single emission point whose
    sinks fan out. Today the only trace is one ``_logger.warning`` in
    ``_warn_config_without_folder`` — no audit event on either route, no bus
    event, nothing on the agent, and nothing any surface reads.
    ``module_statuses()`` knows the answer and has exactly one caller: that
    warning.

    The consequence on a real fleet: an upgrade to a box whose bundles were not
    installed produces agents that boot, answer, and look healthy while their
    scheduler never fires and their memory never recalls — visible only to
    whoever happens to read journald at the right moment.

    :func:`test_the_audit_spies_capture_a_real_dispatch` is the control that
    makes this a finding rather than a broken instrument: the same two spies DO
    capture a tool dispatch, so an empty verdict here means nothing was emitted,
    not that nothing was watched.

    A failure here is a product finding, not a broken test. Deleting it hides
    the gap; weakening it to assert the log line only restates the test above.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    config = _config(deployment, _LIVE_MODULES)  # nothing installed
    audited: list[tuple[str, dict[str, Any]]] = []

    async with _booted(deployment, config, audit_spy=audited):
        pass

    actions = {action for action, _ in audited}
    assert any("module" in action for action in actions), (
        "an enabled-but-absent module produced no audit event on either the "
        "AgentTelemetry route or the arctrust single emission point; the only "
        f"record is a WARNING log. Audited actions were: {sorted(actions)}"
    )
