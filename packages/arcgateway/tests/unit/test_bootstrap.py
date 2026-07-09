"""Tests for arcgateway.bootstrap.build_for_embedded.

The composition root is a thin wiring layer. The gateway core knows only the
core ``web`` adapter; every remote platform loads through the generic
adapter-plugin registry into the ``adapters`` tuple. These tests focus on that
delegation and on executor selection by tier — they never assert
platform-specific slots, because the core names no platform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgateway.bootstrap import (
    EmbeddedGateway,
    build_for_embedded,
)
from arcgateway.config import GatewayConfig
from arcgateway.executor import AsyncioExecutor


@pytest.fixture
def empty_team_root(tmp_path: Path) -> Path:
    return tmp_path / "team"


def _config(toml: str) -> GatewayConfig:
    return GatewayConfig.from_toml_str(toml)


# ── Build cases ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_for_embedded_web_only(empty_team_root: Path) -> None:
    """With only [platforms.web].enabled, web is set and no remote adapters load."""
    cfg = _config(
        """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert isinstance(bundle, EmbeddedGateway)
    assert bundle.web_adapter is not None
    assert bundle.web_adapter.name == "web"
    assert bundle.adapters == ()
    assert isinstance(bundle.executor, AsyncioExecutor)


@pytest.mark.asyncio
async def test_build_for_embedded_no_platforms(empty_team_root: Path) -> None:
    """An empty config still yields a usable executor + session_router."""
    cfg = _config("")
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.web_adapter is None
    assert bundle.adapters == ()
    assert isinstance(bundle.executor, AsyncioExecutor)


@pytest.mark.asyncio
async def test_build_for_embedded_federal_tier_uses_subprocess_executor(
    empty_team_root: Path,
) -> None:
    """Federal tier swaps in SubprocessExecutor for OS-level isolation."""
    cfg = _config(
        """
[gateway]
tier = "federal"
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert type(bundle.executor).__name__ == "SubprocessExecutor"


@pytest.mark.asyncio
async def test_build_for_embedded_remote_skipped_without_credentials(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled remote platform whose token is absent is skipped at personal tier.

    Uses telegram as a concrete installed plugin; the registry-skip path is the
    same for any platform whose build() raises AdapterUnavailableError.
    """
    pytest.importorskip("arcgateway_telegram")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = _config(
        """
[platforms.telegram]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert not any(a.name == "telegram" for a in bundle.adapters)


@pytest.mark.asyncio
async def test_build_for_embedded_remote_loads_into_adapters_tuple(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled remote platform with credentials lands in the adapters tuple."""
    pytest.importorskip("arcgateway_telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:test-token")
    cfg = _config(
        """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true

[platforms.telegram]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.web_adapter is not None
    assert [a.name for a in bundle.adapters] == ["telegram"]


# ── DID → directory resolution (roster/chat parity) ──────────────────────────


def _write_agent_dir(root: Path, dir_name: str, did: str, name: str) -> None:
    d = root / dir_name
    d.mkdir(parents=True)
    (d / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\n[identity]\ndid = "{did}"\n', encoding="utf-8"
    )


def test_load_did_index_resolves_bare_and_suffixed_dirs(tmp_path: Path) -> None:
    """`arc agent create <name>` makes a bare `<name>/` dir; the legacy layout is
    `<name>_agent/`. Both must resolve, matching team_roster's discovery — else the
    roster lists an agent that chat execution can't find (regression: chat runs
    failed with "no agent matches DID")."""
    from arcgateway.bootstrap import _load_did_index, _resolve_agent_dir

    root = tmp_path / "team"
    _write_agent_dir(root, "procurement", "did:arc:local:executor/aaa", "procurement")
    _write_agent_dir(root, "picking_agent", "did:arc:local:executor/bbb", "picking")

    index = _load_did_index(root)
    assert index["did:arc:local:executor/aaa"] == root / "procurement"
    assert index["did:arc:local:executor/bbb"] == root / "picking_agent"
    assert _resolve_agent_dir(root, "did:arc:local:executor/aaa") == root / "procurement"
    assert _resolve_agent_dir(root, "did:arc:local:executor/missing") is None
