"""P14-A scheduler contract: C1 enqueue-don't-await, C2 disabled_reason + re-arm, C4 row status.

These drive the real engine over a real ``ScheduleStore`` on disk. Only the
signed dispatch edge is replaced, so every assertion is about the engine's own
bookkeeping.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry, ScheduleMetadata
from arcagent.modules.scheduler.occurrence import next_fire_at
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore


class _Bus:
    """Records every event the engine emits."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [data for name, data in self.events if name == event]


def _engine(
    tmp_path: Path,
    dispatch: Any,
    *,
    config: SchedulerConfig | None = None,
    entry: ScheduleEntry | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SchedulerEngine, ScheduleStore, _Bus]:
    store = ScheduleStore(tmp_path / "schedules.json")
    store.add(
        entry or ScheduleEntry(id="s1", type="interval", prompt="Check status", every_seconds=60)
    )
    bus = _Bus()

    async def run_fn(*args: Any, **kwargs: Any) -> str:
        return "unused"

    engine = SchedulerEngine(
        store,
        config or SchedulerConfig(enabled=True),
        None,
        run_fn,
        bus=bus,  # type: ignore[arg-type]
    )

    async def _dispatch(_self: SchedulerEngine, current: ScheduleEntry) -> Any:
        return await dispatch(current)

    monkeypatch.setattr(SchedulerEngine, "_dispatch", _dispatch)
    return engine, store, bus


# --- C1: a start returns within a bound; timeout_seconds never covers execution ----


@pytest.mark.asyncio
async def test_enqueue_does_not_await_a_slow_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def slow(_: ScheduleEntry) -> str:
        await release.wait()
        return "done"

    engine, store, _ = _engine(tmp_path, slow, monkeypatch=monkeypatch)

    began = time.monotonic()
    await engine._tick()
    assert time.monotonic() - began < 0.5, "the tick must return while the start is still slow"
    assert engine.in_flight == {"s1"}
    assert store.get("s1").metadata.run_count == 0  # type: ignore[union-attr]

    release.set()
    await engine.drain()
    done = store.get("s1")
    assert done is not None and done.metadata.run_count == 1
    assert engine.in_flight == set()


