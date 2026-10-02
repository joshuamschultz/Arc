"""Pulse module — periodic ambient awareness for agents.

Reads pulse.md for a check list, executes all overdue checks
via agent_run_fn. pulse.md is operator-authored: the agent's own write/edit/bash
tools are denied (protected path, ASI01/ASI06); operators edit it through the
audited arcui file route.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from arcagent.core.control_contract import SignedControlRevision

# --- Models ---


class PulseCheck(BaseModel):
    """A single check defined in pulse.md."""

    name: str = Field(min_length=1)
    interval_minutes: int = Field(gt=0)
    action: str = Field(min_length=1)
    approval: SignedControlRevision | None = None


class PulseCheckState(BaseModel):
    """Runtime state for a single check."""

    last_run: str | None = None
    last_result: str | None = None
    consecutive_failures: int = 0
    pending_due_at: str | None = None
    pending_definition_digest: str | None = None
    last_revision: int | None = None


class PulseState(BaseModel):
    """Full pulse state persisted to pulse-state.json."""

    checks: dict[str, PulseCheckState] = {}


__all__ = ["PulseCheck", "PulseCheckState", "PulseState"]
