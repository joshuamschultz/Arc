"""Race-condition regression test — Hermes PR #4926.

CRITICAL TEST: This is the primary regression guard for the pre-await race
condition in SessionRouter.handle().

The Bug (Hermes PR #4926):
    Without a synchronous guard, two messages arriving in the same event-loop
    cycle can BOTH pass the ``if session_key not in active_sessions`` check
    before either has inserted into the dict. This spawns two competing agent
    tasks for the same session, leading to:
    - Double responses to the user
    - Interleaved LLM context from two concurrent turns
    - Audit log inconsistencies (two "session created" events)

The Fix:
    Insert into ``_active_sessions`` SYNCHRONOUSLY before any await, within
    the same event-loop tick as the guard check. Python's asyncio cooperative
    scheduling guarantees no other coroutine runs between two synchronous
    statements.

This Test:
    Fires N=20 concurrent InboundEvent.handle() calls with the SAME session key
    via asyncio.gather(). All 20 calls arrive in the same event-loop cycle.
    Asserts:
    - Exactly 1 agent task spawned (the guard worked)
    - Remaining 19 events are in the queue (not dropped)

    The test is run multiple times (parametrized by seed offset) to increase
    confidence that we're not passing due to lucky scheduling.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

# ---------------------------------------------------------------------------
# Fake executor for race tests
# ---------------------------------------------------------------------------


class _GatedExecutor:
    """Executor that blocks until explicitly released.

    This is CRITICAL for the race test: the gate keeps the first turn
    alive so that all 20 concurrent handle() calls arrive while the
    session is "busy". Without the gate, the first turn could complete
    before some handle() calls arrive, making the race window smaller
    and the test less reliable.

    Gate lifecycle:
    - Gates are pre-created in run() so release() works even before
      the stream has started waiting. This avoids a timing issue where
      release() is called before the spawned task has reached gate.wait().
    """

    def __init__(self) -> None:
        self._gates: dict[str, asyncio.Event] = {}

    def release(self, session_key: str) -> None:
        """Allow the blocked turn for session_key to complete.

        Uses setdefault so that release() called before the stream starts
        will pre-set the gate, causing gate.wait() to return immediately
        when the stream eventually runs.
        """
        gate = self._gates.setdefault(session_key, asyncio.Event())
        gate.set()

    def release_all(self) -> None:
        """Release all blocked turns (also pre-creates any missing gates)."""
        for gate in self._gates.values():
            gate.set()

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Pre-create gate before returning the stream iterator."""
        # Pre-create the gate so release() works before _stream runs
        self._gates.setdefault(event.session_key, asyncio.Event())
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        gate = self._gates[event.session_key]
        await gate.wait()
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


# ---------------------------------------------------------------------------
# Race regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_messages_same_session_spawn_single_agent() -> None:
    """CRITICAL: 20 concurrent messages to same session must spawn exactly 1 task.

    This is the primary regression guard for the Hermes PR #4926 pre-await
    race condition.

    Asserts:
    - agent_tasks_spawned[session_key] == 1 (race guard worked)
    - queued events == 19 (remaining messages are queued, not dropped)

    If this test fails, the race-condition guard in SessionRouter.handle()
    has been broken. DO NOT accept this failure as flaky — it indicates a
    real correctness bug.
    """
    executor = _GatedExecutor()
    router = SessionRouter(executor=executor)

    agent_did = "did:arc:agent:bot"
    user_did = "did:arc:user:alice"
    session_key = build_session_key(agent_did, user_did)

    n_messages = 20
    events = [
        InboundEvent(
            platform="telegram",
            chat_id="12345",
            user_did=user_did,
            agent_did=agent_did,
            session_key=session_key,
            message=f"concurrent message {i}",
        )
        for i in range(n_messages)
    ]

    # Fire all 20 handle() calls concurrently.
    # asyncio.gather schedules them all before any runs — they all arrive
    # in the same event-loop iteration for the first synchronous segment.
    await asyncio.gather(*[router.handle(e) for e in events])

    # Give the event loop one tick to actually start the spawned task
    await asyncio.sleep(0)

    tasks_spawned = router.agent_tasks_spawned.get(session_key, 0)
    queued_count = len(router.queued_events.get(session_key, []))

    assert tasks_spawned == 1, (
        f"RACE BUG: Expected exactly 1 agent task spawned for session {session_key!r}, "
        f"but got {tasks_spawned}. "
        f"The pre-await race guard in SessionRouter.handle() has been broken. "
        f"See Hermes PR #4926 and arcgateway/session.py module docstring."
    )

    assert queued_count == n_messages - 1, (
        f"Expected {n_messages - 1} queued events (19 after 1 spawned), "
        f"but got {queued_count}. Events may have been dropped."
    )

    # Clean up: release the gate so the task can complete
    executor.release_all()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("n_messages", [5, 10, 20, 50])
