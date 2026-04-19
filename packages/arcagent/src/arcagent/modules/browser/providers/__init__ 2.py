"""Browser providers subpackage for T4.9 Playwright-based browser automation.

Exports:
    LocalPlaywrightProvider   — launches a local Playwright headless browser
    BrowserbaseProvider       — connects to Browserbase remote browser API

Both providers expose the same async interface:
    ``connect() -> playwright.async_api.Browser``
    ``disconnect() -> None``

Playwright is an optional dependency (``arcagent[browser]``). Importing
this package when Playwright is not installed will raise ``BrowserNotAvailable``
at runtime rather than at import time, so the module can still be loaded
in environments where browser automation is not needed.
"""

from __future__ import annotations

from arcagent.modules.browser.providers.browserbase import BrowserbaseProvider
from arcagent.modules.browser.providers.local_playwright import LocalPlaywrightProvider

__all__ = ["BrowserbaseProvider", "LocalPlaywrightProvider"]
