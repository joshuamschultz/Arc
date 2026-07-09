"""Tests for module bus — async event dispatch, priority, veto, lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import EventContext, ModuleBus


@pytest.fixture()
def config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
    )


@pytest.fixture()
def mock_telemetry() -> MagicMock:
    tel = MagicMock()
    tel.audit_event = MagicMock()
    return tel


@pytest.fixture()
def bus(config: ArcAgentConfig, mock_telemetry: MagicMock) -> ModuleBus:
    return ModuleBus()


class TestEventContext:
    def test_initial_state(self) -> None:
        ctx = EventContext(
            event="agent:pre_tool",
            data={"tool": "read_file"},
            agent_did="did:arc:test:executor/abcd",
            trace_id="abc123",
        )
        assert ctx.event == "agent:pre_tool"
        assert not ctx.is_vetoed
        assert ctx.veto_reason == ""

    def test_veto(self) -> None:
        ctx = EventContext(
            event="agent:pre_tool",
            data={},
            agent_did="test",
            trace_id="abc",
        )
        ctx.veto("policy violation")
        assert ctx.is_vetoed
        assert ctx.veto_reason == "policy violation"

    def test_first_veto_wins(self) -> None:
        ctx = EventContext(
            event="agent:pre_tool",
            data={},
            agent_did="test",
            trace_id="abc",
        )
        ctx.veto("first reason")
        ctx.veto("second reason")
        assert ctx.veto_reason == "first reason"


class TestEventContextImmutability:
    def test_data_is_snapshot(self) -> None:
        """EventContext data is a copy, not a reference to the original."""
        original = {"tool": "read_file"}
        ctx = EventContext(event="test", data=original, agent_did="test", trace_id="abc")
        # Mutating original does not affect ctx
        original["tool"] = "evil_tool"
        assert ctx.data["tool"] == "read_file"

    async def test_emit_data_not_mutated_by_handlers(self, bus: ModuleBus) -> None:
        """Handlers cannot mutate emit caller's data dict."""
        original_data = {"tool": "read_file"}

        async def mutating_handler(ctx: EventContext) -> None:
            ctx.data["injected"] = "evil"

        bus.subscribe("test", mutating_handler)
        await bus.emit("test", original_data)
        # Original data is not mutated
        assert "injected" not in original_data


class TestSubscribe:
    def test_register_handler(self, bus: ModuleBus) -> None:
        handler = AsyncMock()
        bus.subscribe("agent:pre_tool", handler)
        assert bus.handler_count("agent:pre_tool") == 1

    def test_register_multiple_handlers(self, bus: ModuleBus) -> None:
        bus.subscribe("agent:pre_tool", AsyncMock())
        bus.subscribe("agent:pre_tool", AsyncMock())
        assert bus.handler_count("agent:pre_tool") == 2

    def test_register_with_priority(self, bus: ModuleBus) -> None:
        bus.subscribe("agent:pre_tool", AsyncMock(), priority=10)
        bus.subscribe("agent:pre_tool", AsyncMock(), priority=200)
        assert bus.handler_count("agent:pre_tool") == 2


class TestEmit:
    async def test_handler_receives_event(self, bus: ModuleBus) -> None:
        handler = AsyncMock()
        bus.subscribe("agent:pre_tool", handler)
        await bus.emit("agent:pre_tool", {"tool": "read_file"})
        handler.assert_called_once()
        ctx = handler.call_args[0][0]
        assert isinstance(ctx, EventContext)
        assert ctx.data["tool"] == "read_file"

    async def test_priority_ordering(self, bus: ModuleBus) -> None:
        """Lower priority runs first."""
        order: list[int] = []

        async def handler_10(ctx: EventContext) -> None:
            order.append(10)

        async def handler_100(ctx: EventContext) -> None:
            order.append(100)

        async def handler_200(ctx: EventContext) -> None:
            order.append(200)

        bus.subscribe("test", handler_200, priority=200)
        bus.subscribe("test", handler_10, priority=10)
        bus.subscribe("test", handler_100, priority=100)

        await bus.emit("test", {})
        assert order == [10, 100, 200]

    async def test_same_priority_concurrent(self, bus: ModuleBus) -> None:
        """Same-priority handlers run concurrently (both complete)."""
        results: list[str] = []

        async def handler_a(ctx: EventContext) -> None:
            await asyncio.sleep(0.01)
            results.append("a")

        async def handler_b(ctx: EventContext) -> None:
            await asyncio.sleep(0.01)
            results.append("b")

        bus.subscribe("test", handler_a, priority=100)
        bus.subscribe("test", handler_b, priority=100)

        await bus.emit("test", {})
        assert sorted(results) == ["a", "b"]

    async def test_no_handlers_returns_context(self, bus: ModuleBus) -> None:
        ctx = await bus.emit("unknown_event", {"key": "val"})
        assert isinstance(ctx, EventContext)
        assert not ctx.is_vetoed


class TestErrorIsolation:
    async def test_handler_exception_doesnt_crash_others(self, bus: ModuleBus) -> None:
        results: list[str] = []

        async def failing_handler(ctx: EventContext) -> None:
            msg = "handler failed"
            raise RuntimeError(msg)

        async def good_handler(ctx: EventContext) -> None:
            results.append("good")

        bus.subscribe("test", failing_handler, priority=10)
        bus.subscribe("test", good_handler, priority=100)

        await bus.emit("test", {})
        assert "good" in results


class TestHandlerTimeout:
    async def test_handler_timeout(self, bus: ModuleBus) -> None:
        """Handler exceeding timeout is cancelled."""
        results: list[str] = []

        async def slow_handler(ctx: EventContext) -> None:
            await asyncio.sleep(10)
            results.append("slow")  # Should not reach here

        async def fast_handler(ctx: EventContext) -> None:
            results.append("fast")

        bus.subscribe("test", slow_handler, priority=10, timeout_seconds=0.1)
        bus.subscribe("test", fast_handler, priority=100)

        await bus.emit("test", {})
        assert "fast" in results
        assert "slow" not in results


class TestVetoFlow:
    async def test_veto_propagates_to_context(self, bus: ModuleBus) -> None:
        async def veto_handler(ctx: EventContext) -> None:
            ctx.veto("blocked by policy")

        bus.subscribe("agent:pre_tool", veto_handler, priority=10)

        ctx = await bus.emit("agent:pre_tool", {"tool": "shell_exec"})
        assert ctx.is_vetoed
        assert ctx.veto_reason == "blocked by policy"

    async def test_all_handlers_run_even_after_veto(self, bus: ModuleBus) -> None:
        """All handlers still run after veto — first veto wins but
        subsequent handlers still execute."""
        results: list[str] = []

        async def veto_handler(ctx: EventContext) -> None:
            ctx.veto("blocked")
            results.append("veto")

        async def logging_handler(ctx: EventContext) -> None:
            results.append("logged")

        bus.subscribe("agent:pre_tool", veto_handler, priority=10)
        bus.subscribe("agent:pre_tool", logging_handler, priority=200)

        ctx = await bus.emit("agent:pre_tool", {})
        assert ctx.is_vetoed
        assert "veto" in results
        assert "logged" in results
