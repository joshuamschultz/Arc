"""Strategy interface and selection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from types import MappingProxyType
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
    def description(self) -> str:
        """One-line summary the selector shows the model.

        Defaults to the stock ``strategy_<name>_description`` markdown, so a
        strategy's copy lives in the prompt folder like every other prompt in
        the system rather than inline in Python. Override only to compute it.
        """
        return _stock(f"strategy_{self.name}_description")

    @property
    def prompt_guidance(self) -> str:
        """Model-facing guidance injected into the system prompt.

        Defaults to the stock ``strategy_<name>`` markdown (same convention as
        :attr:`description`). The text teaches the LLM when this strategy applies
        and what the loop will do; override only to compute it. Empty when a
        strategy ships no such file, so a bare strategy still loads.
        """
        return _stock(f"strategy_{self.name}")

    @property
    def auto_selectable(self) -> bool:
        """Whether the selector may offer this strategy when none was named.

        Defaults to True, so a third-party strategy behaves as it always did.
        A strategy that only makes sense when a caller asks for it by name
        overrides this — being installed is not the same as being a candidate
        for an arbitrary task.
        """
        return True

    @abstractmethod
    async def __call__(
        self, model: Any, state: RunState, sandbox: Sandbox, max_turns: int
    ) -> LoopResult: ...


STRATEGIES: dict[str, Strategy] = {}


def _stock(prompt_name: str) -> str:
    """A stock prompt body, or "" when a strategy ships none.

    Strategy copy lives as markdown under ``arcrun/context/`` and is resolved
    through arcprompt, so an operator overlay is honored at prompt-assembly time
    the same as any other stock prompt.
    """
    from arcprompt import PromptMissing, load_stock

    try:
        return load_stock("arcrun", prompt_name)
    except PromptMissing:
        return ""


def available_strategies() -> MappingProxyType[str, Strategy]:
    """Return a read-only view of the registered execution strategies."""
    if not STRATEGIES:
        _load_strategies()
    return MappingProxyType(STRATEGIES)


def _strategy_classes(module: Any) -> list[type[Strategy]]:
    """Concrete Strategy subclasses DEFINED in ``module`` — not ones it imported.

    The ``__module__`` check is what keeps the base ``Strategy`` (imported into
    every strategy file) and any shared helper class out of the discovered set.
    """
    import inspect

    return [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, Strategy)
        and obj.__module__ == module.__name__
        and not inspect.isabstract(obj)
    ]


def _register(classes: Iterable[type[Strategy]]) -> dict[str, Strategy]:
    """Instantiate each class, keyed by ``name``; raise on a duplicate name.

    A duplicate is a hard error rather than a silent last-writer-wins, so two
    files can never quietly shadow each other. Every strategy must construct
    with no arguments.
    """
    registered: dict[str, Strategy] = {}
    for cls in classes:
        strategy = cls()
        if strategy.name in registered:
            raise ValueError(
                f"duplicate strategy name {strategy.name!r}: "
                f"{type(registered[strategy.name]).__name__} and {cls.__name__}"
            )
        registered[strategy.name] = strategy
    return registered


def _load_strategies() -> None:
    """Discover and register every strategy in this package — in-tree drop-ins.

    A strategy is a plugin: any module under ``arcrun/strategies/`` that defines
    a concrete :class:`Strategy` subclass is registered by its ``name``, with no
    central list to edit. Add, remove, or replace a file and the available set
    changes to match — interchangeable by construction.

    The scan root is in-tree, so it ships and is signed with the release wheel;
    discovery therefore adds no untrusted-load surface. An external, unsigned
    strategy would arrive through a separate signature-verified path (the module
    model), never this scan.
    """
    import importlib
    import pkgutil

    classes: list[type[Strategy]] = []
    for info in pkgutil.iter_modules(__path__):
        if info.ispkg or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        classes.extend(_strategy_classes(module))
    # Built atomically off to the side: a duplicate-name raise leaves STRATEGIES
    # empty so the next call re-scans and re-raises, rather than half-populated.
    STRATEGIES.update(_register(classes))


async def select_strategy(
    allowed: list[str] | None,
    model: Any,
    state: RunState,
) -> str:
    """Pick a strategy: one allowed means take it, otherwise the model chooses.

    ``allowed=None`` means every **auto-selectable** strategy is on the table. Each
    run is its own opportunity to pick the shape that fits the task, so the
    default is open and an operator narrows it deliberately rather than having
    to opt in to capability they already installed.

    The cost of an open default is one selection call per run. That is the
    intended trade: a run that would benefit from fanning out should not be
    forced through a single linear chain because nobody edited a config file.
    """
    if not STRATEGIES:
        _load_strategies()

    if allowed is None:
        allowed = [name for name, s in STRATEGIES.items() if s.auto_selectable]
    if not allowed:
        raise ValueError("allowed_strategies is empty; a run must permit at least one strategy")
    unknown = [s for s in allowed if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}. available: {list(STRATEGIES)}")
    if len(allowed) == 1:
        return allowed[0]

    # Model-based selection via tool calling
    from arcrun._messages import content_text, system_message, user_message

    bus = state.event_bus
    bus.emit(
        "strategy.selection.start",
        {
            "allowed_strategies": allowed,
            "task": content_text(state.messages[-1].content) if state.messages else "",
        },
    )

    import arcllm

    select_tool = arcllm.Tool(
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
        user_message(content_text(state.messages[-1].content) if state.messages else ""),
    ]

    # Imported here rather than at module scope: ``react`` imports ``Strategy``
    # from this module, so a top-level import would be circular.
    from arcrun.strategies.react import accumulate_usage

    try:
        invoke_kwargs: dict[str, Any] = {}
        if state.deadline is not None:
            invoke_kwargs["_arc_deadline"] = state.deadline
        response = await state.await_work(
            model.invoke(selection_messages, tools=[select_tool], **invoke_kwargs)
        )
        # Choosing a strategy costs real tokens and real money. Leaving that
        # uncounted would understate every run's usage and hide the spend from
        # the budget breaker, which reads these same counters (LLM10).
        accumulate_usage(state, response)
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
                return str(chosen)
    except Exception as exc:  # reason: fail-open — continue
        bus.emit("strategy.selection.error", {"error": str(exc)})

    bus.emit(
        "strategy.selection.fallback",
        {
            "attempted": allowed,
            "defaulted_to": "react",
        },
    )
    return "react"
