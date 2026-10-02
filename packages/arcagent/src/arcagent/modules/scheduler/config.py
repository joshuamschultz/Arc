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
    # How long a breaker-tripped schedule stays off before it re-arms itself. A
    # breaker exists to stop a hot loop, not to turn an outage into a permanent
    # off switch that nobody is told about.
    breaker_rearm_seconds: int = 900
    # How far past its due time a schedule may be before it counts as missed.
    missed_fire_grace_seconds: int = 120
    # Where operator notices (breaker trip, missed fire, failure) are delivered:
    # ``platform:chat_id[:thread_id]``. Empty falls back to the schedule's own
    # ``deliver_to``; with neither the notice is logged loudly, never dropped silently.
    operator_notify_target: str = ""
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
