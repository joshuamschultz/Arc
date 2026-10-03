"""TeamBusObserver survives a starved/slow bus: bounded polls, quiet timeouts.

DGX evidence: the nats-server answers in ~0.2 ms, yet the poll loop hit a
5 s client-side request timeout every few minutes and logged a full traceback
each time. Those are event-loop stalls on the shared arc process, not a slow
broker, so the observer must bound each poll, log one WARNING (trace at DEBUG),
back off, and recover. Interleaving is forced with Events and an injected
``sleep`` — never real sleeps racing a clock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcui.team_stream import TeamBusObserver, TeamStreamHub


class _WS:
    pass


class _Channel:
    def __init__(self, name: str) -> None:
        self.name = name


class _Message:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self) -> dict[str, Any]:
        return dict(self._data)


class _Service:
    """First ``list_channels`` hangs forever; later calls answer normally."""

    def __init__(self, *, hang_first: bool = True, hang_always: bool = False) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._hang_first = hang_first
        self._hang_always = hang_always

    async def list_channels(self) -> list[_Channel]:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._hang_always or (self._hang_first and self.calls == 1):
                await asyncio.Event().wait()  # never set: only the poll timeout frees it
            return [_Channel("ops")]
        finally:
            self.active -= 1

    async def list_channel_messages(
        self, channel_name: str, after_seq: int, limit: int
    ) -> list[_Message]:
        if after_seq >= 1:
            return []
        return [
            _Message({"seq": 1, "sender": "agent://a", "to": ["channel://ops"], "body": "one"})
        ]


class _Sleeper:
    """Injected sleep: records each delay and wakes the test."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._slept = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self._slept.set()
        await asyncio.sleep(0)

    async def wait_for(self, count: int) -> None:
        while len(self.delays) < count:
            self._slept.clear()
            await self._slept.wait()


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_slow_poll_times_out_once_then_recovers(caplog: pytest.LogCaptureFixture) -> None:
    service = _Service()
    hub = TeamStreamHub()
    sock = _WS()
    hub.register(sock)
    sleeper = _Sleeper()
    observer = TeamBusObserver(service, hub)

    with caplog.at_level("DEBUG", logger="arcui.team_stream"):
        task = asyncio.create_task(
            observer.run(interval=1.0, poll_timeout=0.01, max_backoff=8.0, sleep=sleeper)
        )
        await sleeper.wait_for(2)
        await _stop(task)

    assert hub.queue_for(sock).get_nowait()["body"] == "one"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(r.levelname == "DEBUG" and r.exc_info for r in caplog.records)
    assert sleeper.delays[0] > 1.0  # backed off after the timeout
    assert sleeper.delays[1] == 1.0  # back to the normal cadence once healthy


async def test_backoff_grows_then_caps() -> None:
    sleeper = _Sleeper()
    observer = TeamBusObserver(_Service(hang_always=True), TeamStreamHub())
    task = asyncio.create_task(
        observer.run(interval=1.0, poll_timeout=0.01, max_backoff=4.0, sleep=sleeper)
    )
    await sleeper.wait_for(5)
    await _stop(task)
    assert sleeper.delays[:5] == [2.0, 4.0, 4.0, 4.0, 4.0]


async def test_polls_never_overlap() -> None:
    service = _Service(hang_always=True)
    sleeper = _Sleeper()
    observer = TeamBusObserver(service, TeamStreamHub())
    task = asyncio.create_task(
        observer.run(interval=0.0, poll_timeout=0.01, max_backoff=0.0, sleep=sleeper)
    )
    await sleeper.wait_for(4)
    await _stop(task)
    assert service.calls >= 3
    assert service.max_active == 1
