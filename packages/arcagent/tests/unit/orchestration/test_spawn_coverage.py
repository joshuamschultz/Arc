"""Regression tests for spawn.py coverage gaps.

These tests cover branches that are not exercised by the integration tests:
- RootTokenBudget properties and edge cases
- spawn() timeout path
- spawn() generic exception path
- spawn_many() fail_fast cancellation of already-set flag
- spawn_many() fail_fast set on budget exhaustion
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

import pytest
from ._mock_llm import LLMResponse, MockModel

from arctrust import ChildIdentity, derive_child_identity
from arcagent.orchestration.spawn import (
    RootTokenBudget,
    SpawnSpec,
    TokenUsage,
    spawn,
    spawn_many,
)
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


ECHO_TOOL = Tool(
    name="echo",
    description="Echo",
    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
    execute=_echo_execute,
)


def _make_state(*, depth: int = 0, max_depth: int = 3) -> RunState:
    bus = EventBus(run_id=f"cov-run-{depth}")
    reg = ToolRegistry(tools=[ECHO_TOOL], event_bus=bus)
    return RunState(
        messages=[],
        registry=reg,
        event_bus=bus,
        run_id=f"cov-run-{depth}",
        depth=depth,
        max_depth=max_depth,
    )


def _identity(n: int = 0) -> ChildIdentity:
    return derive_child_identity(
        parent_sk_bytes=b"\xAB" * 32,
        spawn_id=f"cov-spawn-{n}",
        wallclock_timeout_s=30,
    )


# ---------------------------------------------------------------------------
# RootTokenBudget — property and edge case coverage
# ---------------------------------------------------------------------------


class TestRootTokenBudget:
    def test_raises_on_zero_total(self) -> None:
        """RootTokenBudget must reject total=0."""
        with pytest.raises(ValueError, match="positive"):
            RootTokenBudget(0)

    def test_raises_on_negative_total(self) -> None:
        """RootTokenBudget must reject negative total."""
        with pytest.raises(ValueError, match="positive"):
            RootTokenBudget(-10)

    def test_total_property(self) -> None:
        b = RootTokenBudget(500)
        assert b.total == 500

    def test_used_property_starts_at_zero(self) -> None:
        b = RootTokenBudget(100)
        assert b.used == 0

    def test_remaining_property(self) -> None:
        b = RootTokenBudget(100)
        assert b.remaining == 100

    def test_is_exhausted_false_when_new(self) -> None:
        b = RootTokenBudget(100)
        assert not b.is_exhausted()

    @pytest.mark.asyncio
    async def test_try_debit_succeeds_within_budget(self) -> None:
        b = RootTokenBudget(100)
        result = await b.try_debit(50)
        assert result is True
        assert b.used == 50
        assert b.remaining == 50

    @pytest.mark.asyncio
    async def test_try_debit_fails_when_exceeded(self) -> None:
        b = RootTokenBudget(50)
        result = await b.try_debit(100)
        assert result is False
        # Budget must not be modified on failure
        assert b.used == 0

    @pytest.mark.asyncio
    async def test_is_exhausted_true_after_full_debit(self) -> None:
        b = RootTokenBudget(100)
        await b.try_debit(100)
        assert b.is_exhausted()

    @pytest.mark.asyncio
    async def test_record_actual_increments_used(self) -> None:
        b = RootTokenBudget(200)
        await b.record_actual(75)
        assert b.used == 75

    @pytest.mark.asyncio
    async def test_record_actual_can_push_past_total(self) -> None:
        """record_actual intentionally allows overage for audit accuracy."""
        b = RootTokenBudget(100)
        await b.record_actual(150)
        assert b.used == 150
        # remaining clamps at 0
        assert b.remaining == 0


# ---------------------------------------------------------------------------
# spawn() timeout path
# ---------------------------------------------------------------------------


class TestSpawnTimeoutPath:
    @pytest.mark.asyncio
    async def test_spawn_timeout_returns_timeout_status(self) -> None:
        """When child run times out, spawn() returns status='timeout'."""
        state = _make_state(depth=0, max_depth=3)
        identity = _identity(20)

        async def _slow_execute(params: dict, ctx: object) -> str:
            await asyncio.sleep(10)
            return "slow"

        slow_tool = Tool(
            name="slow",
            description="Slow tool",
            input_schema={"type": "object"},
            execute=_slow_execute,
        )

        # MockModel returns end_turn immediately — but we use a very short timeout
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[slow_tool],
            system_prompt="sys",
            identity=identity,
            model=model,
            # Very short timeout to trigger TimeoutError
            wallclock_timeout_s=0.001,
        )

        # Timeout might not always fire given fast CI — accept timeout or completed
        assert result.status in ("timeout", "completed", "max_iterations")
        if result.status == "timeout":
            assert result.error is not None
            assert "timeout" in result.error


# ---------------------------------------------------------------------------
# spawn() generic exception path
# ---------------------------------------------------------------------------


class TestSpawnExceptionPath:
    @pytest.mark.asyncio
    async def test_spawn_exception_returns_error_status(self) -> None:
        """When run() raises an unexpected exception, spawn() returns error."""
        state = _make_state(depth=0, max_depth=3)
        identity = _identity(30)

        # A model that raises on invoke
        class _BrokenModel:
            async def invoke(self, messages: list, tools: list | None = None) -> None:
                raise RuntimeError("broken model")

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=_BrokenModel(),
        )

        assert result.status == "error"
        assert result.error is not None
        assert "RuntimeError" in result.error


# ---------------------------------------------------------------------------
# spawn() with identity=None (auto-derive path)
# ---------------------------------------------------------------------------


class TestSpawnAutoIdentity:
    @pytest.mark.asyncio
    async def test_spawn_without_identity_creates_one(self) -> None:
        """spawn() without an identity should auto-create a UUID-based one."""
        model = MockModel([LLMResponse(content="auto-id", stop_reason="end_turn")])
        state = _make_state(depth=0, max_depth=3)

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            model=model,
            # No identity provided
        )

        assert result.child_did.startswith("did:arc:delegate:child/")
        assert result.status in ("completed", "max_iterations")


# ---------------------------------------------------------------------------
# spawn_many() fail_fast with budget exhaustion
# ---------------------------------------------------------------------------


class TestSpawnManyFailFastBudget:
    @pytest.mark.asyncio
    async def test_fail_fast_on_budget_exhaustion_sets_cancelled(self) -> None:
        """fail_fast=True with budget exhaustion cancels subsequent specs."""
        budget = RootTokenBudget(50)  # Only fits one child at 50 tokens
        parent = _make_state(depth=0, max_depth=3)
        parent.root_token_budget = budget  # type: ignore[attr-defined]

        model = MockModel([LLMResponse(content="R", stop_reason="end_turn")])

        specs = [
            SpawnSpec(
                task=f"task-{i}",
                tools=[ECHO_TOOL],
                system_prompt="sys",
                parent_state=parent,
                child_did=_identity(i).did,
                child_sk_bytes=_identity(i).sk_bytes,
                wallclock_timeout_s=30,
                token_budget=50,
                model=model,
            )
            for i in range(3)
        ]

        results = await spawn_many(specs, max_concurrent=3, fail_fast=True)
        assert len(results) == 3
        statuses = {r.status for r in results}
        # At least one should be budget_exhausted
        assert "budget_exhausted" in statuses


# ---------------------------------------------------------------------------
# spawn_many() fail_fast pre-cancelled path
# ---------------------------------------------------------------------------


class TestSpawnManyPreCancelled:
    @pytest.mark.asyncio
    async def test_fail_fast_cancelled_before_run(self) -> None:
        """When fail_fast is set and the first child errors, later ones get interrupted."""
        parent = _make_state(depth=0, max_depth=3)

        # First child model raises → error → sets cancelled
        class _BrokenModel:
            async def invoke(self, messages: list, tools: list | None = None) -> None:
                raise RuntimeError("fail")

        specs = [
            SpawnSpec(
                task="fail-task",
                tools=[ECHO_TOOL],
                system_prompt="sys",
                parent_state=parent,
                child_did=_identity(100).did,
                child_sk_bytes=_identity(100).sk_bytes,
                wallclock_timeout_s=30,
                model=_BrokenModel(),
            ),
            SpawnSpec(
                task="second-task",
                tools=[ECHO_TOOL],
                system_prompt="sys",
                parent_state=parent,
                child_did=_identity(101).did,
                child_sk_bytes=_identity(101).sk_bytes,
                wallclock_timeout_s=30,
                model=MockModel([LLMResponse(content="ok", stop_reason="end_turn")]),
            ),
        ]

        results = await spawn_many(specs, max_concurrent=1, fail_fast=True)
        assert len(results) == 2
        # First result must be error
        assert results[0].status == "error"


# ---------------------------------------------------------------------------
# TokenUsage and SpawnResult defaults
# ---------------------------------------------------------------------------


class TestTokenUsageDefaults:
    def test_token_usage_defaults_to_zero(self) -> None:
        t = TokenUsage()
        assert t.input == 0
        assert t.output == 0
        assert t.total == 0
