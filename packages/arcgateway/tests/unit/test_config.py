"""Tests for arcgateway.config — focuses on the [platforms.web] block.

The Telegram and Slack sections are exercised via existing adapter tests
(``test_cli_smoke``); this file specifically covers the new web platform
plumbing introduced by SPEC-023.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.paths import gateway_pairing_db, gateway_runtime_dir

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


# ---------------------------------------------------------------------------
# Defaults honor ARC_CONFIG_DIR (matches arctrust.trust_store's own fix)
# ---------------------------------------------------------------------------


def test_runtime_dir_default_honors_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    cfg = GatewayConfig.from_toml_str("")
    assert cfg.gateway.runtime_dir == gateway_runtime_dir(tmp_path).resolve()


def test_pairing_db_path_default_honors_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    cfg = GatewayConfig.from_toml_str("")
    assert cfg.pairing.db_path == gateway_pairing_db(tmp_path).resolve()


def test_runtime_dir_default_falls_back_to_home_without_arc_config_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    cfg = GatewayConfig.from_toml_str("")
    # Under the operator root, not the install home: runtime state must
    # survive replacing ~/.arc.
    assert cfg.gateway.runtime_dir == gateway_runtime_dir(Path.home() / "arc")


# --- [ui] public_base_url (alpha-2 item 75b) --------------------------------


def test_ui_public_base_url_defaults_unset() -> None:
    assert GatewayConfig.from_toml_str("[gateway]\n").ui.public_base_url is None


def test_ui_public_base_url_https_is_accepted_and_normalised() -> None:
    cfg = GatewayConfig.from_toml_str('[ui]\npublic_base_url = "https://arc.example.com/"\n')
    assert cfg.ui.public_base_url == "https://arc.example.com"


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8420", "http://localhost:8420", "http://[::1]:8420"]
)
def test_ui_public_base_url_allows_loopback_http_at_personal(url: str) -> None:
    cfg = GatewayConfig.from_toml_str(f'[ui]\npublic_base_url = "{url}"\n')
    assert cfg.ui.public_base_url == url


@pytest.mark.parametrize(
    "toml",
    [
        '[ui]\npublic_base_url = "http://arc.example.com"\n',
        '[gateway]\ntier = "enterprise"\n[ui]\npublic_base_url = "http://127.0.0.1:8420"\n',
        '[gateway]\ntier = "federal"\n[ui]\npublic_base_url = "http://localhost"\n',
        '[ui]\npublic_base_url = "ftp://arc.example.com"\n',
        '[ui]\npublic_base_url = "https://user:pw@arc.example.com"\n',
        '[ui]\npublic_base_url = "https://arc.example.com/?token=x"\n',
        '[ui]\npublic_base_url = "https://arc.example.com/#auth=x"\n',
        '[ui]\npublic_base_url = "arc.example.com"\n',
    ],
)
def test_ui_public_base_url_rejects_insecure_or_credentialed_urls(toml: str) -> None:
    with pytest.raises(ValueError):
        GatewayConfig.from_toml_str(toml)


def test_loop_lag_monitor_is_off_unless_the_operator_enables_it() -> None:
    assert GatewayConfig.from_toml_str("[platforms.web]\n").ui.loop_lag_monitor is False
    enabled = GatewayConfig.from_toml_str("[ui]\nloop_lag_monitor = true\n")
    assert enabled.ui.loop_lag_monitor is True
