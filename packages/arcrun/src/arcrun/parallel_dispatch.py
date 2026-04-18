"""Parallel tool dispatch — SPEC-017 Phase 4 (R-020 through R-025).

Read-only batches run concurrently via
``asyncio.gather(return_exceptions=True)`` bounded by a semaphore.
Any state-modifying tool — or an implicit dependency between two
read-only calls — forces sequential execution.

Classification comes from the registry (``get_classification``).
Unknown tools are conservatively treated as ``state_modifying``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Sequence key injected into each tool call at dispatch time so audit
# events can reconstruct submission order regardless of completion
# order. Underscore prefix signals "reserved internal metadata".
_SEQ_KEY = "_seq"


@runtime_checkable
class ClassificationRegistry(Protocol):
    """Registry able to report tool classifications.

    Matches ``arcagent.core.tool_registry.ToolRegistry.get_classification``.
    Any object with the same shape works — arcrun does not depend on
    arcagent.
    """

    def get_classification(self, name: str) -> str: ...


@dataclass(frozen=True)
class BatchVerdict:
    """Result of classifying a batch for parallelism.

    ``can_parallelize`` is the single bit arcrun cares about.
    ``reason`` is an explanation string for audit + debugging.
    """

    can_parallelize: bool
    reason: str


class BatchClassifier:
    """Decide whether a tool-call batch may run in parallel.

    The rules, in order:
      1. Any state-modifying tool → sequential
      2. Any unknown tool → sequential (fail-closed)
      3. Any two calls sharing an argument value that looks like a
         path (contains ``/`` or ``\\``) → sequential
      4. Otherwise → parallel
    """

    def __init__(self, registry: ClassificationRegistry) -> None:
        self._registry = registry

    def classify(self, calls: list[Any]) -> BatchVerdict:
        if not calls:
            return BatchVerdict(can_parallelize=False, reason="empty_batch")

        for call in calls:
            cls = self._registry.get_classification(call.name)
            if cls != "read_only":
                return BatchVerdict(
                    can_parallelize=False,
                    reason=f"state_modifying tool in batch: {call.name}",
                )

        if self._has_shared_path_arg(calls):
            return BatchVerdict(
                can_parallelize=False,
                reason="implicit dependency: shared path argument across calls",
            )

        return BatchVerdict(can_parallelize=True, reason="all_read_only")

    @staticmethod
    def _has_shared_path_arg(calls: list[Any]) -> bool:
        """Return True if any two calls share a path-like argument value.

        Path heuristic: value is a string containing ``/`` or ``\\``.
        False positives are acceptable — we only lose parallelism.
        False negatives would cause a race; keep the check simple but
        cover the obvious write-then-read pattern.
        """
        seen: set[str] = set()
        for call in calls:
            for value in call.arguments.values():
                if not isinstance(value, str):
                    continue
                if "/" not in value and "\\" not in value:
                    continue
                if value in seen:
                    return True
                seen.add(value)
        return False


class ParallelDispatcher:
    """Run a batch of tool calls with bounded concurrency.

    Parameters
    ----------
    max_parallel:
        Upper bound on concurrent in-flight calls. Enforced by
        ``asyncio.Semaphore``.
    assign_seq:
        When ``True``, each call's ``arguments`` dict is annotated
        with an ``_seq`` key (monotonic int) at dispatch time. Audit
        events downstream use this to reconstruct submission order.
    """

    def __init__(
        self,
        *,
        max_parallel: int = 10,
        assign_seq: bool = False,
    ) -> None:
        if max_parallel < 1:
            msg = f"max_parallel must be >= 1, got {max_parallel}"
            raise ValueError(msg)
        self._max_parallel = max_parallel
        self._assign_seq = assign_seq

    async def dispatch(
        self,
        calls: list[Any],
        runner: Callable[[Any], Awaitable[tuple[Any, Any]]],
    ) -> list[tuple[Any, Any]]:
        """Run ``calls`` via ``runner`` concurrently, bounded by semaphore.

        Results are returned in submission order regardless of
        completion order. ``runner`` exceptions are captured and
        returned in the result slot as the exception object — partial
        failure does not abort the batch (R-023).
        """
        if not calls:
            return []

        if self._assign_seq:
            for idx, call in enumerate(calls):
                call.arguments[_SEQ_KEY] = idx

        semaphore = asyncio.Semaphore(self._max_parallel)

        async def _bounded(call: Any) -> tuple[Any, Any]:
            async with semaphore:
                try:
                    return await runner(call)
                except Exception as exc:  # pragma: no cover — passed through
                    return call, exc

        coros = [_bounded(call) for call in calls]
        # gather preserves submission order in the returned list even
        # though completion order is arbitrary.
        return await asyncio.gather(*coros)


class SequentialDispatcher:
    """Run calls one at a time. Submission order preserved."""

    async def dispatch(
        self,
        calls: list[Any],
        runner: Callable[[Any], Awaitable[tuple[Any, Any]]],
    ) -> list[tuple[Any, Any]]:
        results: list[tuple[Any, Any]] = []
        for call in calls:
            try:
                results.append(await runner(call))
            except Exception as exc:
                results.append((call, exc))
        return results


async def dispatch_batch(
    calls: list[Any],
    runner: Callable[[Any], Awaitable[tuple[Any, Any]]],
    *,
    classifier: BatchClassifier,
    max_parallel: int = 10,
) -> list[tuple[Any, Any]]:
    """High-level entry point: classify then dispatch.

    Read-only batches go through :class:`ParallelDispatcher`; anything
    else (state-modifying, unknown, implicit dep) goes through
    :class:`SequentialDispatcher`. Either way, submission order is
    preserved in the result list.
    """
    verdict = classifier.classify(calls)
    if verdict.can_parallelize:
        return await ParallelDispatcher(max_parallel=max_parallel).dispatch(calls, runner)
    return await SequentialDispatcher().dispatch(calls, runner)


__all__ = [
    "BatchClassifier",
    "BatchVerdict",
    "ClassificationRegistry",
    "ParallelDispatcher",
    "SequentialDispatcher",
    "dispatch_batch",
]

# Silence unused import in case ``Any`` used only via Protocol
_ = Any
