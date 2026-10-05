"""SPEC-071 — ``agent:moment`` emission at the real loop sites.

Loop sites announce candidate moments over the module bus so a Brain (arcmemory)
can decide whether to stage a proactive recall. The user's own turn is NOT a moment
site: its retrieval is Context prep's single pass.

1. **User-turn (real dispatch)** — boots a real :class:`ArcAgent` (SPEC-066
   signed-bundle install, exactly as ``test_live_modules_e2e.py`` does),
   subscribes a recorder to the agent's live :class:`ModuleBus` for
   ``agent:moment``, and drives one real turn via ``agent.run(...)``. Only the
   LLM (``arcrun.run_stream``) is stubbed. The recorder must see nothing.
2. **task_start (focused)** — configures the tasks module's runtime exactly as
   ``tests/unit/modules/tasks/test_dispatch.py`` does, drives the real
   ``_dispatch_tick`` -> ``_run_task`` path with a fake ``agent_run_fn``, and
   asserts ordering: the ``task_start`` moment must be observed before the
   run callback is invoked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcrun import TurnEndEvent
from arctrust import AgentIdentity
from packages.arcagent.tests.integration.test_live_modules_e2e import (
    _booted,
    _config,
    _deployment,
    _install,
)

from arcagent.core.module_bus import EventContext

# --------------------------------------------------------------------------
# 1. User-turn moments (entity_seen + topic_shift) — real dispatch
# --------------------------------------------------------------------------


class _MomentRecorder:
    """Records every ``agent:moment`` EventContext handed to the bus."""

    def __init__(self) -> None:
        self.events: list[EventContext] = []

    async def __call__(self, ctx: EventContext) -> None:
        self.events.append(ctx)


async def test_user_turn_emits_no_agent_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real turn over real dispatch retrieves once, in Context prep, and emits no moment.

    The dispatcher used to emit ``entity_seen`` + ``topic_shift`` before assembly,
    each a retrieval pass on the first-model-call path. The request's cues now seed
    the one ``ContextRetrieval`` pass; loop sites (``task_start``, ``decision_point``)
    still emit moments.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)
    config = _config(deployment, ("memory",))
    recorder = _MomentRecorder()

    async with _booted(deployment, config) as agent:
        assert agent._bus is not None
        agent._bus.subscribe("agent:moment", recorder, module_name="test-recorder")

        session = await agent.session("moment-user-turn")
        events = [event async for event in agent.run("Ada owns payments", session=session)]

    # Harness sanity: the turn itself must have completed, or "no moment" proves nothing.
    assert events, "the turn produced no stream events at all"
    assert isinstance(events[-1], TurnEndEvent), (
        f"the real dispatch path did not complete a turn: {events[-1]!r}"
    )
    assert recorder.events == [], (
        f"a user turn still emits agent:moment: {[c.data.get('kind') for c in recorder.events]}"
    )


# --------------------------------------------------------------------------
# 2. task_start moment — focused _run_task drive, real dispatch tick
# --------------------------------------------------------------------------


class _OrderRecorder:
    """Shared ordering witness: both the bus handler and the fake run
    callback append their own tag here, so ordering is asserted on one list
    instead of comparing wall-clock timestamps across two unrelated spies."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.moment_events: list[EventContext] = []

    async def on_moment(self, ctx: EventContext) -> None:
        self.order.append(f"moment:{ctx.data.get('kind')}")
        self.moment_events.append(ctx)

    async def on_agent_run(self, text: str, *, session_key: str, run_id: str | None = None) -> str:
        del text, session_key, run_id
        self.order.append("agent_run_fn")
        return "ok"


async def test_task_start_emits_agent_moment_before_agent_run_fn(
    tmp_path: Path, arcstore_opener: Any
) -> None:
    """``_run_task`` must announce ``kind="task_start"`` before the model runs.

    Mirrors ``tests/unit/modules/tasks/test_dispatch.py``'s ``dispatch_state``
    fixture exactly (real store, dispatch on, a fake ``agent_run_fn``), plus a
    real :class:`ModuleBus` a recorder subscribes to for ``agent:moment``.
    Today ``_run_task`` never touches a bus at all, so the recorder must see
    zero moment events while the run callback still fires normally — proving
    the gap is the missing emit, not a broken dispatch tick.
    """
    from arcagent.core.module_bus import ModuleBus
    from arcagent.modules.tasks import _runtime
    from arcagent.modules.tasks.capabilities import _dispatch_tick, create_task

    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config={"enabled": True, "dispatch": True},
        telemetry=None,
        workspace=tmp_path,
        identity=identity,
        arcstore_opener=arcstore_opener,
    )
    st = _runtime.state()
    witness = _OrderRecorder()
    st.agent_run_fn = witness.on_agent_run
    # No ``bus`` field exists on tasks' ``_State`` yet (T-987 adds the wiring).
    # Setting it dynamically is harmless — ``_run_task`` does not read it
    # today, which is exactly the gap this test proves.
    bus = ModuleBus()
    bus.subscribe("agent:moment", witness.on_moment, module_name="test-recorder")
    st.bus = bus  # type: ignore[attr-defined]  # dynamic: no bus field until T-987

    try:
        created = await create_task(title="Ship the marketing page")
        import json

        task_id = json.loads(created)["id"]

        await _dispatch_tick()

        # Harness sanity: the dispatch tick must have actually run the task
        # through the real agent_run_fn. If this fails, the RED reason would
        # be a broken harness, not a missing feature.
        assert "agent_run_fn" in witness.order, (
            "the dispatch tick never invoked agent_run_fn at all — broken harness"
        )
        after = await st.store.get(task_id)
        assert after is not None and after.status != "todo", (
            "the task was never claimed off todo — broken harness"
        )
    finally:
        _runtime.reset()

    assert witness.moment_events, (
        "no agent:moment kind=task_start event was emitted before agent_run_fn — "
        "_run_task does not emit yet (T-987)"
    )
    kinds = [ctx.data.get("kind") for ctx in witness.moment_events]
    assert "task_start" in kinds, f"expected a task_start moment, got kinds={kinds}"

    first_moment_pos = next(i for i, tag in enumerate(witness.order) if tag.startswith("moment:"))
    run_pos = witness.order.index("agent_run_fn")
    assert first_moment_pos < run_pos, (
        f"agent:moment must be emitted BEFORE agent_run_fn is awaited; order was {witness.order}"
    )
