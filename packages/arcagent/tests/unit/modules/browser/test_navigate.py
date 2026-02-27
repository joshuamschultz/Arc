"""Tests for navigate tools — URL policy, navigation, back/forward/reload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import URLBlockedError


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestURLPolicy:
    """URL security enforcement."""

    def test_denylist_blocks_matching_domain(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "denylist", "url_patterns": ["*.evil.com"]}  # type: ignore[arg-type]
        )
        with pytest.raises(URLBlockedError, match="blocked"):
            _check_url_policy("https://malware.evil.com/payload", config.security)

    def test_denylist_allows_non_matching(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "denylist", "url_patterns": ["*.evil.com"]}  # type: ignore[arg-type]
        )
        _check_url_policy("https://example.com/safe", config.security)  # Should not raise

    def test_allowlist_blocks_non_matching(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "allowlist", "url_patterns": ["*.trusted.com"]}  # type: ignore[arg-type]
        )
        with pytest.raises(URLBlockedError, match="not in allowlist"):
            _check_url_policy("https://untrusted.com", config.security)

    def test_allowlist_allows_matching(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={"url_mode": "allowlist", "url_patterns": ["*.trusted.com"]}  # type: ignore[arg-type]
        )
        _check_url_policy("https://app.trusted.com", config.security)  # Should not raise

    def test_blocks_dangerous_scheme(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("file:///etc/passwd", config.security)

        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("javascript:alert(1)", config.security)

    def test_blocks_data_scheme(self) -> None:
        """data: scheme is blocked by default."""
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("data:text/html,<h1>hi</h1>", config.security)

    def test_blocks_blob_scheme(self) -> None:
        """blob: scheme is blocked by default."""
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy("blob:http://example.com/abc", config.security)

    def test_allows_https(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        _check_url_policy("https://example.com", config.security)  # Should not raise

    def test_allows_http(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        _check_url_policy("http://localhost:3000", config.security)  # Should not raise


class TestNavigateTool:
    """browser_navigate tool execution."""

    async def test_navigate_success(self) -> None:
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        bus = _make_bus()

        # Mock responses: Page.navigate, Page.loadEventFired, Runtime.evaluate (current URL),
        # Runtime.evaluate (title)
        cdp.send.side_effect = [
            {"frameId": "frame-1"},  # Page.navigate
            {},  # Page.loadEventFired
            {"result": {"value": "https://example.com"}},  # Runtime.evaluate (current URL)
            {"result": {"value": "Test Page"}},  # Runtime.evaluate (title)
        ]

        tools = create_navigate_tools(cdp, config, bus)
        nav_tool = next(t for t in tools if t.name == "browser_navigate")

        result = await nav_tool.execute(url="https://example.com")
        assert "example.com" in result
        assert "[EXTERNAL WEB CONTENT]" in result
        cdp.send.assert_any_call("Page", "navigate", {"url": "https://example.com"})

    async def test_navigate_blocked_url_emits_event(self) -> None:
        """Blocked URL emits browser.url_blocked event."""
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_navigate_tools(cdp, config, bus)
        nav_tool = next(t for t in tools if t.name == "browser_navigate")

        with pytest.raises(URLBlockedError):
            await nav_tool.execute(url="file:///etc/passwd")

        bus.emit.assert_called_once_with("browser.url_blocked", {"url": "file:///etc/passwd"})

    async def test_navigate_redirect_blocked(self) -> None:
        """Post-redirect URL validation blocks redirects to denied domains."""
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = _make_cdp()
        config = BrowserConfig(
            security={"url_mode": "denylist", "url_patterns": ["*.evil.com"]}  # type: ignore[arg-type]
        )
        bus = _make_bus()

        # Mock: navigate succeeds, but final URL is on a denied domain
        cdp.send.side_effect = [
            {"frameId": "frame-1"},  # Page.navigate
            {},  # Page.loadEventFired
            # Runtime.evaluate (redirect URL)
            {"result": {"value": "https://attack.evil.com/phish"}},
            {},  # Page.navigate (to about:blank)
        ]

        tools = create_navigate_tools(cdp, config, bus)
        nav_tool = next(t for t in tools if t.name == "browser_navigate")

        with pytest.raises(URLBlockedError, match="blocked"):
            await nav_tool.execute(url="https://safe-looking.com")


class TestGoBackForwardReload:
    """Navigation history tools."""

    async def test_go_back(self) -> None:
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = _make_cdp()
        cdp.send.side_effect = [
            {},  # Page.goBack
            {"result": {"value": "https://example.com"}},  # Runtime.evaluate (current URL)
        ]
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_navigate_tools(cdp, config, bus)
        back_tool = next(t for t in tools if t.name == "browser_go_back")

        result = await back_tool.execute()
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_go_forward(self) -> None:
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = _make_cdp()
        cdp.send.side_effect = [
            {},  # Page.goForward
            {"result": {"value": "https://example.com/next"}},  # Runtime.evaluate (current URL)
        ]
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_navigate_tools(cdp, config, bus)
        fwd_tool = next(t for t in tools if t.name == "browser_go_forward")

        result = await fwd_tool.execute()
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_reload(self) -> None:
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = _make_cdp()
        cdp.send.return_value = {}
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_navigate_tools(cdp, config, bus)
        reload_tool = next(t for t in tools if t.name == "browser_reload")

        await reload_tool.execute()
        cdp.send.assert_called_with("Page", "reload")
