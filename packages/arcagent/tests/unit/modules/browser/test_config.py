"""Tests for browser module config — validation, defaults, extra='forbid'."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


class TestBrowserSecurityConfig:
    """Security config: URL policy, scheme blocking, toggles."""

    def test_defaults(self) -> None:
        from arcagent.modules.browser.config import BrowserSecurityConfig

        cfg = BrowserSecurityConfig()
        assert cfg.url_mode == "denylist"
        assert cfg.url_patterns == []
        assert cfg.blocked_schemes == [
            "file",
            "chrome",
            "chrome-extension",
            "javascript",
            "data",
            "blob",
            "ftp",
        ]
        assert cfg.allow_js_execution is True
        assert cfg.allow_downloads is True
        assert cfg.download_path == "/tmp/arcagent-downloads"
        assert cfg.redact_inputs is False
        assert cfg.max_page_text_length == 50_000
        assert cfg.max_screenshot_width == 1920
        assert cfg.max_screenshot_height == 1080

    def test_url_mode_enum_allowlist(self) -> None:
        from arcagent.modules.browser.config import BrowserSecurityConfig

        cfg = BrowserSecurityConfig(url_mode="allowlist")
        assert cfg.url_mode == "allowlist"

    def test_url_mode_enum_rejects_invalid(self) -> None:
        from arcagent.modules.browser.config import BrowserSecurityConfig

        with pytest.raises(ValidationError):
            BrowserSecurityConfig(url_mode="invalid")  # type: ignore[arg-type]

    def test_extra_forbid_rejects_typos(self) -> None:
        from arcagent.modules.browser.config import BrowserSecurityConfig

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BrowserSecurityConfig(url_mod="denylist")  # type: ignore[call-arg]


class TestBrowserConnectionConfig:
    """CDP connection settings."""

    def test_defaults(self) -> None:
        from arcagent.modules.browser.config import BrowserConnectionConfig

        cfg = BrowserConnectionConfig()
        assert cfg.cdp_url == ""
        assert cfg.chrome_path == ""
        assert cfg.headless is True
        assert cfg.remote_debugging_port == 0
        assert cfg.chrome_flags == []
        assert cfg.startup_timeout_seconds == 10

    def test_extra_forbid(self) -> None:
        from arcagent.modules.browser.config import BrowserConnectionConfig

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BrowserConnectionConfig(headles=True)  # type: ignore[call-arg]


class TestBrowserTimeoutConfig:
    """Per-tool timeout defaults."""

    def test_defaults(self) -> None:
        from arcagent.modules.browser.config import BrowserTimeoutConfig

        cfg = BrowserTimeoutConfig()
        assert cfg.navigate == 30
        assert cfg.click == 5
        assert cfg.type_ == 5
        assert cfg.screenshot == 10
        assert cfg.read_page == 15
        assert cfg.execute_js == 10
        assert cfg.fill_form == 30
        assert cfg.default == 10


class TestBrowserCookieConfig:
    """Cookie persistence settings."""

    def test_defaults(self) -> None:
        from arcagent.modules.browser.config import BrowserCookieConfig

        cfg = BrowserCookieConfig()
        assert cfg.persist is False
        assert cfg.encryption_key_env == "ARCAGENT_BROWSER_COOKIE_KEY"
        assert cfg.storage_path == ""


class TestBrowserConfig:
    """Root config composing all sub-configs."""

    def test_defaults(self) -> None:
        from arcagent.modules.browser.config import BrowserConfig

        cfg = BrowserConfig()
        assert cfg.accessibility_tree_depth == 10
        assert cfg.security.url_mode == "denylist"
        assert cfg.connection.headless is True
        assert cfg.timeouts.navigate == 30
        assert cfg.cookies.persist is False

    def test_nested_override(self) -> None:
        from arcagent.modules.browser.config import BrowserConfig

        cfg = BrowserConfig(
            security={"url_mode": "allowlist", "url_patterns": ["*.example.com"]},  # type: ignore[arg-type]
            connection={"headless": False},  # type: ignore[arg-type]
        )
        assert cfg.security.url_mode == "allowlist"
        assert cfg.security.url_patterns == ["*.example.com"]
        assert cfg.connection.headless is False

    def test_from_dict(self) -> None:
        """Config can be constructed from a raw dict (as passed by module loader)."""
        from arcagent.modules.browser.config import BrowserConfig

        raw = {
            "accessibility_tree_depth": 5,
            "security": {"allow_js_execution": False},
            "timeouts": {"navigate": 60},
        }
        cfg = BrowserConfig(**raw)
        assert cfg.accessibility_tree_depth == 5
        assert cfg.security.allow_js_execution is False
        assert cfg.timeouts.navigate == 60

    def test_extra_forbid_root(self) -> None:
        from arcagent.modules.browser.config import BrowserConfig

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BrowserConfig(unknown_field="bad")  # type: ignore[call-arg]
