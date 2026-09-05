"""ProgressManager — never-silent pacing (SPEC-077 COMP-010, REQ-013, D-765).

Wraps a slow dispatch (~90 s of real agent work) so audible silence never reads
as broken: an instant ack, periodic heartbeats, then the result — or a hard
timeout instead of infinite "thinking". Fillers are generic and never narrate
unverified backend content mid-flight (LLM10 bounded consumption).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

EventKind = Literal["ack", "heartbeat", "result", "timeout"]


@dataclass(frozen=True)
class ProgressEvent:
    """A cue for the voice layer to speak or play."""

    kind: EventKind
    text: str


Emit = Callable[[ProgressEvent], Awaitable[None]]


class ProgressManager:
    """Ack -> heartbeats -> result, or a hard-timeout failure."""

    def __init__(
        self,
        *,
        ack: str = "On it.",
        heartbeat: str = "Still working on it.",
        timeout_message: str = "That took too long — I've stopped. Try again?",
        heartbeat_interval: float = 8.0,
        timeout: float = 120.0,
    ) -> None:
        self.ack = ack
        self.heartbeat = heartbeat
        self.timeout_message = timeout_message
        self.heartbeat_interval = heartbeat_interval
        self.timeout = timeout

    async def run(self, work: Awaitable[str], *, emit: Emit) -> str | None:
        """Run ``work`` with spoken pacing. Returns its value, or None on timeout."""
        await emit(ProgressEvent("ack", self.ack))
        task: asyncio.Task[str] = asyncio.ensure_future(work)
        loop = asyncio.get_event_loop()
        start = loop.time()
        try:
            while True:
                elapsed = loop.time() - start
                if elapsed >= self.timeout:
                    await emit(ProgressEvent("timeout", self.timeout_message))
                    return None
                wait = min(self.heartbeat_interval, self.timeout - elapsed)
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), timeout=wait)
                except TimeoutError:
                    if loop.time() - start >= self.timeout:
                        await emit(ProgressEvent("timeout", self.timeout_message))
                        return None
                    await emit(ProgressEvent("heartbeat", self.heartbeat))
                    continue
                await emit(ProgressEvent("result", result))
                return result
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


__all__ = ["Emit", "EventKind", "ProgressEvent", "ProgressManager"]
