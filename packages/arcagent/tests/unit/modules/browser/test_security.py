"""Security tests for browser module — URL policy, scheme blocking,
redirect bypass prevention, JS toggle, content marking as external."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import URLBlockedError


class TestURLAllowlist:
    """Allowlist mode: only allowed domains pass."""

    def test_allows_listed_domain(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "allowlist",
                "url_patterns": ["*.gov", "*.mil"],
            }
        )
        _check_url_policy("https://agency.gov/portal", config.security)

    def test_blocks_unlisted_domain(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "allowlist",
                "url_patterns": ["*.gov"],
            }
        )
        with pytest.raises(URLBlockedError, match="not in allowlist"):
            _check_url_policy("https://evil.com", config.security)

    def test_wildcard_matches_subdomain(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

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
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig(
            security={  # type: ignore[arg-type]
                "url_mode": "denylist",
                "url_patterns": ["*.malware.com", "evil.org"],
            }
        )
        with pytest.raises(URLBlockedError, match="blocked"):
            _check_url_policy("https://payload.malware.com", config.security)

    def test_allows_unlisted_domain(self) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

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
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        with pytest.raises(URLBlockedError, match="Scheme"):
            _check_url_policy(url, config.security)

    @pytest.mark.parametrize("scheme", ["https", "http"])
    def test_allows_safe_schemes(self, scheme: str) -> None:
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()
        _check_url_policy(f"{scheme}://example.com", config.security)


class TestJSExecutionToggle:
    """JS execution controlled by config toggle."""

    def test_js_tools_included_when_enabled(self) -> None:
        from arcagent.modules.browser.tools import create_browser_tools

        config = BrowserConfig(security={"allow_js_execution": True})  # type: ignore[arg-type]
        cdp = AsyncMock()
        ax = MagicMock()
        bus = MagicMock()

        tools = create_browser_tools(cdp, ax, config, bus)
        tool_names = [t.name for t in tools]
        assert "browser_execute_js" in tool_names

    def test_js_tools_excluded_when_disabled(self) -> None:
        from arcagent.modules.browser.tools import create_browser_tools

        config = BrowserConfig(security={"allow_js_execution": False})  # type: ignore[arg-type]
        cdp = AsyncMock()
        ax = MagicMock()
        bus = MagicMock()

        tools = create_browser_tools(cdp, ax, config, bus)
        tool_names = [t.name for t in tools]
        assert "browser_execute_js" not in tool_names


class TestDownloadToggle:
    """Download tools controlled by config toggle."""

    def test_download_tools_included_when_enabled(self) -> None:
        from arcagent.modules.browser.tools import create_browser_tools

        config = BrowserConfig(security={"allow_downloads": True})  # type: ignore[arg-type]
        cdp = AsyncMock()
        ax = MagicMock()
        bus = MagicMock()

        tools = create_browser_tools(cdp, ax, config, bus)
        tool_names = [t.name for t in tools]
        assert "browser_download_file" in tool_names

    def test_download_tools_excluded_when_disabled(self) -> None:
        from arcagent.modules.browser.tools import create_browser_tools

        config = BrowserConfig(security={"allow_downloads": False})  # type: ignore[arg-type]
        cdp = AsyncMock()
        ax = MagicMock()
        bus = MagicMock()

        tools = create_browser_tools(cdp, ax, config, bus)
        tool_names = [t.name for t in tools]
        assert "browser_download_file" not in tool_names


class TestExternalContentMarking:
    """All browser content must be marked as external/untrusted."""

    async def test_read_page_marks_external(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.read import create_read_tools

        cdp = AsyncMock()
        cdp.send.return_value = {
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
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = MagicMock()
        bus.emit = AsyncMock()

        tools = create_read_tools(cdp, ax, config, bus)
        read_tool = next(t for t in tools if t.name == "browser_read_page")

        result = await read_tool.execute()
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_navigate_marks_external(self) -> None:
        from arcagent.modules.browser.tools.navigate import create_navigate_tools

        cdp = AsyncMock()
        cdp.send.side_effect = [
            {"frameId": "f1"},  # Page.navigate
            {},  # Page.loadEventFired
            {"result": {"value": "https://example.com"}},  # Runtime.evaluate (URL)
            {"result": {"value": "Test Page"}},  # Runtime.evaluate (title)
        ]
        config = BrowserConfig()
        bus = MagicMock()
        bus.emit = AsyncMock()

        tools = create_navigate_tools(cdp, config, bus)
        nav_tool = next(t for t in tools if t.name == "browser_navigate")

        result = await nav_tool.execute(url="https://example.com")
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_js_result_marks_external(self) -> None:
        from arcagent.modules.browser.tools.javascript import create_javascript_tools

        cdp = AsyncMock()
        cdp.send.return_value = {"result": {"type": "string", "value": "injected"}}
        config = BrowserConfig()
        bus = MagicMock()
        bus.emit = AsyncMock()

        tools = create_javascript_tools(cdp, config, bus)
        js_tool = next(t for t in tools if t.name == "browser_execute_js")

        result = await js_tool.execute(expression="'injected'")
        assert "[EXTERNAL WEB CONTENT]" in result
