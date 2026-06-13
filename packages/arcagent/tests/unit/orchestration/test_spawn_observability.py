"""Spawn identity + lineage observability (SPEC-028 FR-3).

A spawned child must record under its OWN operational identity:
- its run_events spool under the child actor_did (F5, task 3.1),
- its llm_calls carry the child agent_did/agent_label via a task-local contextvar
  (F4 / C2, tasks 3.2/3.2b),
- a spawn_event captures the parent→child lineage edge (task 3.3),
- and the path degrades (run_events still tagged) rather than breaks (task 3.5).

arcrun stays a pure loop — all spawn logic is arcagent (task 3.6).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

import pytest
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool
from arctrust import derive_child_identity

from arcagent.orchestration.spawn import spawn, spawn_many
from arcagent.orchestration.spawn_handle import SpawnSpec

from ._mock_llm import LLMResponse, MockModel

_PARENT_DID = "did:arc:acme:agent:parent/aabbccdd"


async def _echo(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


ECHO_TOOL = Tool(
    name="echo",
    description="Echo",
    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
    execute=_echo,
)


def _parent_state(*, actor_did: str | None = _PARENT_DID, depth: int = 0) -> RunState:
    bus = EventBus(run_id="parent-run", spool_actor_did=actor_did)
    reg = ToolRegistry(tools=[ECHO_TOOL], event_bus=bus)
    return RunState(
        messages=[], registry=reg, event_bus=bus, run_id="parent-run", depth=depth, max_depth=3
    )


def _identity(n: int = 0):
    return derive_child_identity(
        parent_sk_bytes=b"\xcd" * 32, spawn_id=f"obs-{n}", wallclock_timeout_s=30
    )


def _telemetry_model(rendezvous: object | None = None) -> object:
    """A real TelemetryModule wrapping a stub provider, so it spools llm_call records.

    When ``rendezvous`` (an asyncio.Barrier) is supplied, every inner ``invoke``
    waits on it — forcing concurrent children to be *simultaneously in-flight*
    with their identity bound, which is what actually exercises the contextvar
    isolation (a sequential mock could not catch a regression to a global race).
    """
    from arcllm.modules.telemetry import TelemetryModule
    from arcllm.types import LLMResponse as RealResponse
    from arcllm.types import Usage as RealUsage

    response = RealResponse(
        content="done",
        tool_calls=[],
        stop_reason="end_turn",
        model="stub-model",
        usage=RealUsage(input_tokens=10, output_tokens=5, total_tokens=15),
    )

    class _StubProvider:  # duck-typed: TelemetryModule only needs .name + .invoke
        name = "stub"

        async def invoke(self, messages: object, tools: object = None, **kwargs: object) -> object:
            if rendezvous is not None:
                await rendezvous.wait()  # both children block here, identities bound
            return response

    # Configured with the PARENT identity — children must override it via contextvar.
    return TelemetryModule({"agent_did": _PARENT_DID, "agent_label": "parent"}, _StubProvider())


def _capture():
    """Patch every producer's spool binding into one shared list."""
    records: list = []
    patches = [
        patch("arcagent.orchestration.spawn._spool_record", records.append),
        patch("arcrun.events._spool_record", records.append),
        patch("arcllm.modules.telemetry._spool_record", records.append),
    ]
    return records, patches


@pytest.mark.asyncio
async def test_child_run_events_tagged() -> None:
    """Task 3.1 (F5) — the child's run_events spool under the child actor_did."""
    state = _parent_state()
    identity = _identity(1)
    records, patches = _capture()
    model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])
    with patches[0], patches[1], patches[2]:
        await spawn(
            parent_state=state,
            task="t",
            tools=[ECHO_TOOL],
            system_prompt="s",
            identity=identity,
            model=model,
        )
    child_run_events = [r for r in records if r.kind == "run_event"]
    assert child_run_events, "child run_events must spool"
    assert all(r.actor_did == identity.did for r in child_run_events)
    assert all(r.actor_did != _PARENT_DID for r in child_run_events)


@pytest.mark.asyncio
async def test_child_llm_calls_separated() -> None:
    """Task 3.2 (F4) — the child's llm_calls carry the child identity, not the parent's."""
    state = _parent_state()
    identity = _identity(2)
    records, patches = _capture()
    with patches[0], patches[1], patches[2]:
        await spawn(
            parent_state=state,
            task="t",
            tools=[ECHO_TOOL],
            system_prompt="s",
            identity=identity,
            model=_telemetry_model(),
            role="researcher",
        )
    llm_calls = [r for r in records if r.kind == "llm_call"]
    assert llm_calls, "child llm_call must spool"
    assert all(r.actor_did == identity.did for r in llm_calls)
    assert all(r.actor_did != _PARENT_DID for r in llm_calls)
    assert all(r.agent_label and r.agent_label != "parent" for r in llm_calls)


