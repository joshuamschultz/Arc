"""Browser module — CDP-based browser interaction tools for web automation.

Provides headless Chrome control via Chrome DevTools Protocol (CDP).
Tools auto-register on module load and are managed by ArcRun like
any other tool.
"""

from __future__ import annotations

from arcagent.modules.browser.browser_module import BrowserModule
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import (
    BrowserError,
    BrowserTimeoutError,
    CDPConnectionError,
    ElementNotFoundError,
    URLBlockedError,
)

__all__ = [
    "BrowserConfig",
    "BrowserError",
    "BrowserModule",
    "BrowserTimeoutError",
    "CDPConnectionError",
    "ElementNotFoundError",
    "URLBlockedError",
]
