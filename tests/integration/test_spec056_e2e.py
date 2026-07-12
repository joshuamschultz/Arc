"""SPEC-056 Phase E (E1) — the multi-agent task flow, front-to-back, for real.

Drives the mission-control task surface (``arcagent.modules.tasks``) through
three independent agent runtimes — alice, bob, and a third agent (carol) —
sharing ONE arcstore ``tasks`` collection on a tmp ``store/arcui.db`` and one
in-memory arcteam ``EntityRegistry``. Nothing about the task/store/registry
path is stubbed: every agent gets its own real ``_runtime._State`` (its own
``arcstore.tasks.TaskStore`` connection to the shared db, per production
topology — arcstore/backends/sqlite.py's own docstring: "each instance owns
its own DB file... a shared file across instances produces SQLITE_BUSY storms
above ~2-3 concurrent writers", which does not apply here since every step
below is sequential, not concurrent).

DEVIATION FROM THE PLAN'S RESEARCH-INSIGHTS NOTE: PLAN.md's Phase E section
suggests mirroring ``test_spec031_e2e.py`` with a real ``nats-server``
subprocess. This test does not spin one up — per the task brief, it injects
the registry + messenger the way ``packages/arcagent/tests/unit/modules/
tasks/test_assign_notify.py`` does, using a shared in-memory fake for the
notify->adopt handoff (step 3 below) instead of live NATS delivery. This
keeps the test deterministic and dependency-free while still exercising every
OTHER real seam: real arcstore atomic claim/assign, real arcteam handle
resolution, real ``@tool`` capability functions, and the real assignee-side
``handle_task_assigned`` adopt handler.

Only the LLM/run loop is out of scope entirely — this suite never touches
arcrun; it calls the ``tasks`` module's tool functions directly, exactly as
the tool registry would dispatch them.

Scenario (mirrors the task brief 1:1):

1. alice ``create_task``s an unowned team-backlog task -> status ``backlog``.
2. alice ``assign_task``s it to ``@bob`` -> arcstore owner=bob, status=todo,
   AND exactly one ``TASK_ASSIGNED`` message lands in the shared messenger
   addressed to bob's inbox.
3. That delivered message is fed to bob's ``handle_task_assigned`` -> bob
   adopts it -> the real notify->adopt handoff -> task in_progress/owner=bob.
4. A second unowned task; bob's ``claim_task`` (already at his one-active
   cap) returns ``continue_current``; carol's ``claim_task`` grabs the
   still-unowned second task from backlog -> ``assigned``.
5. bob ``decompose_task``s his in-progress task into sub-tasks (parent_id +
   parent ``blocked_by``).
6. The final board, read back via a FRESH ``arcstore.tasks.TaskStore.list()``
   over the shared db (exactly what arcui's ``Observe.tasks`` reads) — proves
   owners/statuses are durable and agent-agnostic, not just visible to the
   agent that wrote them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcagent.modules.tasks import _runtime
from arcagent.modules.tasks.capabilities import (
    assign_task,
    claim_task,
    create_task,
    decompose_task,
)
from arcagent.modules.tasks.handlers import handle_task_assigned
from arcagent.modules.tasks.store import open_store
from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType, Message, MsgType
from arctrust import AgentIdentity, OperatorKey


class _SharedMessenger:
    """Records every ``send`` — a shared in-memory stand-in for a live
    ``arcteam.messenger.MessagingService`` (see the module docstring's
    DEVIATION note). The test itself plays delivery: it pulls the captured
    envelope back out and feeds it to the assignee's handler, mirroring
    ``test_assign_notify.py``'s ``_FakeMessenger`` pattern one level up —
    shared across agents here instead of scoped to a single test.
    """

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> Message:
        self.sent.append(message)
        return message


def _make_registry() -> EntityRegistry:
    """A fresh in-memory ``EntityRegistry`` shared by all three agents."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, OperatorKey.generate().into_signer())
    return EntityRegistry(backend, audit)


async def _register(handle: str, identity: AgentIdentity, registry: EntityRegistry) -> None:
    """Register ``identity`` under ``handle`` so ``@handle`` refs resolve."""
    await registry.register(
        Entity(
            did=identity.did,
            handle=handle,
            id=f"agent://{handle}",
            name=handle.title(),
            type=EntityType.AGENT,
            public_key=identity.public_key.hex(),
        )
    )


