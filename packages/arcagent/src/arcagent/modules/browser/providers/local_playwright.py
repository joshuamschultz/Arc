"""Local Playwright browser provider (T4.9).

Launches a Playwright-managed local headless (or headed) browser.
Only usable when ``sandbox != "strict"`` — the policy layer enforces
this before this provider is ever constructed.

Playwright is an optional dependency (``arcagent[browser]``). This
module performs a lazy import so that the provider can be imported
without Playwright installed; the ``BrowserNotAvailableError`` error is
raised at ``connect()`` time rather than at module load.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.browser.errors import BrowserNotAvailableError

_logger = logging.getLogger("arcagent.modules.browser.providers.local")


class LocalPlaywrightProvider:
    """Manages a local Playwright browser lifecycle.

    Responsibilities:
    - Lazy-import Playwright so that the package is optional
    - Launch a Chromium browser (headless flag from config)
    - Expose the raw ``playwright.async_api.Browser`` to callers
    - Ensure clean shutdown: close browser then stop Playwright

    Usage::

        provider = LocalPlaywrightProvider(headless=True)
        browser = await provider.connect()
        # ... use browser ...
        await provider.disconnect()
    """

    def __init__(self, headless: bool = True) -> None:
        """Initialise without launching the browser.

        Args:
            headless: Pass ``False`` to launch a visible browser window
                (only useful for local debugging; always True in CI/CD).
        """
        self._headless = headless
        self._playwright: Any = None  # playwright.async_api.Playwright at runtime
        self._browser: Any = None  # playwright.async_api.Browser at runtime

    @property
    def browser(self) -> Any:
        """The connected Playwright Browser instance, or None."""
        return self._browser

    async def connect(self) -> Any:
        """Launch Playwright and start the browser.

        Returns:
            The Playwright ``Browser`` instance.

        Raises:
            BrowserNotAvailableError: If Playwright is not installed.
        """
        try:
            from playwright.async_api import (  # type: ignore[import-not-found]  # optional dep
                async_playwright,
            )
        except ImportError as exc:
            raise BrowserNotAvailableError(
                message=(
                    "Playwright is not installed. "
                    "Install arcagent[browser] to enable local browser automation."
                ),
                details={"install_hint": "pip install 'arcagent[browser]'"},
            ) from exc

        _logger.info("Launching local Playwright browser (headless=%s)", self._headless)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        _logger.info("Local Playwright browser launched")
        return self._browser

    async def disconnect(self) -> None:
        """Close the browser and stop Playwright.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                _logger.debug("Error closing Playwright browser", exc_info=True)
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                _logger.debug("Error stopping Playwright", exc_info=True)
            self._playwright = None

        _logger.info("Local Playwright browser disconnected")
