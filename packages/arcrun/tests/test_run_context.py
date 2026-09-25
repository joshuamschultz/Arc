"""ArcRun's public request identity follows nested and failed model calls."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from packages.arcrun.tests.conftest import LLMResponse

import arcrun
from arcrun.types import Tool


class _Response:
    content = "done"
    usage = None


@pytest.mark.asyncio
async def test_nested_oneshot_restores_parent_run_identity() -> None:
    seen: list[str | None] = []

    class Inner:
        async def invoke(self, _messages: Any, **_kwargs: Any) -> _Response:
            seen.append(arcrun.current_run_id())
            return _Response()

    class Outer:
        async def invoke(self, _messages: Any, **_kwargs: Any) -> _Response:
            seen.append(arcrun.current_run_id())
            await arcrun.run_oneshot(Inner(), user="inner", run_id="inner")
            seen.append(arcrun.current_run_id())
            return _Response()

    await arcrun.run_oneshot(Outer(), user="outer", run_id="outer")
    assert seen == ["outer", "inner", "outer"]
    assert arcrun.current_run_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_oneshot_restores_run_identity(failure: str) -> None:
    class Failing:
        async def invoke(self, _messages: Any, **_kwargs: Any) -> _Response:
            assert arcrun.current_run_id() == "failing"
            if failure == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("provider failed")

    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await arcrun.run_oneshot(Failing(), user="fail", run_id="failing")
    assert arcrun.current_run_id() is None


@pytest.mark.asyncio
async def test_run_async_task_inherits_its_own_run_identity() -> None:
    seen: list[str | None] = []

    class Model:
        async def invoke(self, _messages: Any, **_kwargs: Any) -> LLMResponse:
            seen.append(arcrun.current_run_id())
            return LLMResponse(content="done")

    async def execute(_args: dict[str, Any], _ctx: Any) -> str:
        return "done"

    provider = arcrun.StaticProvider(
        [Tool(name="echo", description="echo", input_schema={"type": "object"}, execute=execute)]
    )
    handle = await arcrun.run_async(
        Model(), provider, "system", "task", allowed_strategies=["react"], run_id="async-run"
    )
    assert arcrun.current_run_id() is None
    await handle.result()
    assert seen == ["async-run"]
    assert arcrun.current_run_id() is None
