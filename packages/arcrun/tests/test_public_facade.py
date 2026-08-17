"""Tests for ArcRun's stable root-level integration contracts."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

import arcrun


def test_run_state_remains_internal() -> None:
    assert "RunState" not in arcrun.__all__
    assert not hasattr(arcrun, "RunState")


def test_parent_run_context_is_immutable() -> None:
    bus = arcrun.EventBus("parent-1")
    context = arcrun.ParentRunContext(
        run_id="parent-1",
        depth=1,
        max_depth=3,
        event_bus=bus,
        tokens_used={"total": 7},
        cost_usd=0.25,
        tool_calls_made=2,
    )

    with pytest.raises(FrozenInstanceError):
        context.depth = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        context.tokens_used["total"] = 8  # type: ignore[index]


def test_public_errors_are_root_exported() -> None:
    assert arcrun.ExecutionIsolationError.__name__ == "ExecutionIsolationError"


def test_model_config_path_is_root_exported() -> None:
    assert arcrun.model_config_path().name == "config.toml"


def test_available_strategies_is_read_only() -> None:
    strategies = arcrun.available_strategies()

    assert isinstance(strategies, MappingProxyType)
    assert set(strategies) == {"react", "code", "dynamic"}
    with pytest.raises(TypeError):
        strategies["other"] = object()  # type: ignore[index,assignment]


async def test_dispatch_ready_is_bounded_ordered_and_isolates_failures() -> None:
    active = 0
    peak = 0

    async def run_item(item: int) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01 * (3 - item))
            if item == 1:
                raise ValueError("item failed")
            return str(item)
        finally:
            active -= 1

    outcomes = await arcrun.dispatch_ready([0, 1, 2], run_item, max_parallel=2)

    assert outcomes[0] == "0"
    assert isinstance(outcomes[1], ValueError)
    assert outcomes[2] == "2"
    assert peak == 2


async def test_dispatch_ready_rejects_invalid_limit() -> None:
    async def run_item(item: int) -> int:
        return item

    with pytest.raises(ValueError, match="max_parallel must be >= 1"):
        await arcrun.dispatch_ready([1], run_item, max_parallel=0)
