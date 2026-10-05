"""A turn that has begun but has no model loop yet.

A turn is live from the moment a person's message opens it, not from the moment
its model loop starts. Between the two sits Context prep (strategy pick, recall,
prompt assembly), which can take many seconds. A message arriving then must join
the turn — be held and drained at the first turn boundary — instead of opening a
second one or being refused. ``PendingTurn`` is the registered stand-in for the
run handle during that window: it takes steers, follow-ups and a cancel exactly
as the loop's handle does, and hands everything it holds to the real handle the
instant the loop exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import arcrun

# Matches arcrun's own injection queue bound, so a message accepted while the
# turn is preparing always fits once the loop's queue takes it over.
_QUEUE_MAXSIZE = 16


@dataclass
class PendingTurnState:
    """The slice of a run's state that injection and cancellation touch."""

    run_id: str
    steer_queue: asyncio.Queue[arcrun.Injection] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    )
    followup_queue: asyncio.Queue[arcrun.Injection] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    )
    cancelled_by: str = ""
    cancel_reason: str = ""


class PendingTurn:
    """Registered for a turn from its first await until its loop's handle exists."""

    def __init__(self, run_id: str) -> None:
        self._state = PendingTurnState(run_id=run_id)
        self._cancelled = False

    @property
    def state(self) -> PendingTurnState:
        return self._state

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def steer(self, caller_did: str, message: str | list[arcrun.ContentBlock]) -> None:
        self._state.steer_queue.put_nowait(arcrun.Injection.new(caller_did, message))

    async def follow_up(self, caller_did: str, message: str | list[arcrun.ContentBlock]) -> None:
        self._state.followup_queue.put_nowait(arcrun.Injection.new(caller_did, message))

    async def cancel(self, caller_did: str, reason: str | None = None) -> None:
        """Stop the turn before its loop starts; held messages are discarded.

        Prep itself is bounded and is left to finish; the cancel is replayed onto
        the loop's state at hand-over, so the loop ends at once with the same
        attributed, structured cancelled result any mid-run cancel produces.
        """
        if not caller_did:
            raise ValueError("caller_did is required to cancel a run")
        self._state.cancelled_by = caller_did
        self._state.cancel_reason = reason or ""
        self._cancelled = True
        for queue in (self._state.steer_queue, self._state.followup_queue):
            while not queue.empty():
                queue.get_nowait()

    def hand_over(self, handle: arcrun.RunHandle) -> None:
        """Move everything held, and any cancel, onto the real loop's state."""
        pairs = (
            (self._state.steer_queue, handle.state.steer_queue),
            (self._state.followup_queue, handle.state.followup_queue),
        )
        for held, live in pairs:
            while not held.empty():
                live.put_nowait(held.get_nowait())
        if self._cancelled:
            live_state = handle.state
            live_state.cancelled_by = self._state.cancelled_by
            live_state.cancel_reason = self._state.cancel_reason
            live_state.cancel_event.set()
