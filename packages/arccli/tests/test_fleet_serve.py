"""`serve_fleet_agents` — start every team agent so its inbox loop runs (MSG4).

`arc ui start --team-root` must bring the WHOLE served fleet always-on: each team
agent is loaded, started (which spawns its messaging_inbox_loop), and registered
in the shared FleetRegistry the gateway factory reuses. These tests drive that
function with fake agents (no real ArcAgent/NATS) and assert: every agent is
started + registered under its DID, LIVE-warm fires per agent, and one bad agent
is skipped without blocking the rest.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcgateway.fleet import FleetRegistry, current_fleet, set_current_fleet

from arccli.commands import _serve
from arccli.commands import ui as ui_cmd


@pytest.fixture(autouse=True)
def _clear_fleet() -> Iterator[None]:
    set_current_fleet(None)
    yield
    set_current_fleet(None)


class _FakeAgent:
    def __init__(self, did: str, *, fail: bool = False) -> None:
        self.did = did
        self._fail = fail
        self.started = False

    async def startup(self) -> None:
        if self._fail:
            raise RuntimeError("boom")
        self.started = True


def _team(tmp_path: Path, names: list[str]) -> Path:
    team_root = tmp_path / "team"
    for name in names:
        agent_dir = team_root / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "arcagent.toml").write_text(f'[agent]\nname = "{name}"\n', encoding="utf-8")
    return team_root


def _install_loader(monkeypatch: pytest.MonkeyPatch, agents: dict[str, _FakeAgent]) -> None:
    """Patch _load_arcagent to return a fake agent keyed by the dir name."""

    def fake_load(agent_dir: Path) -> tuple[Any, Any, Path]:
        return agents[agent_dir.name], None, agent_dir / "arcagent.toml"

    monkeypatch.setattr("arccli.commands.agent._common._load_arcagent", fake_load)


@pytest.mark.asyncio
async def test_starts_registers_and_warms_every_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_root = _team(tmp_path, ["josh_agent", "marketer_agent"])
    agents = {
        "josh_agent": _FakeAgent("did:arc:local:agent/josh1234"),
        "marketer_agent": _FakeAgent("did:arc:local:agent/mark5678"),
    }
    _install_loader(monkeypatch, agents)
    fleet = FleetRegistry()
    warmed: list[str] = []

    async def warm(did: str, agent: Any) -> None:
        warmed.append(did)
        assert fleet.get(did) is agent  # warm receives the SAME started instance

    count = await _serve.serve_fleet_agents(team_root, fleet, warm=warm)

    assert count == 2
    assert all(a.started for a in agents.values())  # inbox loop would be spawned
    assert set(fleet.dids()) == {
        "did:arc:local:agent/josh1234",
        "did:arc:local:agent/mark5678",
    }
    # The gateway factory can now reuse the SAME started instance for web chat.
    assert fleet.get("did:arc:local:agent/josh1234") is agents["josh_agent"]
    assert sorted(warmed) == ["did:arc:local:agent/josh1234", "did:arc:local:agent/mark5678"]


@pytest.mark.asyncio
async def test_one_bad_agent_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_root = _team(tmp_path, ["good_agent", "bad_agent"])
    agents = {
        "good_agent": _FakeAgent("did:arc:local:agent/good"),
        "bad_agent": _FakeAgent("did:arc:local:agent/bad", fail=True),
    }
    _install_loader(monkeypatch, agents)
    fleet = FleetRegistry()

    count = await _serve.serve_fleet_agents(team_root, fleet)

    assert count == 1  # only the good agent started
    assert fleet.dids() == ["did:arc:local:agent/good"]
    assert fleet.get("did:arc:local:agent/bad") is None


@pytest.mark.asyncio
async def test_warm_failure_does_not_stop_consuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_root = _team(tmp_path, ["josh_agent"])
    agents = {"josh_agent": _FakeAgent("did:arc:local:agent/josh1234")}
    _install_loader(monkeypatch, agents)
    fleet = FleetRegistry()

    async def warm(did: str) -> None:
        raise RuntimeError("registry down")

    count = await _serve.serve_fleet_agents(team_root, fleet, warm=warm)

    # LIVE-status is cosmetic; the agent is still started and in the fleet.
    assert count == 1
    assert fleet.get("did:arc:local:agent/josh1234") is agents["josh_agent"]


class TestFleetEnabled:
    def test_no_gateway_config_still_runs_fleet(self) -> None:
        assert ui_cmd._fleet_enabled(None) is True

    def test_personal_tier_runs_fleet(self) -> None:
        from arcgateway.config import GatewayConfig

        cfg = GatewayConfig.from_toml_str("[gateway]\ntier = \"personal\"\n")
        assert ui_cmd._fleet_enabled(cfg) is True

    def test_federal_tier_skips_fleet(self) -> None:
        from arcgateway.config import GatewayConfig

        cfg = GatewayConfig.from_toml_str("[gateway]\ntier = \"federal\"\n")
        assert ui_cmd._fleet_enabled(cfg) is False


class TestRegisterFleetStartup:
    @pytest.mark.asyncio
    async def test_installs_fleet_and_hook_that_serves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adopted: dict[str, Any] = {}

        class _Cache:
            def get(self, did: str) -> Any:
                return adopted.get(did)

            def put(self, did: str, agent: Any) -> None:
                adopted[did] = agent

        app = SimpleNamespace(
            state=SimpleNamespace(
                _extra_startup_hooks=[],
                embedded_agent_cache=_Cache(),
                agent_registry=None,
            )
        )
        fake_agent = SimpleNamespace(_config=None)

        seen: dict[str, Any] = {}

        async def fake_serve(team_root: Path, fleet: Any, *, warm: Any = None) -> int:
            seen["team_root"] = team_root
            seen["fleet"] = fleet
            # warm adopts the already-started instance into the executor cache
            await warm("did:arc:local:agent/x", fake_agent)
            return 3

        monkeypatch.setattr(_serve, "serve_fleet_agents", fake_serve)

        team_root = tmp_path / "team"
        fleet = ui_cmd._register_fleet_startup(app, team_root)

        assert current_fleet() is fleet  # gateway factory will reuse these instances
        assert len(app.state._extra_startup_hooks) == 1

        await app.state._extra_startup_hooks[0]()  # run the lifespan hook
        assert seen["team_root"] == team_root
        assert seen["fleet"] is fleet
        assert adopted == {"did:arc:local:agent/x": fake_agent}
