"""CodeExec strategy: augment system prompt for code-first problem solving."""

from __future__ import annotations

from typing import Any

from arcrun._messages import system_message
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.strategies.react import react_loop
from arcrun.types import LoopResult

_DEFAULT_PREFIX = """\
You have access to a Python execution tool (execute_python). \
Write executable Python code to solve tasks.

GUIDELINES:
- Write focused scripts (20-50 lines) solving one sub-problem at a time
- You will receive {stdout, stderr, exit_code, duration_ms} after each execution
- Each execution is stateless - variables do NOT persist between calls
- If code fails, examine the error and fix your approach
- After 3 failures on the same approach, try a fundamentally different method
- Use code for: computation, data processing, logic, file operations
- Use other tools for: external APIs, user confirmation, security-sensitive ops
"""


class CodeExecStrategy(Strategy):
    """Augments system prompt to encourage code-first problem solving.

    Delegates to react_loop after prompt augmentation.
    """

    def __init__(self, system_prompt_prefix: str | None = None) -> None:
        self._prefix = system_prompt_prefix or _DEFAULT_PREFIX

    @property
    def name(self) -> str:
        return "code"

    @property
    def description(self) -> str:
        return (
            "Write and execute Python code to solve tasks. Best for computation, "
            "data processing, and problems where code is more effective than "
            "predefined tool calls."
        )

    async def __call__(
        self,
        model: Any,
        state: RunState,
        sandbox: Sandbox,
        max_turns: int,
    ) -> LoopResult:
        original = state.messages[0].content
        state.messages[0] = system_message(self._prefix + "\n" + original)

        state.event_bus.emit(
            "code.prompt.augmented",
            {
                "original_length": len(original),
                "augmented_length": len(state.messages[0].content),
            },
        )

        return await react_loop(model, state, sandbox, max_turns)
