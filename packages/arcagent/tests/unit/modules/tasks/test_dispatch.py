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

    async def __call__(self, text: str, *, session_key: str) -> str:
        self.calls.append((text, session_key))
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
