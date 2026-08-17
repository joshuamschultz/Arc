"""The host boundary — the only way a dynamic script reaches the outside world.

A dynamic script is model-authored, so it is untrusted input by construction.
What keeps that safe is not the interpreter alone but the narrowness of this
surface: the grammar forbids imports, attribute access and arbitrary calls, so
the *only* effects a script can produce are the ones named on
:class:`ScriptHost`. Auditing "what can a generated script do" is therefore
reading one Protocol rather than reasoning about a language.

Nothing here performs I/O. ``dynamic/`` defines the contract; the strategy binds
it to real child runs, and :mod:`arcrun.dynamic.validate` binds it to stubs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

MAX_PARALLEL = 64
"""Jobs accepted by one ``parallel()`` call. Refused before any spawn."""

MAX_HOST_CALLS = 1_000
"""Result-bearing host calls per run — the journal's hard ceiling."""

DEFAULT_AGENT_CALLS = 32
"""Child runs a script may start when the caller names no budget."""

MAX_AGENT_CALLS = 256
"""Ceiling a caller may raise the agent-call budget to."""

PauseKind = Literal["user", "verification", "no_progress", "infra"]
"""Why a script stopped short. Each maps to a distinct resumable state."""


class HostError(Exception):
    """Base for every failure raised across the host boundary."""


class AgentQuotaExceeded(HostError):  # noqa: N818 — domain convention: named for the condition
    """The script asked for more child runs than its budget allows."""


class BudgetExceeded(HostError):  # noqa: N818 — domain convention: named for the condition
    """A token or cost ceiling tripped. Terminal — never catchable in script."""


class Cancelled(HostError):  # noqa: N818 — domain convention: named for the condition
    """An operator cancelled the run. Terminal — never catchable in script."""


class HostFailure(HostError):  # noqa: N818 — domain convention: named for the condition
    """The host could not service a call for a reason the script cannot fix."""


TERMINAL_ERRORS = (BudgetExceeded, Cancelled)
"""Failures that end the run outright rather than becoming a script value.

Deliberately *not* journaled for the call in flight: a run resumed after the
operator raises a budget must re-issue that exact call live, not replay a
canned failure and fail again forever.
"""


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One child run a script has asked for.

    ``capability_mode`` filters the child's tools down from the parent's frozen
    registry; it can only ever narrow. ``output_schema`` makes the child's
    result a validated object instead of prose.
    """

    prompt: str
    label: str = ""
    capability_mode: str = "all"
    output_schema: dict[str, Any] | None = None
    max_turns: int | None = None
    phase: str = ""


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """What a child run produced, as the script sees it.

    A child that fails is an ordinary value with ``success=False``, never an
    exception: partial failure is something the script decides about, not
    something that unwinds it.
    """

    agent_id: str
    success: bool
    output: Any
    cancelled: bool = False
    tokens_used: int = 0
    error: str = ""

    def to_value(self) -> dict[str, Any]:
        """Render as the plain mapping the script namespace exposes."""
        return {
            "agent_id": self.agent_id,
            "success": self.success,
            "output": self.output,
            "cancelled": self.cancelled,
            "tokens_used": self.tokens_used,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BudgetState:
    """What the script learns when it calls ``budget()``."""

    agent_calls_spent: int = 0
    agent_calls_total: int = 0
    tokens_used: int = 0

    def to_value(self) -> dict[str, Any]:
        """Render as the plain mapping the script namespace exposes."""
        return {
            "agent_calls_spent": self.agent_calls_spent,
            "agent_calls_total": self.agent_calls_total,
            "agent_calls_remaining": max(0, self.agent_calls_total - self.agent_calls_spent),
            "tokens_used": self.tokens_used,
        }


class ScriptHost(Protocol):
    """Every effect a dynamic script can have. There is no other surface."""

    async def spawn(self, spec: AgentSpec) -> AgentOutcome:
        """Run one child agent to completion and return its outcome."""
        ...

    async def spawn_many(self, specs: list[AgentSpec]) -> list[AgentOutcome]:
        """Run a batch concurrently; outcomes come back in submission order."""
        ...

    def phase(self, title: str) -> None:
        """Name the stage now starting, for progress display only."""
        ...

    def log(self, message: str) -> None:
        """Emit a progress line to the operator."""
        ...

    def budget(self) -> BudgetState:
        """Report spend so a script can scale its own depth."""
        ...

    def scratch_write(self, name: str, content: str) -> str:
        """Persist run-scoped working text; returns the path written."""
        ...

    def scratch_read(self, name: str) -> str:
        """Read back run-scoped working text."""
        ...


@dataclass(slots=True)
class ScriptOutcome:
    """How a script run ended.

    ``paused`` is the only non-terminal state: it carries a reason and expects
    the run to resume from its journal once the reason is resolved.
    """

    status: Literal["completed", "paused", "budget_exceeded", "cancelled", "failed"]
    result: Any = None
    kind: str = ""
    message: str = ""
    error: str = ""
    phases_seen: list[str] = field(default_factory=list)
    agent_calls: int = 0

    @property
    def is_resumable(self) -> bool:
        """True when resuming is meaningful rather than a fresh start."""
        return self.status in ("paused", "budget_exceeded")


__all__ = [
    "DEFAULT_AGENT_CALLS",
    "MAX_AGENT_CALLS",
    "MAX_HOST_CALLS",
    "MAX_PARALLEL",
    "TERMINAL_ERRORS",
    "AgentOutcome",
    "AgentQuotaExceeded",
    "AgentSpec",
    "BudgetExceeded",
    "BudgetState",
    "Cancelled",
    "HostError",
    "HostFailure",
    "PauseKind",
    "ScriptHost",
    "ScriptOutcome",
]
