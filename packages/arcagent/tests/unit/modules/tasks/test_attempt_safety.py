"""Workflow attempt safety leftovers: non-idempotent tools, reclaim stamps, pinned fail.

* A tool declared ``idempotent = false`` is never re-run for a retried or
  reclaimed attempt without an operator's ``metadata.operator_retry_ok``.
* A reclaim stamps ``metadata.reclaimed_at`` on the row.
* ``fail_task`` from a stale executor cannot fail a newer attempt's row.
* The reclaim lease is derived from the dispatch timeout, never shorter.
"""

from __future__ import annotations

import json
from typing import Any

import arcrun
from arcstore.tasks import RECLAIM_MARGIN_S

from .test_attempt_keys import _claim, _key, _meta, _todo_node, state  # noqa: F401


class _Tool:
    def __init__(self, *, idempotent: bool) -> None:
        self.calls = 0
        self.idempotent = idempotent

    async def execute(self, args: dict[str, Any], context: arcrun.ToolContext) -> str:
        self.calls += 1
        return '{"posted": true}'

    def to_arcrun_tools(self) -> list[arcrun.Tool]:
        return [
            arcrun.Tool(
                name="post_message",
                description="side effect",
                input_schema={"type": "object"},
                execute=self.execute,
                idempotent=self.idempotent,
            )
        ]


async def _second_attempt(st: Any, **meta: Any) -> Any:
    """A node row on its second claim (the first attempt was abandoned)."""
    await _todo_node(st, **meta)
    first = await _claim(st)
    await st.store.requeue(
        first.id,
        actor_did="did:arc:x/runner",
        last_error="abandoned",
        next_attempt_at="2000-01-01T00:00:00+00:00",
        expected_attempts=first.attempts,
    )
    return await _claim(st)


async def _run(st: Any, row: Any) -> None:
    from arcagent.modules.tasks.capabilities import _run_tool_node
    from arcagent.modules.tasks.node_execution import node_from_task

    node = node_from_task(row)
    assert node is not None
    await _run_tool_node(st, row, node, st.identity.did)


async def test_non_idempotent_tool_refused_on_retried_attempt(state: Any) -> None:
    tool = _Tool(idempotent=False)
    state.tool_registry = tool
    row = await _second_attempt(state)
    assert row.attempts == 2

    await _run(state, row)

    assert tool.calls == 0
    after = await state.store.get(row.id)
    assert after is not None and after.status == "failed"
    assert after.last_error and "operator_retry_ok" in after.last_error


async def test_non_idempotent_tool_runs_first_attempt(state: Any) -> None:
    tool = _Tool(idempotent=False)
    state.tool_registry = tool
    await _todo_node(state)
    row = await _claim(state)

    await _run(state, row)

    assert tool.calls == 1


async def test_operator_retry_ok_lets_a_non_idempotent_retry_run(state: Any) -> None:
    tool = _Tool(idempotent=False)
    state.tool_registry = tool
    row = await _second_attempt(state, operator_retry_ok=True)

    await _run(state, row)

    assert tool.calls == 1


async def test_idempotent_tool_retries_freely(state: Any) -> None:
    tool = _Tool(idempotent=True)
    state.tool_registry = tool
    row = await _second_attempt(state)

    await _run(state, row)

    assert tool.calls == 1


async def test_reclaimed_row_refuses_non_idempotent_rerun(state: Any) -> None:
    tool = _Tool(idempotent=False)
    state.tool_registry = tool
    await _todo_node(state)
    row = await _claim(state)
    await state.store.requeue(
        row.id,
        actor_did="did:arc:x/runner",
        last_error="reclaimed",
        next_attempt_at="2000-01-01T00:00:00+00:00",
        expected_attempts=row.attempts,
        metadata_patch={"reclaimed_at": "2026-01-01T00:00:00+00:00"},
    )
    again = await _claim(state)

    await _run(state, again)

    assert tool.calls == 0


