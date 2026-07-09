"""Shared fixtures for scheduler tests — SPEC-002."""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import MagicMock

from arcagent.modules.scheduler.models import ScheduleEntry


def make_config() -> MagicMock:
    """Create a mock SchedulerConfig with all required fields."""
    cfg = MagicMock()
    cfg.min_interval_seconds = 60
    cfg.max_schedules = 50
    cfg.max_prompt_length = 500
    cfg.default_timeout_seconds = 300
    cfg.max_timeout_seconds = 3600
    cfg.circuit_breaker_threshold = 3
    cfg.check_interval_seconds = 30
    cfg.store_path = "schedules.json"
    cfg.enabled = True
    return cfg


def make_entry(
    id: str = "sched_test",  # noqa: A002 - matches ScheduleEntry field
    type: Literal["cron", "interval", "once"] = "interval",  # noqa: A002 - matches field

    prompt: str = "Heartbeat",
    every_seconds: int = 300,
    enabled: bool = True,
    **kwargs: Any,
) -> ScheduleEntry:
    """Create a valid ScheduleEntry for tests."""
    return ScheduleEntry(
        id=id,
        type=type,
        prompt=prompt,
        every_seconds=every_seconds,
        enabled=enabled,
        **kwargs,
    )
