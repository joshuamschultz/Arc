"""Tests for the Mattermost plugin registration and config validation."""

from __future__ import annotations

import pytest
from arcgateway.adapters.registry import AdapterBuildContext, AdapterUnavailableError

from arcgateway_mattermost import PLUGIN, MattermostAdapter, MattermostPlatformConfig
from arcgateway_mattermost.plugin import build


async def _noop_on_message(event) -> None:  # type: ignore[no-untyped-def]
    return None


def _ctx(raw: dict, tier: str = "personal") -> AdapterBuildContext:
    return AdapterBuildContext(
        name="mattermost",
        raw_config=raw,
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier=tier,
    )


def test_plugin_identity() -> None:
    assert PLUGIN.name == "mattermost"
    assert PLUGIN.build is build


def test_config_resolves_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_T", "pat-123")
    cfg = MattermostPlatformConfig(enabled=True, bot_token_env="MM_T")
    assert cfg.resolve_bot_token() == "pat-123"


def test_build_returns_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_BOT_TOKEN", "pat-abc")
    adapter = build(
        _ctx({"enabled": True, "server_url": "https://mm.example.com"})
    )
    assert isinstance(adapter, MattermostAdapter)
    assert adapter.name == "mattermost"


def test_build_raises_without_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_BOT_TOKEN", "pat-abc")
    with pytest.raises(AdapterUnavailableError):
        build(_ctx({"enabled": True}))


def test_build_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MM_BOT_TOKEN", raising=False)
    with pytest.raises(AdapterUnavailableError):
        build(_ctx({"enabled": True, "server_url": "https://mm.example.com"}))


def test_adapter_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    from arcgateway.adapters.base import BasePlatformAdapter

    monkeypatch.setenv("MM_BOT_TOKEN", "pat-abc")
    adapter = build(_ctx({"enabled": True, "server_url": "https://mm.example.com"}))
    assert isinstance(adapter, BasePlatformAdapter)
    assert "send_with_id" in type(adapter).__dict__


def test_federal_airgap_guard_rejects_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_BOT_TOKEN", "pat-abc")
    # Public URL at federal tier → adapter __init__ raises ValueError (propagates).
    with pytest.raises(ValueError):
        build(
            _ctx(
                {"enabled": True, "server_url": "https://mattermost.example.com"},
                tier="federal",
            )
        )
