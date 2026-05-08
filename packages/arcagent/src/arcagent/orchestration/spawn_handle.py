"""Spawn result and spec dataclasses.

Sibling of ``arcagent.orchestration.spawn``. Owns the structured return
type returned by ``spawn()`` plus the declarative spec consumed by
``spawn_many()``. Both are pure data — lifted out so they can be
imported without dragging in the spawn orchestrator's transitive
dependency tree.

Re-exported through ``arcagent.orchestration.spawn`` and
``arcagent.orchestration`` so existing imports
(``from arcagent.orchestration.spawn import SpawnResult, SpawnSpec``,
 ``from arcagent.orchestration import SpawnResult``) keep working
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from arcrun.state import RunState
from arcrun.types import SandboxConfig, Tool
from pydantic import BaseModel

from arcagent.orchestration.token_budget import TokenUsage

# Sensible default for spawn wall-clock timeout
_DEFAULT_SPAWN_TIMEOUT_SECONDS = 300

_SpawnStatus = Literal[
    "completed",
    "max_iterations",
    "timeout",
    "interrupted",
    "error",
    "budget_exhausted",
]


class SpawnResult(BaseModel):
    """Structured result returned by a spawned child run.

    Attributes:
        child_run_id: UUID of the child run (for audit correlation).
        child_did: DID of the child identity used.
        status: Terminal status of the child run.
        summary: Natural-language summary (passed back to the LLM).
        tokens: Token usage for the child run.
        tool_trace: Ordered list of tool names the child invoked.
        audit_chain_tip: SHA-256 hex of the last audit log entry (tamper-evidence).
        duration_s: Wall-clock seconds the child ran.
        error: Error message if status is not "completed". None otherwise.
    """

    child_run_id: str
    child_did: str
    status: _SpawnStatus
    summary: str
    tokens: TokenUsage
    tool_trace: list[str]
    audit_chain_tip: str
    duration_s: float
    error: str | None = None


@dataclass
class SpawnSpec:
    """Declarative specification for a single child spawn.

    Used with spawn_many() to spawn multiple children in parallel.
    """

    task: str
    tools: list[Tool]
    system_prompt: str
    parent_state: RunState
    child_did: str
    child_sk_bytes: bytes
    wallclock_timeout_s: float = _DEFAULT_SPAWN_TIMEOUT_SECONDS
    model: Any = None
    token_budget: int | None = None
    context: str | None = None
    max_turns: int = 25
    sandbox: SandboxConfig | None = None
