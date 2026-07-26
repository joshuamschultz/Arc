"""Unit tests for the attach-or-serve resolver (serve.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arctui.serve import (
    Endpoint,
    GatewayNeedsTokenError,
    GatewaySpawnError,
    ensure_gateway,
    read_persisted_token,
    write_persisted_token,
)


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_attach_when_reachable_with_explicit_token() -> None:
    ep = await ensure_gateway(
        agent_id="employee",
        team_root=Path("/team"),
        token="explicit",
        probe=lambda _url: True,
        spawn=lambda *a, **k: pytest.fail("must not spawn when reachable"),
        read_token=lambda: None,
        sleep=_noop_sleep,
    )
    assert ep == Endpoint(
        base_url="http://127.0.0.1:8420",
        token="explicit",
        agent_id="employee",
        spawned=False,
        process=None,
    )


@pytest.mark.asyncio
async def test_attach_reachable_falls_back_to_persisted_token() -> None:
    ep = await ensure_gateway(
        agent_id="employee",
        team_root=Path("/team"),
        token=None,
        probe=lambda _url: True,
        spawn=lambda *a, **k: pytest.fail("must not spawn"),
        read_token=lambda: "from-file",
        sleep=_noop_sleep,
    )
    assert ep.token == "from-file"
    assert ep.spawned is False


@pytest.mark.asyncio
async def test_reachable_but_no_token_anywhere_raises_needs_token() -> None:
    with pytest.raises(GatewayNeedsTokenError):
        await ensure_gateway(
            agent_id="employee",
            team_root=Path("/team"),
            token=None,
            probe=lambda _url: True,
            spawn=lambda *a, **k: pytest.fail("must not spawn"),
            read_token=lambda: None,
            sleep=_noop_sleep,
        )


@pytest.mark.asyncio
async def test_spawns_when_unreachable_then_attaches_when_healthy() -> None:
    # Not healthy on the first two probes, healthy on the third.
    health = iter([False, False, True])
    spawn_calls: list[tuple] = []

    def _spawn(team_root: Path, host: str, port: int, token: str) -> str:
        spawn_calls.append((team_root, host, port, token))
        return "PROC"

    ep = await ensure_gateway(
        agent_id="employee",
        team_root=Path("/team"),
        token=None,
        probe=lambda _url: next(health),
        spawn=_spawn,
        read_token=lambda: None,
        mint=lambda: "minted",
        sleep=_noop_sleep,
        wait_timeout=10.0,
        poll_interval=0.1,
    )
    assert ep.spawned is True
    assert ep.token == "minted"
    assert ep.process == "PROC"
    # Spawned exactly once, with the minted token + resolved team_root/host/port.
    assert spawn_calls == [(Path("/team"), "127.0.0.1", 8420, "minted")]


@pytest.mark.asyncio
async def test_spawn_never_healthy_raises_spawn_error() -> None:
    with pytest.raises(GatewaySpawnError):
        await ensure_gateway(
            agent_id="employee",
            team_root=Path("/team"),
            token=None,
            probe=lambda _url: False,
            spawn=lambda *a, **k: "PROC",
            read_token=lambda: None,
            mint=lambda: "minted",
            sleep=_noop_sleep,
            wait_timeout=0.3,
            poll_interval=0.1,
        )


@pytest.mark.asyncio
async def test_custom_host_port_shape_base_url() -> None:
    ep = await ensure_gateway(
        agent_id="coder",
        team_root=Path("/team"),
        host="192.0.2.9",
        port=9001,
        token="t",
        probe=lambda _url: True,
        read_token=lambda: None,
        sleep=_noop_sleep,
    )
    assert ep.base_url == "http://192.0.2.9:9001"


def test_persisted_token_roundtrip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "viewer-token"
    write_persisted_token("secret-tok", path=path)
    assert read_persisted_token(path=path) == "secret-tok"
    # 0600 — readable only by owner (a token on disk).
    assert (path.stat().st_mode & 0o777) == 0o600


def test_read_persisted_token_missing_returns_none(tmp_path: Path) -> None:
    assert read_persisted_token(path=tmp_path / "nope") is None
