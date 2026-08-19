"""SPEC-056 Phase 1 — lifecycle reliability engine (TDD).

The dispatch loop must be self-healing: a run that fails, errors, or times out
is retried with backoff and finally dead-lettered; a task orphaned in_progress
by a restart is reclaimed; an operator can cancel a running task. These drive
``_dispatch_tick`` (retry/timeout) and ``_reliability_tick`` (cancel + reclaim)
directly against a real ``TaskStore`` — the run itself is a stub whose behavior
(fail/hang) each test controls.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity

_OPERATOR = "did:arc:test:human/operator"


class _FailingRun:
    """Run stub that always raises — drives the failure/retry path."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self._exc = exc or RuntimeError("run blew up")

    async def __call__(self, text: str, *, session_key: str, run_id: str | None = None) -> str:
        self.calls += 1
        raise self._exc


class _HangingRun:
    """Run stub that blocks until cancelled — drives timeout/cancel paths."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __call__(self, text: str, *, session_key: str, run_id: str | None = None) -> str:
        self.started.set()
        await asyncio.Event().wait()  # never returns on its own
        return "unreachable"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
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
    yield st, identity
    _runtime.reset()


async def _seed_todo(st: Any, identity: AgentIdentity, task_id: str, **overrides: Any) -> None:
    from arcstore.tasks import Task

    from arcagent.modules.tasks import _runtime

    await _runtime.ensure_store()
    fields: dict[str, Any] = {
        "id": task_id,
        "title": "Do it",
        "creator_did": identity.did,
        "owner_did": identity.did,
        "status": "todo",
    }
    fields.update(overrides)
    await st.store.create(Task(**fields))


@pytest.mark.asyncio
class TestRetryAndDeadLetter:
    async def test_failed_run_is_requeued_with_backoff(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        st.agent_run_fn = _FailingRun()
        await _seed_todo(st, identity, "t1", max_attempts=3)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "todo"  # requeued, not terminal
        assert task.attempts == 1
        assert task.last_error and "run blew up" in task.last_error
        assert task.next_attempt_at is not None  # backoff gate set

    async def test_backoff_gate_blocks_immediate_redispatch(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        run = _FailingRun()
        st.agent_run_fn = run
        await _seed_todo(st, identity, "t1", max_attempts=3)

        await _dispatch_tick()  # attempt 1 fails -> requeued with future backoff
        await _dispatch_tick()  # backoff not elapsed -> must NOT re-dispatch

        assert run.calls == 1
        task = await st.store.get("t1")
        assert task is not None and task.status == "todo" and task.attempts == 1

    async def test_exhausted_retries_dead_letter(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        st.agent_run_fn = _FailingRun()
        # One attempt allowed: the first failure is terminal.
        await _seed_todo(st, identity, "t1", max_attempts=1)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "failed"
        assert task.resolution and "retries exhausted" in task.resolution
        assert task.completed_at is not None

    async def test_timeout_is_a_failed_attempt(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        st.config = st.config.model_copy(update={"task_timeout_seconds": 0.05})
        st.agent_run_fn = _HangingRun()
        await _seed_todo(st, identity, "t1", max_attempts=1)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "failed"  # exhausted (max_attempts=1)
        assert task.last_error and "timeout" in task.last_error


@pytest.mark.asyncio
class TestStuckReclaim:
    async def test_first_pass_reclaims_orphaned_in_progress(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _reliability_tick

        st, identity = state
        await _seed_todo(st, identity, "t1", max_attempts=3)
        # Simulate a pre-restart orphan: in_progress in the store, but no live
        # run in this fresh process (st.running is empty, reclaim_done is False).
        await st.store.start_task("t1", identity.did, run_id="r1")
        assert not st.running

        await _reliability_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "todo"  # reclaimed for re-dispatch
        assert task.last_error and "stuck" in task.last_error
        assert st.reclaim_done is True

    async def test_steady_state_respects_threshold(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _reliability_tick

        st, identity = state
        st.reclaim_done = True  # past the startup pass
        st.config = st.config.model_copy(update={"stuck_reclaim_seconds": 9999})
        await _seed_todo(st, identity, "t1", max_attempts=3)
        await st.store.start_task("t1", identity.did, run_id="r1")  # started just now

        await _reliability_tick()

        task = await st.store.get("t1")
        # Fresh started_at, huge threshold -> not yet stale -> left running.
        assert task is not None and task.status == "in_progress"


@pytest.mark.asyncio
class TestCancel:
    async def test_reliability_tick_cancels_a_live_run(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick, _reliability_tick

        st, identity = state
        run = _HangingRun()
        st.agent_run_fn = run
        await _seed_todo(st, identity, "t1", max_attempts=3)

        dispatch = asyncio.ensure_future(_dispatch_tick())
        await asyncio.wait_for(run.started.wait(), timeout=1)  # run is in flight
        assert "t1" in st.running

        # Operator requests cancel; the watcher observes it and stops the run.
        assert await st.store.request_cancel("t1", actor_did=_OPERATOR) is not None
        await _reliability_tick()
        await asyncio.wait_for(dispatch, timeout=1)

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "failed"
        assert task.resolution == "cancelled"
        assert "t1" not in st.running

    async def test_cancel_with_no_live_run_dead_letters_directly(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _reliability_tick

        st, identity = state
        await _seed_todo(st, identity, "t1", max_attempts=3)
        await st.store.start_task("t1", identity.did, run_id="r1")
        await st.store.request_cancel("t1", actor_did=_OPERATOR)
        st.reclaim_done = True  # isolate the cancel branch from startup reclaim
        assert "t1" not in st.running  # process restarted after the request

        await _reliability_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "failed" and task.resolution == "cancelled"


class _CappedRun:
    """Run stub mimicking a breaker-halted loop.

    A budget/turn/token cap does NOT raise: the arcrun breaker synthesizes a
    ``completion_payload`` with ``status="failed"`` and ``error=<reason>``, emits
    ``loop.complete``, and RETURNS a ``RunResult``. The agent never called
    ``complete_task``. This stub reproduces exactly that return shape.
    """

    def __init__(self, reason: str = "max_cost") -> None:
        self.calls = 0
        self.reason = reason

    async def __call__(self, text: str, *, session_key: str, run_id: str | None = None, **_: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            content=f"{self.reason} limit reached before task completed.",
            completion_payload={
                "status": "failed",
                "summary": f"{self.reason} limit reached before task completed.",
                "error": self.reason,
            },
            completion_tool=None,
        )


@pytest.mark.asyncio
class TestBudgetCapSettlement:
    """A loop the breaker halts must terminally fail its task — the reported bug.

    Before the fix the capped run returned normally, ``_await_run`` treated it as
    a clean finish, and the row sat ``in_progress`` (dashboard "running") until
    stuck-reclaim guessed it dead — while a workflow that materialised it as a
    node never finalised.
    """

    async def test_cost_cap_without_completion_fails_task(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        run = _CappedRun(reason="max_cost")
        st.agent_run_fn = run
        # max_attempts=3 proves a cap is TERMINAL now, not a retryable attempt:
        # a cap is deterministic, so retrying only re-burns the ceiling.
        await _seed_todo(st, identity, "t1", max_attempts=3)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "failed"
        assert task.attempts == 1  # not retried
        assert task.last_error and "max_cost" in task.last_error
        assert task.resolution and "limit reached" in task.resolution
        assert task.completed_at is not None
        assert run.calls == 1

    async def test_max_turns_cap_also_fails_task(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        st.agent_run_fn = _CappedRun(reason="max_turns")
        await _seed_todo(st, identity, "t1", max_attempts=2)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "failed"
        assert task.last_error and "max_turns" in task.last_error

    async def test_agent_completion_wins_over_capped_payload(self, state: Any) -> None:
        """If the agent finished via its own tool, that terminal row wins.

        Guards against clobbering a genuine ``done`` when a stray halt payload
        rides along — ``_settle_capped_run`` only touches an ``in_progress`` row.
        """
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state

        async def run(text: str, *, session_key: str, run_id: str | None = None, **_: Any) -> Any:
            await st.store.finish(
                "t1", status="done", resolution="did it", actor_did=identity.did
            )
            return SimpleNamespace(
                completion_payload={"status": "failed", "error": "max_cost", "summary": "x"},
                completion_tool=None,
            )

        st.agent_run_fn = run
        await _seed_todo(st, identity, "t1", max_attempts=3)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "done"  # the agent's terminal is not overridden

    async def test_clean_return_without_halt_reason_is_left_for_reclaim(self, state: Any) -> None:
        """A normal end-of-turn with no completion (and no cap) is NOT auto-failed.

        Only a breaker halt is a terminal here; an ordinary run that ends without
        calling ``complete_task`` stays ``in_progress`` for stuck-reclaim, exactly
        as before — the fix must not over-reach.
        """
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state

        async def run(text: str, *, session_key: str, run_id: str | None = None, **_: Any) -> Any:
            return SimpleNamespace(
                completion_payload={"status": "success", "summary": "done thinking"},
                completion_tool=None,
            )

        st.agent_run_fn = run
        await _seed_todo(st, identity, "t1", max_attempts=3)

        await _dispatch_tick()

        task = await st.store.get("t1")
        assert task is not None
        assert task.status == "in_progress"  # untouched — reclaim owns this case


@pytest.mark.asyncio
async def test_create_task_stamps_config_max_attempts(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import create_task

    st, _identity = state
    st.config = st.config.model_copy(update={"default_max_attempts": 5})
    created = json.loads(await create_task(title="configured"))
    assert created["max_attempts"] == 5
