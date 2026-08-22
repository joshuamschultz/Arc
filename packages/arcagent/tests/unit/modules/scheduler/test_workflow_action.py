"""T-856 / SPEC-061 COMP-017 — the typed schedule action, through the real tick.

The acceptance criterion is explicit: "Test drives the scheduler tick, not the
action handler directly." So every dispatch assertion here starts a REAL
``SchedulerEngine``, lets its REAL timer loop evaluate a REAL store, and lets
its REAL worker drain the queue. The only injected double is the workflows
module's control plane — which is arcteam's half of the system, not arcagent's.
Everything between the timer and ``plane.run`` is production code.

What that buys: if the ``action`` branch in ``SchedulerEngine._dispatch`` were
ever deleted, these tests fail. A test that called ``_dispatch`` directly would
not — and shipping a correct predicate behind dead activating wiring is this
repository's recurring failure mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arctrust import AgentIdentity

from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ActiveHours, ScheduleEntry
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore


class _RecordingPlane:
    """arcteam's control plane, recording the runs the scheduler starts."""

    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.gate = asyncio.Event()
        self.hold = False
        self.fail = False

    async def run(self, workflow_id: str, **kwargs: Any) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("workflow engine is down")
        self.started.append((workflow_id, dict(kwargs.get("input") or {})))
        if self.hold:
            await self.gate.wait()
        record = SimpleNamespace(run_id=f"run_{len(self.started)}", status="running")
        return SimpleNamespace(ok=True, run=record, errors=(), bundle=None)


@pytest.fixture
def plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingPlane]:
    """Configure the workflows runtime with a recording control plane.

    ``ARC_CONFIG_DIR`` goes through monkeypatch so it is put back: a raw
    ``os.environ`` assignment outlives this test and every package after it,
    pointing the whole process's deployment root at a tmp dir with no operator
    key in it.
    """
    from arcagent.modules.workflows import _runtime

    recorder = _RecordingPlane()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    _runtime.reset()
    _runtime.configure(
        config={"enabled": True},
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        control_plane=recorder,
    )
    yield recorder
    _runtime.reset()


def _engine(tmp_path: Path, **config: Any) -> tuple[SchedulerEngine, list[str], ScheduleStore]:
    """A real engine over a real store, with a recording prompt-action callback."""
    prompts: list[str] = []

    async def agent_run_fn(prompt: str, **_kw: Any) -> str:
        prompts.append(prompt)
        return "ran"

    store = ScheduleStore(tmp_path / "schedules.json")
    engine = SchedulerEngine(
        store=store,
        config=SchedulerConfig(check_interval_seconds=1, **config),
        telemetry=None,  # type: ignore[arg-type]  # engine only forwards it
        agent_run_fn=agent_run_fn,
    )
    return engine, prompts, store


def _workflow_entry(**overrides: Any) -> ScheduleEntry:
    data: dict[str, Any] = {
        "id": "sched_wf",
        "type": "interval",
        "action": "workflow_run",
        "workflow_id": "customer-onboarding",
        "workflow_input": {"account": "acme"},
        "every_seconds": 60,
    }
    data.update(overrides)
    return ScheduleEntry.model_validate(data)


async def _tick_until(predicate: Any, engine: SchedulerEngine, timeout: float = 2.0) -> None:
    """Run the engine's real loops until ``predicate`` holds or time runs out."""
    await engine.start()
    engine.set_agent_run_fn(engine._agent_run_fn)  # unblock the readiness gate
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
    finally:
        await engine.stop(timeout=0.5)


@pytest.mark.asyncio
class TestTypedTriggerThroughTheRealTick:
    async def test_due_workflow_schedule_starts_a_real_run(
        self, tmp_path: Path, plane: _RecordingPlane
    ) -> None:
        engine, prompts, store = _engine(tmp_path)
        store.add(_workflow_entry())

        await _tick_until(lambda: bool(plane.started), engine)

        assert plane.started == [("customer-onboarding", {"account": "acme"})]
        # No model in the decision path: the prompt callback was never touched.
        assert prompts == []

    async def test_prompt_action_still_goes_to_the_agent_loop(
        self, tmp_path: Path, plane: _RecordingPlane
    ) -> None:
        engine, prompts, store = _engine(tmp_path)
        store.add(
            ScheduleEntry.model_validate(
                {"id": "sched_p", "type": "interval", "prompt": "check email", "every_seconds": 60}
            )
        )

        await _tick_until(lambda: bool(prompts), engine)

        assert prompts == ["check email"]
        assert plane.started == []

    async def test_quiet_hours_suppress_a_workflow_firing(
        self, tmp_path: Path, plane: _RecordingPlane
    ) -> None:
        engine, _prompts, store = _engine(tmp_path)
        # A one-minute window in the far past of the day, so "now" is outside it.
        store.add(
            _workflow_entry(active_hours=ActiveHours(start="00:00", end="00:01", timezone="UTC"))
        )

        await _tick_until(lambda: False, engine, timeout=0.2)

        assert plane.started == []

    async def test_disabled_schedule_never_fires(
        self, tmp_path: Path, plane: _RecordingPlane
    ) -> None:
        engine, _prompts, store = _engine(tmp_path)
        store.add(_workflow_entry(enabled=False))

        await _tick_until(lambda: False, engine, timeout=0.2)

        assert plane.started == []


