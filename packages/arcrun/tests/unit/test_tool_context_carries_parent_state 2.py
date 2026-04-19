"""Architecture test: arcrun.executor populates ToolContext.parent_state.

This test verifies the structural guarantee that the execute_tool_call()
function in arcrun.executor passes parent_state=state when constructing
ToolContext.  This is the M3 gap-close requirement: delegate_tool must
be able to read ctx.parent_state.depth rather than defaulting to 0.

Two verification approaches:
1. AST scan — confirms parent_state= is present in the ToolContext() call
   inside executor.py.  This is a static guarantee that cannot be bypassed
   at runtime.
2. Runtime check — actually calls execute_tool_call and confirms the
   ToolContext received by the tool has parent_state set correctly.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import pathlib

import pytest

# Path to the executor module under test.
_EXECUTOR_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "src"
    / "arcrun"
    / "executor.py"
)


# ---------------------------------------------------------------------------
# AST scan: static structural guarantee
# ---------------------------------------------------------------------------


class TestToolContextParentStatePresentInAST:
    """Verify that the ToolContext() call in executor.py sets parent_state."""

    def _load_ast(self) -> ast.Module:
        source = _EXECUTOR_PATH.read_text(encoding="utf-8")
        return ast.parse(source)

    def _find_tool_context_calls(self, tree: ast.Module) -> list[ast.Call]:
        """Return all Call nodes whose function is 'ToolContext'."""
        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "ToolContext":
                    calls.append(node)
                elif isinstance(func, ast.Attribute) and func.attr == "ToolContext":
                    calls.append(node)
        return calls

    def test_tool_context_constructed_in_executor(self) -> None:
        """executor.py must construct at least one ToolContext."""
        tree = self._load_ast()
        calls = self._find_tool_context_calls(tree)
        assert len(calls) >= 1, (
            "executor.py does not construct ToolContext; "
            "parent_state plumbing cannot be verified"
        )

    def test_tool_context_call_includes_parent_state_kwarg(self) -> None:
        """Every ToolContext() call in executor.py must pass parent_state=."""
        tree = self._load_ast()
        calls = self._find_tool_context_calls(tree)
        assert calls, "No ToolContext() calls found in executor.py"

        for call in calls:
            kwarg_names = {kw.arg for kw in call.keywords}
            assert "parent_state" in kwarg_names, (
                f"ToolContext() at line {call.lineno} in executor.py "
                f"does not set parent_state keyword argument. "
                f"Found kwargs: {kwarg_names}"
            )

    def test_parent_state_assigned_from_state(self) -> None:
        """The parent_state keyword must be assigned the value 'state' (the RunState arg)."""
        tree = self._load_ast()
        calls = self._find_tool_context_calls(tree)
        assert calls

        for call in calls:
            for kw in call.keywords:
                if kw.arg == "parent_state":
                    # The value should be the Name 'state'
                    assert isinstance(kw.value, ast.Name), (
                        "parent_state in ToolContext() is not a simple name reference; "
                        f"found {type(kw.value).__name__} at line {call.lineno}"
                    )
                    assert kw.value.id == "state", (
                        f"parent_state in ToolContext() references '{kw.value.id}', "
                        "expected 'state' (the RunState parameter)"
                    )


# ---------------------------------------------------------------------------
# Runtime check: ToolContext.parent_state is populated during execution
# ---------------------------------------------------------------------------


class TestToolContextParentStateRuntime:
    """Confirm parent_state reaches Tool.execute via a real execute_tool_call() call."""

    @pytest.mark.asyncio
    async def test_tool_receives_parent_state_in_context(self) -> None:
        """Tool.execute receives ctx.parent_state pointing to the live RunState."""
        from arcrun.events import EventBus
        from arcrun.executor import execute_tool_call
        from arcrun.registry import ToolRegistry
        from arcrun.sandbox import Sandbox
        from arcrun.state import RunState
        from arcrun.types import SandboxConfig, Tool, ToolContext

        received_ctx: list[ToolContext] = []

        async def _capture_ctx(params: dict, ctx: ToolContext) -> str:
            received_ctx.append(ctx)
            return "captured"

        dummy_tool = Tool(
            name="dummy",
            description="captures ctx",
            input_schema={"type": "object", "properties": {}, "required": []},
            execute=_capture_ctx,
        )

        bus = EventBus(run_id="arch-test")
        registry = ToolRegistry(tools=[dummy_tool], event_bus=bus)
        state = RunState(
            messages=[],
            registry=registry,
            event_bus=bus,
            run_id="arch-test",
            depth=2,  # non-default depth so we can verify propagation
            max_depth=5,
        )
        sandbox = Sandbox(config=SandboxConfig(), event_bus=bus)

        # Build a minimal tool-call object
        class FakeTC:
            id = "tc-arch"
            name = "dummy"
            arguments: dict = {}

        await execute_tool_call(FakeTC(), state, sandbox)

        assert len(received_ctx) == 1
        ctx = received_ctx[0]
        assert ctx.parent_state is state, (
            "ToolContext.parent_state must point to the live RunState"
        )
        assert ctx.parent_state.depth == 2, (
            "parent_state.depth must match the RunState depth passed to execute_tool_call"
        )
