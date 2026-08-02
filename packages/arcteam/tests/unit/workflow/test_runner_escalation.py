"""SPEC-061 — a tick that never stops failing must not look like a healthy idle one.

``run_forever``'s per-run isolation (inside ``tick()``) is proven separately by
``test_one_poisoned_run_does_not_stop_the_tick_for_the_others`` and must stay
untouched. This file covers the level above it: ``tick()`` itself can still
raise (its call to list active runs sits outside the per-run guard), and
nothing used to escalate when that kept happening — a log line every interval
is indistinguishable, from the outside, from a runner making progress.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcteam.workflow.narrator import RunNarrator

from .conftest import RUNNER_DID, Definition, Node, RecordingSender, RecordingSink
from .test_runner_frontier import build

CHANNEL = "channel://escalation"

WIRED = Definition(
    id="wired",
    channel=CHANNEL,
    nodes=(Node(id="only", kind="agent", agent="@sales"),),
)


class FlakyRunStore:
    """Wraps a real run store; ``active_runs`` can be told to always raise.

    Stands in for the store connection actually dropping — the one call in
    ``tick()`` that sits OUTSIDE the per-run try/except, so it is the only way
    a whole ``tick()`` call can raise.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.poisoned = False

    async def active_runs(self) -> Any:
        if self.poisoned:
            raise RuntimeError("store connection lost")
        return await self._inner.active_runs()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_repeated_tick_failure_is_escalated_via_audit_and_narration(
    stores: Any, registry: Any
) -> None:
    """The threshold is the REQ'd behavior: 3 consecutive whole-tick failures page.

    A tick that raises once is noise (a transient hiccup); this proves the 3rd
    consecutive one produces an operator-visible audit event carrying the
    failure count and the last error, plus a channel post since a narrator is
    wired — not merely another log line.
    """
    _, runs, _ = stores
    flaky = FlakyRunStore(runs)
    sink = RecordingSink()
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    runner = build(stores, registry, WIRED, audit_sink=sink, narrator=narrator)
    runner._runs = flaky  # swap in the flaky wrapper after construction

    # One healthy tick first, so the runner has actually seen the run's bound
    # channel before the store goes flaky — proving escalation narrates to a
    # channel learned from real activity, not a hardcoded one. ``start_run``
    # itself only calls ``advance``, never ``tick``, so this needs an explicit
    # tick to populate the cache.
    await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    await runner.tick()
    assert runner._last_known_channels == [CHANNEL]

    flaky.poisoned = True
    task = asyncio.create_task(runner.run_forever(interval=0.005))
    try:
        for _ in range(400):
            if any(e.action == "workflow.runner.degraded" for e in sink.events):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("escalation audit event never fired within the timeout")
    finally:
        await _cancel(task)

    degraded = [e for e in sink.events if e.action == "workflow.runner.degraded"]
    assert degraded[0].extra["consecutive_failures"] == 3, (
        "the FIRST escalation must land at exactly the threshold, not before or after"
    )
    assert "store connection lost" in degraded[0].extra["last_error"]

    narrated = [m for m in sender.sent if m.meta.get("event") == "runner.degraded"]
    assert narrated, "a wired narrator must post the escalation to a channel"
    assert narrated[0].to == [CHANNEL]


async def test_a_successful_tick_resets_the_consecutive_failure_count(
    stores: Any, registry: Any
) -> None:
    """Two failures, a success, then two more must NOT reach the threshold.

    Mirrors the scheduler's circuit breaker: consecutive_failures is reset by
    any successful tick, not accumulated across an interruption.
    """
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink)

    script: list[BaseException | None] = [
        RuntimeError("blip 1"),
        RuntimeError("blip 2"),
        None,  # a real, successful tick — must reset the streak
        RuntimeError("blip 3"),
        RuntimeError("blip 4"),
    ]
    calls = {"n": 0}

    async def scripted_tick() -> int:
        i = calls["n"]
        calls["n"] += 1
        # Anything beyond the script pads with success, so a generous test
        # timeout can never accidentally manufacture a 3rd consecutive
        # failure that the script itself never intended.
        outcome = script[i] if i < len(script) else None
        if outcome is not None:
            raise outcome
        return 0

    runner.tick = scripted_tick  # type: ignore[method-assign]
    task = asyncio.create_task(runner.run_forever(interval=0.005))
    await asyncio.sleep(0.005 * (len(script) + 5))
    await _cancel(task)

    assert calls["n"] >= len(script), "the loop must have run through the whole script"
    degraded = [e for e in sink.events if e.action == "workflow.runner.degraded"]
    assert degraded == [], (
        "no run of 3 CONSECUTIVE failures ever occurred — the success in the "
        "middle must have reset the counter, or this would have escalated"
    )


async def test_one_poisoned_run_still_does_not_stop_the_tick(stores: Any, registry: Any) -> None:
    """The pre-existing per-run isolation this change must not weaken.

    A run whose ``advance`` always raises must not prevent other runs from
    advancing, tick after tick, and must not by itself trip the new
    whole-tick escalation — that escalation is for ``tick()`` raising, and
    the per-run guard means a poisoned run alone never makes it do that.
    """
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink)

    async def always_raise(run_id: str) -> Any:
        raise RuntimeError("this run is poisoned")

    await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    runner.advance = always_raise  # type: ignore[method-assign]

    task = asyncio.create_task(runner.run_forever(interval=0.005))
    await asyncio.sleep(0.005 * 8)
    await _cancel(task)

    degraded = [e for e in sink.events if e.action == "workflow.runner.degraded"]
    assert degraded == [], "a poisoned RUN must not trip the whole-tick escalation"
