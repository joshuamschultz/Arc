"""Item 20 P20-4 — a dispatched task carries its task and workflow ids on every act.

A workflow node's tool call is audited by the real arcagent ``ToolRegistry``
(arctrust pipeline, operator-signed WORM chain); the signed ``policy.evaluate``
record must name the agent DID as the actor and carry ``workflow_run_id``,
``node_id`` and ``task_id`` beside the run and call ids. An agent node's turn is
dispatched inside the same task scope, so the turn sees the ids too.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arctrust import AgentIdentity, causal
from arctrust.audit import WormSink, verify_chain, worm_policy_sink
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_policy import build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport

_RUN = "flow-run-7"


def _meta(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "workflow": "wf",
        "workflow_version": 1,
        "flow_run_id": _RUN,
        "node_id": "push",
        "node_kind": "tool",
        "iteration": 0,
        "idempotency_key": f"wf/{_RUN}/push/0",
        "upstream": {},
        "strategy": [],
        "artifacts": [],
        "tool": "crm_update",
        "args": {"record": "r1"},
        "mode": "live",
    }
    block.update(overrides)
    return block


@pytest.fixture
def identity() -> AgentIdentity:
    return AgentIdentity.generate(org="test", agent_type="exec")


@pytest.fixture
def state(tmp_path: Path, arcstore_opener: Any, identity: AgentIdentity) -> Iterator[Any]:
    from arcagent.modules.tasks import _runtime

    _runtime.reset()
    _runtime.configure(
        config={"enabled": True, "dispatch": True, "default_max_attempts": 3},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=identity,
        arcstore_opener=arcstore_opener,
    )
    yield _runtime.state()
    _runtime.reset()


def _registry(tmp_path: Path, identity: AgentIdentity, seen: list[Any]) -> tuple[Any, Any, Any]:
    operator = generate_keypair()
    chain = tmp_path / "audit-chain.jsonl"
    worm = WormSink(chain, InProcessSigner(operator.private_key))
    telemetry = MagicMock()
    span = MagicMock()
    span.__aenter__ = AsyncMock(return_value=MagicMock())
    span.__aexit__ = AsyncMock(return_value=False)
    telemetry.tool_span = MagicMock(return_value=span)
    registry = ToolRegistry(
        config=ArcAgentConfig(agent=AgentConfig(name="t"), llm=LLMConfig(model="test/m")).tools,
        bus=ModuleBus(),
        telemetry=telemetry,
        policy_pipeline=build_pipeline(
            tier="enterprise",
            agent_registry={identity.did: identity.public_key},
            audit_sink=worm_policy_sink(worm),
        ),
        identity=identity,
        tier="enterprise",
    )

    async def crm_update(**_: Any) -> str:
        seen.append(causal.current())
        return '{"updated": true}'

    registry.register(
        RegisteredTool(
            name="crm_update",
            description="crm",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=crm_update,
            classification="read_only",
        )
    )
    return registry, worm, (chain, operator.public_key)


async def _opened() -> Any:
    from arcagent.modules.tasks.capabilities import _state

    return await _state()


async def _claimed(st: Any, task_id: str, **overrides: Any) -> Any:
    from arcagent.modules.tasks.models import Task

    await st.store.create(
        Task(
            id=task_id,
            title="push it",
            status="todo",
            owner_did=st.identity.did,
            creator_did="did:arc:user:josh",
            max_attempts=3,
            metadata=_meta(**overrides),
        )
    )
    started, _ = await st.store.start_task(
        task_id, st.identity.did, attempt_key=f"{_RUN}:push:0:1"
    )
    assert started is not None
    return started


async def test_a_workflow_node_tool_call_carries_run_node_task_and_agent(
    state: Any, tmp_path: Path, identity: AgentIdentity
) -> None:
    from arcagent.modules.tasks.capabilities import _run_task

    state = await _opened()
    seen: list[causal.CausalContext | None] = []
    registry, worm, (chain, public_key) = _registry(tmp_path, identity, seen)
    state.tool_registry = registry
    task = await _claimed(state, "task_wf")

    await _run_task(state, task, "pinned", identity.did)

    (ctx,) = seen
    assert ctx is not None
    assert (ctx.initiator, ctx.initiator_id) == ("agent", identity.did)
    assert ctx.workflow_run_id == _RUN
    assert ctx.node_id == "push"
    assert ctx.task_id == "task_wf"
    assert ctx.on_behalf_of == "did:arc:workflow:wf"

    worm.close()
    assert verify_chain(chain, public_key)
    records = [json.loads(line)["event"] for line in chain.read_text().splitlines()]
    (policy,) = [r for r in records if r["action"] == "policy.evaluate"]
    assert policy["actor_did"] == identity.did
    assert policy["causal"]["workflow_run_id"] == _RUN
    assert policy["causal"]["node_id"] == "push"
    assert policy["causal"]["task_id"] == "task_wf"


async def test_an_agent_node_turn_runs_inside_the_task_scope(
    state: Any, identity: AgentIdentity
) -> None:
    from arcagent.modules.tasks.capabilities import _dispatch_tick

    state = await _opened()
    seen: list[causal.CausalContext | None] = []

    async def agent_turn(_text: str, *, session_key: str, run_id: str, **_kw: Any) -> Any:
        seen.append(causal.current())
        row = await state.store.get("task_agent")
        await state.store.complete_attempt(
            row.id,
            attempt_key=row.metadata["attempt_key"],
            attempts=row.attempts,
            resolution="done",
            output={"ok": True},
            actor_did=state.identity.did,
        )

    state.agent_run_fn = agent_turn
    from arcagent.modules.tasks.models import Task

    await state.store.create(
        Task(
            id="task_agent",
            title="think",
            status="todo",
            owner_did=identity.did,
            creator_did="did:arc:user:josh",
            max_attempts=3,
            metadata=_meta(node_kind="agent", tool=None, args={}),
        )
    )
    with causal.bind(causal.root("system", "did:arc:system:tasks_dispatch_loop")):
        await _dispatch_tick()

    (ctx,) = seen
    assert ctx is not None
    assert ctx.initiator == "workflow"
    assert ctx.initiator_id == "did:arc:workflow:wf"
    assert ctx.on_behalf_of == "did:arc:user:josh"
    assert (ctx.task_id, ctx.workflow_run_id, ctx.node_id) == ("task_agent", _RUN, "push")


async def test_an_ordinary_task_turn_acts_for_its_creator(
    state: Any, identity: AgentIdentity
) -> None:
    from arcagent.modules.tasks.capabilities import _dispatch_tick
    from arcagent.modules.tasks.models import Task

    state = await _opened()
    seen: list[causal.CausalContext | None] = []

    async def agent_turn(_text: str, *, session_key: str, run_id: str, **_kw: Any) -> Any:
        seen.append(causal.current())

    state.agent_run_fn = agent_turn
    await state.store.create(
        Task(
            id="task_plain",
            title="plain",
            status="todo",
            owner_did=identity.did,
            creator_did="did:arc:user:josh",
            max_attempts=3,
        )
    )
    await _dispatch_tick()

    (ctx,) = seen
    assert ctx is not None
    assert (ctx.initiator, ctx.initiator_id) == ("agent", identity.did)
    assert ctx.on_behalf_of == "did:arc:user:josh"
    assert ctx.task_id == "task_plain"
    assert ctx.workflow_run_id is None
