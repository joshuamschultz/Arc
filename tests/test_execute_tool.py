"""Tests for make_execute_tool() factory and execution."""
import json

import pytest

from arcrun.types import Tool, ToolContext
from arcrun.events import EventBus


def _make_ctx() -> ToolContext:
    import asyncio

    return ToolContext(
        run_id="test",
        tool_call_id="tc1",
        turn_number=1,
        event_bus=EventBus(run_id="test"),
        cancelled=asyncio.Event(),
    )


class TestMakeExecuteTool:
    def test_factory_returns_tool(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        assert isinstance(tool, Tool)

    def test_tool_name_is_execute_python(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        assert tool.name == "execute_python"

    def test_tool_has_code_in_schema(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        assert "code" in tool.input_schema["properties"]
        assert "code" in tool.input_schema["required"]

    def test_tool_timeout_is_none(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        assert tool.timeout_seconds is None


class TestExecuteToolExecution:
    @pytest.mark.asyncio
    async def test_simple_print_stdout(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "print('hello world')"}, ctx)
        result = json.loads(raw)
        assert result["stdout"].strip() == "hello world"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_stderr_capture(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "import sys; sys.stderr.write('oops\\n')"}, ctx)
        result = json.loads(raw)
        assert "oops" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exit_code_on_failure(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "raise ValueError('bad')"}, ctx)
        result = json.loads(raw)
        assert result["exit_code"] != 0

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool(timeout_seconds=1)
        ctx = _make_ctx()
        raw = await tool.execute({"code": "import time; time.sleep(30)"}, ctx)
        result = json.loads(raw)
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    @pytest.mark.asyncio
    async def test_output_truncation(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool(max_output_bytes=50)
        ctx = _make_ctx()
        raw = await tool.execute({"code": "print('x' * 1000)"}, ctx)
        result = json.loads(raw)
        assert len(result["stdout"]) <= 50

    @pytest.mark.asyncio
    async def test_structured_json_result(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "print(42)"}, ctx)
        result = json.loads(raw)
        assert "stdout" in result
        assert "stderr" in result
        assert "exit_code" in result
        assert "duration_ms" in result

    @pytest.mark.asyncio
    async def test_minimal_env_no_home_leak(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "import os; print(os.environ.get('HOME', ''))"}, ctx)
        result = json.loads(raw)
        assert "/Users/" not in result["stdout"]

    @pytest.mark.asyncio
    async def test_temp_dir_isolation(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "import os; print(os.getcwd())"}, ctx)
        result = json.loads(raw)
        assert "/tmp" in result["stdout"].lower() or "tmp" in result["stdout"].lower()

    @pytest.mark.asyncio
    async def test_duration_ms_present(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool()
        ctx = _make_ctx()
        raw = await tool.execute({"code": "print(1)"}, ctx)
        result = json.loads(raw)
        assert isinstance(result["duration_ms"], (int, float))
        assert result["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_extra_env(self):
        from arcrun.builtins import make_execute_tool

        tool = make_execute_tool(extra_env={"MY_VAR": "test123"})
        ctx = _make_ctx()
        raw = await tool.execute({"code": "import os; print(os.environ['MY_VAR'])"}, ctx)
        result = json.loads(raw)
        assert result["stdout"].strip() == "test123"
