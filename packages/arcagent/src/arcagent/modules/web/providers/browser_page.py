"""Keyless page extraction through the browser module's backend seam.

Basic site lookup has to work on a box with no accounts and no API keys.
Parallel, Firecrawl and Tavily all bill for the privilege, so none of them can
be the out-of-the-box default. The browser module already owns a
zero-dependency, federal-safe Chrome DevTools Protocol backend that either
launches a local headless Chrome or attaches to a remote endpoint, so this
provider drives a page through that same
:class:`~arcagent.modules.browser.backends.protocols.BrowserBackend` /
:class:`~arcagent.modules.browser.backends.protocols.BrowserSession` seam
rather than adding a second fetch engine to the codebase.

**The browser module is optional and may be absent.** Modules reach a box as
separately installed signed bundles, so a deployment approved for ``web`` need
not carry ``browser``. Nothing here imports it at module-import time: the seam
is resolved on demand by :func:`build_browser_backend`, and its absence is a
loud typed failure — never a silent no-op, and never a tool the agent is
offered but cannot run.

A session is opened and closed per extraction. The web module has no lifecycle
hook to close one on shutdown, and a Chrome process nobody owns is worse than a
launch we pay for on each call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from arcagent.modules.web.errors import ExtractFailed
from arcagent.modules.web.protocols import ExtractResult

if TYPE_CHECKING:  # the browser module is a runtime-optional import
    from arcagent.modules.browser.backends.protocols import BrowserBackend, BrowserSession

_logger = logging.getLogger("arcagent.modules.web.providers.browser_page")

#: Read the loaded document in one round trip. Constant, first-party script —
#: no model-authored expression ever reaches ``Runtime.evaluate`` here, which is
#: what ``[modules.browser.config.security] allow_js_execution`` governs (ASI05).
#: ``browser_navigate`` already reads ``document.title`` the same way.
_READ_PAGE_JS = """
(() => {
  const links = Array.from(document.querySelectorAll('a[href]'))
    .map((a) => a.href)
    .filter((href) => href.startsWith('http'));
  return JSON.stringify({
    url: document.location.href,
    title: document.title || '',
    text: (document.body && document.body.innerText) || '',
    links: Array.from(new Set(links)).slice(0, 500),
  });
})()
"""


def build_browser_backend(*, cdp_url: str, tier: str) -> BrowserBackend:
    """Build a browser backend for ``tier``, resolving the optional seam now.

    Construction only — no process launches and no socket opens here, so both
    the startup availability probe and the first real extraction can call this
    and get the same verdict. The federal remote-browser rule is enforced by
    ``build_backend`` itself, so a federal deployment with no ``cdp_url`` fails
    here rather than after a subprocess has already started.

    Raises:
        ExtractFailed: The browser module is not installed on this deployment.
        BrowserError: The tier forbids the requested backend.
    """
    try:
        from arcagent.modules.browser.backends import build_backend
        from arcagent.modules.browser.config import BrowserConfig, BrowserConnectionConfig
    except ImportError as exc:
        raise ExtractFailed(
            "keyless web extraction needs the browser module, which is not "
            "installed on this deployment — run `arc module install browser`, "
            "or set [modules.web.config] extract_provider to a configured "
            "provider with an API key",
            details={"provider": "browser", "missing_module": "arcagent.modules.browser"},
        ) from exc

    return build_backend(
        BrowserConfig(tier=tier, connection=BrowserConnectionConfig(cdp_url=cdp_url))
    )


class BrowserPageProvider:
    """WebExtractProvider that reads a page over CDP. No API key.

    Construct via :meth:`create`. Satisfies
    :class:`~arcagent.modules.web.protocols.WebExtractProvider` by duck-typing,
    exactly like the three paid adapters beside it.
    """

    name = "browser"

    def __init__(self, *, cdp_url: str = "", tier: str = "personal", timeout_s: float = 30.0):
        self._cdp_url = cdp_url
        self._tier = tier
        self._timeout_s = timeout_s

    @classmethod
    def create(
        cls, *, cdp_url: str = "", tier: str = "personal", timeout_s: float = 30.0
    ) -> BrowserPageProvider:
        """Factory mirroring the paid providers' ``create`` — minus the key."""
        return cls(cdp_url=cdp_url, tier=tier, timeout_s=timeout_s)

    async def extract(self, url: str) -> ExtractResult:
        """Load ``url`` in a browser session and return its text and links.

        The caller (:func:`arcagent.modules.web.capabilities._extract`) has
        already cleared ``url`` against the allowlist, and applies the size cap
        and PII redaction to whatever comes back — this provider adds no path
        around any of them.

        Raises:
            ExtractFailed: The browser module is absent, the backend refused
                the tier, the page did not load within ``request_timeout_s``,
                or the session errored.
        """
        backend = build_browser_backend(cdp_url=self._cdp_url, tier=self._tier)
        try:
            async with asyncio.timeout(self._timeout_s):
                session = await backend.open()
                payload = await _load_and_read(session, url)
        except ExtractFailed:
            raise
        except TimeoutError as exc:
            raise ExtractFailed(
                f"browser extraction timed out after {self._timeout_s}s",
                details={"url": url, "provider": "browser"},
            ) from exc
        except Exception as exc:  # reason: re-raise as the module's typed error
            raise ExtractFailed(
                f"browser extraction failed: {type(exc).__name__}: {exc}",
                details={"url": url, "provider": "browser"},
            ) from exc
        finally:
            await _close_quietly(backend)

        return ExtractResult(
            url=str(payload.get("url") or url),
            title=str(payload.get("title") or ""),
            content=str(payload.get("text") or ""),
            links=[str(link) for link in payload.get("links") or []],
            fetched_at=time.time(),
        )


async def _load_and_read(session: BrowserSession, url: str) -> dict[str, Any]:
    """Navigate to ``url`` and read the loaded document in one evaluation."""
    await session.send("Page", "navigate", {"url": url})
    try:
        await session.send("Page", "loadEventFired")
    except Exception:  # reason: fail-open — a page that never fires load is still readable
        _logger.debug("loadEventFired not received for %s (ignored)", url, exc_info=True)

    result = await session.send(
        "Runtime", "evaluate", {"expression": _READ_PAGE_JS, "returnByValue": True}
    )
    raw = result.get("result", {}).get("value")
    if not isinstance(raw, str):
        raise ExtractFailed(
            "browser returned no readable document",
            details={"url": url, "provider": "browser"},
        )
    parsed: Any = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ExtractFailed(
            "browser returned a malformed document payload",
            details={"url": url, "provider": "browser"},
        )
    return parsed


async def _close_quietly(backend: BrowserBackend) -> None:
    """Release the browser, never masking the failure that got us here."""
    try:
        await backend.close()
    except Exception:  # reason: teardown must not replace the original error
        _logger.warning("browser backend close failed", exc_info=True)


__all__ = ["BrowserPageProvider", "build_browser_backend"]
