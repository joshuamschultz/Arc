"""Typed admission contract between the ArcAgent nucleus and a run owner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal, Protocol

import arcrun
from pydantic import BaseModel, ConfigDict, Field


class CanonicalRunRequest(BaseModel):
    """Every option that can change a run's effects or destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    session_key: str = Field(min_length=1)
    input_text: str
    parts: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    allowed_strategies: list[str] | None = None
    reply_target: str | None = None
    reply_label: str | None = None
    caller_did: str | None = None
    purpose: Literal["manual", "message", "schedule", "pulse", "workflow"]
    occurrence_id: str = Field(min_length=1)

    def canonical_bytes(self) -> bytes:
        """Serialize the complete effect request with stable key ordering."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()

    def digest(self) -> str:
        """Return the exact body hash an authorization must sign."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


RunInvoker = Callable[[CanonicalRunRequest], Awaitable[arcrun.RunResult]]


class AcceptedRunOwner(Protocol):
    """Injectable owner that authorizes, persists, and invokes one run."""

    async def execute(
        self,
        request: CanonicalRunRequest,
        *,
        signed_authorization: bytes,
        deadline: datetime,
        max_result_bytes: int,
        invoke: RunInvoker,
    ) -> arcrun.RunResult: ...


class RunAdmissionRefusedError(RuntimeError):
    """Signed request failed before an agent effect could start."""


class RunOutcomeUnknownError(RuntimeError):
    """An accepted run may have performed effects that require reconciliation."""
