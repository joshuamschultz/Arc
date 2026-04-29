"""Race regression stress test — 100 independent runs of the Hermes PR #4926 guard.

G1.3 — Race regression test passes 100 runs.

Background:
    The pre-await race in SessionRouter.handle() is non-deterministic by nature.
    A single run may pass even with a broken guard if the asyncio scheduler
    happens to order coroutines favourably. Running 100 independent iterations
    makes a false-pass statistically negligible: a broken implementation that
    races 10% of the time would need to be "lucky" 100 times consecutively,
    which has probability (0.90)^100 ≈ 0.000026 — effectively zero in CI.

What each run tests:
    - Create a fresh SessionRouter and _GatedExecutor for isolation.
    - Fire N=20 concurrent InboundEvent.handle() calls with the SAME session key
      via asyncio.gather(), simulating a burst of messages from one user.
    - Assert exactly 1 agent task spawned (race guard held).
    - Assert exactly 19 events queued (no drops).
    - Release the gate and let the router drain to a clean state.

Marker:
    @pytest.mark.slow — opt in via ``pytest -m slow``.
    This test suite takes ~5-10 seconds and is not run on every push.
    It is required as part of the M1 acceptance gate (G1.3).

Usage::

    # Run just the stress suite
    uv run pytest packages/arcgateway/tests/integration/test_race_regression_stress.py -m slow -v

    # Run as part of full M1 gate
    make m1-gates
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.slow

_N_MESSAGES = 20
_N_RUNS = 100

_AGENT_DID = "did:arc:agent:stress-bot"
_USER_DID = "did:arc:user:stress-tester"


class _GatedExecutor:
    """Executor whose turns block until explicitly released.

    Identical to the one in test_race_regression.py — duplicated here
    to keep the stress test self-contained (easier to read in CI logs).
    The gate keeps the first turn alive so ALL concurrent handle() calls
    arrive while the session is "busy", maximising the race window.
    """

    def __init__(self) -> None:
        self._gates: dict[str, asyncio.Event] = {}

    def release(self, session_key: str) -> None:
        """Allow the blocked turn for session_key to complete.

        Pre-creates the gate so release() is safe to call before
        _stream() has started waiting.
        """
        gate = self._gates.setdefault(session_key, asyncio.Event())
        gate.set()

    def release_all(self) -> None:
        """Release all active gates."""
        for gate in self._gates.values():
            gate.set()

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Pre-create gate before returning the stream."""
        self._gates.setdefault(event.session_key, asyncio.Event())
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        await self._gates[event.session_key].wait()
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


# ---------------------------------------------------------------------------
# Core single-run helper
# ---------------------------------------------------------------------------


async def _run_once(run_index: int) -> str | None:
    """Execute one race-regression run.

    Returns:
        None if the run passes.
        A human-readable failure description if the race guard failed.
    """
    executor = _GatedExecutor()
    router = SessionRouter(executor=executor)
    session_key = build_session_key(_AGENT_DID, _USER_DID)

    events = [
        InboundEvent(
            platform="telegram",
            chat_id="12345",
            user_did=_USER_DID,
            agent_did=_AGENT_DID,
            session_key=session_key,
            message=f"stress run {run_index} message {i}",
        )
        for i in range(_N_MESSAGES)
    ]

    # Fire all N messages concurrently — simulates a burst from one user.
    await asyncio.gather(*[router.handle(e) for e in events])
    # One event-loop tick to let the spawned task register.
    await asyncio.sleep(0)

    tasks_spawned = router.agent_tasks_spawned.get(session_key, 0)
    queued_count = len(router.queued_events.get(session_key, []))

    failure: str | None = None

    if tasks_spawned != 1:
        failure = (
            f"Run {run_index}: RACE BUG — expected 1 agent task spawned, "
            f"got {tasks_spawned}. "
            f"The pre-await race guard in SessionRouter.handle() has been broken. "
            f"See Hermes PR #4926."
        )
    elif queued_count != _N_MESSAGES - 1:
        failure = (
            f"Run {run_index}: QUEUE BUG — expected {_N_MESSAGES - 1} queued events, "
            f"got {queued_count}. Events may have been dropped or double-counted."
        )

    # Always release the gate so the event loop can clean up tasks.
    executor.release_all()
    await asyncio.sleep(0.02)

    return failure


