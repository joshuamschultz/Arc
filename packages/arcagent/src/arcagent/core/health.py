"""Component health registry (SPEC-085 COMP-015, REQ-473).

Components register a named check with :class:`HealthRegistry`; the registry
runs every check and aggregates the results into a single readiness verdict
via :func:`overall`. This module owns the registry only — it does not know
about NATS, arcui, or any other concrete component. Callers (Supervisor,
observability) register their own checks and wire real components elsewhere.

Fail-closed: a check that raises is caught inside :meth:`HealthRegistry.check_all`
and reported as an unhealthy :class:`ComponentStatus` rather than propagating.
An unreachable component must never crash the readiness probe.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Union

from pydantic import BaseModel, ConfigDict

HealthCheck = Callable[[], Union["ComponentStatus", Awaitable["ComponentStatus"]]]


class ComponentStatus(BaseModel):
    """Result of a single component health check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    healthy: bool
    detail: str


class HealthRegistry:
    """Collects named component health checks and runs them on demand."""

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}

    def register(self, name: str, check: HealthCheck) -> None:
        """Register a health check under ``name``.

        ``check`` may be sync or async; either returning a ``ComponentStatus``.
        Registering the same name again replaces the prior check.
        """
        self._checks[name] = check

    async def check_all(self) -> dict[str, ComponentStatus]:
        """Run every registered check and return its result by name.

        Fail-closed: a check that raises is caught here and reported as
        unhealthy — the exception never propagates out of this method.
        """
        results: dict[str, ComponentStatus] = {}
        for name, check in self._checks.items():
            results[name] = await self._run_check(check)
        return results

    @staticmethod
    async def _run_check(check: HealthCheck) -> ComponentStatus:
        try:
            outcome = check()
            if isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:  # fail-closed contract: total catch, never propagate
            return ComponentStatus(healthy=False, detail=str(exc))
        return outcome


def overall(results: dict[str, ComponentStatus]) -> str:
    """Aggregate per-component results into ``"ready"`` or ``"degraded"``."""
    if all(status.healthy for status in results.values()):
        return "ready"
    return "degraded"
