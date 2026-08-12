"""Race-condition regression test — Hermes PR #4926, re-aimed by SPEC-065.

CRITICAL TEST: This is the primary regression guard for the pre-await race on
the inbound path.

The Bug (Hermes PR #4926):
    Two messages from the same user arrive in the same event-loop cycle. Without
    a guard that spans the read and the act, both read "this session is idle"
    and both open a turn, leading to:
    - Double responses to the user
    - Interleaved LLM context from two concurrent turns
    - Audit log inconsistencies (two "session created" events)

Where the guard lives now:
    It used to be a synchronous check-and-insert in ``SessionRouter.handle()``,
    backed by a per-session FIFO in the gateway. SPEC-065 deleted that FIFO: the
    router decides nothing about runs and hands every message to the agent, which
    serialises the decision per session (``SessionRunCoordinator.delivery``) and
    either opens a turn or injects into the one in flight.

    So the assertions moved with the guard. They are no longer counting router
    tasks and queue entries — those would pass with the real guard removed. They
    are measured on the agent: exactly ONE run opens, and every other message
    lands in that run's injection queue rather than being lost.

This Test:
    Fires N concurrent messages with the SAME session key through the REAL
    router, the REAL AsyncioExecutor and a REAL ArcAgent whose model parks the
    turn, so the run is genuinely in flight while the rest of the burst arrives.
    A parked model is what makes ``asyncio.gather`` interleave: an agent that
    returned instantly would let each message complete before the next began, and
    the test would pass with the guard removed.

    Asserts, for every concurrency level:
    - Exactly 1 run opened for the session (the guard held)
    - Every other message is accounted for: queued on that run, or — past the
      run's injection capacity — refused loudly to the sender. Never lost.

    The test is parametrised across concurrency levels to increase confidence
    that we are not passing due to lucky scheduling.

CAPACITY CHANGE, DELIBERATE (SPEC-065 T-932):
    The deleted gateway FIFO held 100 messages per session and dropped silently
    past that. The live run's injection queues hold 16 (arcrun ``RunState``), and
    the surplus now comes back to the sender as an error instead of vanishing.
    Retention shrank; silence did too. See the SDD open question on whether to
    reinstate a flood cap beside the agent's session lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from arcgateway.executor import InboundEvent
from arcgateway.session import build_session_key

from .race_harness import AGENT_DID, INJECTION_CAPACITY, RaceHarness, race_harness

__all__ = ["race_harness"]  # re-exported fixture

pytestmark = pytest.mark.asyncio


async def _burst(harness: RaceHarness, n_messages: int, *, user: str) -> str:
    """Fire ``n_messages`` concurrent messages at one session and settle."""
    session_key = build_session_key(AGENT_DID, user)
    events = [
        InboundEvent(
            platform="telegram",
            chat_id="12345",
            user_did=user,
            agent_did=AGENT_DID,
            session_key=session_key,
            message=f"concurrent message {i}",
        )
        for i in range(n_messages)
    ]
    await asyncio.gather(*[harness.router.handle(e) for e in events])
    await harness.settle_burst(session_key, n_messages)
    return session_key


async def test_concurrent_messages_same_session_open_a_single_run(
    race_harness: RaceHarness,
) -> None:
    """CRITICAL: 20 concurrent messages to one session must open exactly 1 run.

    If this test fails, the pre-await guard on the inbound path has been broken.
    DO NOT accept this failure as flaky — it indicates a real correctness bug.
    """
    n_messages = 20
    session_key = await _burst(race_harness, n_messages, user="did:arc:user:alice")

    assert race_harness.runs_opened(session_key) == 1, (
        f"RACE BUG: expected exactly 1 run opened for session {session_key!r}, "
        f"but got {race_harness.runs_opened(session_key)}. The guard in "
        f"ArcAgent.deliver_message / SessionRunCoordinator.delivery has been "
        f"broken. See Hermes PR #4926 and arcgateway/session.py module docstring."
    )


@pytest.mark.parametrize("n_messages", [5, 10, 20, 50])
async def test_race_regression_various_concurrency_levels(
    race_harness: RaceHarness, n_messages: int
) -> None:
    """Run the race test at multiple concurrency levels.

    Higher concurrency increases the probability of exposing race windows in
    incorrect implementations. Every level must produce exactly one run, and
    every message must be accounted for — none silently disappears.
    """
    session_key = await _burst(race_harness, n_messages, user=f"did:arc:user:n{n_messages}")

    assert race_harness.runs_opened(session_key) == 1, (
        f"Race detected at n={n_messages}: {race_harness.runs_opened(session_key)} runs "
        f"opened instead of 1. Hermes PR #4926 regression."
    )
    accounted = 1 + race_harness.injected(session_key) + race_harness.refused(session_key)
    assert accounted == n_messages, (
        f"At n={n_messages}: only {accounted} messages accounted for "
        f"(1 opened the turn, {race_harness.injected(session_key)} queued on it, "
        f"{race_harness.refused(session_key)} refused). The rest vanished."
    )


@pytest.mark.parametrize("n_messages", [2, 5, 10, INJECTION_CAPACITY + 1])
async def test_burst_within_capacity_is_retained_in_full(
    race_harness: RaceHarness, n_messages: int
) -> None:
    """Up to the live run's injection capacity, every message is retained.

    One message opens the turn; the rest are queued on it and nothing is refused.
    """
    session_key = await _burst(race_harness, n_messages, user=f"did:arc:user:c{n_messages}")

    assert race_harness.runs_opened(session_key) == 1
    assert race_harness.injected(session_key) == n_messages - 1, (
        f"At n={n_messages} (capacity {INJECTION_CAPACITY}) every message but the "
        f"first must be queued on the live run; got {race_harness.injected(session_key)}."
    )
    assert race_harness.refused(session_key) == 0, "nothing may be refused below capacity"


async def test_burst_past_capacity_is_refused_loudly_not_dropped(
    race_harness: RaceHarness,
) -> None:
    """Past capacity the sender is told; the surplus never disappears in silence.

    This is the behaviour that replaced the deleted gateway FIFO's 100-deep
    bound. The bound is smaller and the overflow is now visible.
    """
    n_messages = INJECTION_CAPACITY + 5
    session_key = await _burst(race_harness, n_messages, user="did:arc:user:flood")

    assert race_harness.runs_opened(session_key) == 1, "overflow must not open a second run"
    assert race_harness.injected(session_key) == INJECTION_CAPACITY, (
        "the live run must be filled to capacity before anything is refused"
    )
    assert race_harness.refused(session_key) == n_messages - 1 - INJECTION_CAPACITY

    await _wait_until(
        lambda: any("[agent-error]" in m for m in race_harness.adapter.sent),
        what="the sender to be told its message could not be taken",
    )


async def test_different_sessions_each_open_their_own_run(race_harness: RaceHarness) -> None:
    """N messages to N different sessions open N runs (full concurrency).

    Validates that per-session serialisation does not accidentally serialise
    legitimate concurrent sessions against each other.
    """
    n_sessions = 20
    users = [f"did:arc:user:u{i}" for i in range(n_sessions)]
    sessions = [build_session_key(AGENT_DID, u) for u in users]

    await asyncio.gather(
        *[
            race_harness.router.handle(
                InboundEvent(
                    platform="telegram",
                    chat_id="12345",
                    user_did=u,
                    agent_did=AGENT_DID,
                    session_key=sk,
                    message="hello",
                )
            )
            for u, sk in zip(users, sessions, strict=True)
        ]
    )
    for sk in sessions:
        await race_harness.settle_burst(sk, 1)

    for sk in sessions:
        assert race_harness.runs_opened(sk) == 1, f"session {sk} should have opened 1 run"
        assert race_harness.injected(sk) == 0, (
            f"session {sk} got a single message; nothing should be queued on its run"
        )


async def test_burst_messages_are_answered_by_the_run_they_joined(
    race_harness: RaceHarness,
) -> None:
    """Nothing is lost: the queued messages are consumed by the run when it resumes.

    The old FIFO proved this by draining itself. The equivalent proof now is that
    the parked run, once released, actually takes the injected messages off its
    queue instead of leaving them behind.
    """
    n_messages = 4
    session_key = await _burst(race_harness, n_messages, user="did:arc:user:drain")

    assert race_harness.injected(session_key) == n_messages - 1

    handle = race_harness.handles[session_key]
    race_harness.model.open_gate()
    await _wait_until(
        lambda: handle.state.followup_queue.qsize() == 0,
        what="the live run to consume every injected message",
    )
    assert race_harness.runs_opened(session_key) == 1, (
        "consuming the injections must not have opened a second run"
    )


async def _wait_until(predicate: Callable[[], bool], *, what: str, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")
