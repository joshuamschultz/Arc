"""Configuration for the scheduler module.

Owned by the scheduler module — not part of core config.
Loaded from ``[modules.scheduler.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from pydantic import BaseModel


class SchedulerConfig(BaseModel):
    """Scheduler module configuration."""

    enabled: bool = False
    min_interval_seconds: int = 60
    max_schedules: int = 50
    max_prompt_length: int = 500
    default_timeout_seconds: int = 300
    max_timeout_seconds: int = 3600
    circuit_breaker_threshold: int = 3
    check_interval_seconds: int = 30
    store_path: str = "schedules.json"
