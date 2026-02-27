"""Strategy interface and selection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcrun.sandbox import Sandbox
    from arcrun.state import RunState
    from arcrun.types import LoopResult


class Strategy(ABC):
    """Base class for execution strategies."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    async def __call__(
        self, model: Any, state: RunState, sandbox: Sandbox, max_turns: int
    ) -> LoopResult: ...


STRATEGIES: dict[str, Strategy] = {}


def _load_strategies() -> None:
    from arcrun.strategies.code import CodeExecStrategy
    from arcrun.strategies.react import ReactStrategy

    for s in (ReactStrategy(), CodeExecStrategy()):
        STRATEGIES[s.name] = s


async def select_strategy(
    allowed: list[str] | None,
    model: Any,
    state: RunState,
) -> str:
    """Pick strategy. Single=direct. Multiple=model picks. None=react."""
    if not STRATEGIES:
        _load_strategies()

    if allowed is None:
        return "react"
    unknown = [s for s in allowed if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}. available: {list(STRATEGIES)}")
    if len(allowed) == 1:
        return allowed[0]

    # Model-based selection via tool calling
    from arcrun._messages import system_message, user_message

    bus = state.event_bus
    bus.emit(
        "strategy.selection.start",
        {
            "allowed_strategies": allowed,
            "task": state.messages[-1].content if state.messages else "",
        },
    )

    from arcllm.types import Tool as LLMTool

    select_tool = LLMTool(
        name="select_strategy",
        description="Select the best execution strategy for this task",
        parameters={
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": allowed},
                "reasoning": {"type": "string"},
            },
            "required": ["strategy"],
        },
    )

    strategy_descriptions = "\n".join(
        f"- {name}: {STRATEGIES[name].description}" for name in allowed
    )
    tool_names = state.registry.names()

    selection_messages = [
        system_message(
            f"Select the best execution strategy for the task below.\n\n"
            f"Available strategies:\n{strategy_descriptions}\n\n"
            f"Available tools: {', '.join(tool_names)}\n\n"
            f"Call select_strategy with your choice."
        ),
        user_message(state.messages[-1].content if state.messages else ""),
    ]

    try:
        response = await model.invoke(selection_messages, tools=[select_tool])
        if response.tool_calls:
            chosen = response.tool_calls[0].arguments.get("strategy")
            reasoning = response.tool_calls[0].arguments.get("reasoning", "")
            if chosen in allowed:
                bus.emit(
                    "strategy.selection.complete",
                    {
                        "selected": chosen,
                        "reasoning": reasoning,
                    },
                )
                return chosen
    except Exception as exc:
        bus.emit("strategy.selection.error", {"error": str(exc)})

    bus.emit(
        "strategy.selection.fallback",
        {
            "attempted": allowed,
            "defaulted_to": "react",
        },
    )
    return "react"
