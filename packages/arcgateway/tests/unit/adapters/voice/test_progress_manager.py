"""T-022 (RED) — the progress manager (SPEC-077 COMP-010, REQ-013, D-765).

Audible silence must never read as broken: an instant ack, periodic heartbeats
while a slow turn runs, then the result — or a hard-timeout failure instead of
infinite "thinking". Tests use tiny intervals so no real waiting is needed.
"""

from __future__ import annotations

import asyncio

from arcgateway.adapters.voice.ux.progress import ProgressEvent, ProgressManager


async def _collect() -> tuple[list[ProgressEvent], asyncio.Future]:
    events: list[ProgressEvent] = []

    async def emit(ev: ProgressEvent) -> None:
        events.append(ev)

    return events, emit  # type: ignore[return-value]


async def test_ack_is_emitted_before_the_result() -> None:
    events: list[ProgressEvent] = []

    async def emit(ev: ProgressEvent) -> None:
        events.append(ev)

    async def work() -> str:
        return "two meetings"

    mgr = ProgressManager(heartbeat_interval=0.02, timeout=1.0)
    result = await mgr.run(work(), emit=emit)

    assert result == "two meetings"
    assert events[0].kind == "ack"
    assert events[-1].kind == "result"
    assert events[-1].text == "two meetings"


async def test_slow_work_emits_at_least_one_heartbeat() -> None:
    events: list[ProgressEvent] = []

    async def emit(ev: ProgressEvent) -> None:
        events.append(ev)

    async def work() -> str:
        await asyncio.sleep(0.08)
        return "done"

    mgr = ProgressManager(heartbeat_interval=0.02, timeout=1.0)
    await mgr.run(work(), emit=emit)

    assert any(ev.kind == "heartbeat" for ev in events)


async def test_hung_work_times_out_instead_of_infinite_silence() -> None:
    events: list[ProgressEvent] = []

    async def emit(ev: ProgressEvent) -> None:
        events.append(ev)

    async def work() -> str:
        await asyncio.sleep(10)
        return "never"

    mgr = ProgressManager(heartbeat_interval=0.02, timeout=0.06)
    result = await mgr.run(work(), emit=emit)

    assert result is None
    assert events[-1].kind == "timeout"
