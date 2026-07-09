"""Tests for the Slack plugin registration and config validation."""

from __future__ import annotations

import pytest
from arcgateway.adapters.registry import AdapterBuildContext, AdapterUnavailableError

from arcgateway_slack import PLUGIN, SlackAdapter, SlackPlatformConfig
from arcgateway_slack.plugin import build


async def _noop_on_message(event) -> None:  # type: ignore[no-untyped-def]
    return None


def _ctx(raw: dict, tier: str = "personal") -> AdapterBuildContext:
    return AdapterBuildContext(
        name="slack",
        raw_config=raw,
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier=tier,
    )


def test_plugin_identity() -> None:
    assert PLUGIN.name == "slack"
    assert PLUGIN.build is build


def test_config_resolves_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SB", "xoxb-1")
    monkeypatch.setenv("SA", "xapp-1")
    cfg = SlackPlatformConfig(enabled=True, bot_token_env="SB", app_token_env="SA")
    assert cfg.resolve_bot_token() == "xoxb-1"
    assert cfg.resolve_app_token() == "xapp-1"


def test_build_returns_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
    adapter = build(_ctx({"enabled": True, "allowed_user_ids": ["U1"]}))
    assert isinstance(adapter, SlackAdapter)
    assert adapter.name == "slack"


def test_build_threads_agent_did_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
    adapter = build(_ctx({"enabled": True, "agent_did": "did:arc:agent:slack"}))
    assert adapter._agent_did == "did:arc:agent:slack"


def test_build_uses_default_agent_did(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
    adapter = build(_ctx({"enabled": True}))
    assert adapter._agent_did == "did:arc:agent:default"


def test_build_raises_without_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
    with pytest.raises(AdapterUnavailableError):
        build(_ctx({"enabled": True}))


def test_build_raises_without_app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    with pytest.raises(AdapterUnavailableError):
        build(_ctx({"enabled": True}))


def test_adapter_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    from arcgateway.adapters.base import BasePlatformAdapter

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
    adapter = build(_ctx({"enabled": True}))
    assert isinstance(adapter, BasePlatformAdapter)
    assert "send_with_id" in type(adapter).__dict__
