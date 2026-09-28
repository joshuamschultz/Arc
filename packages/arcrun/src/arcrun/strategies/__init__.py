"""Strategy interface and selection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from arcprompt import PromptMissing, PromptSource, StockPromptSource

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
        """One-line summary the selector shows the model, from the shipped files.

        A run never reads this for a built-in strategy: it resolves
        ``strategy_<name>_description`` through the run's ``PromptSource`` so an
        operator's edit wins (see :func:`strategy_description`). This is the
        fallback for a strategy that ships no such prompt, and the plain listing
        a caller with no run (``arc agent strategies``) prints.
        """
        return _shipped(f"strategy_{self.name}_description")

    @property
    def prompt_guidance(self) -> str:
        """Model-facing guidance for this strategy's turns, from the shipped files.

        Same convention as :attr:`description`: a run resolves ``strategy_<name>``
        through its ``PromptSource`` (see :func:`strategy_guidance`) and falls
        back to this only when no such prompt ships. Empty for a bare strategy,
        so it still loads.
        """
        return _shipped(f"strategy_{self.name}")

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


def _shipped(prompt_name: str) -> str:
    """The shipped ``arcrun/context/<prompt_name>`` body, or "" when none ships."""
    return _optional(StockPromptSource(), prompt_name, lambda: "")


def _optional(source: PromptSource, prompt_name: str, fallback: Callable[[], str]) -> str:
    """``arcrun/<prompt_name>`` through ``source``; ``fallback()`` only when none ships.

    Only :class:`~arcprompt.PromptMissing` falls back. A rejected override
    (unsigned, tampered, unparseable) propagates, so the run fails closed rather
    than quietly sending stock in its place.
    """
    try:
        return source.resolve("arcrun", prompt_name)
    except PromptMissing:
        return fallback()


def strategy_description(name: str, source: PromptSource) -> str:
    """What the selector shows the model about strategy ``name`` — editable via ``source``."""
    return _optional(
        source, f"strategy_{name}_description", lambda: available_strategies()[name].description
    )


def strategy_guidance(name: str, source: PromptSource) -> str:
    """The guidance strategy ``name``'s turns carry — editable via ``source``."""
    return _optional(
        source, f"strategy_{name}", lambda: available_strategies()[name].prompt_guidance
    )


def use_strategy_guidance(state: RunState, name: str) -> None:
    """Make ``name``'s guidance the one strategy guidance in the run's system messages.

    Only the strategy that actually runs steers the run: a guidance message left
    over from another strategy (a dynamic run falling back to react, or a
    resumed transcript that already carries one) is removed first. The message
    goes after the host's own system segments so their byte-stable prefix, and
    the provider cache built on it, is untouched.
    """
    from arcrun._messages import content_text, system_message

    guidance = strategy_guidance(name, state.prompt_source)
    stale = {state.strategy_guidance, guidance} - {""}
    kept = [
        m
        for m in state.messages
        if not (getattr(m, "role", "") == "system" and content_text(m.content) in stale)
    ]
    if guidance:
        leading = next(
            (i for i, m in enumerate(kept) if getattr(m, "role", "") != "system"), len(kept)
        )
        kept.insert(leading, system_message(guidance))
    state.messages[:] = kept
    state.strategy_guidance = guidance


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

    # Resolved before the fail-open block below: a rejected operator override
    # of the selection prompt must end the run, never degrade to a silent react.
    selection_system = _selection_system_text(allowed, state)

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

    # The task is untrusted content: it rides the user turn, never the
    # instruction channel (LLM01).
    selection_messages = [
        system_message(selection_system),
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


def _selection_system_text(allowed: list[str], state: RunState) -> str:
    """The selection call's instructions: ``strategy_select`` plus what is on the table.

    Every word comes through the run's ``PromptSource`` — the operator-editable
    ``arcrun/strategy_select`` body and each allowed strategy's
    ``strategy_<name>_description``. The tool names are listed so the model can
    rule out a strategy whose tools this run does not have.
    """
    source = state.prompt_source
    listing = "\n".join(f"- {name}: {strategy_description(name, source)}" for name in allowed)
    tools = ", ".join(state.registry.names()) or "none"
    parts = [source.resolve("arcrun", "strategy_select"), f"Strategies:\n{listing}"]
    parts.append(f"Tools: {tools}")
    return "\n\n".join(parts)