async def test_race_regression_various_concurrency_levels(n_messages: int) -> None:
    """Run the race test at multiple concurrency levels.

    Higher concurrency increases the probability of exposing race windows
    in incorrect implementations. All levels should produce exactly 1 spawn.
    """
    executor = _GatedExecutor()
    router = SessionRouter(executor=executor)

    session_key = "fixed_session_key_for_race_test"

    events = [
        InboundEvent(
            platform="slack",
            chat_id="C999",
            user_did="did:arc:user:racetest",
            agent_did="did:arc:agent:racetest",
            session_key=session_key,
            message=f"message {i}",
        )
        for i in range(n_messages)
    ]

    await asyncio.gather(*[router.handle(e) for e in events])
    await asyncio.sleep(0)

    tasks_spawned = router.agent_tasks_spawned.get(session_key, 0)

    assert tasks_spawned == 1, (
        f"Race detected at n={n_messages}: got {tasks_spawned} tasks instead of 1. "
        f"Hermes PR #4926 regression."
    )

    expected_queued = n_messages - 1
    actual_queued = len(router.queued_events.get(session_key, []))
    assert actual_queued == expected_queued, (
        f"At n={n_messages}: expected {expected_queued} queued events, got {actual_queued}. "
        f"Events may be dropped or double-spawned."
    )

    executor.release_all()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_different_sessions_all_spawn_tasks() -> None:
    """N messages to N different sessions spawn N tasks (full concurrency).

    Validates that the race guard does not accidentally prevent legitimate
    concurrent sessions from spawning.
    """
    executor = _GatedExecutor()
    router = SessionRouter(executor=executor)

    n_sessions = 20
    sessions = [f"session_{i:04d}" for i in range(n_sessions)]

    events = [
        InboundEvent(
            platform="telegram",
            chat_id="12345",
            user_did=f"did:arc:user:u{i}",
            agent_did="did:arc:agent:bot",
            session_key=sessions[i],
            message="hello",
        )
        for i in range(n_sessions)
    ]

    await asyncio.gather(*[router.handle(e) for e in events])
    await asyncio.sleep(0)

    # Each session gets exactly one task
    for sk in sessions:
        assert router.agent_tasks_spawned.get(sk, 0) == 1, (
            f"Session {sk} should have exactly 1 task spawned."
        )

    # No queueing — each session got only one message
    for sk in sessions:
        assert router.queue_depth(sk) == 0, (
            f"Session {sk} should have empty queue (single message, no queuing needed)."
        )

    executor.release_all()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_queued_events_not_dropped_after_gate_release() -> None:
    """Events queued behind a busy session are processed after the gate opens.

    Validates the queue-drain path: after the first turn completes, queued
    events are replayed sequentially, spawning one additional task per event.
    """
    executor = _GatedExecutor()
    router = SessionRouter(executor=executor)

    session_key = "drain_test_session"
    n_queued = 3

    # First message starts the session
    first_event = InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did="did:arc:user:alice",
        agent_did="did:arc:agent:bot",
        session_key=session_key,
        message="first",
    )
    await router.handle(first_event)

    # Queue additional messages while gate is closed
    for i in range(n_queued):
        await router.handle(
            InboundEvent(
                platform="telegram",
                chat_id="1",
                user_did="did:arc:user:alice",
                agent_did="did:arc:agent:bot",
                session_key=session_key,
                message=f"queued {i}",
            )
        )

    assert router.agent_tasks_spawned.get(session_key, 0) == 1
    assert len(router.queued_events.get(session_key, [])) == n_queued

    # Yield to the event loop so the first task reaches gate.wait()
    # before we release. This ensures the gate release is seen correctly.
    await asyncio.sleep(0)

    # Release the gate — queue should drain
    executor.release(session_key)
    # Give the event loop enough time to drain all queued turns.
    # Each queued turn requires: gate.wait() (immediate, already set) +
    # one event-loop tick to schedule + one to run. 0.2s is generous.
    await asyncio.sleep(0.2)

    # Total tasks spawned = 1 (original) + n_queued (drained)
    total_spawned = router.agent_tasks_spawned.get(session_key, 0)
    assert total_spawned == 1 + n_queued, (
        f"After drain, expected {1 + n_queued} total tasks, got {total_spawned}."
    )

    # Queue should be empty after drain
    assert router.queue_depth(session_key) == 0, "Queue should be empty after drain."
