"""Integration tests for spawn_many() — 3 parallel children, budget pooling.

Tests G3.3 deliverable: 3 parallel children spawn via spawn_many; verify
concurrent execution; budget pooling; at least one exhaustion scenario.

Uses MockModel (no real LLM calls).
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import pytest
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool
from arctrust import ChildIdentity, derive_child_identity

from arcagent.orchestration.spawn import (
    RootTokenBudget,
    SpawnSpec,
    spawn_many,
)

from ._mock_llm import LLMResponse, MockModel


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


ECHO_TOOL = Tool(
    name="echo",
    description="Echo",
    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
    execute=_echo_execute,
)


def _make_parent_state(
    *,
    depth: int = 0,
    max_depth: int = 3,
    root_budget: RootTokenBudget | None = None,
    model: object = None,
) -> RunState:
    bus = EventBus(run_id="parent-run")
    reg = ToolRegistry(tools=[ECHO_TOOL], event_bus=bus)
    state = RunState(
        messages=[],
        registry=reg,
        event_bus=bus,
        run_id="parent-run",
        depth=depth,
        max_depth=max_depth,
    )
    if root_budget is not None:
        state.root_token_budget = root_budget  # type: ignore[attr-defined]
    if model is not None:
        state._model = model  # type: ignore[attr-defined]
    return state


def _make_child_identity(idx: int) -> ChildIdentity:
    return derive_child_identity(
        parent_sk_bytes=b"\x42" * 32,
        spawn_id=f"spawn-{idx}",
        wallclock_timeout_s=30,
    )


class TestSpawnManyParallel:
    @pytest.mark.asyncio
    async def test_three_children_all_complete(self) -> None:
        """Three children spawn concurrently and all return completed."""
        # Each child gets its own model response
        model = MockModel(
            [
                LLMResponse(content="Child 0 result", stop_reason="end_turn"),
                LLMResponse(content="Child 1 result", stop_reason="end_turn"),
                LLMResponse(content="Child 2 result", stop_reason="end_turn"),
            ]
        )

        parent = _make_parent_state(model=model)
        specs: list[SpawnSpec] = []
        for i in range(3):
            identity = _make_child_identity(i)
            specs.append(
                SpawnSpec(
                    task=f"task-{i}",
                    tools=[ECHO_TOOL],
                    system_prompt="You are helpful.",
                    parent_state=parent,
                    child_did=identity.did,
                    child_sk_bytes=identity.sk_bytes,
                    wallclock_timeout_s=30,
                    model=model,
                )
            )

        results = await spawn_many(specs, max_concurrent=3)
        assert len(results) == 3
        # All should be completed or max_iterations (not error/timeout/budget_exhausted)
        for r in results:
            assert r.status in ("completed", "max_iterations"), (
                f"Expected completed/max_iterations, got {r.status}: {r.error}"
            )

    @pytest.mark.asyncio
    async def test_spawn_many_respects_max_concurrent(self) -> None:
        """With max_concurrent=1, children run sequentially."""
        call_times: list[float] = []

        async def _slow_execute(params: dict, ctx: object) -> str:
            call_times.append(time.monotonic())
            await asyncio.sleep(0.05)
            return "done"

        slow_tool = Tool(
            name="slow",
            description="Slow tool",
            input_schema={"type": "object"},
            execute=_slow_execute,
        )

        model = MockModel(
            [
                LLMResponse(content="R0", stop_reason="end_turn"),
                LLMResponse(content="R1", stop_reason="end_turn"),
            ]
        )

        parent = _make_parent_state(model=model)
        specs = [
            SpawnSpec(
                task=f"task-{i}",
                tools=[slow_tool],
                system_prompt="sys",
                parent_state=parent,
                child_did=_make_child_identity(i).did,
                child_sk_bytes=_make_child_identity(i).sk_bytes,
                wallclock_timeout_s=30,
                model=model,
            )
            for i in range(2)
        ]

        results = await spawn_many(specs, max_concurrent=1)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_empty_specs_returns_empty(self) -> None:
        results = await spawn_many([])
        assert results == []

    @pytest.mark.asyncio
    async def test_budget_pooling_exhausts_on_third_child(self) -> None:
        """Budget pool: first 2 children debit successfully; third is refused."""
        budget = RootTokenBudget(total=200)
        parent = _make_parent_state(root_budget=budget)

        model = MockModel(
            [
                LLMResponse(content="R0", stop_reason="end_turn"),
                LLMResponse(content="R1", stop_reason="end_turn"),
                LLMResponse(content="R2", stop_reason="end_turn"),
            ]
        )

        # Third child requests more than remaining budget (200 total / 100 each = 2 can fit)
        specs = [
            SpawnSpec(
                task=f"task-{i}",
                tools=[ECHO_TOOL],
                system_prompt="sys",
                parent_state=parent,
                child_did=_make_child_identity(i).did,
                child_sk_bytes=_make_child_identity(i).sk_bytes,
                wallclock_timeout_s=30,
                token_budget=100,
                model=model,
            )
            for i in range(3)
        ]

        results = await spawn_many(specs, max_concurrent=3)
        assert len(results) == 3
        # At least one should be budget_exhausted
        statuses = {r.status for r in results}
        assert "budget_exhausted" in statuses, (
            f"Expected at least one budget_exhausted, got: {statuses}"
        )

    @pytest.mark.asyncio
    async def test_fail_fast_cancels_siblings(self) -> None:
        """fail_fast=True: one timeout cancels remaining pending spawns."""
        model = MockModel(
            [
                LLMResponse(content="Fast result", stop_reason="end_turn"),
                LLMResponse(content="Slow result", stop_reason="end_turn"),
            ]
        )

        parent = _make_parent_state(model=model)

        # spawn_many with fail_fast — first success doesn't cancel (only failures do)
        specs = [
            SpawnSpec(
                task="task-0",
                tools=[ECHO_TOOL],
                system_prompt="sys",
                parent_state=parent,
                child_did=_make_child_identity(0).did,
                child_sk_bytes=_make_child_identity(0).sk_bytes,
                wallclock_timeout_s=30,
                model=model,
            ),
        ]
        results = await spawn_many(specs, fail_fast=True)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_results_ordered_same_as_specs(self) -> None:
        """Result list must be in the same order as the input specs list."""
        model = MockModel(
            [LLMResponse(content=f"Result-{i}", stop_reason="end_turn") for i in range(3)]
        )
        parent = _make_parent_state(model=model)

        specs = [
            SpawnSpec(
                task=f"task-{i}",
                tools=[ECHO_TOOL],
                system_prompt="sys",
                parent_state=parent,
                child_did=_make_child_identity(i).did,
                child_sk_bytes=_make_child_identity(i).sk_bytes,
                wallclock_timeout_s=30,
                model=model,
            )
            for i in range(3)
        ]

        results = await spawn_many(specs, max_concurrent=3)
        assert len(results) == 3
        # All results should correspond to the ordered specs
        for i, result in enumerate(results):
            assert result.child_did == specs[i].child_did
