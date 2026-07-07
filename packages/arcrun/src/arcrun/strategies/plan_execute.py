"""Plan-Execute strategy — concurrent execution of independent items (SPEC-043).

The parallel Executor half of an LLMCompiler-style split (arXiv 2312.04511): the
Planner + Task-Fetching Unit (dependency resolution, the DAG frontier) live in
arcagent's ``PlanOrchestrator``; this strategy is the dumb parallel Executor. It
receives a **flat list of INDEPENDENT, ready items** — opaque tasks, never a DAG
— and runs them concurrently through the one wired concurrency primitive
(``parallel_dispatch.ParallelDispatcher``), returning per-item outcomes in
submission order with partial failures isolated (REQ-050/051/055/056).

Boundary (2.4): this strategy never sees ``Plan``, ``depends_on``, or replan. It
runs what it is handed; the plan owner decides *what* is independent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from arcrun.parallel_dispatch import ParallelDispatcher
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.types import LoopResult

# A runner turns one opaque ready item into its outcome (an awaitable). The item
# and outcome types are the caller's — arcrun neither constructs nor inspects
# them (they are NOT Plan steps; the boundary holds).
ItemRunner = Callable[[Any], Awaitable[Any]]


class PlanExecuteStrategy(Strategy):
    """Run a batch of independent ready items concurrently, gated per item.

    The concurrency mechanism is the wired ``ParallelDispatcher`` (REQ-056) — the
    same primitive the react loop dispatches tool batches through — so there is
    exactly one gather path in the engine. Each item runs as a bounded sub-run
    supplied by the caller's runner; a failing item is captured as its own
    outcome and never aborts a sibling (REQ-055).
    """

    @property
    def name(self) -> str:
        return "plan_execute"

    @property
    def description(self) -> str:
        return (
            "Execute a set of independent, ready plan branches concurrently. "
            "Best when a plan's frontier has multiple steps with no unmet "
            "dependencies — they run in parallel, each fully gated."
        )

    @property
    def prompt_guidance(self) -> str:
        return (
            "## Plan-Execute Strategy\n"
            "Independent ready steps are dispatched concurrently. Dependency "
            "ordering is resolved by the planner before dispatch; you never see "
            "the dependency graph — only the items that are ready now."
        )

    async def run_ready(
        self,
        items: list[Any],
        runner: ItemRunner,
        *,
        max_parallel: int = 10,
    ) -> list[Any]:
        """Dispatch independent ``items`` concurrently; return outcomes in order.

        Submission order is preserved regardless of completion order and a
        per-item failure surfaces as that item's outcome rather than raising
        (REQ-050/055). Concurrency is semaphore-bounded by ``max_parallel``
        (REQ-056). The dispatcher wraps each result as ``(item, outcome)``; we
        return just the outcomes so the caller reads per-item results directly.
        """
        if not items:
            return []
        dispatcher = ParallelDispatcher(max_parallel=max_parallel)
        paired = await dispatcher.dispatch(items, lambda item: _wrap(item, runner))
        return [outcome for _item, outcome in paired]

    async def __call__(
        self, model: Any, state: RunState, sandbox: Sandbox, max_turns: int
    ) -> LoopResult:
        """Not driven through the react ``run()`` entry.

        plan_execute is invoked by the plan owner via :meth:`run_ready` with an
        explicit item batch + runner — it has no single-task loop. Selecting it
        through the generic run entry yields an empty result rather than
        crashing (a plan with no ready frontier is a no-op, not an error).
        """
        return LoopResult(
            content=None,
            turns=state.turn_count,
            tool_calls_made=state.tool_calls_made,
            tokens_used=state.tokens_used.copy(),
            strategy_used="plan_execute",
            cost_usd=state.cost_usd,
            events=state.event_bus.events,
        )


async def _wrap(item: Any, runner: ItemRunner) -> tuple[Any, Any]:
    """Adapt a single-arg item runner to the dispatcher's ``(item, outcome)`` shape."""
    return item, await runner(item)


__all__ = ["ItemRunner", "PlanExecuteStrategy"]
