"""Security tests for browser module — URL policy, scheme blocking,
redirect bypass prevention, JS toggle, content marking as external."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.capabilities import (
    browser_download_file,
    browser_execute_js,
    browser_navigate,
    browser_read_page,
)
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import CapabilityDisabledError, URLBlockedError


class TestURLAllowlist:
    """Allowlist mode: only allowed domains pass."""

    def test_allows_listed_domain(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "allowlist",
                "url_patterns": ["*.gov", "*.mil"],
            }
        )
        _check_url_policy("https://agency.gov/portal", config.security)

    def test_blocks_unlisted_domain(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "allowlist",
                "url_patterns": ["*.gov"],
            }
        )
        with pytest.raises(URLBlockedError, match="not in allowlist"):
            _check_url_policy("https://evil.com", config.security)

    def test_wildcard_matches_subdomain(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "allowlist",
                "url_patterns": ["*.example.com"],
            }
        )
        _check_url_policy("https://app.example.com/page", config.security)
        _check_url_policy("https://deep.sub.example.com", config.security)


class TestURLDenylist:
    """Denylist mode: blocked domains rejected."""

    def test_blocks_listed_domain(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "denylist",
                "url_patterns": ["*.malware.com", "evil.org"],
            }
        )
        with pytest.raises(URLBlockedError, match="blocked"):
            _check_url_policy("https://payload.malware.com", config.security)

    def test_allows_unlisted_domain(self) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "denylist",
                "url_patterns": ["*.malware.com"],
            }
        )
        _check_url_policy("https://safe.com", config.security)


class TestSchemeBlocking:
    """Dangerous URL schemes are blocked regardless of domain policy."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "file:///C:/Windows/system32",
            "chrome://settings",
            "chrome-extension://abc/popup.html",
            "javascript:alert(document.cookie)",
        ],
    )
    def test_blocks_dangerous_schemes(self, url: str) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy(url, config.security)

    @pytest.mark.parametrize("scheme", ["https", "http"])
    def test_allows_safe_schemes(self, scheme: str) -> None:
        from arcagent.modules.browser.url_policy import _check_url_policy

        config = BrowserConfig()
        _check_url_policy(f"{scheme}://example.com", config.security)


class TestJSExecutionToggle:
    """JS execution controlled by config toggle, honoured on the live path."""

    async def test_js_runs_when_enabled(self, configure_browser: Callable[..., _State]) -> None:
        cdp = AsyncMock()
        cdp.send = AsyncMock(return_value={"result": {"type": "string", "value": "ok"}})
        config = BrowserConfig(security={"allow_js_execution": True})  # type: ignore[arg-type]
        configure_browser(config, cdp=cdp)

        result = await browser_execute_js("'ok'")
        assert "ok" in result

    async def test_js_refused_when_disabled(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = AsyncMock()
        cdp.send = AsyncMock()
        config = BrowserConfig(security={"allow_js_execution": False})  # type: ignore[arg-type]
        configure_browser(config, cdp=cdp)

        with pytest.raises(CapabilityDisabledError):
            await browser_execute_js("'blocked'")
        cdp.send.assert_not_called()


class TestDownloadToggle:
    """Download controlled by config toggle, honoured on the live path."""

    async def test_download_runs_when_enabled(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = AsyncMock()
        cdp.send = AsyncMock(return_value={})
        config = BrowserConfig(security={"allow_downloads": True})  # type: ignore[arg-type]
        configure_browser(config, cdp=cdp)

        result = await browser_download_file("https://example.com/file.pdf")
        assert "download" in result.lower()

    async def test_download_refused_when_disabled(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = AsyncMock()
        cdp.send = AsyncMock()
        config = BrowserConfig(security={"allow_downloads": False})  # type: ignore[arg-type]
        configure_browser(config, cdp=cdp)

        with pytest.raises(CapabilityDisabledError):
            await browser_download_file("https://example.com/file.pdf")
        cdp.send.assert_not_called()


class TestExternalContentMarking:
    """All browser content must be marked as external/untrusted."""

    async def test_read_page_marks_external(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = AsyncMock()
        cdp.send = AsyncMock(
            return_value={
                "nodes": [
                    {
                        "nodeId": "1",
                        "role": {"value": "heading"},
                        "name": {"value": "Test"},
                        "backendDOMNodeId": 1,
                        "childIds": [],
                        "ignored": False,
                    },
                ]
            }
        )
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_read_page()
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_navigate_marks_external(self, configure_browser: Callable[..., _State]) -> None:
        cdp = AsyncMock()
        cdp.send.side_effect = [
            {"frameId": "f1"},  # Page.navigate
            {},  # Page.loadEventFired
            {"result": {"value": "https://example.com"}},  # Runtime.evaluate (URL)
            {"result": {"value": "Test Page"}},  # Runtime.evaluate (title)
        ]
        configure_browser(cdp=cdp)

        result = await browser_navigate("https://example.com")
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_js_result_marks_external(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = AsyncMock()
        cdp.send = AsyncMock(return_value={"result": {"type": "string", "value": "injected"}})
        configure_browser(cdp=cdp)

        result = await browser_execute_js("'injected'")
        assert "[EXTERNAL WEB CONTENT]" in result
