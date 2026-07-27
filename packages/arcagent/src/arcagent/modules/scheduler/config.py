"""Configuration for the scheduler module.

Owned by the scheduler module — not part of core config.
Loaded from ``[modules.scheduler.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class SchedulerConfig(ModuleConfig):
    """Scheduler module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    enabled: bool = False
    min_interval_seconds: int = 60
    max_schedules: int = 50
    max_prompt_length: int = 500
    default_timeout_seconds: int = 300
    max_timeout_seconds: int = 3600
    circuit_breaker_threshold: int = 3
    check_interval_seconds: int = 30
    store_path: str = "schedules.json"
    # IANA timezone that cron ("0 8 * * *") and one-time ("at") schedules are
    # evaluated in, so "8am" means 8am local, not 8am UTC. Empty = the server's
    # local timezone (set the machine's tz to control it). Interval schedules are
    # duration-based and timezone-independent.
    timezone: str = ""

    def validation_context(self) -> dict[str, int]:
        """Limits threaded into ScheduleEntry validation so operator config
        (not hardcoded defaults) decides the interval floor and timeout ceiling.
        """
        return {
            "min_interval_seconds": self.min_interval_seconds,
            "max_timeout_seconds": self.max_timeout_seconds,
        }
