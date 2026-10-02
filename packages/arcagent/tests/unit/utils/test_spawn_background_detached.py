"""Item 20 — background work never inherits the foreground request's causal context.

``spawn_background`` used ``asyncio.create_task``, which copies the caller's
context: a distillation started during a UI-driven run was attributed to that
session and correlated with that run. Interleaving is forced with events so an
inherited binding would be observed, not raced past.
"""

from __future__ import annotations

import asyncio

from arctrust import causal

from arcagent.core.config import EvalConfig
from arcagent.utils.model_helpers import spawn_background


async def test_background_work_is_its_own_system_root() -> None:
    seen: list[causal.CausalContext | None] = []
    started = asyncio.Event()
    release = asyncio.Event()
    tasks: set[asyncio.Task[None]] = set()

    async def job() -> None:
        seen.append(causal.current())
        started.set()
        await release.wait()

    with causal.bind(causal.root("ui_session", "did:arc:user:josh")), causal.refine(run_id="R"):
        spawn_background(
            job(),
            background_tasks=tasks,
            semaphore=asyncio.Semaphore(1),
            eval_config=EvalConfig(),
            audit_event_name="consolidation_error",
        )
        await started.wait()
        release.set()
        await asyncio.gather(*tasks)

    (ctx,) = seen
    assert ctx is not None
    assert ctx.initiator == "system"
    assert ctx.run_id is None
    assert ctx.initiator_id == f"did:arc:system:{job.__qualname__}"