async def _agent_state(
    identity: AgentIdentity,
    *,
    data_dir: str,
    registry: EntityRegistry,
    messenger: Any = None,
) -> _runtime._State:
    """Boot one agent's tasks runtime against the shared db/registry and
    finish its lazy async wiring, exactly like a real agent's dispatcher
    would (sync ``configure()`` immediately followed by ``ensure_store()``
    on first use). Returns the bound ``_State`` so the test can
    ``_runtime.bind()`` back into this agent whenever it needs to act.
    """
    _runtime.configure(
        config={"enabled": True, "data_dir": data_dir},
        telemetry=MagicMock(),
        workspace=Path(data_dir),
        identity=identity,
        registry=registry,
        messenger=messenger,
    )
    await _runtime.ensure_store()
    return _runtime.state()


async def test_spec056_multi_agent_task_flow_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ARCSTORE_DATA_DIR is the highest-precedence override (SPEC-026 §13.2) —
    # cleared so every agent's `data_dir` (pointed at tmp_path) actually wins.
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    data_dir = str(tmp_path)

    registry = _make_registry()
    messenger = _SharedMessenger()

    alice_identity = AgentIdentity.generate(org="local", agent_type="agent")
    bob_identity = AgentIdentity.generate(org="local", agent_type="agent")
    carol_identity = AgentIdentity.generate(org="local", agent_type="agent")
    await _register("alice", alice_identity, registry)
    await _register("bob", bob_identity, registry)
    await _register("carol", carol_identity, registry)

    try:
        # Only alice ever calls assign_task in this scenario, so only her
        # runtime is given the shared messenger — bob/carol never notify.
        alice_state = await _agent_state(
            alice_identity, data_dir=data_dir, registry=registry, messenger=messenger
        )
        bob_state = await _agent_state(bob_identity, data_dir=data_dir, registry=registry)
        carol_state = await _agent_state(carol_identity, data_dir=data_dir, registry=registry)

        # 1. alice creates an unowned team-backlog task.
        _runtime.bind(alice_state)
        backlog_task = json.loads(await create_task(title="Ship the release", owner=""))
        assert backlog_task["status"] == "backlog"
        assert backlog_task["owner_did"] is None

        # 2. alice assigns it to bob: durable owner write + exactly one
        # TASK_ASSIGNED notify to bob's inbox.
        assigned = json.loads(await assign_task(id=backlog_task["id"], to_handle="@bob"))
        assert assigned["owner_did"] == bob_identity.did
        assert assigned["status"] == "todo"
        assert len(messenger.sent) == 1
        envelope = messenger.sent[0]
        assert envelope.to == ["agent://bob"]
        assert envelope.msg_type == MsgType.TASK_ASSIGNED
        assert "@bob" in envelope.body
        assert backlog_task["id"] in envelope.body

        # 3. Feed the delivered message to bob's adopt handler — the real
        # notify->adopt handoff.
        _runtime.bind(bob_state)
        adopted = json.loads(await handle_task_assigned(envelope))
        assert adopted["task"]["id"] == backlog_task["id"]
        assert adopted["task"]["status"] == "in_progress"
        assert adopted["task"]["owner_did"] == bob_identity.did

        # 4a. A second unowned task; bob is already at his one-in_progress
        # cap, so claiming returns his existing task, not the new one.
        _runtime.bind(alice_state)
        second_task = json.loads(await create_task(title="Write the changelog", owner=""))
        assert second_task["status"] == "backlog"

        _runtime.bind(bob_state)
        capped = json.loads(await claim_task())
        assert capped["reason"] == "continue_current"
        assert capped["task"]["id"] == backlog_task["id"]

        # 4b. A third agent grabs the still-unowned second task from backlog.
        _runtime.bind(carol_state)
        grabbed = json.loads(await claim_task())
        assert grabbed["reason"] == "assigned"
        assert grabbed["task"]["id"] == second_task["id"]
        assert grabbed["task"]["owner_did"] == carol_identity.did

        # 5. bob decomposes his in-progress task into sub-tasks.
        _runtime.bind(bob_state)
        decomposed = json.loads(
            await decompose_task(
                id=backlog_task["id"],
                subtasks=[{"title": "Draft release notes"}, {"title": "Tag the release"}],
            )
        )
        sub_ids = [s["id"] for s in decomposed["subtasks"]]
        assert len(sub_ids) == 2
        assert set(decomposed["parent"]["blocked_by"]) >= set(sub_ids)

        # 6. Read the final board back the way arcui's Observe.tasks does —
        # a fresh TaskStore over the shared db, independent of any agent's
        # runtime state.
        board_store = await open_store(data_dir)
        board = {task.id: task for task in await board_store.list()}

        assert board[backlog_task["id"]].owner_did == bob_identity.did
        assert board[backlog_task["id"]].status == "in_progress"
        assert board[second_task["id"]].owner_did == carol_identity.did
        assert board[second_task["id"]].status == "in_progress"
        for sub_id in sub_ids:
            assert board[sub_id].owner_did == bob_identity.did
            assert board[sub_id].status == "todo"
    finally:
        _runtime.reset()