# ---------------------------------------------------------------------------
# Stress test
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_race_regression_100_runs() -> None:
    """Run the pre-await race regression check 100 independent times.

    G1.3: Race regression test passes 100 runs.

    Each run fires N=20 concurrent messages at the same session key and
    asserts:
      - Exactly 1 agent task spawned per session (race guard held).
      - Exactly 19 events queued (no drops, no double-routes).

    All 100 runs must pass. Any failure indicates the race-condition guard
    in SessionRouter.handle() has been broken.

    If this test flakes (passes sometimes, fails sometimes), the guard is
    probabilistically broken — treat any failure as a definite regression.
    """
    failures: list[str] = []

    for run_index in range(_N_RUNS):
        failure = await _run_once(run_index)
        if failure is not None:
            failures.append(failure)

    if failures:
        summary = (
            f"Race regression FAILED: {len(failures)}/{_N_RUNS} runs detected a race.\n\n"
            "Failures:\n" + "\n".join(f"  {f}" for f in failures) + "\n\n"
            "Background (Hermes PR #4926):\n"
            "  The pre-await race occurs when two coroutines both pass the\n"
            "  ``if session_key not in _active_sessions`` check before either\n"
            "  has inserted into the dict. The fix is a SYNCHRONOUS insertion\n"
            "  with no await between the check and the dict write.\n"
            "  See arcgateway/session.py module docstring for full explanation.\n"
        )
        raise AssertionError(summary)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_race_regression_stress_all_n20_produce_single_task() -> None:
    """Verify that all 100 runs each produce exactly 1 agent task for n=20 messages.

    This is the formal G1.3 assertion expressed as a single test with
    explicit per-run bookkeeping. Complements the loop above with a
    cleaner assertion message for CI reporting.
    """
    per_run_results: list[tuple[int, int, int]] = []  # (run_idx, spawned, queued)

    for run_index in range(_N_RUNS):
        executor = _GatedExecutor()
        router = SessionRouter(executor=executor)
        session_key = build_session_key(_AGENT_DID, _USER_DID)

        events = [
            InboundEvent(
                platform="slack",
                chat_id="C_STRESS",
                user_did=_USER_DID,
                agent_did=_AGENT_DID,
                session_key=session_key,
                message=f"run={run_index} msg={i}",
            )
            for i in range(_N_MESSAGES)
        ]

        await asyncio.gather(*[router.handle(e) for e in events])
        await asyncio.sleep(0)

        spawned = router.agent_tasks_spawned.get(session_key, 0)
        queued = len(router.queued_events.get(session_key, []))
        per_run_results.append((run_index, spawned, queued))

        executor.release_all()
        await asyncio.sleep(0.02)

    # All 100 runs must produce exactly 1 spawn + 19 queued.
    bad_runs = [r for r in per_run_results if r[1] != 1 or r[2] != _N_MESSAGES - 1]

    assert not bad_runs, (
        f"G1.3 FAILED: {len(bad_runs)}/{_N_RUNS} runs did not satisfy "
        f"'exactly 1 spawn + {_N_MESSAGES - 1} queued' for n={_N_MESSAGES} concurrent messages.\n"
        "Failing runs (run_index, tasks_spawned, queued_count):\n"
        + "\n".join(f"  run={r[0]} spawned={r[1]} queued={r[2]}" for r in bad_runs)
        + "\n\n"
        "This is the G1.3 M1 Acceptance Gate for SPEC-018.\n"
        "Any failure here = the pre-await race guard is broken.\n"
    )
