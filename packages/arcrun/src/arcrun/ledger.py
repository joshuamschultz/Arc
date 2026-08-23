"""Optional caller-owned exactly-once seam for external tool execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ToolExecutionIntent:
    """Durable identity and immutable input for one external tool invocation."""

    invocation_key: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments_digest: str


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """Known terminal outcome returned to a replaying run exactly once."""

    invocation_key: str
    content: str
    success: bool


@dataclass(frozen=True)
class ToolLedgerEntry:
    """State returned atomically when a caller attempts an invocation."""

    status: Literal["new", "completed", "unresolved"]
    outcome: ToolExecutionOutcome | None = None


class ToolExecutionLedger(Protocol):
    """Caller-supplied durable coordination boundary; ArcRun owns no storage."""

    async def begin(self, intent: ToolExecutionIntent) -> ToolLedgerEntry:
        """Atomically record intent or return the existing invocation state."""

    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        """Persist a known outcome after the external side effect returns."""


def tool_invocation_key(
    run_id: str, tool_call_id: str, tool_name: str, arguments: dict[str, object]
) -> str:
    """Return deterministic identity for one model-issued tool invocation."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    raw = "\x00".join((run_id, tool_call_id, tool_name, canonical)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "ToolExecutionIntent",
    "ToolExecutionLedger",
    "ToolExecutionOutcome",
    "ToolLedgerEntry",
    "tool_invocation_key",
]
