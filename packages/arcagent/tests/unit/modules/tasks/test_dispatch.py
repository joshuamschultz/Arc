"""SPEC-056 Phase D — agent-side task dispatch loop (TDD).

Board assignment (arcui ``PATCH /api/tasks/{id}``) and tool assignment
(``assign_task``) both land as a durable arcstore owner write — the task
becomes ``owner_did=self, status=todo``. Nothing wakes the agent to work it:
the tasks module was, by design, a passive tool surface. This suite drives the
missing piece — a per-agent dispatcher that, when ``dispatch`` is enabled,
polls the shared store for a ready owned ``todo`` task, starts it, and invokes
the agent's own run callback (the ``agent_run_fn`` bound at ``agent:ready`` —
the same seam messaging uses) so the task actually runs.

The dispatcher is uniform: it reacts to the owner write regardless of which
surface produced it, so a UI assignment and a teammate ``assign_task`` are
handled identically without arcui ever needing to sign an inter-agent
envelope (it runs in a separate process and cannot).

Assumed seams (do not exist before Phase D):
1. ``TasksConfig.dispatch: bool`` — opt-in toggle (secure by default = off).
2. ``_runtime._State.agent_run_fn`` — the bound run callback.
3. ``capabilities._dispatch_tick()`` — one poll-and-run tick, factored out of
   the ``@background_task`` loop so it is directly testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity


class _RunRecorder:
    """Stand-in for ``ArcAgent.run_collected`` — records each invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.run_ids: list[str | None] = []

    async def __call__(self, text: str, *, session_key: str, run_id: str | None = None) -> str:
        self.calls.append((text, session_key))
        self.run_ids.append(run_id)
        return "ok"


@pytest.fixture
def dispatch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Runtime configured with a real store, dispatch ON, and a run recorder."""
    from arcagent.modules.tasks import _runtime

    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config={"enabled": True, "data_dir": str(tmp_path), "dispatch": True},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=identity,
    )
    st = _runtime.state()
    rec = _RunRecorder()
    st.agent_run_fn = rec
    yield st, identity, rec
    _runtime.reset()


@pytest.mark.asyncio
class TestDispatchTick:
    async def test_starts_owned_todo_task_and_invokes_run(
        self, dispatch_state: Any
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick, create_task

        st, identity, rec = dispatch_state
        created = json.loads(await create_task(title="Do the marketing", description="ship it"))
        assert created["owner_did"] == identity.did
        assert created["status"] == "todo"

        await _dispatch_tick()

        # The task ran exactly once, in its own session, with its id in the prompt.
        assert len(rec.calls) == 1
        prompt, session_key = rec.calls[0]
        assert created["id"] in prompt
        assert "Do the marketing" in prompt
        assert created["id"] in session_key

        # And it was moved out of the todo pool into in_progress.
        after = await st.store.get(created["id"])
        assert after is not None
        assert after.status == "in_progress"

        # The run is linked deterministically: the task carries the run_id and
        # the SAME id was handed to the run (so the loop's spooled events, which
        # the arcui timeline joins on, share it).
        assert after.run_id is not None
        assert rec.run_ids == [after.run_id]

    async def test_noop_when_dispatch_disabled(self, dispatch_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick, create_task

        st, _identity, rec = dispatch_state
        st.config = st.config.model_copy(update={"dispatch": False})
        await create_task(title="Do the marketing")

        await _dispatch_tick()

        assert rec.calls == []

    async def test_respects_single_in_progress_cap(self, dispatch_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick, create_task

        st, identity, rec = dispatch_state
        first = json.loads(await create_task(title="First"))
        await create_task(title="Second")
        # Agent already has one task in flight.
        await st.store.start_task(first["id"], identity.did)

        await _dispatch_tick()

        # No second run stacked while one is already in progress.
        assert rec.calls == []

    async def test_noop_when_no_todo_tasks(self, dispatch_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        _st, _identity, rec = dispatch_state
        await _dispatch_tick()
        assert rec.calls == []

    async def test_picks_highest_priority_first(self, dispatch_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick, create_task

        _st, _identity, rec = dispatch_state
        await create_task(title="Low one", priority="low")
        critical = json.loads(await create_task(title="Critical one", priority="critical"))

        await _dispatch_tick()

        assert len(rec.calls) == 1
        prompt, _session = rec.calls[0]
        assert critical["id"] in prompt

    async def test_skips_task_with_unmet_dependencies(self, dispatch_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick, create_task

        _st, _identity, rec = dispatch_state
        # A task blocked by a dependency that will never be done is not ready.
        await create_task(title="Blocked", blocked_by=["task_missing_dep"])

        await _dispatch_tick()

        assert rec.calls == []


@pytest.mark.asyncio
class TestDispatchLoopIsPeriodic:
    """The ``@background_task`` loop must run the tick REPEATEDLY, not once.

    ``register_task`` spawns the decorated fn exactly once, so the ``while True``
    has to live inside it. A one-shot body (the reported bug) runs a single tick
    at agent startup and never sees a task assigned afterward — exactly what
    happened on the fleet: a board assignment made minutes after startup sat in
    ``todo`` forever with no run. The other dispatch tests call ``_dispatch_tick``
    directly, so they never exercised the loop and never caught this.
    """

    async def test_loop_ticks_repeatedly_and_picks_up_late_task(
        self, dispatch_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arcagent.modules.tasks.capabilities as caps

        st, identity, rec = dispatch_state
        monkeypatch.setattr(caps, "_DISPATCH_TICK", 0.01)

        ran = asyncio.Event()

        async def _recorder(text: str, *, session_key: str, run_id: str | None = None) -> str:
            rec.calls.append((text, session_key))
            rec.run_ids.append(run_id)
            ran.set()
            return "ok"

        st.agent_run_fn = _recorder

        loop_task = asyncio.ensure_future(caps.tasks_dispatch_loop(None))
        try:
            # Loop is running with nothing to do — it must keep spinning.
            await asyncio.sleep(0.05)
            assert rec.calls == []

            # Assign a task AFTER the loop is already running (the fleet case).
            created = json.loads(await caps.create_task(title="Late task"))
            assert created["owner_did"] == identity.did

            # A still-ticking loop picks it up; a one-shot loop never would.
            await asyncio.wait_for(ran.wait(), timeout=2.0)
            assert any(created["id"] in prompt for prompt, _ in rec.calls)

            after = await st.store.get(created["id"])
            assert after is not None and after.status == "in_progress"
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
