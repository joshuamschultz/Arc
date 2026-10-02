"""A workflow test run stubs anything that changes the world (J3 F6, G8).

The runner flags every node row of a test run ``mode = "test"``. The agent that
executes the row is the one place that knows a tool's classification, so it is the
one place that can refuse to perform a ``state_modifying`` call: it records an echo
of the call as the node output instead, and a downstream node still sees data. A
script node is never run either, because an unsigned draft's code is exactly what
a test run has not been trusted with. Agent nodes run under a small cost cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import arcrun
import pytest
from arctrust import AgentIdentity

_RUN = "test-abc123"


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
        "mode": "test",
        "max_cost_usd": 0.5,
    }
    block.update(overrides)
    return block


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


class _Registry:
    """A tool registry with one tool of the requested classification."""

    def __init__(self, classification: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._classification = classification

    async def execute(self, args: dict[str, Any], context: arcrun.ToolContext) -> str:
        del context
        self.calls.append(args)
        return '{"updated": true}'

    def to_arcrun_tools(self) -> list[arcrun.Tool]:
        return [
            arcrun.Tool(
                name="crm_update",
                description="writes to the CRM",
                input_schema={"type": "object"},
                execute=self.execute,
                classification=self._classification,
            )
        ]


async def _claimed(st: Any, **overrides: Any) -> Any:
    from arcagent.modules.tasks.capabilities import _state
    from arcagent.modules.tasks.models import Task

    st = await _state()
    await st.store.create(
        Task(
            id="task_tm",
            title="push it",
            status="todo",
            owner_did=st.identity.did,
            creator_did=st.identity.did,
            max_attempts=3,
            metadata=_meta(**overrides),
        )
    )
    started, _ = await st.store.start_task(
        "task_tm", st.identity.did, attempt_key=f"{_RUN}:push:0:1"
    )
    assert started is not None
    return started


async def _run_tool(st: Any, task: Any) -> Any:
    from arcagent.modules.tasks.capabilities import _run_tool_node
    from arcagent.modules.tasks.node_execution import node_from_task

    node = node_from_task(task)
    assert node is not None
    await _run_tool_node(st, task, node, st.identity.did)
    return await st.store.get(task.id)


async def test_state_modifying_tools_are_stubbed_and_run_never_scheduled(state: Any) -> None:
    registry = _Registry("state_modifying")
    state.tool_registry = registry

    row = await _run_tool(state, await _claimed(state))

    assert registry.calls == [], "the CRM was never touched"
    assert row.status == "done"
    assert row.output == {"stubbed": True, "tool": "crm_update", "args": {"record": "r1"}}


async def test_an_unclassified_tool_is_treated_as_state_modifying(state: Any) -> None:
    registry = _Registry("")
    state.tool_registry = registry

    row = await _run_tool(state, await _claimed(state))

    assert registry.calls == []
    assert row.output is not None and row.output["stubbed"] is True


async def test_a_read_only_tool_still_runs_in_a_test(state: Any) -> None:
    registry = _Registry("read_only")
    state.tool_registry = registry

    row = await _run_tool(state, await _claimed(state))

    assert registry.calls == [{"record": "r1"}]
    assert row.output == {"updated": True}


async def test_a_live_run_executes_a_state_modifying_tool(state: Any) -> None:
    registry = _Registry("state_modifying")
    state.tool_registry = registry

    row = await _run_tool(state, await _claimed(state, mode="live"))

    assert registry.calls == [{"record": "r1"}]
    assert row.output == {"updated": True}


async def test_a_script_node_is_never_executed_in_a_test(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _run_script_node
    from arcagent.modules.tasks.node_execution import node_from_task

    task = await _claimed(
        state, node_kind="script", script="scripts/drop_tables.py", tool=None, args={}
    )
    node = node_from_task(task)
    assert node is not None

    await _run_script_node(state, task, node, state.identity.did)

    row = await state.store.get(task.id)
    assert row.status == "done"
    assert row.output == {"stubbed": True, "script": "scripts/drop_tables.py"}


async def test_an_agent_node_in_a_test_runs_under_the_cost_cap(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _dispatch_tick

    seen: dict[str, Any] = {}

    async def agent_turn(text: str, *, session_key: str, run_id: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        row = await state.store.get("task_tm")
        await state.store.complete_attempt(
            row.id,
            attempt_key=row.metadata["attempt_key"],
            attempts=row.attempts,
            resolution="done",
            output={"ok": True},
            actor_did=state.identity.did,
        )

    state.agent_run_fn = agent_turn
    from arcagent.modules.tasks.capabilities import _state
    from arcagent.modules.tasks.models import Task

    st = await _state()
    await st.store.create(
        Task(
            id="task_tm",
            title="think",
            status="todo",
            owner_did=st.identity.did,
            creator_did=st.identity.did,
            max_attempts=3,
            metadata=_meta(node_kind="agent", tool=None, args={}),
        )
    )
    await _dispatch_tick()

    assert seen["max_cost_usd"] == 0.50
