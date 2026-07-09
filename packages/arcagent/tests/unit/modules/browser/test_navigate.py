"""Tests for navigate tools — URL policy, navigation, back/forward/reload."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.capabilities import (
    browser_go_back,
    browser_go_forward,
    browser_navigate,
    browser_reload,
)
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import URLBlockedError


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


class TestURLPolicy:
    """URL security enforcement."""

    def test_denylist_blocks_matching_domain(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "denylist", "url_patterns": ["*.evil.com"]}  # type: ignore[arg-type]
        )
        with pytest.raises(URLBlockedError, match="blocked"):
            _check_url_policy("https://malware.evil.com/payload", config.security)

    def test_denylist_allows_non_matching(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "denylist", "url_patterns": ["*.evil.com"]}  # type: ignore[arg-type]
        )
        _check_url_policy("https://example.com/safe", config.security)  # Should not raise

    def test_allowlist_blocks_non_matching(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "allowlist", "url_patterns": ["*.trusted.com"]}  # type: ignore[arg-type]
        )
        with pytest.raises(URLBlockedError, match="not in allowlist"):
            _check_url_policy("https://untrusted.com", config.security)

    def test_allowlist_allows_matching(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "allowlist", "url_patterns": ["*.trusted.com"]}  # type: ignore[arg-type]
        )
        _check_url_policy("https://app.trusted.com", config.security)  # Should not raise

    def test_blocks_dangerous_scheme(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("file:///etc/passwd", config.security)

        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("javascript:alert(1)", config.security)

    def test_blocks_data_scheme(self) -> None:
        """data: scheme is blocked by default."""
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("data:text/html,<h1>hi</h1>", config.security)

    def test_blocks_blob_scheme(self) -> None:
        """blob: scheme is blocked by default."""
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("blob:http://example.com/abc", config.security)

    def test_allows_https(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        _check_url_policy("https://example.com", config.security)  # Should not raise

    def test_allows_http(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        _check_url_policy("http://localhost:3000", config.security)  # Should not raise


class TestNavigateTool:
    """browser_navigate tool execution."""

    async def test_navigate_success(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        cdp.send.side_effect = [
            {"frameId": "frame-1"},  # Page.navigate
            {},  # Page.loadEventFired
            {"result": {"value": "https://example.com"}},  # Runtime.evaluate (current URL)
            {"result": {"value": "Test Page"}},  # Runtime.evaluate (title)
        ]
        configure_browser(cdp=cdp)

        result = await browser_navigate("https://example.com")
        assert "example.com" in result
        assert "[EXTERNAL WEB CONTENT]" in result
        cdp.send.assert_any_call("Page", "navigate", {"url": "https://example.com"})

    async def test_navigate_blocked_url_emits_event(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Blocked URL emits browser.url_blocked event."""
        cdp = _make_cdp()
        st = configure_browser(cdp=cdp)

        with pytest.raises(URLBlockedError):
            await browser_navigate("file:///etc/passwd")

        st.bus.emit.assert_called_once_with("browser.url_blocked", {"url": "file:///etc/passwd"})

    async def test_navigate_redirect_blocked(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Post-redirect URL validation blocks redirects to denied domains."""
        cdp = _make_cdp()
        cdp.send.side_effect = [
            {"frameId": "frame-1"},  # Page.navigate
            {},  # Page.loadEventFired
            # Runtime.evaluate (redirect URL)
            {"result": {"value": "https://attack.evil.com/phish"}},
            {},  # Page.navigate (to about:blank)
        ]
        config = BrowserConfig(
            security={"url_mode": "denylist", "url_patterns": ["*.evil.com"]}  # type: ignore[arg-type]
        )
        configure_browser(config, cdp=cdp)

        with pytest.raises(URLBlockedError, match="blocked"):
            await browser_navigate("https://safe-looking.com")


class TestGoBackForwardReload:
    """Navigation history tools."""

    async def test_go_back(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        cdp.send.side_effect = [
            {},  # Page.goBack
            {"result": {"value": "https://example.com"}},  # Runtime.evaluate (current URL)
        ]
        configure_browser(cdp=cdp)

        result = await browser_go_back()
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_go_forward(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        cdp.send.side_effect = [
            {},  # Page.goForward
            {"result": {"value": "https://example.com/next"}},  # Runtime.evaluate (current URL)
        ]
        configure_browser(cdp=cdp)

        result = await browser_go_forward()
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_reload(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        cdp.send.return_value = {}
        configure_browser(cdp=cdp)

        await browser_reload()
        cdp.send.assert_called_with("Page", "reload")
