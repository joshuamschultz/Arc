"""Race regression stress test — 100 independent runs of the Hermes PR #4926 guard.

G1.3 — Race regression test passes 100 runs.

Background:
    The pre-await race on the inbound path is non-deterministic by nature. A
    single run may pass even with a broken guard if the asyncio scheduler
    happens to order coroutines favourably. Running 100 independent iterations
    makes a false-pass statistically negligible: a broken implementation that
    races 10% of the time would need to be "lucky" 100 times consecutively,
    which has probability (0.90)^100 ≈ 0.000026 — effectively zero in CI.

What each run tests:
    - A fresh session (a distinct user) against a real agent whose model parks
      every turn, so the first turn is genuinely in flight for the whole burst.
    - Fire N concurrent messages at that session via ``asyncio.gather()``.
    - Assert exactly 1 run opened (the guard held).
    - Assert the other N-1 are all queued on that run (no drops).

    N sits at the live run's injection capacity, so "no drops" is an absolute
    claim here: a burst larger than that is refused loudly, and that boundary is
    covered by ``test_race_regression.py`` instead.

Where the guard lives:
    In the agent, not the gateway — see the module docstring of
    ``test_race_regression.py``. These assertions are therefore measured on the
    agent's decisions, not on router bookkeeping.

Marker:
    @pytest.mark.slow — deselect with ``-m "not slow"``.
    It is required as part of the M1 acceptance gate (G1.3).

Usage::

    # Run just the stress suite
    uv run pytest packages/arcgateway/tests/integration/test_race_regression_stress.py -m slow -v

    # Run as part of full M1 gate
    make m1-gates
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcgateway.executor import InboundEvent
from arcgateway.session import build_session_key

from .race_harness import (
    AGENT_DID,
    INJECTION_CAPACITY,
    RaceHarness,
    build_harness,
    teardown_harness,
)

pytestmark = pytest.mark.slow

# One message opens the turn; the rest fill the live run's injection queue
# exactly. Nothing may be refused at this size.
_N_MESSAGES = INJECTION_CAPACITY + 1
_N_RUNS = 100


async def _run_once(harness: RaceHarness, run_index: int) -> str | None:
    """Execute one race-regression run against a fresh session.

    Returns:
        None if the run passes.
        A human-readable failure description if the guard failed.
    """
    user_did = f"did:arc:user:stress-{run_index}"
    session_key = build_session_key(AGENT_DID, user_did)

    events = [
        InboundEvent(
            platform="telegram",
            chat_id="12345",
            user_did=user_did,
            agent_did=AGENT_DID,
            session_key=session_key,
            message=f"stress run {run_index} message {i}",
        )
        for i in range(_N_MESSAGES)
    ]

    # Fire all N messages concurrently — simulates a burst from one user.
    await asyncio.gather(*[harness.router.handle(e) for e in events])
    await harness.settle_burst(session_key, _N_MESSAGES)

    opened = harness.runs_opened(session_key)
    if opened != 1:
        return (
            f"Run {run_index}: RACE BUG — expected 1 run opened for the session, "
            f"got {opened}. The guard in ArcAgent.deliver_message / "
            f"SessionRunCoordinator.delivery has been broken. See Hermes PR #4926."
        )
    injected = harness.injected(session_key)
    if injected != _N_MESSAGES - 1:
        return (
            f"Run {run_index}: LOST MESSAGES — expected {_N_MESSAGES - 1} messages "
            f"queued on the live run, got {injected} "
            f"({harness.refused(session_key)} refused). Nothing may be dropped at "
            f"or below the run's injection capacity of {INJECTION_CAPACITY}."
        )
    return None


@pytest.mark.asyncio
async def test_race_regression_100_runs(tmp_path: Path) -> None:
    """Run the pre-await race regression check 100 independent times.

    G1.3: Race regression test passes 100 runs.

    Each run fires N concurrent messages at a fresh session and asserts:
      - Exactly 1 run opened for that session (guard held).
      - The remaining N-1 are queued on it (no drops, no double-routes).

    All 100 runs must pass. Any failure indicates the guard has been broken.
    If this test flakes (passes sometimes, fails sometimes), the guard is
    probabilistically broken — treat any failure as a definite regression.
    """
    harness = await build_harness(tmp_path)
    try:
        failures = [
            failure
            for run_index in range(_N_RUNS)
            if (failure := await _run_once(harness, run_index)) is not None
        ]
    finally:
        await teardown_harness(harness)

    if failures:
        summary = (
            f"Race regression FAILED: {len(failures)}/{_N_RUNS} runs detected a race.\n\n"
            "Failures:\n" + "\n".join(f"  {f}" for f in failures) + "\n\n"
            "Background (Hermes PR #4926):\n"
            "  The pre-await race occurs when two messages both read 'this session\n"
            "  is idle' before either has opened a turn. The guard must span the\n"
            "  read AND the act — it lives in the agent's session coordinator, not\n"
            "  in the gateway. See arcgateway/session.py module docstring.\n"
        )
        raise AssertionError(summary)
