"""schedule:failed (and missed/re-armed) must have a subscriber that reaches the operator."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from arcagent.modules.scheduler import _runtime
from arcagent.modules.scheduler.capabilities import (
    notify_operator_of_failure,
    notify_operator_of_missed_fire,
)
from arcagent.modules.scheduler.config import SchedulerConfig


class _Deliver:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, target: str, text: str) -> None:
        self.sent.append((target, text))


def _configure(tmp_path: Path, **config: object) -> _Deliver:
    _runtime.reset()
    _runtime.configure(
        config=SchedulerConfig(enabled=True, **config),  # type: ignore[arg-type]
        telemetry=MagicMock(),
        workspace=tmp_path,
    )
    deliver = _Deliver()
    _runtime.state().channel_deliver_fn = deliver
    return deliver


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    yield
    _runtime.reset()


@pytest.mark.asyncio
async def test_a_breaker_trip_reaches_the_operator_with_the_reason(tmp_path: Path) -> None:
    deliver = _configure(tmp_path, operator_notify_target="telegram:42")
    ctx = SimpleNamespace(
        data={
            "schedule_name": "workflow:nightly-meeting-ingest",
            "error": "fence is no longer current",
            "consecutive_failures": 3,
            "breaker_tripped": True,
        }
    )

    await notify_operator_of_failure(ctx)

    assert len(deliver.sent) == 1
    target, text = deliver.sent[0]
    assert target == "telegram:42"
    assert "nightly-meeting-ingest" in text and "switched off" in text
    assert "fence is no longer current" in text


@pytest.mark.asyncio
async def test_a_missed_fire_reaches_the_operator(tmp_path: Path) -> None:
    deliver = _configure(tmp_path, operator_notify_target="telegram:42")
    ctx = SimpleNamespace(
        data={"schedule_name": "morning", "late_seconds": 5000, "due_at": "2026-10-01T03:00:00Z"}
    )

    await notify_operator_of_missed_fire(ctx)

    assert "5000s past due" in deliver.sent[0][1]


@pytest.mark.asyncio
async def test_notice_falls_back_to_the_schedules_own_target(tmp_path: Path) -> None:
    deliver = _configure(tmp_path)

    await notify_operator_of_failure(
        SimpleNamespace(data={"schedule_name": "x", "error": "boom", "deliver_to": "telegram:7"})
    )

    assert deliver.sent[0][0] == "telegram:7"


@pytest.mark.asyncio
async def test_no_delivery_path_does_not_raise(tmp_path: Path) -> None:
    _configure(tmp_path)
    _runtime.state().channel_deliver_fn = None

    await notify_operator_of_failure(SimpleNamespace(data={"error": "boom"}))
