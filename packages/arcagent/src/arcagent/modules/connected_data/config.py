"""Configuration for the optional connected-data synchronization module."""

from __future__ import annotations

from pydantic import Field

from arcagent.connected_data import SyncLimits
from arcagent.core.module_config import ModuleConfig


class ConnectedDataConfig(ModuleConfig):
    """Bounded synchronization policy owned by the connected-data module."""

    interval_seconds: float = Field(default=60.0, gt=0)
    global_concurrency: int = Field(default=4, gt=0)
    limits: SyncLimits = Field(default_factory=SyncLimits)


__all__ = ["ConnectedDataConfig"]
