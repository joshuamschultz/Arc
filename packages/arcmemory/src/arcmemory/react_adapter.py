"""The SINGLE arcrun seam for arcmemory (the only module that imports arcrun).

Confining every arcrun-specific symbol here (the ``import``, the ``run`` call, and
the breach/timeout -> degrade mapping) means a future harness (openclaw / Hermes)
is a *sibling* adapter, not a package-wide refactor. The agentic consolidation
engine calls :func:`run_react_loop` and never touches arcrun directly.

Degrade, not crash: an arcrun-less install (``ImportError``), a wall-clock timeout,
or a bounded-loop breach (max_turns / max_tokens / runaway_loop / error_cascade —
arcrun returns these as a ``completion_payload`` with ``status="failed"``, never a
raise) all map to a :class:`ReactOutcome` with ``degraded=True`` so the caller can
fall back to the deterministic pipeline distiller with no data loss.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from arcmemory.tools import MemoryTool

try:  # arcrun is an additive, guarded dependency — absence degrades to pipeline.
    from arcrun import StaticProvider, run
    from arcrun.types import Tool as _ArcRunTool

    _ARCRUN_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via monkeypatch
    _ARCRUN_AVAILABLE = False

# Bounded-loop breach reasons arcrun surfaces on ``completion_payload.error``.
_BREACH_REASONS = frozenset(
    {"max_turns", "max_tokens", "max_cost", "runaway_loop", "error_cascade"}
)


@dataclass
class ReactOutcome:
    """The engine-neutral result of one bounded ReAct run.

    ``degraded`` is the single signal the consolidation engine reads: on True it
    falls back to the pipeline distiller (``reason`` says why — a breach label,
    ``timeout``, or ``arcrun-absent``).
    """

    content: str | None = None
    degraded: bool = False
    reason: str | None = None
    turns: int = 0
    tool_calls_made: int = 0
    tokens_used: dict[str, Any] = field(default_factory=dict)


# The injectable engine seam — the consolidation engine depends on this signature,
# not on arcrun, so a test can substitute a fake loop runner.
ReactLoop = Callable[..., Awaitable[ReactOutcome]]


def _to_arcrun_tool(mtool: MemoryTool) -> Any:
    """Map one neutral :class:`MemoryTool` onto an arcrun ``Tool``.

    Memory tools are context-free (they need no ToolContext), so the wrapper
    accepts and ignores it. ``classification`` rides through so pure reads
    parallelize and writes stay sequential.
    """

    async def _execute(args: dict[str, Any], _ctx: Any) -> str:
        return await mtool.execute(args)

    return _ArcRunTool(
        name=mtool.name,
        description=mtool.description,
        input_schema=mtool.input_schema,
        execute=_execute,
        classification=mtool.classification,
    )


def _outcome_from_result(result: Any) -> ReactOutcome:
    """Map an arcrun ``LoopResult`` onto a :class:`ReactOutcome` (breach -> degrade)."""
    payload = result.completion_payload or {}
    reason = payload.get("error")
    degraded = payload.get("status") == "failed" or reason in _BREACH_REASONS
    return ReactOutcome(
        content=result.content,
        degraded=degraded,
        reason=(reason or "loop_failed") if degraded else None,
        turns=result.turns,
        tool_calls_made=result.tool_calls_made,
        tokens_used=dict(result.tokens_used or {}),
    )


async def run_react_loop(
    *,
    model: Any,
    tools: list[MemoryTool],
    system_prompt: str,
    task: str,
    max_turns: int,
    max_tokens: int,
    timeout_seconds: float,
    actor_did: str,
) -> ReactOutcome:
    """Run one bounded ReAct loop over the memory tools; never raise, degrade instead.

    Reentrant-safe: awaits arcrun on the current loop (no ``asyncio.run``, no new
    loop). The wall-clock cap is enforced with ``asyncio.timeout`` because arcrun
    has no run-level timeout param; a breach returns a failed ``completion_payload``
    rather than raising, so both paths funnel into the degrade signal.
    """
    if not _ARCRUN_AVAILABLE:
        return ReactOutcome(degraded=True, reason="arcrun-absent")
    provider = StaticProvider([_to_arcrun_tool(t) for t in tools])
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await run(
                model,
                provider,
                system_prompt,
                task,
                allowed_strategies=["react"],
                max_turns=max_turns,
                max_tokens=max_tokens,
                actor_did=actor_did,
            )
    except TimeoutError:
        return ReactOutcome(degraded=True, reason="timeout")
    return _outcome_from_result(result)


__all__ = ["ReactLoop", "ReactOutcome", "run_react_loop"]
