"""CodeExec strategy: augment system prompt for code-first problem solving."""

from __future__ import annotations

from typing import Any

from arcprompt import load_stock

from arcrun._messages import system_message
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.strategies.react import react_loop
from arcrun.types import LoopResult


class CodeExecStrategy(Strategy):
    """Augments system prompt to encourage code-first problem solving.

    Delegates to react_loop after prompt augmentation.
    """

    @property
    def name(self) -> str:
        return "code"

    @property
    def description(self) -> str:
        return load_stock("arcrun", "strategy_code_description")

    @property
    def prompt_guidance(self) -> str:
        return load_stock("arcrun", "strategy_code")

    async def __call__(
        self,
        model: Any,
        state: RunState,
        sandbox: Sandbox,
        max_turns: int,
    ) -> LoopResult:
        original = state.messages[0].content
        prefix = load_stock("arcrun", "code_exec_prefix")
        state.messages[0] = system_message(prefix + "\n" + original)

        state.event_bus.emit(
            "code.prompt.augmented",
            {
                "original_length": len(original),
                "augmented_length": len(state.messages[0].content),
            },
        )

        return await react_loop(model, state, sandbox, max_turns)
