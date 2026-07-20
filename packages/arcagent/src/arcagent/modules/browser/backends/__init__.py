"""Pluggable browser backends.

A backend produces the live CDP :class:`BrowserSession` the browser
tools drive. Which one an agent gets is chosen by
``[modules.browser.config] provider`` and resolved by
:func:`build_backend`.

Available backends:
    cdp          — raw Chrome DevTools Protocol (local launch or remote
                   attach); the zero-dependency, federal-safe default
    browserbase  — managed remote browser via the Browserbase service

Attach any other CDP-speaking service by adding one adapter file that
implements :class:`BrowserBackend` and a branch in
:func:`build_backend`.
"""

from arcagent.modules.browser.backends.browserbase import BrowserbaseBackend
from arcagent.modules.browser.backends.cdp import CDPBackend
from arcagent.modules.browser.backends.protocols import BrowserBackend, BrowserSession
from arcagent.modules.browser.backends.select import UnknownBackendError, build_backend

__all__ = [
    "BrowserBackend",
    "BrowserSession",
    "BrowserbaseBackend",
    "CDPBackend",
    "UnknownBackendError",
    "build_backend",
]
