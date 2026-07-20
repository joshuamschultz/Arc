"""Backend selection: map ``provider`` config to a concrete backend.

One place decides which browser backend an agent gets, and enforces the
federal remote-browser rule before any process could launch. Adding a
new service means adding one branch here plus its adapter file.
"""

from __future__ import annotations

from arcagent.modules.browser.backends.browserbase import BrowserbaseBackend
from arcagent.modules.browser.backends.cdp import CDPBackend
from arcagent.modules.browser.backends.protocols import BrowserBackend
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import BrowserError
from arcagent.modules.browser.policy import enforce_sandbox_policy


class UnknownBackendError(BrowserError):
    """Configured ``provider`` does not map to any known backend."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            code="BROWSER_UNKNOWN_BACKEND",
            message=f"Unknown browser provider {provider!r}",
            details={"provider": provider},
        )


def build_backend(config: BrowserConfig) -> BrowserBackend:
    """Construct the backend named by ``config.provider``.

    ``cdp`` (default) launches or attaches to Chrome over raw CDP;
    ``browserbase`` uses the managed Browserbase service. The federal
    tier forbids a locally auto-launched Chrome, enforced here before the
    backend is handed back so no subprocess can start.
    """
    provider = config.provider
    if provider == "cdp":
        enforce_sandbox_policy(config.tier, config.connection)
        return CDPBackend(config.connection)
    if provider == "browserbase":
        return BrowserbaseBackend(config.browserbase, tier=config.tier)
    raise UnknownBackendError(provider)


__all__ = ["UnknownBackendError", "build_backend"]
