"""Tests for arcgateway.config — focuses on the [platforms.web] block.

The Telegram and Slack sections are exercised via existing adapter tests
(``test_cli_smoke``); this file specifically covers the new web platform
plumbing introduced by SPEC-023.
"""

from __future__ import annotations

from arcgateway.config import GatewayConfig, WebPlatformConfig


def test_web_platform_config_defaults_disabled() -> None:
    """A bare [platforms.web] block parses with safe defaults."""
    cfg = GatewayConfig.from_toml_str("[platforms.web]\n")
    assert cfg.platforms.web.enabled is False
    assert cfg.platforms.web.max_connections == 50
    assert cfg.platforms.web.idle_timeout_seconds == 3600
    assert cfg.platforms.web.max_frame_bytes == 65536


def test_web_platform_config_full() -> None:
    """A fully-populated [platforms.web] block parses every field."""
    toml = """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true
agent_did = "did:arc:agent:concierge"
max_connections = 200
idle_timeout_seconds = 7200
max_frame_bytes = 131072
"""
    cfg = GatewayConfig.from_toml_str(toml)
    assert cfg.platforms.web.enabled is True
    assert cfg.platforms.web.agent_did == "did:arc:agent:concierge"
    assert cfg.platforms.web.max_connections == 200
    assert cfg.platforms.web.idle_timeout_seconds == 7200
    assert cfg.platforms.web.max_frame_bytes == 131072


def test_web_effective_agent_did_falls_back_to_gateway() -> None:
    """When [platforms.web].agent_did is unset, fall back to [gateway].agent_did."""
    toml = """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true
"""
    cfg = GatewayConfig.from_toml_str(toml)
    assert cfg.effective_agent_did("web") == "did:arc:agent:default"


def test_web_effective_agent_did_overrides_gateway() -> None:
    """Platform-level agent_did takes precedence over [gateway].agent_did."""
    toml = """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true
agent_did = "did:arc:agent:concierge"
"""
    cfg = GatewayConfig.from_toml_str(toml)
    assert cfg.effective_agent_did("web") == "did:arc:agent:concierge"


def test_web_platform_config_default_when_section_absent() -> None:
    """Platforms.web is a default-constructed model when no [platforms.web] block exists."""
    cfg = GatewayConfig.from_toml_str("")
    assert isinstance(cfg.platforms.web, WebPlatformConfig)
    assert cfg.platforms.web.enabled is False
