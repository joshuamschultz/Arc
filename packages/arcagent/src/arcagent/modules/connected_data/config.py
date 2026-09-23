"""Configuration for the optional connected-data synchronization module."""

from __future__ import annotations

from pydantic import Field

from arcagent.connected_data import SyncLimits
from arcagent.core.module_config import ModuleConfig


class ConnectedDataConfig(ModuleConfig):
    """Bounded synchronization policy owned by the connected-data module."""

    # Hourly. A minute was a per-agent number that became a fleet number: every
    # agent granted a connection polls it independently, so six agents sharing
    # one account meant six provider round trips a minute, and enough of them
    # failed under that load to keep the source permanently marked failed. A
    # document store does not change fast enough to be worth that.
    interval_seconds: float = Field(default=3600.0, gt=0)
    global_concurrency: int = Field(default=4, gt=0)
    limits: SyncLimits = Field(default_factory=SyncLimits)
    # A run that crashed or hung is retried after this, doubling per consecutive
    # failure up to the cap: soon enough to recover from a blip, never a spin.
    restart_backoff_seconds: float = Field(default=30.0, gt=0)
    restart_backoff_max_seconds: float = Field(default=1800.0, gt=0)
    # A run still going this long past its own time bound is stuck on a call
    # that never returns; it is cancelled, audited and retried.
    stall_grace_seconds: float = Field(default=120.0, gt=0)


__all__ = ["ConnectedDataConfig"]
