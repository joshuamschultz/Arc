"""Integration tests — Browser Module with real headless Chrome.

Requires Chrome/Chromium installed and accessible. Skipped in CI
unless ARCAGENT_BROWSER_INTEGRATION=1 is set.

Tests real CDP communication: launch Chrome, navigate, read AX tree,
click, type, screenshot.
"""

from __future__ import annotations

import base64
import os
import shutil

import pytest

# Skip entire module unless explicitly opted in
pytestmark = pytest.mark.skipif(
    os.environ.get("ARCAGENT_BROWSER_INTEGRATION", "") != "1",
    reason="Set ARCAGENT_BROWSER_INTEGRATION=1 to run browser integration tests",
)


def _chrome_available() -> bool:
    """Check if Chrome/Chromium is on PATH."""
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if shutil.which(name):
            return True
    # macOS application path
    mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return os.path.isfile(mac_path)


@pytest.fixture
def browser_config():
    """Create a browser config for integration testing."""
    from arcagent.modules.browser.config import BrowserConfig

    return BrowserConfig(
        connection={"headless": True},  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
class TestBrowserIntegration:
    """Real headless Chrome integration tests."""

    async def test_connect_and_navigate(self, browser_config) -> None:
        """Launch Chrome, navigate to a data URI, read the title."""
        if not _chrome_available():
            pytest.skip("Chrome not found on system")

        from arcagent.modules.browser.cdp_client import CDPClientManager

        cdp = CDPClientManager(browser_config.connection)
        try:
            await cdp.connect()
            assert cdp.connected

            # Navigate to a simple data URI page
            page_html = (
                "<html><head><title>ArcAgent Test</title></head>"
                "<body><h1>Hello Browser</h1>"
                "<button id='btn'>Click Me</button>"
                "<input id='inp' type='text' placeholder='Type here'/>"
                "</body></html>"
            )
            data_url = f"data:text/html,{page_html}"
            await cdp.send("Page", "navigate", {"url": data_url})

            # Read the title
            result = await cdp.send(
                "Runtime",
                "evaluate",
                {"expression": "document.title"},
            )
            title = result.get("result", {}).get("value", "")
            assert title == "ArcAgent Test"
        finally:
            await cdp.disconnect()

        assert not cdp.connected

    async def test_read_accessibility_tree(self, browser_config) -> None:
        """Navigate and read the accessibility tree."""
        if not _chrome_available():
            pytest.skip("Chrome not found on system")

        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.cdp_client import CDPClientManager

        cdp = CDPClientManager(browser_config.connection)
        try:
            await cdp.connect()

            page_html = (
                "<html><body>"
                "<h1>Test Heading</h1>"
                "<p>Paragraph text</p>"
                "<button>Submit</button>"
                "</body></html>"
            )
            await cdp.send(
                "Page", "navigate",
                {"url": f"data:text/html,{page_html}"},
            )

            ax = AccessibilityManager(cdp, browser_config)
            snapshot = await ax.snapshot()

            assert "Test Heading" in snapshot
            assert "Submit" in snapshot
        finally:
            await cdp.disconnect()

    async def test_screenshot(self, browser_config) -> None:
        """Navigate and capture a screenshot."""
        if not _chrome_available():
            pytest.skip("Chrome not found on system")

        from arcagent.modules.browser.cdp_client import CDPClientManager

        cdp = CDPClientManager(browser_config.connection)
        try:
            await cdp.connect()

            await cdp.send(
                "Page", "navigate",
                {"url": "data:text/html,<html><body><h1>Screenshot</h1></body></html>"},
            )

            result = await cdp.send(
                "Page",
                "captureScreenshot",
                {"format": "png"},
            )
            data = result.get("data", "")
            assert len(data) > 0

            # Verify it's valid base64 PNG
            decoded = base64.b64decode(data)
            assert decoded[:4] == b"\x89PNG"
        finally:
            await cdp.disconnect()

    async def test_click_and_type(self, browser_config) -> None:
        """Navigate, click a button, type into an input."""
        if not _chrome_available():
            pytest.skip("Chrome not found on system")

        from arcagent.modules.browser.cdp_client import CDPClientManager

        cdp = CDPClientManager(browser_config.connection)
        try:
            await cdp.connect()

            page_html = (
                "<html><body>"
                "<input id='inp' type='text'/>"
                "<button id='btn' onclick=\"document.getElementById('inp').value='clicked'\">Go</button>"
                "</body></html>"
            )
            await cdp.send(
                "Page", "navigate",
                {"url": f"data:text/html,{page_html}"},
            )

            # Click the button via JS
            await cdp.send(
                "Runtime", "evaluate",
                {"expression": "document.getElementById('btn').click()"},
            )

            # Verify the input was updated
            result = await cdp.send(
                "Runtime", "evaluate",
                {"expression": "document.getElementById('inp').value"},
            )
            value = result.get("result", {}).get("value", "")
            assert value == "clicked"

            # Type into the input via JS
            await cdp.send(
                "Runtime", "evaluate",
                {"expression": "document.getElementById('inp').value = 'typed text'"},
            )
            result = await cdp.send(
                "Runtime", "evaluate",
                {"expression": "document.getElementById('inp').value"},
            )
            assert result.get("result", {}).get("value") == "typed text"
        finally:
            await cdp.disconnect()