@pytest.mark.asyncio
async def test_concurrent_children_not_cross_attributed() -> None:
    """Task 3.2b (C2) — concurrent spawn_many children never cross-attribute llm_calls.

    A shared asyncio.Barrier(2) gates the inner invoke so BOTH children are
    provably in-flight with their identity bound at the same instant. If the
    contextvar regressed to a process-global, the second child's bind would
    clobber the first before either records → cross-attribution → this fails.
    """
    import asyncio

    parent = _parent_state()
    rendezvous = asyncio.Barrier(2)
    model = _telemetry_model(rendezvous)  # shared model; both children rendezvous in invoke
    specs = [
        SpawnSpec(
            task=f"t{i}",
            tools=[ECHO_TOOL],
            system_prompt="s",
            parent_state=parent,
            child_did=_identity(10 + i).did,
            child_sk_bytes=_identity(10 + i).sk_bytes,
            wallclock_timeout_s=30,
            model=model,
        )
        for i in range(2)
    ]
    child_by_label = {f"child:{s.child_did.rsplit('/', 1)[-1]}:d1": s.child_did for s in specs}
    records, patches = _capture()
    with patches[0], patches[1], patches[2]:
        await spawn_many(specs, max_concurrent=2)
    llm_calls = [r for r in records if r.kind == "llm_call"]
    assert len(llm_calls) == 2  # both children actually ran (and interleaved)
    # Each child's llm_call carries ITS OWN identity — label and did agree per row.
    for r in llm_calls:
        assert r.actor_did != _PARENT_DID
        assert child_by_label[r.agent_label] == r.actor_did
    # Both distinct children represented — no collapse onto a single (clobbered) identity.
    assert {r.actor_did for r in llm_calls} == set(child_by_label.values())


@pytest.mark.asyncio
async def test_spawn_lineage_recorded() -> None:
    """Task 3.3 — a spawn_event records the parent→child edge (parent/child/role/depth)."""
    state = _parent_state()
    identity = _identity(3)
    records, patches = _capture()
    model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])
    with patches[0], patches[1], patches[2]:
        await spawn(
            parent_state=state,
            task="t",
            tools=[ECHO_TOOL],
            system_prompt="s",
            identity=identity,
            model=model,
            role="researcher",
        )
    spawn_events = [r for r in records if r.kind == "spawn_event"]
    assert len(spawn_events) == 1
    ev = spawn_events[0]
    assert ev.parent_did == _PARENT_DID
    assert ev.child_did == identity.did
    assert ev.role == "researcher"
    assert ev.depth == 1  # parent depth 0 → child depth 1


@pytest.mark.asyncio
async def test_child_identity_degrades_safely() -> None:
    """Task 3.5 — a non-telemetry model still tags run_events; it degrades, not breaks."""
    state = _parent_state()
    identity = _identity(4)
    records, patches = _capture()
    model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])  # no telemetry
    with patches[0], patches[1], patches[2]:
        result = await spawn(
            parent_state=state,
            task="t",
            tools=[ECHO_TOOL],
            system_prompt="s",
            identity=identity,
            model=model,
        )
    assert result.status in ("completed", "max_iterations")
    # run_events still spool under the child identity even with no llm_call separation.
    run_events = [r for r in records if r.kind == "run_event"]
    assert run_events and all(r.actor_did == identity.did for r in run_events)


@pytest.mark.asyncio
async def test_make_spawn_tool_records_lineage_and_child_identity() -> None:
    """Task 3.1/3.3 — the LLM-facing spawn_task tool also tags the child run and
    emits a spawn_event (the live path in agent_dispatch/arccli, not just spawn())."""
    from arcrun.types import ToolContext

    from arcagent.orchestration.spawn import make_spawn_tool

    tool = make_spawn_tool(
        model=MockModel([LLMResponse(content="ok", stop_reason="end_turn")]),
        tools=[ECHO_TOOL],
        system_prompt="sys",
    )
    parent = _parent_state()
    ctx = ToolContext(
        run_id=parent.run_id,
        tool_call_id="tc1",
        turn_number=1,
        event_bus=parent.event_bus,
        cancelled=parent.cancel_event,
        parent_state=parent,
    )
    records, patches = _capture()
    with patches[0], patches[1], patches[2]:
        out = await tool.execute({"task": "do it"}, ctx)
    assert out  # tool returned the child result, not an error

    spawn_events = [r for r in records if r.kind == "spawn_event"]
    assert len(spawn_events) == 1
    assert spawn_events[0].parent_did == _PARENT_DID
    assert spawn_events[0].child_did.startswith("did:arc:spawn:child/")
    assert spawn_events[0].depth == 1

    child_did = spawn_events[0].child_did
    run_events = [r for r in records if r.kind == "run_event"]
    assert run_events and all(r.actor_did == child_did for r in run_events)


def test_arcstore_off_silences_child() -> None:
    """Posture preserved: parent not recording (actor_did=None) → child spools nothing."""

    async def _run() -> list:
        state = _parent_state(actor_did=None)
        records, patches = _capture()
        model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])
        with patches[0], patches[1], patches[2]:
            await spawn(
                parent_state=state,
                task="t",
                tools=[ECHO_TOOL],
                system_prompt="s",
                identity=_identity(5),
                model=model,
            )
        return records

    import asyncio

    records = asyncio.run(_run())
    assert [r for r in records if r.kind in ("run_event", "spawn_event")] == []
