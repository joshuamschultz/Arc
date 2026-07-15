"""Pulse module — periodic ambient awareness for agents.

Reads pulse.md for a check list, executes all overdue checks
via agent_run_fn. Both humans and agents can edit pulse.md.
"""

from __future__ import annotations

from pydantic import BaseModel

# --- Models ---


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


__all__ = ["PulseCheck", "PulseCheckState", "PulseState"]
