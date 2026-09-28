"""Plan-Execute strategy — concurrent execution of independent items (SPEC-043).

The parallel Executor half of an LLMCompiler-style split (arXiv 2312.04511): the
Planner + Task-Fetching Unit (dependency resolution, the DAG frontier) live in
a host planner; this strategy is the dumb parallel executor. It
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

from arcrun.parallel_dispatch import dispatch_ready
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.strategies.react import react_loop
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
    def auto_selectable(self) -> bool:
        # Opt-in only: a caller (e.g. ArcFlow) asks for plan_execute by name to
        # fan out independent items. It must never be auto-offered for an
        # arbitrary task — being registered is not being a candidate.
        return False

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
        return await dispatch_ready(items, runner, max_parallel=max_parallel)

    async def __call__(
        self, model: Any, state: RunState, sandbox: Sandbox, max_turns: int
    ) -> LoopResult:
        """A pinned run's task is the one ready item: run it as a gated loop.

        The generic run entry hands this strategy a single task, not a batch. A
        single task is a frontier of one independent item, so it runs the way
        every item does — one bounded, fully gated loop — carrying this
        strategy's guidance, and returns that loop's answer. A plan owner with a
        real batch calls :meth:`run_ready` instead.
        """
        return await react_loop(model, state, sandbox, max_turns)


__all__ = ["ItemRunner", "PlanExecuteStrategy"]
