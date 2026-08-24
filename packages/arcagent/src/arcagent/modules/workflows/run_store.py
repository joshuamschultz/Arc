"""Opening the shared workflow run plane, which an agent does not own.

A workflow an agent authors is its own. A workflow RUN spans agents — the
runner is a fleet singleton and the run history is read by the dashboard and
the control plane — so the run plane belongs to the orchestration layer above
this agent. An agent alone has no run plane, and says so rather than inventing
one.
"""

from __future__ import annotations

from typing import Any


class RunStoreUnavailableError(RuntimeError):
    """No orchestration layer supplied a run plane to this agent."""


async def open_run_store(*, fleet: Any = None, opener: Any = None) -> tuple[Any, Any]:
    """Open the fleet's run plane and return it with the backend that owns it."""
    if fleet is None:
        raise RunStoreUnavailableError(
            "workflow runs are a fleet capability; this agent was not given one"
        )
    store, backend = await fleet.open_run_store(opener=opener)
    return store, backend


__all__ = ["RunStoreUnavailableError", "open_run_store"]