@pytest.mark.asyncio
class TestOverlapSkips:
    async def test_a_firing_whose_prior_run_is_in_flight_skips(
        self, tmp_path: Path, plane: _RecordingPlane
    ) -> None:
        """REQ-249 — the ``_in_flight`` guard is the overlap policy."""
        engine, _prompts, store = _engine(tmp_path)
        store.add(_workflow_entry())
        plane.hold = True

        first = asyncio.create_task(engine._tick())
        await asyncio.sleep(0.05)
        await engine._tick()  # overlapping firing while the first still runs

        plane.gate.set()
        await first
        assert len(plane.started) == 1


@pytest.mark.asyncio
class TestCircuitBreaker:
    async def test_repeated_workflow_failures_trip_the_existing_breaker(
        self, tmp_path: Path, plane: _RecordingPlane
    ) -> None:
        engine, _prompts, store = _engine(tmp_path, circuit_breaker_threshold=2)
        store.add(_workflow_entry())
        plane.fail = True

        entry = store.load()[0]
        await engine.execute(entry)
        await engine.execute(store.load()[0])

        assert store.load()[0].enabled is False

    async def test_a_refusal_is_raised_not_swallowed(
        self, tmp_path: Path, arcstore_opener: Any
    ) -> None:
        """A swallowed refusal would let a broken trigger fire forever.

        Drives the REAL control plane (no double): with no runner hosted in this
        process it refuses, and the run entry must turn that returned refusal
        into a raise so the scheduler's breaker can count it.
        """
        from arcagent.modules.workflows import _runtime
        from arcagent.modules.workflows.run_entry import start_workflow_run

        _runtime.reset()
        _runtime.configure(
            config={"enabled": True},
            workspace=tmp_path,
            identity=AgentIdentity.generate(org="local", agent_type="agent"),
            arcstore_opener=arcstore_opener,
        )
        with pytest.raises(RuntimeError, match="refused to start"):
            await start_workflow_run("nope")
        _runtime.reset()


class TestScheduleEntryValidation:
    """The typed action is validated where every other type-specific field is."""

    def test_workflow_action_requires_a_workflow_id(self) -> None:
        with pytest.raises(ValueError, match="requires 'workflow_id'"):
            ScheduleEntry.model_validate(
                {"id": "s", "type": "interval", "action": "workflow_run", "every_seconds": 60}
            )

    def test_workflow_action_must_not_carry_a_prompt(self) -> None:
        with pytest.raises(ValueError, match="must not carry a prompt"):
            ScheduleEntry.model_validate(
                {
                    "id": "s",
                    "type": "interval",
                    "action": "workflow_run",
                    "workflow_id": "w",
                    "prompt": "do the thing",
                    "every_seconds": 60,
                }
            )

    def test_prompt_action_still_requires_a_prompt(self) -> None:
        with pytest.raises(ValueError, match="requires 'prompt'"):
            ScheduleEntry.model_validate({"id": "s", "type": "interval", "every_seconds": 60})

    def test_default_action_is_prompt(self) -> None:
        entry = ScheduleEntry.model_validate(
            {"id": "s", "type": "interval", "prompt": "x", "every_seconds": 60}
        )
        assert entry.action == "prompt"

    def test_workflow_entry_has_a_readable_label(self) -> None:
        assert _workflow_entry().label == "workflow:customer-onboarding"

    def test_type_specific_validation_still_applies_to_a_workflow_entry(self) -> None:
        with pytest.raises(ValueError, match="requires 'expression'"):
            ScheduleEntry.model_validate(
                {"id": "s", "type": "cron", "action": "workflow_run", "workflow_id": "w"}
            )
