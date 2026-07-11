"""FleetRegistry + the factory's always-on reuse (MSG4).

The embedded gateway factory must reuse an always-on agent instead of building a
second ArcAgent for the same DID — otherwise a web-chatted agent opens a second
durable NATS consumer that competes with the always-on inbox loop. These tests
prove the factory short-circuits to the fleet instance (one instance per agent)
and falls through to the normal load path when no fleet is running.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from arcgateway.bootstrap import _make_agent_factory
from arcgateway.fleet import FleetRegistry, current_fleet, set_current_fleet


@pytest.fixture(autouse=True)
def _clear_fleet() -> Iterator[None]:
    """Every test starts and ends with no process fleet installed."""
    set_current_fleet(None)
    yield
    set_current_fleet(None)


class TestFleetRegistry:
    def test_add_get_roundtrip(self) -> None:
        fleet = FleetRegistry()
        sentinel = object()
        fleet.add("did:arc:local:agent/a1", sentinel)
        assert fleet.get("did:arc:local:agent/a1") is sentinel
        assert fleet.get("did:arc:local:agent/missing") is None
        assert fleet.dids() == ["did:arc:local:agent/a1"]
        assert len(fleet) == 1

    def test_current_fleet_set_and_clear(self) -> None:
        assert current_fleet() is None
        fleet = FleetRegistry()
        set_current_fleet(fleet)
        assert current_fleet() is fleet
        set_current_fleet(None)
        assert current_fleet() is None


class TestFactoryReuse:
    async def test_factory_returns_fleet_instance_without_rebuilding(self, tmp_path: Path) -> None:
        """A DID in the fleet is returned as-is — the same instance every call,
        never a second ArcAgent (one durable consumer per agent)."""
        fleet = FleetRegistry()
        sentinel = object()
        fleet.add("did:arc:local:agent/marketer", sentinel)
        set_current_fleet(fleet)

        factory = _make_agent_factory(tmp_path / "team")
        first = await factory("did:arc:local:agent/marketer")
        second = await factory("did:arc:local:agent/marketer")
        assert first is sentinel
        assert second is sentinel

    async def test_factory_falls_through_when_did_not_in_fleet(self, tmp_path: Path) -> None:
        """A DID absent from the fleet takes the normal load path — which, with an
        empty team_root, resolves no agent dir and raises (proving no short-circuit)."""
        set_current_fleet(FleetRegistry())  # empty fleet
        team_root = tmp_path / "team"
        team_root.mkdir()
        factory = _make_agent_factory(team_root)
        with pytest.raises(FileNotFoundError):
            await factory("did:arc:local:agent/unknown")
