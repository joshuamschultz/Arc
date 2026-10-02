"""P14-B step 2 — idempotent node execution keyed by run + node + iteration + attempt.

The contract: exactly once per attempt key, at least once per node across
attempts. The claim stamps the key; only an executor holding the SAME attempt
may record a result; an executor reached twice for a recorded attempt runs no
side effect; a retry is a new key and may run again. Agent nodes pin their turn's
run id to the key, so a replayed dispatch of one attempt is one run.

Driven over a real ``arcstore.tasks.TaskStore`` and the real executors.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import arcrun
import pytest
from arctrust import AgentIdentity

_RUN = "run-ak"


def _meta(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "workflow": "wf",
        "workflow_version": 1,
        "flow_run_id": _RUN,
        "node_id": "post",
        "node_kind": "tool",
        "iteration": 0,
        "idempotency_key": f"wf/{_RUN}/post/0",
        "upstream": {},
        "strategy": [],
        "artifacts": [],
        "tool": "post_message",
        "args": {"text": "hi"},
    }
    block.update(overrides)
    return block


def _key(attempts: int, node: str = "post") -> str:
    return f"{_RUN}:{node}:0:{attempts}"


@pytest.fixture
def state(tmp_path: Path, arcstore_opener: Any) -> Iterator[Any]:
    from arcagent.modules.tasks import _runtime

    _runtime.reset()
    _runtime.configure(
        config={"enabled": True, "dispatch": True, "default_max_attempts": 3},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        arcstore_opener=arcstore_opener,
    )
    yield _runtime.state()
    _runtime.reset()


class _CountingTool:
    """A side-effecting tool: every execution is one external effect."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.contexts: list[arcrun.ToolContext] = []
        self.fail_first = fail_first

    async def execute(self, args: dict[str, Any], context: arcrun.ToolContext) -> str:
        self.contexts.append(context)
        if self.fail_first and len(self.contexts) == 1:
            raise RuntimeError("upstream 503")
        return '{"posted": true}'

    def to_arcrun_tools(self) -> list[arcrun.Tool]:
        return [
            arcrun.Tool(
                name="post_message",
                description="side effect",
                input_schema={"type": "object"},
                execute=self.execute,
            )
        ]


async def _todo_node(st: Any, **overrides: Any) -> Any:
    from arcagent.modules.tasks.capabilities import _state
    from arcagent.modules.tasks.models import Task

    st = await _state()
    task = Task(
        id="task_ak",
        title="post it",
        status="todo",
        owner_did=st.identity.did,
        creator_did=st.identity.did,
        max_attempts=3,
        metadata=_meta(**overrides),
    )
    return await st.store.create(task)


async def _claim(st: Any, task_id: str = "task_ak") -> Any:
    row = await st.store.get(task_id)
    started, _ = await st.store.start_task(
        task_id, st.identity.did, attempt_key=_key(row.attempts + 1)
    )
    assert started is not None
    return started


async def test_start_task_stamps_attempt_key_in_the_claim(state: Any) -> None:
    await _todo_node(state)
    started = await _claim(state)
    assert started.status == "in_progress"
    assert started.attempts == 1
    assert started.metadata["attempt_key"] == _key(1)
    assert started.metadata["attempt_started_at"]
    assert started.metadata["tool"] == "post_message", "the claim keeps the node block"


async def test_complete_requires_matching_attempt_key(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _run_tool_node
    from arcagent.modules.tasks.node_execution import node_from_task

    tool = _CountingTool()
    state.tool_registry = tool
    await _todo_node(state)
    stale = await _claim(state)
    # The attempt is reclaimed and re-claimed by a new executor while the old
    # one is still running: the old one now holds a dead key.
    await state.store.requeue(
        stale.id,
        actor_did="did:arc:x/runner",
        last_error="reclaimed",
        next_attempt_at="2000-01-01T00:00:00+00:00",
    )
    current = await _claim(state)
    assert current.attempts == 2

    node = node_from_task(stale)
    assert node is not None
    await _run_tool_node(state, stale, node, state.identity.did)

    row = await state.store.get(stale.id)
    assert row.status == "in_progress" and row.attempts == 2
    assert row.output is None
    assert "attempt_result_key" not in row.metadata
    assert (
        await state.store.complete_attempt(
            stale.id,
            attempt_key=_key(1),
            attempts=1,
            resolution="forged",
            output={"posted": True},
            actor_did=state.identity.did,
        )
        is None
    )


async def test_replay_of_recorded_attempt_runs_no_side_effect(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _run_tool_node
    from arcagent.modules.tasks.node_execution import node_from_task

    tool = _CountingTool()
    state.tool_registry = tool
    await _todo_node(state)
    claimed = await _claim(state)
    node = node_from_task(claimed)
    assert node is not None

    await _run_tool_node(state, claimed, node, state.identity.did)
    await _run_tool_node(state, claimed, node, state.identity.did)

    assert len(tool.contexts) == 1
    row = await state.store.get(claimed.id)
    assert row.status == "done"
    assert row.metadata["attempt_result_key"] == _key(1)


async def test_retry_gets_a_new_key_and_may_execute_again(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _run_tool_node
    from arcagent.modules.tasks.node_execution import node_from_task

    tool = _CountingTool(fail_first=True)
    state.tool_registry = tool
    await _todo_node(state)
    first = await _claim(state)
    await _run_tool_node(state, first, node_from_task(first), state.identity.did)  # type: ignore[arg-type]
    requeued = await state.store.get(first.id)
    assert requeued.status == "todo"

    second = await _claim(state)
    await _run_tool_node(state, second, node_from_task(second), state.identity.did)  # type: ignore[arg-type]

    keys = [context.idempotency_key for context in tool.contexts]
    assert keys == [_key(1), _key(2)]
    row = await state.store.get(first.id)
    assert row.status == "done" and row.metadata["attempt_result_key"] == _key(2)


async def test_tool_context_carries_idempotency_key(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _run_tool_node
    from arcagent.modules.tasks.node_execution import node_from_task

    tool = _CountingTool()
    state.tool_registry = tool
    await _todo_node(state)
    claimed = await _claim(state)
    await _run_tool_node(state, claimed, node_from_task(claimed), state.identity.did)  # type: ignore[arg-type]
    assert tool.contexts[0].idempotency_key == _key(1)


async def test_agent_node_run_id_is_derived_from_attempt_key_and_replay_runs_no_second_turn(
    state: Any,
) -> None:
    from arcagent.modules.tasks.capabilities import _dispatch_tick, _run_task

    turns: list[str] = []

    async def agent_turn(text: str, *, session_key: str, run_id: str, **_: Any) -> Any:
        turns.append(run_id)
        row = await state.store.get("task_ak")
        # The agent finishes its node through its own tools, for this attempt.
        await state.store.complete_attempt(
            row.id,
            attempt_key=row.metadata["attempt_key"],
            attempts=row.attempts,
            resolution="done",
            output={"ok": True},
            actor_did=state.identity.did,
        )
        return None

    state.agent_run_fn = agent_turn
    await _todo_node(state, node_kind="agent", tool=None, args={})
    await _dispatch_tick()

    expected = str(uuid.UUID(hex=hashlib.sha256(_key(1).encode()).hexdigest()[:32]))
    assert turns == [expected]
    row = await state.store.get("task_ak")
    assert row.run_id == expected
    assert row.metadata["attempt_result_key"] == _key(1)

    # The dispatcher reaches the same attempt again (a crash before its ack).
    await _run_task(state, row, expected, state.identity.did)
    assert turns == [expected], "a recorded attempt never runs a second turn"