async def test_requeue_and_dead_letter_stamp_metadata_without_losing_the_node(
    state: Any,
) -> None:
    await _todo_node(state)
    row = await _claim(state)
    stamped = await state.store.requeue(
        row.id,
        actor_did="did:arc:x/runner",
        last_error="e",
        next_attempt_at="2000-01-01T00:00:00+00:00",
        expected_attempts=1,
        metadata_patch={"reclaimed_at": "T"},
    )
    assert stamped is not None
    assert stamped.metadata["reclaimed_at"] == "T"
    assert stamped.metadata["tool"] == "post_message"
    again = await _claim(state)
    dead = await state.store.dead_letter(
        again.id,
        actor_did="did:arc:x/runner",
        resolution="r",
        last_error="e",
        expected_attempts=2,
        metadata_patch={"reclaimed_at": "T2"},
    )
    assert dead is not None and dead.metadata["reclaimed_at"] == "T2"


async def test_stuck_reclaim_by_the_engine_stamps_reclaimed_at(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _reliability_tick

    await _todo_node(state)
    await _claim(state)
    assert not state.running

    await _reliability_tick()

    row = await state.store.get("task_ak")
    assert row is not None and row.status == "todo"
    assert row.metadata.get("reclaimed_at")


async def test_stale_executor_cannot_fail_the_new_attempts_row(state: Any) -> None:
    from arcagent.modules.tasks import node_execution
    from arcagent.modules.tasks.capabilities import fail_task
    from arcagent.modules.tasks.node_execution import node_from_task

    await _todo_node(state)
    stale = await _claim(state)
    await state.store.requeue(
        stale.id,
        actor_did="did:arc:x/runner",
        last_error="reclaimed",
        next_attempt_at="2000-01-01T00:00:00+00:00",
        expected_attempts=1,
    )
    current = await _claim(state)
    assert current.attempts == 2

    stale_node = node_from_task(stale)
    assert stale_node is not None
    token = node_execution.bind_node(stale_node)
    try:
        reply = json.loads(await fail_task(id=stale.id, resolution="late failure"))
    finally:
        node_execution.reset_node(token)

    assert "error" in reply
    row = await state.store.get(stale.id)
    assert row is not None and row.status == "in_progress" and row.attempts == 2


async def test_live_attempt_can_fail_its_own_row(state: Any) -> None:
    from arcagent.modules.tasks import node_execution
    from arcagent.modules.tasks.capabilities import fail_task
    from arcagent.modules.tasks.node_execution import node_from_task

    await _todo_node(state)
    live = await _claim(state)
    node = node_from_task(live)
    assert node is not None
    token = node_execution.bind_node(node)
    try:
        reply = json.loads(await fail_task(id=live.id, resolution="mine"))
    finally:
        node_execution.reset_node(token)

    assert reply["status"] == "failed"
    assert _key(1) == live.metadata["attempt_key"]


async def test_dispatch_stamps_the_effective_timeout_on_the_node_row(state: Any) -> None:
    """The reclaim lease reads the row, so the dispatch timeout must be on it."""
    from arcagent.modules.tasks.capabilities import _dispatch_tick

    class _Run:
        async def __call__(self, text: str, **kwargs: Any) -> str:
            return "ok"

    state.config = state.config.model_copy(update={"task_timeout_seconds": 777.0})
    state.agent_run_fn = _Run()
    await _todo_node(state, node_kind="agent", tool=None)

    await _dispatch_tick()

    row = await state.store.get("task_ak")
    assert row is not None and row.timeout_seconds == 777.0


def test_reclaim_allowance_is_timeout_plus_margin_never_below_the_floor() -> None:
    from arcstore.tasks import reclaim_allowance_s

    assert reclaim_allowance_s(None, 900.0) == 900.0
    assert reclaim_allowance_s(0, 900.0) == 900.0
    assert reclaim_allowance_s(1200, 900.0) == 1200 + RECLAIM_MARGIN_S
    assert reclaim_allowance_s(100, 900.0) == 900.0
