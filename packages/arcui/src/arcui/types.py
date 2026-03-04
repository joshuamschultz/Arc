"""Multi-agent UI event and control types.

Flat Pydantic models for events flowing through the UI (from any layer, any
agent), control messages (browser → agent), and agent registration.
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Max sizes for DoS prevention
_MAX_EVENT_DATA_SIZE = 65_536  # 64 KB
_MAX_CONTROL_DATA_SIZE = 4_096  # 4 KB
_MAX_TOOLS_COUNT = 100
_MAX_MODULES_COUNT = 100


class UIEvent(BaseModel):
    """Flat event envelope for all UI telemetry.

    The ``layer`` field enables filtering. The ``sequence`` field enables
    gap detection on reconnect.
    """

    layer: Literal["llm", "run", "agent", "team"]
    event_type: str = Field(max_length=64, pattern=r"^[a-z_]+$")
    agent_id: str
    agent_name: str
    source_id: str
    timestamp: str
    data: dict[str, Any]
    sequence: int = Field(ge=0)

    @field_validator("data")
    @classmethod
    def _validate_data_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Reject event data payloads larger than 64KB."""
        size = sys.getsizeof(str(v))
        if size > _MAX_EVENT_DATA_SIZE:
            msg = f"Event data too large ({size} bytes, max {_MAX_EVENT_DATA_SIZE})"
            raise ValueError(msg)
        return v


class ControlMessage(BaseModel):
    """Command sent from browser/operator to a specific agent."""

    action: Literal["steer", "cancel", "config", "ping", "shutdown"]
    target: str
    data: dict[str, Any]
    request_id: str

    @field_validator("data")
    @classmethod
    def _validate_data_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Reject control data payloads larger than 4KB."""
        size = sys.getsizeof(str(v))
        if size > _MAX_CONTROL_DATA_SIZE:
            msg = f"Control data too large ({size} bytes, max {_MAX_CONTROL_DATA_SIZE})"
            raise ValueError(msg)
        return v


class ControlResponse(BaseModel):
    """Agent's response to a ControlMessage."""

    request_id: str
    status: str
    data: dict[str, Any]


class AgentRegistration(BaseModel):
    """Identity and capabilities sent by an agent on connect."""

    agent_id: str
    agent_name: str = Field(max_length=256)
    model: str
    provider: str
    team: str | None = None
    tools: list[str] = Field(default_factory=list, max_length=_MAX_TOOLS_COUNT)
    modules: list[str] = Field(default_factory=list, max_length=_MAX_MODULES_COUNT)
    workspace: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    connected_at: str
    last_event_at: str | None = None
    sequence: int = 0
