"""Tests for the Telegram plugin registration and config validation."""

from __future__ import annotations

import pytest
from arcgateway.adapters.registry import AdapterBuildContext, AdapterUnavailableError

from arcgateway_telegram import PLUGIN, TelegramAdapter, TelegramPlatformConfig
from arcgateway_telegram.plugin import build


async def _noop_on_message(event) -> None:  # type: ignore[no-untyped-def]
    return None


def _ctx(raw: dict, tier: str = "personal", require_pairing: bool = False) -> AdapterBuildContext:
    return AdapterBuildContext(
        name="telegram",
        raw_config=raw,
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier=tier,
        require_pairing=require_pairing,
    )


def test_plugin_identity() -> None:
    assert PLUGIN.name == "telegram"
    assert PLUGIN.build is build


def test_config_resolves_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TEST_TOKEN", "secret-123")
    cfg = TelegramPlatformConfig(enabled=True, token_env="TG_TEST_TOKEN")
    assert cfg.resolve_token() == "secret-123"


def test_config_token_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert TelegramPlatformConfig().resolve_token() is None


def test_config_ignores_sibling_keys() -> None:
    cfg = TelegramPlatformConfig.model_validate(
        {"enabled": True, "token_env": "X", "agent_did": "did:a", "unknown": 1}
    )
    assert cfg.enabled is True
    assert cfg.agent_did == "did:a"


def test_build_returns_adapter_when_token_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    adapter = build(_ctx({"enabled": True, "agent_did": "did:arc:agent:tg"}))
    assert isinstance(adapter, TelegramAdapter)
    assert adapter.name == "telegram"
    assert adapter._agent_did == "did:arc:agent:tg"


def test_build_raises_unavailable_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(AdapterUnavailableError):
        build(_ctx({"enabled": True}))


def test_build_uses_default_agent_did(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    adapter = build(_ctx({"enabled": True}))
    assert adapter._agent_did == "did:arc:agent:default"


def test_build_forwards_require_pairing_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """[security].require_pairing=true reaches the built TelegramAdapter."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    adapter = build(_ctx({"enabled": True}, require_pairing=True))
    assert adapter._require_pairing is True


def test_build_defaults_require_pairing_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    adapter = build(_ctx({"enabled": True}))
    assert adapter._require_pairing is False


def test_adapter_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    from arcgateway.adapters.base import BasePlatformAdapter

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    adapter = build(_ctx({"enabled": True}))
    assert isinstance(adapter, BasePlatformAdapter)
    # Telegram returns real message IDs — it must override the default.
    assert "send_with_id" in type(adapter).__dict__
