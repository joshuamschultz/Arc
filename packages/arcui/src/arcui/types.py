"""Multi-agent UI event and control types.

Flat Pydantic models for events flowing through the UI (from any layer, any
agent), control messages (browser → agent), and agent registration.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class UIEvent(BaseModel):
    """Flat event envelope for all UI telemetry.

    The ``layer`` field enables filtering. The ``sequence`` field enables
    gap detection on reconnect.
    """

    layer: Literal["llm", "run", "agent", "team"]
    event_type: str
    agent_id: str
    agent_name: str
    source_id: str
    timestamp: str
    data: dict[str, Any]
    sequence: int = Field(ge=0)


class ControlMessage(BaseModel):
    """Command sent from browser/operator to a specific agent."""

    action: Literal["steer", "cancel", "config", "ping", "shutdown"]
    target: str
    data: dict[str, Any]
    request_id: str


class ControlResponse(BaseModel):
    """Agent's response to a ControlMessage."""

    request_id: str
    status: str
    data: dict[str, Any]


class AgentRegistration(BaseModel):
    """Identity and capabilities sent by an agent on connect."""

    agent_id: str
    agent_name: str
    model: str
    provider: str
    team: str | None = None
    tools: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    workspace: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    connected_at: str
    last_event_at: str | None = None
    sequence: int = 0
