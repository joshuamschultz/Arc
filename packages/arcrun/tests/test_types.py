"""Tests for arcrun type definitions."""

import asyncio

import pytest


async def _noop_execute(params: dict, ctx: object) -> str:
    return "ok"


class TestTool:
    def test_construction_with_all_fields(self):
        from arcrun.types import Tool

        tool = Tool(
            name="search",
            description="Search the web",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            execute=_noop_execute,
        )
        assert tool.name == "search"
        assert tool.description == "Search the web"
        assert tool.input_schema["type"] == "object"
        assert tool.execute is _noop_execute

    def test_execute_is_callable(self):
        from arcrun.types import Tool

        tool = Tool(name="t", description="d", input_schema={}, execute=_noop_execute)
        assert callable(tool.execute)

    @pytest.mark.asyncio
    async def test_execute_returns_string(self):
        from arcrun.types import Tool

        tool = Tool(name="t", description="d", input_schema={}, execute=_noop_execute)
        result = await tool.execute({}, None)
        assert isinstance(result, str)


class TestToolContext:
    def test_construction_with_all_fields(self):
        from arcrun.types import ToolContext

        cancel = asyncio.Event()
        ctx = ToolContext(
            run_id="run-1",
            tool_call_id="tc-1",
            turn_number=3,
            event_bus=None,
            cancelled=cancel,
        )
        assert ctx.run_id == "run-1"
        assert ctx.tool_call_id == "tc-1"
        assert ctx.turn_number == 3
        assert ctx.event_bus is None
        assert ctx.cancelled is cancel

    def test_cancelled_event_works(self):
        from arcrun.types import ToolContext

        cancel = asyncio.Event()
        ctx = ToolContext(
            run_id="r",
            tool_call_id="t",
            turn_number=0,
            event_bus=None,
            cancelled=cancel,
        )
        assert not ctx.cancelled.is_set()
        cancel.set()
        assert ctx.cancelled.is_set()


class TestSandboxConfig:
    def test_defaults_no_sandbox(self):
        from arcrun.types import SandboxConfig

        cfg = SandboxConfig()
        assert cfg.allowed_tools is None
        assert cfg.check is None

    def test_with_allowlist(self):
        from arcrun.types import SandboxConfig

        cfg = SandboxConfig(allowed_tools=["search", "calculate"])
        assert cfg.allowed_tools == ["search", "calculate"]
        assert cfg.check is None

    def test_with_check_callback(self):
        from arcrun.types import SandboxConfig

        async def checker(name: str, params: dict) -> tuple[bool, str]:
            return True, ""

        cfg = SandboxConfig(check=checker)
        assert cfg.check is checker


class TestLoopResult:
    def test_construction_with_all_fields(self):
        from arcrun.types import LoopResult

        result = LoopResult(
            content="done",
            turns=3,
            tool_calls_made=5,
            tokens_used={"input": 100, "output": 50, "total": 150},
            strategy_used="react",
            cost_usd=0.01,
        )
        assert result.content == "done"
        assert result.turns == 3
        assert result.tool_calls_made == 5
        assert result.tokens_used["total"] == 150
        assert result.strategy_used == "react"
        assert result.cost_usd == 0.01

    def test_events_default_empty(self):
        from arcrun.types import LoopResult

        result = LoopResult(
            content=None,
            turns=0,
            tool_calls_made=0,
            tokens_used={},
            strategy_used="react",
            cost_usd=0.0,
        )
        assert result.events == []

    def test_content_can_be_none(self):
        from arcrun.types import LoopResult

        result = LoopResult(
            content=None,
            turns=0,
            tool_calls_made=0,
            tokens_used={},
            strategy_used="react",
            cost_usd=0.0,
        )
        assert result.content is None

    def test_verify_integrity_valid_chain(self):
        from arcrun.events import EventBus
        from arcrun.types import LoopResult

        bus = EventBus(run_id="r")
        bus.emit("a")
        bus.emit("b")

        result = LoopResult(
            content="done",
            turns=1,
            tool_calls_made=0,
            tokens_used={},
            strategy_used="react",
            cost_usd=0.0,
            events=bus.events,
        )
        verification = result.verify_integrity()
        assert verification.valid is True
        assert verification.event_count == 2

    def test_verify_integrity_empty_events(self):
        from arcrun.types import LoopResult

        result = LoopResult(
            content="done",
            turns=1,
            tool_calls_made=0,
            tokens_used={},
            strategy_used="react",
            cost_usd=0.0,
        )
        verification = result.verify_integrity()
        assert verification.valid is True
        assert verification.event_count == 0
