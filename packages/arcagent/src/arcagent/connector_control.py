"""Typed, process-local control contract for live connector reconciliation.

Connection grants are durable deployment state.  A running agent is an optional
in-process projection of that state, so management surfaces may ask a control
owner to refresh it after a mutation without making persistence depend on a
particular gateway or UI process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ConnectorReconcileResult:
    """The connector tools visible to one agent after a reconciliation."""

    status: Literal["applied", "activation_pending"]
    agent: str = ""
    revision: int = 0
    tools: tuple[str, ...] = ()
    detail: str = ""


class ConnectorControl(Protocol):
    """Locate and reconcile a live agent in this process.

    ``None`` means the named agent is not owned by this process.  Callers map
    exactly that answer to ``activation_pending``; a present in-process agent
    always returns a concrete result, including when its connector module has
    no tools enabled.
    """

    async def reconcile(self, agent: str) -> ConnectorReconcileResult | None: ...


__all__ = ["ConnectorControl", "ConnectorReconcileResult"]
