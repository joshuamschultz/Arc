"""Pulse module configuration."""

from __future__ import annotations

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


class PulseConfig(ModuleConfig):
    """Configuration for the pulse module."""

    enabled: bool = True
    interval_seconds: int = Field(default=600, ge=10)
    pulse_file: str = "pulse.md"
    state_file: str = "pulse-state.json"
    timeout_seconds: float = Field(default=300.0, gt=0)
