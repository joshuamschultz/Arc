"""Tests for ComponentHealthChecks (SPEC-085 COMP-015, REQ-473).

``arcagent.core.health`` exposes a ``HealthRegistry`` where components
register named checks and the registry aggregates them:

  - ``registry.register(name: str, check: Callable[[], Awaitable[ComponentStatus] | ComponentStatus])``
  - ``await registry.check_all() -> dict[str, ComponentStatus]``
  - ``overall(results: dict[str, ComponentStatus]) -> str``  ("ready" | "degraded")
  - ``ComponentStatus`` carries ``healthy: bool`` + ``detail: str``.

A check that raises must be caught and reported unhealthy (fail-closed) —
never propagate out of ``check_all()``. Both sync and async check callables
are supported. Async tests run bare (``asyncio_mode = "auto"``), no
``@pytest.mark.asyncio`` decorator needed.

This module (``arcagent.core.health``) does not exist yet. Every failure
below must be an ImportError/AttributeError for the ABSENT module, not a
bug in these tests.
"""

from __future__ import annotations

import json

from arcagent.core.health import ComponentStatus, HealthRegistry, overall


def _to_plain(status: ComponentStatus) -> dict[str, object]:
    """Convert a ComponentStatus to plain data via its documented attributes."""
    return {"healthy": status.healthy, "detail": status.detail}


async def test_check_all_reports_healthy_and_unhealthy_components() -> None:
    """(a) A registered healthy check and an unhealthy check both appear,
    each with correct status + detail."""
    registry = HealthRegistry()
    registry.register("store", lambda: ComponentStatus(healthy=True, detail="connected"))
    registry.register("fleet", lambda: ComponentStatus(healthy=False, detail="NATS unreachable"))

    results = await registry.check_all()

    assert set(results.keys()) == {"store", "fleet"}
    assert results["store"].healthy is True
    assert results["store"].detail == "connected"
    assert results["fleet"].healthy is False
    assert results["fleet"].detail == "NATS unreachable"


async def test_overall_is_degraded_when_any_component_unhealthy() -> None:
    """(b) overall() is "degraded" when any component is unhealthy."""
    registry = HealthRegistry()
    registry.register("store", lambda: ComponentStatus(healthy=True, detail="ok"))
    registry.register("fleet", lambda: ComponentStatus(healthy=False, detail="down"))

    results = await registry.check_all()

    assert overall(results) == "degraded"


async def test_overall_is_ready_when_all_components_healthy() -> None:
    """(b) overall() is "ready" when every component is healthy."""
    registry = HealthRegistry()
    registry.register("store", lambda: ComponentStatus(healthy=True, detail="ok"))
    registry.register("fleet", lambda: ComponentStatus(healthy=True, detail="ok"))

    results = await registry.check_all()

    assert overall(results) == "ready"


async def test_a_raising_check_is_caught_and_reported_unhealthy() -> None:
    """(c) A check that raises is caught (fail-closed), never propagates
    out of check_all(), and shows up as unhealthy with a non-empty detail."""

    def explode() -> ComponentStatus:
        raise ValueError("boom: disk full")

    registry = HealthRegistry()
    registry.register("scheduler", explode)

    results = await registry.check_all()

    assert results["scheduler"].healthy is False
    assert isinstance(results["scheduler"].detail, str)
    assert results["scheduler"].detail != ""


async def test_raising_check_does_not_prevent_overall_from_resolving() -> None:
    """A raising check must not crash overall() either — fail-closed end to end."""

    def explode() -> ComponentStatus:
        raise RuntimeError("kaboom")

    registry = HealthRegistry()
    registry.register("scheduler", explode)

    results = await registry.check_all()

    assert overall(results) == "degraded"


async def test_sync_and_async_check_callables_are_both_supported() -> None:
    """(d) Both sync and async check callables are supported."""

    def sync_check() -> ComponentStatus:
        return ComponentStatus(healthy=True, detail="sync ok")

    async def async_check() -> ComponentStatus:
        return ComponentStatus(healthy=True, detail="async ok")

    registry = HealthRegistry()
    registry.register("sync_component", sync_check)
    registry.register("async_component", async_check)

    results = await registry.check_all()

    assert results["sync_component"].detail == "sync ok"
    assert results["async_component"].detail == "async ok"


async def test_results_are_plain_serializable_data() -> None:
    """(e) Each ComponentStatus exposes plain healthy/detail data that
    round-trips through json.dumps once converted from its attributes —
    no pydantic/SDK object required to read it."""
    registry = HealthRegistry()
    registry.register("store", lambda: ComponentStatus(healthy=True, detail="ok"))

    results = await registry.check_all()

    plain = {name: _to_plain(status) for name, status in results.items()}
    serialized = json.dumps(plain)
    assert json.loads(serialized) == plain
