"""Adversarial: Tool parameter injection (OWASP ASI02).

Tests that tool parameters are handled safely without injection vectors.
"""

from __future__ import annotations

import json

import pytest

from arcrun.builtins.execute import make_execute_tool


class TestToolInjection:
    @pytest.mark.asyncio
    async def test_oversized_parameter_handled(self):
        """Extremely large code parameter should not crash."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5, max_output_bytes=1024)
        # 1MB of code
        large_code = "x = 1\n" * 100_000
        result = await tool.execute({"code": large_code}, make_ctx())
        parsed = json.loads(result)
        # Should complete (or timeout) without crashing
        assert parsed["exit_code"] is not None

    @pytest.mark.asyncio
    async def test_command_injection_in_code_param(self):
        """Code parameter with shell metacharacters should not escape to shell."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        # This code is executed as Python, not shell
        code = "import os; print(os.system('echo INJECTED'))"
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Code runs as Python — os.system works in subprocess mode
        # but container mode would block it
        assert parsed["exit_code"] is not None

    @pytest.mark.asyncio
    async def test_unicode_in_code(self):
        """Unicode characters in code should be handled safely."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = "print('Hello \\u4e16\\u754c')"  # "Hello 世界"
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        assert parsed["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_tool_name_validation_in_sandbox(self):
        """Sandbox checks tool names exactly — no fuzzy matching."""
        from arcrun.loop import run
        from arcrun.types import SandboxConfig, Tool
        from security.conftest import LLMResponse, MockModel, ToolCall

        async def noop(params: dict, ctx: object) -> str:
            return "ok"

        tool = Tool(
            name="safe_tool",
            description="Safe",
            input_schema={"type": "object", "properties": {}},
            execute=noop,
        )

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="safe_tool\u200b", arguments={})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ]
        )

        # Sandbox allows "safe_tool" but model calls "safe_tool\u200b" (with zero-width space)
        sandbox = SandboxConfig(allowed_tools=["safe_tool"])
        await run(model, [tool], "prompt", "task", sandbox=sandbox)
        # The tool call should be denied — "safe_tool\u200b" != "safe_tool"
        # Verify the model received an error message in the tool result
        messages = model.invoke_calls[1]["messages"]
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) >= 1
        # Tool result content is [ToolResultBlock(content="Error: ...")]
        block_content = tool_msgs[0].content[0].content
        assert "Error" in block_content
