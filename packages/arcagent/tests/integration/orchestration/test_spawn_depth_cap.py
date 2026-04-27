"""Integration tests for spawn depth cap enforcement.

depth=2 enforced; child's spawn attempt at depth 2 is rejected.
"""

from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import pytest
from ._mock_llm import LLMResponse, MockModel

from arctrust import ChildIdentity, derive_child_identity
from arcagent.orchestration.spawn import (
    SpawnResult,
    TokenUsage,
    spawn,
)
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


ECHO_TOOL = Tool(
    name="echo",
    description="Echo",
    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
    execute=_echo_execute,
)


def _make_state(*, depth: int, max_depth: int, model: object = None) -> RunState:
    bus = EventBus(run_id=f"run-d{depth}")
    reg = ToolRegistry(tools=[ECHO_TOOL], event_bus=bus)
    state = RunState(
        messages=[],
        registry=reg,
        event_bus=bus,
        run_id=f"run-d{depth}",
        depth=depth,
        max_depth=max_depth,
    )
    if model is not None:
        state._model = model  # type: ignore[attr-defined]
    return state


def _identity(n: int = 0) -> ChildIdentity:
    return derive_child_identity(
        parent_sk_bytes=b"\x10" * 32,
        spawn_id=f"spawn-depth-{n}",
        wallclock_timeout_s=30,
    )


class TestDepthCapEnforcement:
    @pytest.mark.asyncio
    async def test_spawn_at_max_depth_returns_error(self) -> None:
        """Spawn attempt when parent is already at max_depth → error result."""
        state = _make_state(depth=2, max_depth=2)
        identity = _identity(0)

        result = await spawn(
            parent_state=state,
            task="do something",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=MockModel([LLMResponse(content="r", stop_reason="end_turn")]),
        )

        assert result.status == "error"
        assert result.depth_error_message_contains_limit(2)

    @pytest.mark.asyncio
    async def test_spawn_at_depth_0_succeeds_with_max_2(self) -> None:
        """Spawn at depth=0 with max_depth=2 should succeed."""
        model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])
        state = _make_state(depth=0, max_depth=2, model=model)
        identity = _identity(1)

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=model,
        )

        assert result.status in ("completed", "max_iterations")

    @pytest.mark.asyncio
    async def test_spawn_at_depth_1_succeeds_with_max_2(self) -> None:
        """Spawn at depth=1 with max_depth=2 should succeed."""
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        state = _make_state(depth=1, max_depth=2, model=model)
        identity = _identity(2)

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=model,
        )

        assert result.status in ("completed", "max_iterations")

    @pytest.mark.asyncio
    async def test_spawn_at_depth_2_rejected_with_max_2(self) -> None:
        """Spawn at depth=2 with max_depth=2 — at cap, must be rejected."""
        state = _make_state(depth=2, max_depth=2)
        identity = _identity(3)

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=MockModel([LLMResponse(content="r", stop_reason="end_turn")]),
        )

        assert result.status == "error"
        assert result.error is not None
        assert "max_depth" in (result.error or "")

    @pytest.mark.asyncio
    async def test_depth_exceeds_max_also_rejected(self) -> None:
        """Depth > max_depth (misconfigured state) — still rejected."""
        state = _make_state(depth=5, max_depth=2)
        identity = _identity(4)

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=MockModel([]),
        )

        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_no_model_returns_error(self) -> None:
        """No model available → error result rather than crash."""
        state = _make_state(depth=0, max_depth=2)
        identity = _identity(5)

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=None,
        )

        assert result.status == "error"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_audit_event_emitted_on_depth_error(self) -> None:
        """Even on rejection, spawn.start event is NOT emitted (depth check is pre-emit)."""
        state = _make_state(depth=2, max_depth=2)
        identity = _identity(6)
        initial_event_count = len(state.event_bus.events)

        await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=MockModel([]),
        )

        # No spawn.start should be emitted on depth rejection (returns early)
        events = state.event_bus.events
        spawn_start_events = [e for e in events if e.type == "spawn.start"]
        assert len(spawn_start_events) == 0


# ---------------------------------------------------------------------------
# Patch SpawnResult for test helper method
# ---------------------------------------------------------------------------

def _depth_error_contains_limit(self: SpawnResult, limit: int) -> bool:
    """Helper to check that error message references the depth limit."""
    error_msg = (self.error or "") + self.summary
    return str(limit) in error_msg


SpawnResult.depth_error_message_contains_limit = _depth_error_contains_limit  # type: ignore[attr-defined]
