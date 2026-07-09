"""Pulse module — periodic ambient awareness for agents.

Reads pulse.md for a check list, executes all overdue checks
via agent_run_fn. Both humans and agents can edit pulse.md.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- Config & Models ---


class PulseConfig(BaseModel):
    """Configuration for the pulse module."""

    enabled: bool = True
    interval_seconds: int = Field(default=600, ge=10)
    pulse_file: str = "pulse.md"
    state_file: str = "pulse-state.json"
    timeout_seconds: float = Field(default=300.0, gt=0)


class PulseCheck(BaseModel):
    """A single check defined in pulse.md."""

    name: str
    interval_minutes: int
    action: str


class PulseCheckState(BaseModel):
    """Runtime state for a single check."""

    last_run: str | None = None
    last_result: str | None = None
    consecutive_failures: int = 0


class PulseState(BaseModel):
    """Full pulse state persisted to pulse-state.json."""

    checks: dict[str, PulseCheckState] = {}


__all__ = ["PulseCheck", "PulseCheckState", "PulseConfig", "PulseState"]
