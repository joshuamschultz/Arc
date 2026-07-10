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
async def test_build_for_embedded_federal_tier_threads_team_root_to_worker(
    empty_team_root: Path,
) -> None:
    """Task 26 — the federal SubprocessExecutor must receive this gateway's
    own team_root so arc-agent-worker resolves --did via a real DID index
    instead of a fixed, agent-agnostic search path. Without this, a
    multi-agent embedded federal gateway silently serves the wrong agent's
    identity to every session (the DID-blindness bug)."""
    cfg = _config(
        """
[gateway]
tier = "federal"
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.executor._team_root == empty_team_root  # type: ignore[attr-defined]


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


# ── require_pairing wiring (SDD §3.1 DM Pairing — embedded/production path) ──


@pytest.mark.asyncio
async def test_build_for_embedded_require_pairing_false_leaves_pairing_store_unset(
    empty_team_root: Path,
) -> None:
    """Default (require_pairing=false) does not construct a PairingStore.

    This is the embedded-gateway counterpart of the same fix in
    GatewayRunner.from_config — arcui hosts the runtime via
    build_for_embedded, NOT via GatewayRunner, so wiring only runner.py
    left the actual production path (arcui) with pairing permanently
    disabled regardless of config.
    """
    cfg = _config(
        """
[gateway]
agent_did = "did:arc:agent:default"

[security]
require_pairing = false
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.session_router._pairing._pairing_store is None


@pytest.mark.asyncio
async def test_build_for_embedded_require_pairing_true_wires_pairing_store(
    empty_team_root: Path, tmp_path: Path
) -> None:
    """require_pairing=true builds a PairingStore and wires it into SessionRouter."""
    from arcgateway.pairing import PairingStore

    db_path = tmp_path / "pairing.db"
    cfg = _config(
        f"""
[gateway]
agent_did = "did:arc:agent:default"

[security]
require_pairing = true

[pairing]
db_path = "{db_path}"
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert isinstance(bundle.session_router._pairing._pairing_store, PairingStore)
    assert bundle.session_router._pairing._pairing_store._db_path == db_path.expanduser().resolve()


@pytest.mark.asyncio
async def test_build_for_embedded_forwards_require_pairing_to_adapters(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """[security].require_pairing reaches build_adapters -> AdapterBuildContext.

    Without this, a statically-unauthorized Telegram user's message would be
    dropped by the adapter itself before ever reaching the (correctly wired)
    SessionRouter/PairingInterceptor.
    """
    pytest.importorskip("arcgateway_telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:test-token")
    db_path = tmp_path / "pairing.db"
    cfg = _config(
        f"""
[gateway]
agent_did = "did:arc:agent:default"

[security]
require_pairing = true

[pairing]
db_path = "{db_path}"

[platforms.telegram]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    telegram_adapter = next(a for a in bundle.adapters if a.name == "telegram")
    assert telegram_adapter._require_pairing is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_build_for_embedded_allowlisted_user_skips_pairing_on_first_message(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Task #34, the literal live bug: with require_pairing=true, a user in
    [platforms.telegram].allowed_user_ids still got a DM pairing code minted
    on their FIRST message — SessionRouter's PairingInterceptor never
    received the static allowlist from either construction site.

    Drives the REAL build_for_embedded output (the production path arcui
    hosts): an allowlisted telegram user reaches the executor with ZERO
    codes minted; a non-allowlisted user on the same router still gets one.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from arcgateway.executor import Delta, InboundEvent
    from arcgateway.session import build_session_key

    pytest.importorskip("arcgateway_telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:test-token")
    db_path = tmp_path / "pairing.db"
    cfg = _config(
        f"""
[gateway]
agent_did = "did:arc:agent:default"

[security]
require_pairing = true

[pairing]
db_path = "{db_path}"

[platforms.telegram]
enabled = true
allowed_user_ids = [555]
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    router = bundle.session_router

    # The static allowlist really did reach the interceptor.
    assert router._pairing._user_allowlist == {"did:arc:telegram:555"}

    # Swap in a counting executor — proves the message reached agent
    # dispatch without requiring a real agent directory under team_root.
    call_count = 0

    async def _fast_stream(event: InboundEvent):
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)

    class _CountingExecutor:
        async def run(self, event: InboundEvent):
            nonlocal call_count
            call_count += 1
            return _fast_stream(event)

    router._executor = _CountingExecutor()  # type: ignore[assignment]

    pairing_store = router._pairing._pairing_store
    mint_spy = AsyncMock(wraps=pairing_store.mint_code)
    monkeypatch.setattr(pairing_store, "mint_code", mint_spy)

    # The real telegram adapter was never connect()-ed (no live bot process
    # in this test), so it can't actually send a DM. Swap in a mock for the
    # pairing interceptor's own delivery channel — the DM-delivery mechanism
    # itself is already covered by test_pairing_dm_delivery.py; this test's
    # job is proving the allowlist/wiring, not re-testing adapter.send().
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock()
    router._pairing.register_adapter("telegram", mock_adapter)

    allowlisted_did = "did:arc:telegram:555"
    allowed_event = InboundEvent(
        platform="telegram",
        chat_id="chat_allowed",
        user_did=allowlisted_did,
        agent_did=cfg.gateway.agent_did,
        session_key=build_session_key(cfg.gateway.agent_did, allowlisted_did),
        message="hi",
    )
    await router.handle(allowed_event)
    await asyncio.sleep(0.05)

    assert call_count == 1, "allowlisted user must reach the executor on first message"
    mint_spy.assert_not_called()

    # A non-allowlisted user on the SAME router still gets a pairing code —
    # the fix must not have disabled enforcement altogether.
    unlisted_did = "did:arc:telegram:999"
    unlisted_event = InboundEvent(
        platform="telegram",
        chat_id="chat_unlisted",
        user_did=unlisted_did,
        agent_did=cfg.gateway.agent_did,
        session_key=build_session_key(cfg.gateway.agent_did, unlisted_did),
        message="hi",
    )
    await router.handle(unlisted_event)
    await asyncio.sleep(0.05)

    assert call_count == 1, "non-allowlisted user must NOT reach the executor"
    mint_spy.assert_called_once()


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