@pytest.mark.asyncio
async def test_timeout_seconds_never_covers_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = ScheduleEntry(
        id="s1", type="interval", prompt="Check status", every_seconds=60, timeout_seconds=1
    )

    async def takes_two_seconds(_: ScheduleEntry) -> str:
        await asyncio.sleep(2.0)
        return "finished"

    engine, store, _ = _engine(tmp_path, takes_two_seconds, entry=entry, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    done = store.get("s1")
    assert done is not None
    assert done.metadata.last_outcome == "ok", "a 2s run must not be cut by timeout_seconds=1"
    assert done.metadata.run_count == 1


# --- C4: per-row status ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_completed_firing_records_its_own_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(_: ScheduleEntry) -> str:
        return "done"

    engine, store, _ = _engine(tmp_path, ok, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    meta = store.get("s1").metadata  # type: ignore[union-attr]
    assert meta.last_outcome == "ok"
    assert meta.last_error is None
    assert meta.last_fired_at is not None
    assert meta.next_fire_at is not None
    assert datetime.fromisoformat(meta.next_fire_at) > datetime.fromisoformat(meta.last_fired_at)


@pytest.mark.asyncio
async def test_a_failed_firing_records_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_: ScheduleEntry) -> str:
        raise RuntimeError("fence is no longer current")

    engine, store, _ = _engine(tmp_path, boom, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    meta = store.get("s1").metadata  # type: ignore[union-attr]
    assert meta.last_outcome == "error"
    assert "fence is no longer current" in (meta.last_error or "")


@pytest.mark.asyncio
async def test_an_unavailable_start_is_recorded_but_is_not_a_breaker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.core.run_contract import RunAdmissionUnavailableError

    async def refused(_: ScheduleEntry) -> str:
        raise RunAdmissionUnavailableError("runner busy")

    engine, store, _ = _engine(tmp_path, refused, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    row = store.get("s1")
    assert row is not None
    assert row.metadata.last_outcome == "start_unavailable"
    assert row.metadata.consecutive_failures == 0
    assert row.enabled is True


def test_next_fire_at_for_a_cron_row_is_the_next_slot() -> None:
    entry = ScheduleEntry(
        id="c",
        type="cron",
        prompt="Nightly",
        expression="0 22 * * *",
        timezone="America/Chicago",
        metadata=ScheduleMetadata(last_run="2026-10-01T03:00:00+00:00"),
    )

    following = next_fire_at(entry, datetime(2026, 10, 1, 12, 0, tzinfo=UTC))

    assert following == datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 22:00 CDT


def test_next_fire_at_is_none_for_a_disabled_row() -> None:
    entry = ScheduleEntry(id="c", type="interval", prompt="x", every_seconds=60, enabled=False)
    assert next_fire_at(entry, datetime.now(UTC)) is None


# --- C4: a missed-fire signal ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_due_row_that_cannot_fire_raises_one_missed_fire_signal(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path / "schedules.json")
    long_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    store.add(
        ScheduleEntry(
            id="s1",
            type="interval",
            prompt="Check status",
            every_seconds=60,
            metadata=ScheduleMetadata(last_run=long_ago),
        )
    )
    bus = _Bus()
    engine = SchedulerEngine(store, SchedulerConfig(enabled=True), None, None, bus=bus)  # type: ignore[arg-type]

    await engine._tick()
    await engine._tick()
    await engine.drain()

    missed = bus.named("schedule:missed")
    assert len(missed) == 1, "one alarm per missed slot, not one per tick"
    assert missed[0]["schedule_id"] == "s1"
    assert missed[0]["late_seconds"] > 3000
    assert store.get("s1").metadata.last_outcome == "missed"  # type: ignore[union-attr]


# --- C2: disabled_reason, notice, auto re-arm ------------------------------------------


@pytest.mark.asyncio
async def test_breaker_trip_records_reason_and_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_: ScheduleEntry) -> str:
        raise RuntimeError("provider down")

    config = SchedulerConfig(enabled=True, circuit_breaker_threshold=1)
    engine, store, bus = _engine(tmp_path, boom, config=config, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    row = store.get("s1")
    assert row is not None
    assert row.enabled is False
    assert row.metadata.disabled_reason == "breaker"
    assert row.metadata.disabled_at is not None
    failed = bus.named("schedule:failed")
    assert failed and failed[-1]["breaker_tripped"] is True
    assert "provider down" in failed[-1]["error"]


@pytest.mark.asyncio
async def test_a_tripped_breaker_re_arms_itself_after_the_cool_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(_: ScheduleEntry) -> str:
        return "done"

    config = SchedulerConfig(enabled=True, breaker_rearm_seconds=60)
    tripped_at = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    entry = ScheduleEntry(
        id="s1",
        type="interval",
        prompt="Check status",
        every_seconds=60,
        enabled=False,
        metadata=ScheduleMetadata(
            disabled_reason="breaker", disabled_at=tripped_at, consecutive_failures=3
        ),
    )
    engine, store, bus = _engine(tmp_path, ok, config=config, entry=entry, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    row = store.get("s1")
    assert row is not None
    assert row.enabled is True
    assert row.metadata.disabled_reason is None
    assert row.metadata.consecutive_failures == 0
    assert bus.named("schedule:rearmed")


@pytest.mark.asyncio
async def test_an_operator_disabled_row_is_never_re_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(_: ScheduleEntry) -> str:
        return "done"

    config = SchedulerConfig(enabled=True, breaker_rearm_seconds=1)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    entry = ScheduleEntry(
        id="s1",
        type="interval",
        prompt="Check status",
        every_seconds=60,
        enabled=False,
        metadata=ScheduleMetadata(disabled_reason="operator", disabled_at=old),
    )
    engine, store, _ = _engine(tmp_path, ok, config=config, entry=entry, monkeypatch=monkeypatch)

    await engine._tick()
    await engine.drain()

    assert store.get("s1").enabled is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_breaker_that_is_still_cooling_off_stays_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(_: ScheduleEntry) -> str:
        return "done"

    config = SchedulerConfig(enabled=True, breaker_rearm_seconds=3600)
    entry = ScheduleEntry(
        id="s1",
        type="interval",
        prompt="Check status",
        every_seconds=60,
        enabled=False,
        metadata=ScheduleMetadata(
            disabled_reason="breaker", disabled_at=datetime.now(UTC).isoformat()
        ),
    )
    engine, store, _ = _engine(tmp_path, ok, config=config, entry=entry, monkeypatch=monkeypatch)

    await engine._tick()

    assert store.get("s1").enabled is False  # type: ignore[union-attr]
